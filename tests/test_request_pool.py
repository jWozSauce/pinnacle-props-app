import json
import threading
import time
from copy import deepcopy
import pandas as pd
import pytest
from pinnacle_props.request_pool import Pool,workers,retry_after
from pinnacle_props.odds_api import Client,BudgetStop
from test_api import Response,payload,SPORT,BOOKS


class Transport:
    def __init__(self,delay=.03,remaining=900000,rate_limit=False):
        self.lock=threading.Lock();self.active=0;self.maximum=0;self.paid=[];self.remaining=remaining
        self.delay=delay;self.rate_limit=rate_limit;self.limited_at=None;self.calls=[]
    def get(self,url,**kwargs):
        event=url.split('/')[-2]
        with self.lock:
            self.calls.append((event,time.monotonic()))
            if url.endswith('/v4/sports'):return Response(remaining=self.remaining)
            self.active+=1;self.maximum=max(self.maximum,self.active)
            self.paid.append(event);number=len(self.paid)
            limited=self.rate_limit and number==1
            if limited:self.limited_at=time.monotonic();self.limited_event=event
        try:
            time.sleep(self.delay)
            if limited:
                response=Response(status=429,last=0,remaining=self.remaining)
                response.headers['Retry-After']='0.04';return response
            body=payload();body['id']=event;body['commence_time']='2099-01-01T00:00:00Z'
            response=Response(body,last=1,remaining=self.remaining-number)
            response.headers['x-requests-used']=str(number+10)
            return response
        finally:
            with self.lock:self.active-=1


def run(tmp_path,limit=3,cap=10,transport=None,events=None):
    pool=Pool(limit);transport=transport or Transport()
    c=Client(data_dir=tmp_path,key='test-only',run_id='pool-test',run_cap=cap,pool=pool,session=transport,min_interval=0)
    results,errors=pool.map(events or ['g'+str(i) for i in range(6)],lambda e:c.event_odds(SPORT,e,['player_points'],BOOKS))
    return c,pool,transport,results,errors


def test_overlap_accounting_content_order_and_out_of_order_quota(tmp_path):
    c,p,t,results,errors=run(tmp_path)
    assert not any(errors) and 1<t.maximum<=3
    assert [r.payload['id'] for r in results]==['g'+str(i) for i in range(6)]
    state=c._load();assert state['spent']==6 and state['runs']['pool-test']==6 and not state['reservations']
    assert state['remaining']==900000-6
    assert len([x for x in state['records'] if x['actual']==1])==6


@pytest.mark.parametrize('cap,remaining',[(2,900000),(10,100002)])
def test_parallel_reservations_cannot_race_cap_or_floor(tmp_path,cap,remaining):
    c,p,t,results,errors=run(tmp_path,limit=6,cap=cap,transport=Transport(remaining=remaining))
    assert any(errors) and len(t.paid)<=2
    state=c._load();assert state['spent']<=2 and not state['reservations']
    assert p.stopped


def test_duplicate_request_coalesces_even_with_multiple_workers(tmp_path):
    c,p,t,results,errors=run(tmp_path,events=['same']*6)
    assert not any(errors) and t.paid==['same'] and c._load()['spent']==1
    assert len(set(x.request_id for x in results))==1


def test_429_pauses_retries_reduces_concurrency_and_logs_once(tmp_path,monkeypatch):
    # Only skip the retry-loop's two-second delay; condition waits still use real time.
    original=time.sleep
    monkeypatch.setattr('pinnacle_props.odds_api.time.sleep',lambda n:original(n) if n<1 else None)
    c,p,t,results,errors=run(tmp_path,transport=Transport(rate_limit=True),events=['g0','g1','g2','g3'])
    assert not any(errors) and p.limit==1 and p.rate_limit['occurrences']==1
    state=c._load();assert state['spent']==4 and not state['reservations'] and not state['halt']
    assert list(state['rate_limits'])==['pool-test']
    assert sum(r['status']==429 for r in state['records'])==1
    attempts=[at for event,at in t.calls if event==t.limited_event]
    assert attempts[-1]-attempts[0]>=.04


def test_unknown_prior_reservation_blocks_paid_pool_requests(tmp_path):
    c=Client(data_dir=tmp_path,key='test-only');state=c._load()
    state['records']=[dict(request_id='prior',run_id='old',timestamp='2026-10-21T00:00:00+00:00',endpoint='x',estimated=1,actual=None,remaining=900000,status='pending',cache_path='none',outcome='reserved')]
    state['reservations']={'prior':dict(estimated=1,run_id='old')};state['remaining']=900000;c._save(state)
    c,p,t,results,errors=run(tmp_path)
    assert any(errors) and not t.paid and 'prior' in c._load()['reservations']


def test_worker_limits_and_retry_after_contract():
    assert workers({},'scans')==6 and workers({},'closes')==4 and workers({},'historical')==3
    for bad in (0,11,True,2.5):
        with pytest.raises(ValueError):workers({},'scans',bad)
    assert retry_after('2')==2 and retry_after('nan')==2


def test_deadlines_are_thread_local(tmp_path):
    c=Client(data_dir=tmp_path);barrier=threading.Barrier(2);seen=[]
    def job(value):
        with c.deadline(value):barrier.wait(timeout=2);seen.append(c.paid_deadline.isoformat())
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as e:list(e.map(job,['2099-01-01T00:00:00Z','2098-01-01T00:00:00Z']))
    assert set(seen)=={'2099-01-01T00:00:00+00:00','2098-01-01T00:00:00+00:00'} and c.paid_deadline is None


def test_frozen_responses_sequential_parallel_scans_equal(tmp_path,monkeypatch):
    from test_pipeline import settings,PROFILE
    from pinnacle_props.pipeline import run_live
    import pinnacle_props.pipeline as pipeline
    from datetime import datetime,timezone
    class FixedClock:
        @staticmethod
        def now(tz=None):return datetime(2026,10,21,12,tzinfo=timezone.utc)
    monkeypatch.setattr(pipeline,'datetime',FixedClock)
    from types import SimpleNamespace
    monkeypatch.setattr(pipeline,'uuid',SimpleNamespace(uuid4=lambda:'fixed-run-identity'))
    preview=dict(settings=settings(),events=[dict(id='g'+str(i),commence_time='2099-01-01T00:00:00Z') for i in range(6)],bound=12)
    outputs=[];durations=[]
    for concurrency in [1,3]:
        transport=Transport(delay=.03);messages=[];start=time.monotonic()
        m,f,x=run_live(deepcopy(preview),data_dir=tmp_path/str(concurrency),profile=PROFILE,key='test-only',session=transport,progress=messages.append,concurrency=concurrency,paper_track=False)
        durations.append(time.monotonic()-start);outputs.append((m,f,x))
        assert messages[-1]=='Captured 6 of 6 games' and m['credits_charged']==6
    pd.testing.assert_frame_equal(outputs[0][1],outputs[1][1]);pd.testing.assert_frame_equal(outputs[0][2],outputs[1][2])
    assert outputs[0][0]['exclusions']==outputs[1][0]['exclusions']
    assert outputs[0][0]['artifacts']==outputs[1][0]['artifacts']
    assert [r['event_id'] for r in outputs[1][0]['event_results']]==[r['event_id'] for r in outputs[0][0]['event_results']]
    assert durations[1]<durations[0]


def test_cross_process_active_lease_and_abandoned_reservation(tmp_path):
    import subprocess,sys
    lease=tmp_path/'inflight'/'prior.lock';lease.parent.mkdir()
    script="import fcntl,sys; f=open(sys.argv[1],'w'); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.read()"
    child=subprocess.Popen([sys.executable,'-c',script,str(lease)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='ready'
        c=Client(data_dir=tmp_path);state=c._load();state['remaining']=900000
        state['records']=[dict(request_id='prior',run_id='other',timestamp='2026-10-21T00:00:00+00:00',endpoint='x',estimated=1,actual=None,remaining=900000,status='pending',cache_path='none',outcome='reserved')]
        state['reservations']={'prior':dict(estimated=1,run_id='other',inflight='prior.lock')};c._save(state)
        assert c._active_reservation('prior',state['reservations']['prior'])
        c,p,t,values,errors=run(tmp_path,events=['new'])
        assert not any(errors) and c._load()['spent']==1 and 'prior' in c._load()['reservations']
    finally:
        child.stdin.close();child.wait(timeout=5);child.stdout.close()
    assert not c._active_reservation('prior',c._load()['reservations']['prior'])
    c,p,t,values,errors=run(tmp_path,events=['after-crash'])
    assert any(errors) and not t.paid


def test_deadline_after_pacing_releases_unsent_reservation(tmp_path,monkeypatch):
    from datetime import datetime,timezone,timedelta
    now=[datetime(2026,10,21,12,tzinfo=timezone.utc)]
    monkeypatch.setattr('pinnacle_props.odds_api.utcnow',lambda:now[0].isoformat())
    def advance(seconds):now[0]+=timedelta(seconds=2)
    monkeypatch.setattr('pinnacle_props.odds_api.time.sleep',advance)
    transport=Transport(delay=0);pool=Pool(2)
    c=Client(data_dir=tmp_path,key='test-only',run_cap=5,pool=pool,session=transport,min_interval=1,
             paid_deadline='2026-10-21T12:00:01+00:00')
    with pytest.raises(BudgetStop,match='before send'):
        c.event_odds(SPORT,'future',['player_points'],BOOKS)
    state=c._load();assert not transport.paid and state['spent']==0 and not state['reservations']
    assert state['records'][-1]['status']=='not_sent'


def test_staged_cohorts_respect_first30_boundary_and_stop_before_next():
    from pinnacle_props.request_pool import staged
    seen=[];finished=[];pool=Pool(3)
    def before():
        if len(finished)>=5:raise BudgetStop('sample gate')
    def width():return 5-len(finished)
    def fetch(x):seen.append(x);return x
    with pytest.raises(BudgetStop,match='sample gate'):
        for _,value,error in staged(pool,range(10),fetch,before=before,width=width):
            assert error is None;finished.append(value)
    assert sorted(seen)==finished==list(range(5))


def test_two_sides_of_same_pool_respect_shared_daily_close_cap(tmp_path):
    pool=Pool(3);transport=Transport()
    base=Client(data_dir=tmp_path);state=base._load();state['close_daily_cap']=2;base._save(state)
    def fetch(event):
        c=Client(data_dir=tmp_path,key='test-only',run_id='close-'+event,run_cap=10,pool=pool,session=transport,min_interval=0)
        return c.event_odds(SPORT,event,['player_points'],BOOKS)
    values,errors=pool.map(['a','b','c','d'],fetch)
    assert any(errors) and len(transport.paid)<=2 and base._load()['spent']<=2
    assert not base._load()['reservations']


def test_pool_unknown_transient_can_reconcile_only_when_uncharged_and_quiescent(tmp_path,monkeypatch):
    from test_api import Session
    monkeypatch.setattr('pinnacle_props.odds_api.time.sleep',lambda _:None)
    pool=Pool(2)
    c=Client(data_dir=tmp_path,key='test-only',run_cap=2,pool=pool,min_interval=0,
             session=Session(Response(),Response(status=502,headers=False),Response(),Response(payload(),last=1)))
    assert c.event_odds(SPORT,'game-1',['player_points'],BOOKS).status==200
    state=c._load();assert state['spent']==1 and not state['reservations']
    assert state['transient_reconciliations'][0]['used_before']==10


def test_late_free_quota_response_cannot_raise_pool_balance(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    begun=threading.Event()
    class LateQuota:
        def get(self,url,**kwargs):
            if url.endswith('/v4/sports'):
                begun.set();deadline=time.monotonic()+3
                while c._load()['spent']!=1:
                    assert time.monotonic()<deadline;time.sleep(.005)
                return Response(remaining=100001)
            return Response(payload(),last=1,remaining=100000)
    c=Client(data_dir=tmp_path,key='test-only',pool=Pool(2),session=LateQuota(),run_cap=3,min_interval=0)
    state=c._load();state['remaining']=100001;c._save(state)
    c.quota_checked=True;c.quota_time=time.monotonic()
    with ThreadPoolExecutor(1) as executor:
        future=executor.submit(c.sports);assert begun.wait(timeout=3)
        c.event_odds(SPORT,'game-1',['player_points'],BOOKS);future.result(timeout=3)
    assert c._load()['remaining']==100000
    with pytest.raises(BudgetStop,match='floor'):c.event_odds(SPORT,'game-2',['player_points'],BOOKS)


def test_concurrent_counter_update_cannot_erase_an_unknown_charge(tmp_path,monkeypatch):
    original=time.sleep
    monkeypatch.setattr('pinnacle_props.odds_api.time.sleep',lambda n:original(n) if n<1 else None)
    begun=threading.Event()
    class Interleaved:
        def __init__(self):self.quota=0;self.bad=0
        def get(self,url,**kwargs):
            if url.endswith('/v4/sports'):
                self.quota+=1;response=Response();response.headers['x-requests-used']=str(10 if self.quota==1 else 12);return response
            event=url.split('/')[-2]
            if event=='bad':
                self.bad+=1
                if self.bad==1:
                    begun.set();deadline=time.monotonic()+3
                    while c._load().get('provider_used')!=12:
                        assert time.monotonic()<deadline;original(.005)
                    return Response(status=502,headers=False)
            else:assert begun.wait(timeout=3)
            body=payload();body['id']=event;response=Response(body,last=1)
            response.headers['x-requests-used']=str(13 if event=='bad' else 12)
            return response
    pool=Pool(2);c=Client(data_dir=tmp_path,key='test-only',pool=pool,session=Interleaved(),run_cap=3,min_interval=0)
    values,errors=pool.map(['bad','good'],lambda e:c.event_odds(SPORT,e,['player_points'],BOOKS))
    assert not any(errors)
    state=c._load();assert state['spent']==2 and len(state['reservations'])==1
    assert next(iter(state['reservations'].values()))['quota_anchor']==10
    assert not state.get('transient_reconciliations')
