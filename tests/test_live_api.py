from pathlib import Path
import json
import pytest
from pinnacle_props import odds_api as api
from pinnacle_props.live_api import LiveClient,operational_client
from pinnacle_props.request_pool import Pool
from test_api import Session,Response,payload
from test_request_pool import Transport


def pull(c,event='game-1'):return c.event_odds('basketball_nba',event,['player_points'],['pinnacle'])


def test_live_ignores_legacy_halt_cap_and_preserves_every_ledger_byte(tmp_path):
    for name in ('odds_budget.json','odds_usage.csv','odds_history/old.csv'):
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'legacy read only')
    before={str(p):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    log=[];s=Session(Response(),Response(payload(),last=1))
    c=LiveClient(data_dir=tmp_path,run_cap=1,key='fixture',session=s,min_interval=0,progress=log.append)
    pull(c)
    assert c.charged==1 and c.accounting()['account_check']['passed']
    assert len(s.calls)==2 and any('floor: 100,000; PASS' in v for v in log)
    assert all(Path(n).read_bytes()==v for n,v in before.items())
    assert not (tmp_path/'odds.lock').exists() and not (tmp_path/'inflight').exists()


@pytest.mark.parametrize('remaining',[100000,100001,None,'broken'])
def test_floor_checks_whole_estimate_before_any_paid_send(tmp_path,remaining):
    s=Session(Response(remaining=remaining));c=LiveClient(data_dir=tmp_path,run_cap=2,key='fixture',session=s,min_interval=0)
    with pytest.raises(api.BudgetStop):pull(c)
    assert len(s.calls)==1 and '/events/' not in s.calls[0][0]
    assert not (tmp_path/'odds_budget.json').exists()


def test_concurrent_live_run_one_free_check_no_ledger_and_manifest_charges(tmp_path):
    pool=Pool(3);transport=Transport(delay=.04)
    c=LiveClient(data_dir=tmp_path,run_id='live-run',run_cap=6,key='fixture',session=transport,pool=pool,min_interval=0)
    results,errors=pool.map(['g'+str(i) for i in range(6)],lambda e:pull(c,e))
    assert not any(errors) and c.charged==6 and transport.maximum>1
    assert len(c.records)==7 # exactly one free check plus six paid responses
    assert not (tmp_path/'odds_budget.json').exists() and len(c.accounting()['requests'])==6
    assert [r.payload['id'] for r in results]==['g'+str(i) for i in range(6)]


def test_cached_live_response_recovers_without_paid_request_or_ledger(tmp_path):
    c=LiveClient(data_dir=tmp_path,run_id='same',run_cap=1,key='fixture',session=Session(Response(),Response(payload(),last=1)),min_interval=0)
    original=pull(c)
    session=Session();restart=LiveClient(data_dir=tmp_path,run_id='same',run_cap=1,key='fixture',session=session,resume_only=True)
    assert pull(restart).cached and restart.charged==1 and not session.calls
    assert restart.accounting()['requests'][0]['request_id']==original.request_id
    with pytest.raises(api.BudgetStop,match='Prior run'):pull(restart,'different')
    assert not session.calls


def test_unknown_charge_stops_without_retry_and_replay_does_not_spend(tmp_path):
    s=Session(Response(),Response(payload(),last=None))
    c=LiveClient(data_dir=tmp_path,run_id='unknown',run_cap=2,key='fixture',session=s,min_interval=0)
    with pytest.raises(api.BudgetStop):pull(c)
    assert len(s.calls)==2 and not c.accounting()['charges_complete']
    restart=LiveClient(data_dir=tmp_path,run_id='unknown',run_cap=2,key='fixture',session=Session())
    with pytest.raises(api.BudgetStop):pull(restart)
    assert not restart.session.calls


def test_duplicate_in_same_pool_is_charged_once(tmp_path):
    pool=Pool(3);t=Transport()
    c=LiveClient(data_dir=tmp_path,run_id='duplicate',run_cap=1,key='fixture',session=t,pool=pool,min_interval=0)
    results,errors=pool.map(['same']*6,lambda e:pull(c,e))
    assert not any(errors) and c.charged==1 and len(t.paid)==1


def test_budgeted_research_still_refuses_cap_and_large_live_requires_staging(tmp_path):
    c=operational_client(research=True,data_dir=tmp_path,run_cap=0,key='fixture',session=Session(Response()),min_interval=0)
    assert type(c) is api.Client
    with pytest.raises(api.BudgetStop,match='Run credit cap'):pull(c)
    assert len(c.session.calls)==1
    with pytest.raises(ValueError):LiveClient(data_dir=tmp_path,run_cap=20001)


def test_live_stops_after_game_start_during_free_preflight(tmp_path,monkeypatch):
    stamp=['2099-01-01T00:00:00+00:00'];monkeypatch.setattr(api,'utcnow',lambda:stamp[0])
    class Cross(Session):
        def get(self,*a,**kw):
            result=super().get(*a,**kw);stamp[0]='2099-01-01T00:02:00+00:00';return result
    s=Cross(Response());c=LiveClient(data_dir=tmp_path,run_cap=1,key='fixture',session=s,min_interval=0,paid_deadline='2099-01-01T00:01:00Z')
    with pytest.raises(api.BudgetStop,match='Game start'):pull(c)
    assert len(s.calls)==1


def test_verified_zero_charge_429_retries_with_backoff_and_no_ledger(tmp_path):
    pool=Pool(3);transport=Transport(rate_limit=True)
    client=LiveClient(data_dir=tmp_path,run_cap=3,key='fixture',session=transport,pool=pool,min_interval=0)
    values,errors=pool.map(['a','b','c'],lambda event:pull(client,event))
    assert not any(errors) and client.charged==3 and pool.limit==1
    attempts=[at for event,at in transport.calls if event==transport.limited_event]
    assert attempts[-1]-attempts[0]>=.04
    assert len(client.accounting()['requests'])==4
    assert not (tmp_path/'odds_budget.json').exists()


def test_paid_charge_survives_raw_write_failure_in_run_manifest(tmp_path,monkeypatch):
    from pinnacle_props.live_api import failure_receipt
    client=LiveClient(data_dir=tmp_path,run_cap=1,key='fixture',session=Session(Response(),Response(payload(),last=1)),min_interval=0)
    original=api.atomic_write
    def fail_paid(path,data):
        import gzip
        if str(path).endswith('.gz') and '/events/' in json.loads(gzip.decompress(data))['endpoint']:
            raise OSError('simulated full raw volume')
        original(path,data)
    monkeypatch.setattr(api,'atomic_write',fail_paid)
    path=tmp_path/'incomplete_scans/receipt.json'
    with pytest.raises(OSError),failure_receipt(client,path,{'run_id':client.run_id}):pull(client)
    saved=json.loads(path.read_text())
    assert saved['accounting']['credits_charged']==1 and saved['accounting']['charges_complete']
    assert 'simulated' not in path.read_text() and not (tmp_path/'odds_budget.json').exists()


def test_resume_refusal_does_not_erase_previous_failure_charge(tmp_path):
    from pinnacle_props.live_api import failure_receipt
    path=tmp_path/'closes/failed.json';path.parent.mkdir()
    prior={'policy':'account-floor-v1','run_id':'interrupted','requests':[
        {'request_id':'lost-raw','credits':5,'at':'2026-10-21T20:00:00Z'}]}
    path.write_text(json.dumps({'accounting':prior}))
    client=LiveClient(data_dir=tmp_path,run_id='interrupted',run_cap=5,key='fixture',session=Session(),resume_only=True)
    with pytest.raises(api.BudgetStop),failure_receipt(client,path,{'run_id':client.run_id}):pull(client)
    assert json.loads(path.read_text())['accounting']['credits_charged']==5
    assert not client.session.calls
