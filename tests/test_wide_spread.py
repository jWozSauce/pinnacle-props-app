import json
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest
from pinnacle_props.consensus import annotate,VERSION
from pinnacle_props.venue_details import enrich
from pinnacle_props.venue_ui import verdict,venue_summary,display_rows
from pinnacle_props.quote_quality import spread_pp,is_wide,quality
from pinnacle_props.devig import wpo
from pinnacle_props.kelly import stake
from tests.test_venue_details import fixture,PROFILE,CAPTURE


def jarvis():
    f,raw=fixture();row=f.iloc[0].to_dict()
    p=wpo([2.36,1.55])[0]
    row.update(book='espnbet',player_name='Seth Jarvis',prop_type='player_assists',Target_line=.5,PIN_line=.5,
        noVigOver=p,noVigUnder=1-p,target_over_decimal=2.75,edge=p-1/2.75,rule_pass=True)
    records=[]
    for book,over,under in [('pinnacle',2.36,1.55),('novig',2.74,1.37),('prophetx',2.29,1.23),('draftkings',2.5,1.49),('espnbet',2.75,1.4),('fanatics',2.6,None),('bovada',2.6,None),('fanduel',2.6,None),('betonlineag',2.6,None)]:
        for side,price in [('over',over),('under',under)]:
            if price is not None:records.append(dict(sport_key=row['sport_key'],event_id=row['event_id'],player=row['player_name'],market=row['prop_type'],point=.5,book=book,side=side,price=price,last_update='2026-10-25T11:59:00Z'))
    return pd.DataFrame([row]),pd.DataFrame(records)


def test_josh_example_all_surfaces():
    f,raw=jarvis();out=enrich(annotate(f,raw),raw,PROFILE,CAPTURE);r=out.iloc[0]
    venues=json.loads(r.venue_quotes);v={x['venue']:x for x in venues}
    assert verdict(v['prophetx'],r.book)=='Wide spread (no verdict)'
    assert verdict(v['novig'],r.book)=='Against pinnacle'
    assert venue_summary(venues,r.book)=='Exchanges: Novig against, ProphetX wide spread, Polymarket no quote, Kalshi no quote · Market: 0 with, 1 against'
    assert r.island_flag=='Exchanges against Pinnacle' and r.sharp_books_q==1 and r.market_books_q==1 and r.wide_books_q==1
    assert r.advisory_version==4
    p=(r.noVigOver+wpo([2.74,1.37])[0])/2
    assert r.blend_stake==pytest.approx(stake(PROFILE.bankroll,PROFILE.fraction,2.75,p,PROFILE.edge_cap))
    assert r.blend_edge_pp==pytest.approx(100*(p-1/2.75))
    table=display_rows(venues,r.book).set_index('Venue')
    assert table.loc['prophetx','Fair probability (%)']=='—' and table.loc['prophetx','Gap from Pinnacle (pp)']=='—'
    assert table.loc['prophetx','Spread (pp)']==pytest.approx(spread_pp(2.29,1.23))
    assert v['prophetx']['fair_over'] is not None and v['prophetx']['gap_pp'] is not None
    assert table.loc['pinnacle','Spread (pp)']==pytest.approx(spread_pp(2.36,1.55))


def test_no_wide_v3_exact_parity():
    f,raw=fixture();a=enrich(annotate(f,raw,version=3),raw,PROFILE,CAPTURE,version=3)
    b=enrich(annotate(f,raw),raw,PROFILE,CAPTURE)
    pd.testing.assert_frame_equal(a.drop(columns=['advisory_version','venue_quotes']),b[a.columns].drop(columns=['advisory_version','venue_quotes']))
    old=json.loads(a.iloc[0].venue_quotes);new=json.loads(b.iloc[0].venue_quotes)
    assert old==[{k:v for k,v in row.items() if k not in ('wide','spread_pp')} for row in new]
    assert b.iloc[0].wide_books_q==0


def test_threshold_reference_one_side_and_all_wide():
    assert not is_wide(spread_pp(1/.56,1/.56))
    assert is_wide(spread_pp(1/.56001,1/.56))
    assert not is_wide(spread_pp(2.1,2.1))
    assert not quality(dict(venue='novig',over=1.2,under=None))['wide']
    assert not quality(dict(venue='pinnacle',over=1.2,under=1.2))['wide']
    f,raw=jarvis();raw.loc[raw.book.isin(['novig','prophetx','draftkings','espnbet']),'price']=1.5
    out=enrich(annotate(f,raw),raw,PROFILE,CAPTURE).iloc[0]
    assert out.wide_books_q==4 and out.sharp_books_q==0 and out.market_books_q==0
    assert not out.island_flag and pd.isna(out.blend_stake)
    assert verdict(dict(venue='espnbet',over=1.5,under=1.5,same_line=True),'espnbet')=='Wide spread (no verdict)'


def test_wide_peer_removes_island_and_duplicates_count_once():
    from tests.test_signed_consensus import board
    f,raw=board();raw.loc[raw.book=='peer0','price']=1.5
    raw=pd.concat([raw,raw.assign(market='player_points_alternate')],ignore_index=True)
    out=annotate(f,raw).iloc[0]
    assert out.market_books_q==2 and out.wide_books_q==1 and 'ISLAND' not in out.island_flag
    other=raw.copy();other.point=5.5
    assert annotate(f,other).iloc[0].wide_books_q==0


def live_panel():
    import streamlit as st
    from pinnacle_props.venue_ui import expand,table_rows
    expand(st.session_state.frame.iloc[0]);table_rows(st.session_state.frame)


def saved_panel():
    import streamlit as st
    from pinnacle_props.venue_ui import expand
    expand(st.session_state.row,saved=True)


def test_live_and_recovered_old_recommendation_app():
    from pinnacle_props.paper import frozen
    from pinnacle_props.recommendations import from_paper
    f,raw=jarvis();current=enrich(annotate(f,raw),raw,PROFILE,CAPTURE)
    at=AppTest.from_function(live_panel);at.session_state.frame=current;at.run();assert not at.exception
    assert 'Wide spread (no verdict)' in at.dataframe[0].value.Verdict.tolist()
    assert 'Spread (pp)' in at.get('html')[0].proto.body
    old=enrich(annotate(f,raw,version=3),raw,PROFILE,CAPTURE,version=3).iloc[0].to_dict()
    rec=frozen({'scan_id':'old','capture_time':CAPTURE,'settings':{'sport':'nba'}},old,1,paper=True)
    recovered=from_paper(rec,8.61,2.75);before=json.dumps(recovered,sort_keys=True,default=str)
    at=AppTest.from_function(saved_panel);at.session_state.row=recovered;at.run();assert not at.exception
    assert 'Wide spread (no verdict)' in at.dataframe[0].value.Verdict.tolist()
    assert any('ProphetX wide spread' in c.value for c in at.caption)
    assert any('Recorded advisory version: 3' in c.value for c in at.caption)
    assert any(f"stake ${old['blend_stake']:,.2f}" in c.value for c in at.caption)
    assert json.dumps(recovered,sort_keys=True,default=str)==before


def test_v3_sheet_header_append_only_and_read_only_view(tmp_path):
    from pinnacle_props.bet_store import BetStore,PRE_WIDE_HEADER,HEADER,StoreUnavailable
    from tests.test_bet_store import Spreadsheet,record
    sheet=Spreadsheet();ws=sheet.add_worksheet('PP_Bet_Log',5000,len(PRE_WIDE_HEADER))
    rec=record(event_id='fixture');row=[str(rec.get(k,'')) for k in PRE_WIDE_HEADER];ws.values=[PRE_WIDE_HEADER.copy(),row.copy()]
    store=BetStore(tmp_path,spreadsheet=sheet);out=store.read('real')
    assert ws.values[0]==HEADER and ws.values[1]==row and ws.writes==['AR1:AR1']
    assert out.iloc[0].wide_books_q==''
    with pytest.raises(ValueError):store.grade('real',[(out.iloc[0].to_dict(),dict(out.iloc[0],wide_books_q=1))])
