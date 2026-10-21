from datetime import datetime,timedelta,timezone
from pathlib import Path
import json
import pytest
from pinnacle_props import odds_api as api
from pinnacle_props.ledger_history import migrate,full_records
from test_api import client,pull,Response,payload,Session
from test_request_pool import run,Transport


def old_ledger(tmp_path,monkeypatch):
    stamp=(datetime.now(timezone.utc)-timedelta(days=5)).isoformat()
    with monkeypatch.context() as patch:
        patch.setattr(api,'utcnow',lambda:stamp)
        c=client(tmp_path,Response(),Response(payload(),last=1),run_id='old')
        pull(c)
    return c


def test_migration_backup_cache_replay_and_full_provenance(tmp_path,monkeypatch):
    c=old_ledger(tmp_path,monkeypatch);before=c._load();raw=(tmp_path/'odds_budget.json').read_bytes()
    report=migrate(c);after=c._load()
    assert report['archived_records']==2 and after['records']==[] and after['spent']==1
    assert full_records(tmp_path,after)==before['records'] and after['runs']==before['runs']
    assert (Path(report['backup'])/'odds_budget.json').read_bytes()==raw
    assert migrate(c)['status']=='already migrated'
    restarted=api.Client(data_dir=tmp_path,run_id='old',run_cap=10,session=Session())
    assert pull(restarted).cached
    with pytest.raises(api.BudgetStop,match='legacy'):c._save(before)
    assert c._load()['version']==2


def test_compact_pool_keeps_cap_accounting_and_full_history(tmp_path,monkeypatch):
    c=old_ledger(tmp_path,monkeypatch);migrate(c)
    c,p,t,results,errors=run(tmp_path,limit=6,cap=2,transport=Transport())
    assert any(errors) and len(t.paid)<=2
    state=c._load();assert not state['reservations'] and state['spent']==1+len(t.paid)
    assert state['spent']==sum(r['actual'] or 0 for r in full_records(tmp_path,state))
    assert state['runs']['old']==1 and state['runs'].get('pool-test',0)<=2


def test_archive_corruption_or_metadata_tamper_fails_closed(tmp_path,monkeypatch):
    c=old_ledger(tmp_path,monkeypatch);migrate(c);state=c._load()
    state['history'][0]['spent']+=1;state['archived_spent']+=1;state['spent']+=1
    (tmp_path/'odds_budget.json').write_text(json.dumps(state))
    with pytest.raises(api.BudgetStop):c._load()


def test_missing_archive_fails_closed(tmp_path,monkeypatch):
    c=old_ledger(tmp_path,monkeypatch);migrate(c);state=c._load()
    (tmp_path/'odds_history'/state['history'][0]['name']).unlink()
    with pytest.raises(api.BudgetStop):c._load()


def test_interrupted_migration_before_pointer_keeps_original_ledger(tmp_path,monkeypatch):
    c=old_ledger(tmp_path,monkeypatch);before=c._load();raw=(tmp_path/'odds_budget.json').read_bytes()
    original=api.atomic_write
    def fail(path,body):
        if Path(path)==tmp_path/'odds_budget.json':raise OSError('fixture interruption')
        return original(path,body)
    with monkeypatch.context() as patch:
        patch.setattr(api,'atomic_write',fail)
        with pytest.raises(OSError):migrate(c)
    assert (tmp_path/'odds_budget.json').read_bytes()==raw and c._load()==before
    migrate(c)
    assert full_records(tmp_path,c._load())==before['records']
    assert len(list((tmp_path/'odds_history').glob('*.csv')))==1


def test_unresolved_reservations_prevent_migration(tmp_path,monkeypatch):
    c=old_ledger(tmp_path,monkeypatch);state=c._load();row=state['records'][-1]
    row['actual']=None;state['spent']=0;state['reservations']={row['request_id']:{'estimated':row['estimated'],'run_id':'old'}}
    c._save(state)
    with pytest.raises(api.BudgetStop,match='reservations'):migrate(c)
    assert c._load()['version']==1


def test_close_daily_total_survives_history_compaction(tmp_path,monkeypatch):
    c=old_ledger(tmp_path,monkeypatch);state=c._load();state['records'][-1]['run_id']='close-fixture';c._save(state)
    from zoneinfo import ZoneInfo
    day=datetime.fromisoformat(state['records'][-1]['timestamp']).astimezone(ZoneInfo('America/New_York')).date().isoformat()
    assert api.close_day_spend(state,day)==1
    migrate(c)
    assert api.close_day_spend(c._load(),day)==1
