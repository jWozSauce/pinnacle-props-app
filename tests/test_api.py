import csv
import gzip
import json
from pathlib import Path
import requests
import pytest
from pinnacle_props.odds_api import (Client, APIError, BudgetStop, estimated_cost,
                                     normalize, coverage, resolve_key)

KEY='dummy-key-for-testing'
SPORT='basketball_nba'
BOOKS=['pinnacle','draftkings']
MARKETS=['player_points','player_rebounds']


def payload():
    return {'id':'game-1','sport_key':SPORT,'sport_title':'NBA','commence_time':'2026-10-08T00:00:00Z',
            'home_team':'Home','away_team':'Away','bookmakers':[
                {'key':book,'markets':[{'key':'player_points','last_update':'2026-10-07T18:00:00Z',
                                       'outcomes':[{'name':'Over','description':'A Player','point':24.5,'price':1.9},
                                                   {'name':'Under','description':'A Player','point':24.5,'price':1.9}]}]}
                for book in BOOKS]}


class Response:
    def __init__(self,body=None,status=200,last=0,remaining=900000,headers=True):
        self.text=json.dumps([] if body is None else body)
        self.status_code=status
        self.headers={'x-requests-last':str(last),'x-requests-remaining':str(remaining),'x-requests-used':'10'} if headers else {}


class Session:
    def __init__(self,*results): self.results=list(results);self.calls=[]
    def get(self,url,**kwargs):
        self.calls.append((url,kwargs))
        result=self.results.pop(0)
        if isinstance(result,Exception): raise result
        return result


def client(tmp_path,*results,**kwargs):
    return Client(data_dir=tmp_path,key=KEY,session=Session(*results),min_interval=0,run_cap=10,**kwargs)


def state(c): return json.loads((c.data_dir/'odds_budget.json').read_text())
def pull(c):return c.event_odds(SPORT,'game-1',MARKETS,BOOKS)


def test_cost_rule_and_unknown_endpoint():
    path=f'/v4/sports/{SPORT}/events/game-1/odds'
    assert estimated_cost('/v4/sports',{})==0
    assert estimated_cost(f'/v4/sports/{SPORT}/events',{})==0
    assert estimated_cost(path,{'markets':'a,b,a','bookmakers':','.join('book'+str(i) for i in range(11))})==4
    assert estimated_cost(path,{'markets':'a,b','regions':'us,eu'})==4
    with pytest.raises(ValueError):estimated_cost('/v4/historical/sports/nba/odds',{})


def test_paid_response_reserved_reconciled_and_cached(tmp_path):
    c=client(tmp_path,Response(),Response(payload(),last=1),run_id='probe')
    result=pull(c)
    assert result.status==200 and not result.cached
    assert pull(c).cached
    assert len(c.session.calls)==2
    assert state(c)['spent']==1 and not state(c)['reservations']
    ledger=list(csv.DictReader((tmp_path/'odds_usage.csv').open()))
    assert len(ledger)==2 and ledger[1]['estimated']=='2' and ledger[1]['actual']=='1'
    assert all(KEY not in p.read_text() for p in tmp_path.glob('*.json'))
    envelope=json.loads(gzip.decompress(Path(result.cache_path).read_bytes()))
    assert 'apiKey' not in envelope['params'] and KEY not in json.dumps(envelope)
    assert len(normalize(result.payload))==4
    assert sum(r['returned'] for r in coverage(result.payload,MARKETS,BOOKS))==2
    # Explicit same-run cache replay works with no credential/network, including restart.
    restarted=Client(data_dir=tmp_path,run_id='probe',run_cap=10,session=Session())
    assert pull(restarted).cached


def test_floor_and_run_and_project_limits_prevent_send(tmp_path):
    c=client(tmp_path/'floor',Response(remaining=100001))
    with pytest.raises(BudgetStop,match='floor'):pull(c)
    assert len(c.session.calls)==1
    c=client(tmp_path/'project',Response(),project_cap=1)
    with pytest.raises(BudgetStop,match='Project'):pull(c)
    assert len(c.session.calls)==1
    c=Client(data_dir=tmp_path/'run',run_cap=1,key=KEY,session=Session(Response()),min_interval=0)
    with pytest.raises(BudgetStop,match='Run'):pull(c)
    assert len(c.session.calls)==1


def test_missing_headers_and_network_failure_reserve_and_stop(tmp_path):
    for dirname,result in [('headers',Response(payload(),headers=False)),
                           ('network',requests.ConnectionError('apiKey='+KEY))]:
        c=client(tmp_path/dirname,Response(),result)
        with pytest.raises(BudgetStop) as err:pull(c)
        assert KEY not in str(err.value)
        assert len(state(c)['reservations'])==1 and state(c)['halt']
        paths=list((c.data_dir/'raw').rglob('*.gz'))
        assert len(paths)==2
        assert all(KEY.encode() not in gzip.decompress(p.read_bytes()) for p in paths)
        restarted=client(tmp_path/dirname)
        with pytest.raises(BudgetStop):pull(restarted)
        assert not restarted.session.calls


@pytest.mark.parametrize('status',[401,403,404,422,429,500])
def test_error_status_is_recorded_without_retry(tmp_path,status):
    c=client(tmp_path,Response(),Response({'message':KEY},status=status,last=0))
    with pytest.raises(APIError):pull(c)
    assert len(c.session.calls)==2
    assert state(c)['records'][-1]['status']==status
    assert state(c)['spent']==0
    assert not state(c)['reservations']
    assert all(KEY.encode() not in gzip.decompress(p.read_bytes()) for p in (tmp_path/'raw').rglob('*.gz'))


def test_charge_above_bound_or_wrong_returned_market_cost_stops(tmp_path):
    for charge in [2,3]:
        c=client(tmp_path/str(charge),Response(),Response(payload(),last=charge))
        with pytest.raises(BudgetStop,match='charge'):pull(c)
        assert state(c)['spent']==charge


def test_empty_response_costs_zero(tmp_path):
    empty=payload();empty['bookmakers']=[]
    c=client(tmp_path,Response(),Response(empty,last=0))
    assert normalize(pull(c).payload).empty
    assert state(c)['spent']==0


def test_malformed_json_cached_before_parsing(tmp_path):
    bad=Response(last=1);bad.text='not-json'
    c=client(tmp_path,Response(),bad)
    with pytest.raises(BudgetStop,match='JSON'):pull(c)
    assert state(c)['spent']==1
    assert any(b'not-json' in gzip.decompress(p.read_bytes()) for p in (tmp_path/'raw').rglob('*.gz'))


def test_reservation_exists_during_transport_and_excludes_other_process(tmp_path):
    c=client(tmp_path,Response(),Response(payload(),last=1))
    original=c.session.get
    def inspect(url,**kwargs):
        assert len(state(c)['reservations'])==1
        other=client(tmp_path)
        with pytest.raises(BudgetStop,match='running'):other.sports()
        return original(url,**kwargs)
    c.session.get=inspect
    pull(c)


def test_fresh_client_refreshes_account_quota(tmp_path):
    c=client(tmp_path,Response(),Response(payload(),last=1))
    pull(c)
    d=client(tmp_path,Response(remaining=90000))
    with pytest.raises(BudgetStop,match='floor'):pull(d)
    assert len(d.session.calls)==1


def test_key_chain_and_no_missing_key_exception_leak(tmp_path,monkeypatch):
    root=tmp_path/'project';root.mkdir()
    monkeypatch.delenv('ODDS_API_KEY',raising=False)
    with pytest.raises(APIError,match='No Odds API credential'):resolve_key(root)
    (root/'.env').write_text('ODDS_API_KEY=dotenv-test\n')
    assert resolve_key(root,{'ODDS_API_KEY':'streamlit-test'})=='dotenv-test'
    monkeypatch.setenv('ODDS_API_KEY','env-test')
    assert resolve_key(root)=='env-test'
    monkeypatch.delenv('ODDS_API_KEY');(root/'.env').unlink()
    assert resolve_key(root,{'ODDS_API_KEY':'streamlit-test'})=='streamlit-test'


def test_invalid_normalize_is_not_empty_success():
    for bad in [None,{}, {'bookmakers':[]}]:
        with pytest.raises(APIError):normalize(bad)


def test_failed_accounting_cannot_be_bypassed_by_cache(tmp_path):
    c=client(tmp_path,Response(),Response(payload(),last=3))
    with pytest.raises(BudgetStop):pull(c)
    with pytest.raises(APIError,match='accounting'):pull(c)
    assert len(c.session.calls)==2


def test_wrong_event_identity_fails_before_normalization(tmp_path):
    wrong=payload();wrong['id']='different-game'
    c=client(tmp_path,Response(),Response(wrong,last=1))
    with pytest.raises(BudgetStop,match='schema'):pull(c)
    assert state(c)['spent']==1

def test_successful_key_resolution_reused_in_memory_for_quota_and_paid(tmp_path,monkeypatch):
    calls=[]
    def resolver():
        calls.append(1)
        if len(calls)>1:raise APIError('Transient shared document failure')
        return KEY
    monkeypatch.setattr('pinnacle_props.odds_api.resolve_key',resolver)
    c=Client(data_dir=tmp_path,run_id='reuse-secret',run_cap=10,
             session=Session(Response(),Response(payload(),last=1)),min_interval=0)
    assert pull(c).status==200 and len(calls)==1
    assert all(KEY not in p.read_text() for p in tmp_path.glob('*.json'))

def test_agent_key_requires_local_env_and_never_uses_shared_fallback(tmp_path,monkeypatch):
    from pinnacle_props.odds_api import resolve_agent_key
    monkeypatch.setenv('ODDS_API_KEY','environment-must-not-bypass-agent-file')
    monkeypatch.setattr('pinnacle_props.odds_api.subprocess.run',lambda *a,**k:pytest.fail('Shared fallback used'))
    with pytest.raises(APIError,match='no requests sent'):resolve_agent_key(tmp_path)
    (tmp_path/'.env').write_text('ODDS_API_KEY=\n')
    with pytest.raises(APIError,match='no requests sent'):resolve_agent_key(tmp_path)
    (tmp_path/'.env').write_text('ODDS_API_KEY=agent-test-only\n')
    assert resolve_agent_key(tmp_path)=='agent-test-only'

def test_close_lock_waits_and_times_out_without_request(tmp_path,monkeypatch,capsys):
    import pinnacle_props.odds_api as api
    original=api.fcntl.flock;clock=[0.0];tries=[0]
    monkeypatch.setattr(api.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(api.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    def busy_then_free(handle,flags):
        if flags & api.fcntl.LOCK_NB:
            tries[0]+=1
            if tries[0]<=2:raise BlockingIOError()
        return original(handle,flags)
    monkeypatch.setattr(api.fcntl,'flock',busy_then_free)
    client=Client(data_dir=tmp_path,run_id='close-wait-test',session=Session(),key=KEY)
    with client._locked():pass
    assert clock[0]==4 and 'acquired after 4.000s' in capsys.readouterr().out
    def busy(*args):raise BlockingIOError()
    monkeypatch.setattr(api.fcntl,'flock',busy)
    with pytest.raises(BudgetStop):
        with client._locked():pass
    assert clock[0]==184 and not client.session.calls
    assert 'timeout after 180.000s' in capsys.readouterr().out
    other=Client(data_dir=tmp_path,run_id='historical-test',session=Session(),key=KEY)
    with pytest.raises(BudgetStop):
        with other._locked():pass
    assert clock[0]==184

def test_paid_close_deadline_is_checked_under_lock(tmp_path,monkeypatch):
    monkeypatch.setattr('pinnacle_props.odds_api.utcnow',lambda:'2026-10-07T23:59:00+00:00')
    session=Session(Response())
    c=Client(data_dir=tmp_path,run_id='close-late',run_cap=1,key=KEY,session=session,
             paid_deadline='2026-10-07T23:58:00Z')
    with pytest.raises(BudgetStop,match='start reached'):pull(c)
    assert len(session.calls)==1 and c._load()['spent']==0


def test_cloud_has_no_daily_limit_but_reserve_and_floor_survive_restart(tmp_path,monkeypatch):
    first=client(tmp_path,Response(),Response(payload(),last=1),run_id='cloud-first',cloud=True)
    ledger=first._load();ledger.update(spent=3000,cloud_daily_cap=0,runs={'prior':3000},
        records=[dict(request_id='prior',run_id='prior',timestamp='2026-10-21T00:00:00+00:00',actual=3000,estimated=3000,outcome='ok')])
    first._save(ledger)
    pull(first)
    assert state(first)['spent']==3001 and first.project_cap==100000
    second=client(tmp_path,Response(),run_id='cloud-second',cloud=True)
    ledger=second._load();ledger['spent']=99999;ledger['records'][0]['actual']=99998;ledger['runs']['prior']=99998;second._save(ledger)
    with pytest.raises(BudgetStop,match='Project credit cap'):pull(second)
    assert len(second.session.calls)==1 and state(second)['spent']==99999
    # Lifetime reserve does not reopen at midnight; account-floor preflight remains.
    monkeypatch.setattr('pinnacle_props.odds_api.utcnow',lambda:'2026-10-22T04:01:00+00:00')
    third=client(tmp_path,Response(remaining=100001),run_id='cloud-third',cloud=True)
    ledger=third._load();ledger['spent']=3001;ledger['records'][0]['actual']=3000;ledger['runs']['prior']=3000;third._save(ledger)
    with pytest.raises(BudgetStop,match='floor'):pull(third)
    assert len(third.session.calls)==1 and state(third)['spent']==3001


def test_cloud_accounting_stays_isolated_without_daily_setting(tmp_path,monkeypatch):
    monkeypatch.setattr('pinnacle_props.settings.ACCOUNTING_PROJECT_ROOT',tmp_path)
    c=client(tmp_path/'data',cloud=True)
    assert c.accounting_dir==tmp_path/'data' and c.project_cap==100000


def test_budget_reader_waits_for_writer_replace(tmp_path,monkeypatch):
    import threading
    import pinnacle_props.odds_api as api
    c=Client(data_dir=tmp_path,lock_timeout=5)
    c._save(c._load())
    entered=threading.Event();release=threading.Event();read_started=threading.Event();read_done=threading.Event();errors=[];result=[]
    original=api.os.replace
    def delayed_replace(source,target):
        if Path(target).name=='odds_budget.json':
            Path(target).unlink()  # Simulate observed external-volume visibility gap.
            entered.set();assert release.wait(5)
        return original(source,target)
    monkeypatch.setattr(api.os,'replace',delayed_replace)
    state=c._load();state['remaining']=123456
    def writer():
        try:c._save(state)
        except Exception as e:errors.append(e)
    def reader():
        read_started.set()
        try:result.append(Client(data_dir=tmp_path,lock_timeout=5)._load())
        except Exception as e:errors.append(e)
        finally:read_done.set()
    w=threading.Thread(target=writer);w.start();assert entered.wait(5)
    r=threading.Thread(target=reader);r.start();assert read_started.wait(5)
    assert not read_done.wait(.1)
    release.set();w.join(5);r.join(5)
    assert not errors and result[0]['remaining']==123456


def test_missing_budget_retries_five_seconds_then_fails_closed(tmp_path,monkeypatch):
    import pinnacle_props.odds_api as api
    c=Client(data_dir=tmp_path);c._save(c._load());(tmp_path/'odds_budget.json').unlink()
    clock=[0.0]
    monkeypatch.setattr(api.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(api.time,'sleep',lambda delay:clock.__setitem__(0,clock[0]+delay))
    with pytest.raises(BudgetStop,match='after 5 seconds'):c._load()
    assert clock[0]==5 and not (tmp_path/'odds_budget.json').exists()


def test_transient_missing_read_recovers_without_budget_reset(tmp_path,monkeypatch):
    import pinnacle_props.odds_api as api
    c=Client(data_dir=tmp_path);s=c._load();s['remaining']=654321;c._save(s)
    original=Path.read_text;reads=[0]
    def transient(path,*args,**kwargs):
        if path==tmp_path/'odds_budget.json':
            reads[0]+=1
            if reads[0]<3:raise FileNotFoundError()
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',transient)
    assert c._load()['remaining']==654321 and reads[0]==3
