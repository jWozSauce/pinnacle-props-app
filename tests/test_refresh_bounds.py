import json,time,threading
from pathlib import Path
import pandas as pd
import pytest
from pinnacle_props.bet_store import BetStore,HEADER,TABS
from pinnacle_props.bounded_io import Deadline,WaitUnavailable
from pinnacle_props.refresh_perf import RefreshPerf
from tests.test_bet_store import Spreadsheet,record

class BatchSheet(Spreadsheet):
    def __init__(self):
        super().__init__();self.reads=0;self.metadata=0;self.writes_count=0
        for name in TABS.values():
            w=self.add_worksheet(name,5000,len(HEADER));w.title=name;w.values=[HEADER.copy()]
    def worksheets(self):self.metadata+=1;return list(self.tabs.values())
    def values_batch_get(self,ranges):
        self.reads+=1
        assert len(ranges)==2
        return {'valueRanges':[{'values':w.get_all_values()} for w in self.tabs.values()]}
    def batch_update(self,body):self.writes_count+=1;return super().batch_update(body)


def test_refresh_batches_both_logs_and_all_panels_for_sixty_seconds(tmp_path):
    now=[0];sheet=BatchSheet();store=BetStore(tmp_path,spreadsheet=sheet,interactive=True,clock=lambda:now[0])
    for rerun in range(30):
        now[0]=rerun*2
        # linked, already bet, initial Live, fragment Live, Logged/Performance/Paper
        store.read('paper');store.real_keys()
        for panel in range(5):
            for kind in TABS:store.read(kind)
    assert sheet.reads==1 and sheet.metadata==1
    assert store.append('paper',[record(row=i) for i in range(100)])['written']==1 # contract dedupe
    assert sheet.reads==1 and sheet.writes_count==1
    assert len(store.read('paper'))==1
    now[0]=61
    store.read('real');store.read('paper');store.real_keys()
    assert sheet.reads==2 and sheet.metadata==1


def test_outbox_lost_ack_reconciles_one_identity_without_retry(tmp_path):
    now=[0];sheet=BatchSheet();store=BetStore(tmp_path,spreadsheet=sheet,interactive=True,clock=lambda:now[0])
    original=sheet.batch_update;done=threading.Event();deadline=Deadline(.03)
    def uncertain(body):
        def send():
            time.sleep(.10);original(body);done.set()
        return deadline.call(send)
    sheet.batch_update=uncertain;r=record()
    start=time.monotonic();result=store.append('paper',[r]);assert time.monotonic()-start<.2
    assert result['queued']==1 and result['written']==0
    assert store.real_keys() is None
    assert done.wait(1)
    assert len(sheet.tabs[TABS['paper']].values)==2
    # No automatic replay. Explicit later reconciliation reads the successful late write.
    now[0]=61;sheet.batch_update=original
    result=store.append('paper',[r]);assert result['written']==0 and result['queued']==0
    assert sheet.writes_count==1 and len(sheet.tabs[TABS['paper']].values)==2


def test_no_retry_storm_and_rate_budget():
    now=[0];d=Deadline(.1,limit=4,clock=lambda:now[0]);calls=[]
    for _ in range(4):assert d.call(lambda:calls.append(1)) is None
    with pytest.raises(WaitUnavailable):d.call(lambda:calls.append(1))
    assert len(calls)==4
    now[0]=60;d.call(lambda:calls.append(1));assert len(calls)==5


def test_failed_batch_cached_for_all_panels(tmp_path):
    sheet=BatchSheet();count=[]
    def fail(_):count.append(1);raise TimeoutError()
    sheet.values_batch_get=fail
    store=BetStore(tmp_path,spreadsheet=sheet,interactive=True)
    for _ in range(10):
        for k in TABS:assert store.read(k).empty
        assert store.real_keys() is None
    assert len(count)==1
    assert 'Log unavailable, retry' in store.warning


def test_real_submission_forces_fresh_guard(tmp_path):
    sheet=BatchSheet();store=BetStore(tmp_path,spreadsheet=sheet,interactive=True)
    store.read('real');r=record(event_id='e');sheet.tabs[TABS['real']].values.append([r[h] for h in HEADER])
    result=store.append_real([record(event_id='e')])
    assert result['skipped']==1 and result['written']==0
    assert sheet.reads==2 and sheet.writes_count==0


def test_perf_logs_failed_phase_and_memory(tmp_path):
    captions=[];perf=RefreshPerf(tmp_path,captions.append)
    with pytest.raises(ValueError):
        with perf.phase('sheet'):raise ValueError('sensitive detail')
    result=perf.finish('failed')
    files=list((tmp_path/'perf').glob('*.json'));assert len(files)==1
    assert json.loads(files[0].read_text())==result
    assert result['rss_mb'] is None or result['rss_mb']>0
    assert 'sensitive' not in files[0].read_text()
    assert 'sheet' in captions[-1] and result['phases']['sheet']>=0


def test_live_paused_never_reads_or_reruns(tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    from pinnacle_props import live_ui
    monkeypatch.setattr(live_ui,'cards',lambda *a,**k:pytest.fail('read during scan'))
    app=AppTest.from_string("""import streamlit as st
from pinnacle_props.live_ui import render_live
st.session_state['pp_scan_running']=True
render_live('/tmp/no-live-read',store=object(),feed=object())
""").run()
    assert not app.exception and 'paused' in app.info[0].value


def test_full_app_shares_one_read_across_all_tabs(tmp_path,monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    from pinnacle_props import rules
    from tests.test_rules_app import fixture_rows
    sheet=BatchSheet();store=BetStore(tmp_path,spreadsheet=sheet,interactive=True)
    monkeypatch.delenv('PP_SETTLEMENT_ENABLED',raising=False)
    monkeypatch.setattr(rules,'LIVE_PATH',tmp_path/'rules.json')
    monkeypatch.setattr('pinnacle_props.performance_ui.BetStore',lambda *a,**k:store)
    monkeypatch.setattr('pinnacle_props.bet_store.session_store',lambda *a,**k:store)
    monkeypatch.setattr('pinnacle_props.settings.ROOT',tmp_path)
    monkeypatch.setattr('pinnacle_props.bet.read_bets',lambda *a,**k:store.read('real'))
    monkeypatch.setattr('pinnacle_props.clv.rebuild',lambda **k:pd.DataFrame())
    def replay(sport,**kw):
        f=fixture_rows(sport)
        return dict(scan_id='fixture',capture_time='2026-10-09T12:00:00Z',profile_name='Fixture',event_results=[],credits_charged=0,raw_rows=len(f)*4,candidate_pairs=len(f),exclusions={}),f,pd.DataFrame()
    monkeypatch.setattr('pinnacle_props.pipeline.replay',replay)
    app=AppTest.from_file(Path('app.py').resolve());app.secrets['gcp_service_account']={'project_id':'fixture'}
    app.run(timeout=20)
    assert not app.exception and len(app.tabs)==6
    assert sheet.reads==1 and sheet.metadata==1 and sheet.writes_count==0
    app.radio(key='view_mode').set_value('Table').run()
    assert not app.exception and sheet.reads==1
