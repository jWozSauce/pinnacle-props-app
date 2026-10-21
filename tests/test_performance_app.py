from pathlib import Path
import json
import pandas as pd
from streamlit.testing.v1 import AppTest


def test_metrics_tables_and_official_precedence(tmp_path,monkeypatch):
    import pinnacle_props.performance_ui as ui
    from pinnacle_props.bet_store import BetStore
    from tests.test_bet_store import Spreadsheet,record
    store=BetStore(tmp_path,spreadsheet=Spreadsheet())
    store.append('real',[record()]);store.append('paper',[record()])
    real=store.read('real');real.loc[0,['status','profit']]=['won','30'];store.cache['real']=real
    def live(frame,feed):
        f=frame.copy();f['stage']='live';f['verdict']='🔴 behind';f['score']='80–70';f['detail']='points 20 vs 21.5 · Q3';return f
    monkeypatch.setattr(ui,'BetStore',lambda *a,**k:store)
    monkeypatch.setattr(ui,'live_table',live)
    script=f'''import streamlit as st
from pinnacle_props.performance_ui import render
from pathlib import Path
a,b=st.tabs(['Logged','Performance'])
render(a,b,Path({str(tmp_path)!r}))
'''
    app=AppTest.from_string(script).run()
    assert not app.exception
    metrics=[(m.label,m.value) for m in app.metric]
    assert metrics.count(('Profit','$30.00'))==2
    assert not any('Projected' in label for label,value in metrics)
    assert len(app.dataframe)>=3
    app.radio(key='performance_log').set_value('paper').run()
    assert not app.exception and len(app.dataframe)>=8


def test_empty_performance_app(tmp_path,monkeypatch):
    import pinnacle_props.performance_ui as ui
    from pinnacle_props.bet_store import BetStore
    from tests.test_bet_store import Spreadsheet
    store=BetStore(tmp_path,spreadsheet=Spreadsheet())
    monkeypatch.setattr(ui,'BetStore',lambda *a,**k:store)
    app=AppTest.from_string(f'''import streamlit as st
from pinnacle_props.performance_ui import render
from pathlib import Path
a,b=st.tabs(['Logged','Performance']);render(a,b,Path({str(tmp_path)!r}))''').run()
    assert not app.exception
    assert any('No real bets' in i.value for i in app.info)


def test_full_app_enabled_uses_new_panels(tmp_path,monkeypatch):
    from pinnacle_props import rules
    from pinnacle_props.bet_store import BetStore
    from tests.test_bet_store import Spreadsheet
    from tests.test_rules_app import fixture_rows
    import pinnacle_props.performance_ui as ui
    monkeypatch.delenv('PP_SETTLEMENT_ENABLED',raising=False)
    monkeypatch.setattr(rules,'LIVE_PATH',tmp_path/'rules.json')
    store=BetStore(tmp_path,spreadsheet=Spreadsheet())
    monkeypatch.setattr(ui,'BetStore',lambda *a,**k:store)
    monkeypatch.setattr('pinnacle_props.bet_store.session_store',lambda *a,**k:store)
    monkeypatch.setattr('pinnacle_props.settings.ROOT',tmp_path)
    monkeypatch.setattr('pinnacle_props.bet.read_bets',lambda *a,**k:store.read('real'))
    monkeypatch.setattr('pinnacle_props.clv.rebuild',lambda **k:pd.DataFrame())
    def replay(sport,**kwargs):
        f=fixture_rows(sport)
        return dict(scan_id='fixture',capture_time='2026-10-09T12:00:00Z',profile_name='Fixture',event_results=[],credits_charged=0,raw_rows=len(f)*4,candidate_pairs=len(f),exclusions={}),f,pd.DataFrame()
    monkeypatch.setattr('pinnacle_props.pipeline.replay',replay)
    app=AppTest.from_file(Path('app.py').resolve())
    app.secrets['gcp_service_account']={'project_id':'fixture'}
    app.run(timeout=20)
    assert not app.exception
    assert len(app.tabs)==6
    assert app.checkbox(key='paper_track').value is True
    assert app.button(key='paper_all').label=='Bet ALL 4 qualifying (paper round)'
    assert app.tabs[5].label=='Paper'
    assert any(b.label=='Grade pending bets (real + paper)' for b in app.button)
    assert sum(m.label=='Official P/L' for m in app.metric)==1
    assert app.tabs[1].label=='Live'


def test_flags_cards_frozen_table_and_guard_controls(tmp_path,monkeypatch):
    from pinnacle_props import rules
    from pinnacle_props.bet_store import BetStore
    from tests.test_bet_store import Spreadsheet,record
    from tests.test_rules_app import fixture_rows
    import pinnacle_props.performance_ui as ui
    monkeypatch.delenv('PP_SETTLEMENT_ENABLED',raising=False)
    monkeypatch.setattr('pinnacle_props.settings.ROOT',tmp_path)
    monkeypatch.setattr(rules,'LIVE_PATH',tmp_path/'rules.json')
    sheet=Spreadsheet();store=BetStore(tmp_path,spreadsheet=sheet)
    store.append_real([record(event_id='fixture-event',player='Fixture 0',market='player_assists',line=2.5,side='under')])
    monkeypatch.setattr('pinnacle_props.bet_store.session_store',lambda *a,**k:store)
    monkeypatch.setattr('pinnacle_props.bet.read_bets',lambda *a,**k:store.read('real'))
    monkeypatch.setattr(ui,'render',lambda *a,**k:None)
    def replay(sport,**kwargs):
        f=fixture_rows(sport)
        return dict(scan_id='fixture',capture_time='2026-10-09T12:00:00Z',profile_name='Fixture',event_results=[],credits_charged=0,raw_rows=len(f)*4,candidate_pairs=len(f),exclusions={}),f,pd.DataFrame()
    monkeypatch.setattr('pinnacle_props.pipeline.replay',replay)
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:(_ for _ in ()).throw(AssertionError('network forbidden')))
    app=AppTest.from_file(Path('app.py').resolve())
    app.secrets['gcp_service_account']={'project_id':'fixture'}
    app.run(timeout=20)
    assert not app.exception
    assert app.button(key='log_fixture_1').disabled
    table=next(d.value for d in app.dataframe if '⚠' in d.value)
    assert table.iloc[0]['⚠']=='🔴 ALREADY BET' and str(table.index[0]).startswith('🔴')
    assert any('already in your bet log' in w.value for w in app.warning)
    assert any('🔴 Fixture 0' in m.value for m in app.markdown)
    app.checkbox(key='allow_real_duplicates').check().run()
    assert not app.exception and not app.button(key='log_fixture_1').disabled
    sheet.worksheet=lambda _:(_ for _ in ()).throw(RuntimeError('offline'))
    app.button(key='reload_real_check').click().run()
    assert not app.exception and app.button(key='log_fixture_1').disabled
    assert app.button(key='log_selected').disabled
    assert any('CHECK UNAVAILABLE' in w.value for w in app.warning)
