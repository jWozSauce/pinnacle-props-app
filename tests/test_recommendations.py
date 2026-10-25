import json
from datetime import datetime,timezone
from urllib.parse import parse_qs,urlsplit
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest
from pinnacle_props import recommendations as rec
from pinnacle_props.bet_store import BetStore,StoreUnavailable
from pinnacle_props.settle import metadata,settle_row
from tests.test_bet_store import Spreadsheet,record
from tests.test_settle_forward import game

NOW='2026-10-25T01:00:00Z'


def paper(**kw):
    return record(**dict(dict(event_id='recovery-fixture',book='draftkings',logged_at=NOW,
        odds_american=150,fair_p=.45,edge=.05,rule_version='frozen-rule',
        source=json.dumps({'recommendation':{'pin_line':21.5,'pin_over_decimal':2.1,'pin_under_decimal':1.8}})),**kw))


def test_frozen_copy_fill_price_and_source_survive_grade():
    row=paper(status='won',profit=30,stat=25,graded_at=NOW)
    real=rec.from_paper(row,12.34,2.7,note='earlier fill')
    assert (real['stake'],real['price_decimal'],real['status'],real['profit'],real['stat'])==(12.34,2.7,'pending','','')
    assert real['fair_p']==row['fair_p'] and real['edge']==row['edge'] and real['rule_version']=='frozen-rule'
    source=metadata(real)['recommendation'];assert source['frozen_record']['price_decimal']==2.5
    assert source['original_provenance']['recommendation']['pin_under_decimal']==1.8
    graded,_=settle_row(real,game(),[{'player_id':'p','player_name':'A Player','points':25}],now=NOW)
    assert graded['status']=='won' and metadata(graded)['recommendation']==source
    assert row['stake']==20 and row['status']=='won'


def test_recent_cutoffs_missing_dates_and_empty_selection():
    rows=pd.DataFrame([paper(logged_at=t,player=str(i)) for i,t in enumerate([
        '2026-10-23T13:00:00Z','2026-10-23T12:59:59Z',NOW,'2026-10-25T01:00:01Z','bad'])])
    assert rec.recent(rows,now=NOW).player.tolist()==['2','0']
    assert rec.recent(rows,now='2027-01-01T00:00:00Z').empty
    assert rec.recent(rows.iloc[0:0],now=NOW).empty


def test_guard_fresh_duplicate_and_unavailable_paper(tmp_path):
    sheet=Spreadsheet();store=BetStore(tmp_path,spreadsheet=sheet);r=paper();store.append('paper',[r])
    assert rec.log_paper(store,r['row_id'],8.61,2.52)['written']==1
    assert rec.log_paper(store,r['row_id'],8.61,2.52)['written']==0
    assert len(store.read('real',reload=True))==1
    original=store.read('paper',reload=True).iloc[0];assert float(original.stake)==20
    sheet.tabs['PP_Paper_Log'].values=sheet.tabs['PP_Paper_Log'].values[:1]
    with pytest.raises(StoreUnavailable):rec.log_paper(store,r['row_id'],8.61,2.52)


def test_stable_link_across_scan_and_uuid():
    r=paper();s=paper(scan_id='later',stake=8)
    assert rec.token(r)==rec.token(s)
    assert parse_qs(urlsplit(rec.link(r)).query)['recommendation']==[rec.token(r)]
    with pytest.raises(ValueError):rec.link(r,base='http://example.com')


def recovery_app():
    import streamlit as st
    from pinnacle_props.recommendation_ui import recent_panel,forms,linked_panel
    store=st.session_state.store
    a,b=st.tabs(['Logged','Paper'])
    if st.session_state.get('deep'):linked_panel(store)
    with a:recent_panel(store.read('paper'),store,now='2026-10-25T01:00:00Z')
    with b:forms(store.read('paper'),store,prefix='paper_recover')


@pytest.mark.parametrize('prefix',['recent','paper_recover','linked'])
def test_app_button_without_scan_prefills_edits_and_blocks_duplicate(tmp_path,prefix):
    store=BetStore(tmp_path,spreadsheet=Spreadsheet());r=paper();store.append('paper',[r])
    at=AppTest.from_function(recovery_app);at.session_state.store=store
    if prefix=='linked':at.session_state.deep=True;at.query_params['recommendation']=rec.token(r)
    at.run();assert not at.exception
    ident=prefix+'_'+r['row_id']
    assert at.number_input(key=ident+'_stake').value==20
    assert at.number_input(key=ident+'_odds').value==150
    assert at.button(key=ident+'_log').disabled and store.read('real').empty
    at.number_input(key=ident+'_stake').set_value(8.61)
    at.number_input(key=ident+'_odds').set_value(152.)
    at.checkbox(key=ident+'_confirm').check().run()
    at.button(key=ident+'_log').click().run();assert not at.exception
    saved=store.read('real',reload=True).iloc[0].to_dict()
    assert float(saved['stake'])==8.61 and float(saved['price_decimal'])==2.52
    assert metadata(saved)['recommendation']['paper_row_id']==r['row_id']
    assert at.button(key=ident+'_log').disabled
    assert any('ALREADY BET' in e.label for e in at.expander)


def test_live_paper_button(tmp_path,monkeypatch):
    from tests.test_live_ui import setup
    row=paper(start_time='2026-10-21T15:00:00Z',game_date='2026-10-21')
    at,store,_=setup(tmp_path,monkeypatch,[row]);at.radio(key='live_log').set_value('paper').run()
    ident='live_recover_'+row['row_id'];assert not at.exception
    at.checkbox(key=ident+'_confirm').check().run();at.button(key=ident+'_log').click().run()
    assert not at.exception and len(store.read('real',reload=True))==1


def test_telegram_link_text_and_csv():
    from pinnacle_props.telegram_alerts import message
    m={'capture_time':NOW,'settings':{'sport':'nba'},'credits_charged':0}
    r=paper(start_time='2026-10-26T00:00:00Z')
    text,doc=message(m,[r]);assert rec.link(r) in text and doc is None
    text,doc=message(m,[dict(r,player='Player '+str(i)) for i in range(60)])
    assert b'recommendation_url' in doc and rec.link(dict(r,player='Player 0')).encode() in doc


def test_new_scan_frozen_raw_pair_persists_after_settlement():
    from pinnacle_props.paper import frozen
    from tests.test_rules_app import fixture_rows
    from pinnacle_props import rules
    r=fixture_rows('nba').iloc[0].to_dict();r.update(pin_over_decimal=2.2,pin_under_decimal=1.8)
    manifest={'scan_id':'scan-fixture','capture_time':NOW,'settings':{'sport':'nba'},'rules':rules.active()}
    result=frozen(manifest,r,1,paper=True)
    source=metadata(result)['recommendation']
    assert source['pin_line']==float(r['Target_line'])
    assert (source['pin_over_decimal'],source['pin_under_decimal'])==(2.2,1.8)
    assert source['fair_p']==result['fair_p'] and source['rule_version']==result['rule_version']


def test_link_does_not_bypass_cloud_unlock(tmp_path,monkeypatch):
    from pathlib import Path
    from tests.test_access import cloud
    monkeypatch.setattr('pinnacle_props.bet_store.session_store',lambda *a:(_ for _ in ()).throw(AssertionError('must unlock first')))
    at=cloud(monkeypatch);at.query_params['recommendation']=rec.token(paper());at.run()
    assert not at.exception and not at.dataframe
    assert not any(x.label=='Log as real bet' for x in at.button)
