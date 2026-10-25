import json
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest
from pinnacle_props.venue_details import enrich,quotes
from pinnacle_props.kelly import Profile,stake
from pinnacle_props.devig import wpo
from pinnacle_props.bet_store import BetStore,PRE_BLEND_HEADER,HEADER,StoreUnavailable
from pinnacle_props.paper import frozen
from pinnacle_props.settle import metadata
from tests.test_bet_store import Spreadsheet,record
from tests.test_rules_app import fixture_rows

CAPTURE='2026-10-25T12:00:00Z'
PROFILE=Profile(1000,.25,.04)


def fixture():
    r=fixture_rows('nba').iloc[0].to_dict()
    r.update(side='over',Target_line=2.5,PIN_line=2.5,book='draftkings',target_over_decimal=2.5,
        pin_over_decimal=2.,pin_under_decimal=2.,noVigOver=.5,noVigUnder=.5,edge=.1,kellyAmount=stake(1000,.25,2.5,.5,.04))
    rows=[]
    for book,line,over,under in [('pinnacle',2.5,2.,2.),('novig',2.5,2.2,1.8),('prophetx',2.5,2.1,1.9),
         ('draftkings',2.5,2.5,1.6),('fanduel',2.5,2.4,1.65),('betmgm',2.5,2.3,1.7),('fanduel',3.5,3.,1.4)]:
        for side,price in [('Over',over),('Under',under)]:
            rows.append(dict(sport_key=r['sport_key'],event_id=r['event_id'],player=r['player_name'],market=r['prop_type'],point=line,book=book,side=side,price=price,last_update='2026-10-25T11:59:00Z'))
    return pd.DataFrame([r]),pd.DataFrame(rows)


def test_blend_is_secondary_exact_line_and_same_profile():
    f,raw=fixture();out=enrich(f,raw,PROFILE,CAPTURE)
    pd.testing.assert_frame_equal(f,out[f.columns])
    p=(.5+(wpo([2.2,1.8])[0]+wpo([2.1,1.9])[0])/2)/2
    assert out.iloc[0].blend_edge_pp==pytest.approx(100*(p-1/2.5))
    assert out.iloc[0].blend_stake==pytest.approx(stake(1000,.25,2.5,p,.04))
    venues=json.loads(out.iloc[0].venue_quotes)
    assert [v['venue'] for v in venues[:6]]==['pinnacle','novig','prophetx','draftkings','fanduel','betmgm']
    assert venues[-1]['line']==3.5 and not venues[-1]['same_line']
    assert venues[1]['gap_pp']==pytest.approx(100*(wpo([2.2,1.8])[0]-.5))
    assert all(v['age_min']==1 for v in venues)


def test_missing_one_sided_future_other_line_and_duplicate_quotes():
    f,raw=fixture();raw.loc[raw.book.isin(['novig','prophetx']),'point']=3.5
    assert pd.isna(enrich(f,raw,PROFILE,CAPTURE).iloc[0].blend_stake)
    f,raw=fixture();raw=raw[~((raw.book=='novig')&(raw.side=='Under'))]
    raw.loc[raw.book=='prophetx','last_update']='2026-10-25T12:01:00Z'
    assert pd.isna(enrich(f,raw,PROFILE,CAPTURE).iloc[0].blend_edge_pp)
    f,raw=fixture();conflict=raw[raw.book=='novig'].copy();conflict['price']=3.
    out=enrich(f,pd.concat([raw,conflict],ignore_index=True),PROFILE,CAPTURE)
    expected=(.5+wpo([2.1,1.9])[0])/2
    assert out.iloc[0].blend_edge_pp==pytest.approx(100*(expected-.4))


def test_schema_header_only_migration_and_frozen_columns(tmp_path):
    sheet=Spreadsheet();ws=sheet.add_worksheet('PP_Bet_Log',5000,len(PRE_BLEND_HEADER))
    row=record(event_id='event-fixture');original=[str(row[k]) for k in PRE_BLEND_HEADER]
    ws.values=[PRE_BLEND_HEADER.copy(),original.copy()]
    store=BetStore(tmp_path,spreadsheet=sheet);frame=store.read('real',reload=True)
    assert ws.values[0]==HEADER and ws.values[1]==original and ws.writes==['AP1:AQ1']
    old=frame.iloc[0].to_dict()
    with pytest.raises(ValueError):store.grade('real',[(old,dict(old,blend_stake=500))])
    bad=Spreadsheet();ws=bad.add_worksheet('PP_Bet_Log',5000,len(HEADER));ws.values=[PRE_BLEND_HEADER.copy(),original+['unlabelled']]
    with pytest.raises(StoreUnavailable):BetStore(tmp_path/'bad',spreadsheet=bad)._worksheet('real')
    assert ws.writes==[]


def test_frozen_and_recovered_record_retains_primary_and_additive_values():
    from pinnacle_props.recommendations import from_paper
    f,raw=fixture();r=enrich(f,raw,PROFILE,CAPTURE).iloc[0].to_dict()
    m={'scan_id':'fixture','capture_time':CAPTURE,'settings':{'sport':'nba'}}
    rec=frozen(m,r,1,paper=True);actual=from_paper(rec,8.61,2.52)
    assert rec['stake']==r['kellyAmount'] and actual['stake']==8.61
    assert actual['edge']==r['edge'] and actual['fair_p']==r['noVigOver']
    assert actual['blend_stake']==r['blend_stake']
    assert len(metadata(rec)['recommendation']['venues'])==7


def panel_app():
    import streamlit as st
    from pinnacle_props.venue_ui import expand,table_rows
    f=st.session_state.frame
    expand(f.iloc[0]);table_rows(f)


def test_app_order_gap_and_divider():
    f,raw=fixture();at=AppTest.from_function(panel_app);at.session_state.frame=enrich(f,raw,PROFILE,CAPTURE);at.run()
    assert not at.exception
    table=at.dataframe[0].value
    assert table.Venue.tolist()==['pinnacle','novig','prophetx','draftkings','fanduel','betmgm']
    assert 'Gap from Pinnacle (pp)' in table and table.iloc[1]['Gap from Pinnacle (pp)']<0
    assert at.dataframe[1].value.Line.tolist()==[3.5]
    assert at.get('divider') and any('Different lines' in c.value for c in at.caption)
    html=at.get('html')[0].proto.body
    assert '<details class="venue-row">' in html and '<summary>' in html and '<hr>' in html
    assert any('Blended (Pinnacle + exchanges)' in c.value for c in at.caption)


def test_per_venue_direction_boundary_target_implied_and_missing_quotes():
    from pinnacle_props.venue_ui import verdict,venue_summary,display_rows
    rows=[dict(venue=name,same_line=True,gap_pp=gap,line=2.5,price=2.5,fair_p=.5+gap/100)
          for name,gap in [('pinnacle',0),('novig',-1),('prophetx',-1.01),('draftkings',-10),('fanduel',2)]]
    assert verdict(rows[1],'draftkings').lower()=='with pinnacle'
    assert verdict(rows[2],'draftkings').lower()=='against pinnacle'
    assert verdict(dict(rows[1],same_line=False),'draftkings')=='Different line'
    assert verdict(dict(rows[1],gap_pp=None),'draftkings')=='No two-sided quote'
    summary=venue_summary(rows,'draftkings')
    assert 'Novig with, ProphetX against, Polymarket no quote' in summary and 'Market: 1 with, 0 against' in summary
    table=display_rows(rows,'draftkings')
    assert table.iloc[3]['Verdict']=='Your bet' and table.iloc[3]['Implied probability (%)']==40


def test_legacy_snapshot_replay_keeps_unchanged_hashes(tmp_path):
    from tests.test_pipeline import sample_result,settings
    from pinnacle_props.pipeline import _save_snapshot,replay
    selected=settings();selected.pop('venue_details_version')
    result=sample_result(tmp_path)
    manifest,flags,_=_save_snapshot(tmp_path,'legacy',selected,PROFILE,'Player_Props',
        '2026-10-07T19:00:00Z',[result],[],1,1)
    assert 'venue_quotes' not in flags
    actual,again,_=replay('nba','legacy',data_dir=tmp_path)
    assert actual['artifacts']==manifest['artifacts']
    pd.testing.assert_frame_equal(flags,again)
