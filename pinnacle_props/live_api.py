"""TASK-25 live calls: fresh account-floor preflight, raw cache, run-only receipts.

Research continues to use odds_api.Client unchanged. No ledger is opened here.
"""
from contextlib import nullcontext,contextmanager
from datetime import datetime
from pathlib import Path
import gzip
import json
import math
import re
import threading
import time
import uuid
import requests
from . import odds_api as api
from .request_pool import Pool, caller_progress

POLICY='account-floor-v1'


def number(value):
    try:
        n=float(value)
        return int(n) if math.isfinite(n) and n>=0 and n.is_integer() else None
    except (ValueError,TypeError):return None


class LiveClient(api.Client):
    """Reuse request schema/transport helpers, never the budgeted initializer/path."""
    def __init__(self,*,data_dir=api.ROOT/'data',run_id=None,run_cap=0,floor=api.ACCOUNT_FLOOR,
                 key=None,session=None,min_interval=1.,paid_deadline=None,pool=None,
                 progress=None,resume_only=False,**legacy):
        if type(run_cap) is not int or not 0<=run_cap<=20_000:
            raise ValueError('Live estimate must be 0–20,000; use the staged research client above that')
        if type(floor) is not int or floor<api.ACCOUNT_FLOOR:raise ValueError('Account floor cannot be lowered')
        self.data_dir=Path(data_dir);self.run_id=run_id or str(uuid.uuid4())
        if not re.fullmatch(r'[A-Za-z0-9_-]+',self.run_id):raise ValueError('Invalid run id')
        self.run_cap=run_cap;self.floor=floor;self._key=key
        self.pool=pool;self._identities=pool or Pool(1)
        self._provided_session=session;self.session=session or requests.Session()
        self._transport_state=threading.local();self._lock_state=threading.local()
        self._quota_mutex=threading.RLock();self._state_mutex=threading.RLock()
        self._quota_nonce=str(uuid.uuid4());self.min_interval=max(0.,min_interval)
        self.paid_deadline=datetime.fromisoformat(paid_deadline.replace('Z','+00:00')) if paid_deadline else None
        if self.paid_deadline and self.paid_deadline.tzinfo is None:raise ValueError('Paid deadline requires timezone')
        self.quota_check=None;self.remaining=None;self.records=[];self.charges={}
        self.sent_estimate=0;self.failed=False;self.resume_only=resume_only
        self.progress=caller_progress(progress)

    def _load(self):raise RuntimeError('Live runs do not read a ledger')
    def _save(self,state):raise RuntimeError('Live runs do not write a ledger')

    @property
    def charged(self):return sum(r['credits'] or 0 for r in self.charges.values())

    def accounting(self):
        with self._state_mutex:
            return dict(policy=POLICY,run_id=self.run_id,estimate=self.run_cap,
                        account_check=self.quota_check,account_remaining=self.remaining,
                        credits_charged=self.charged,charges_complete=all(r['credits'] is not None for r in self.charges.values()),
                        requests=list(self.charges.values()),rate_limit=self.pool.rate_limit if self.pool else None)

    def preflight(self):
        with self._quota_mutex:
            if self.quota_check is None:
                result=self.request('/v4/sports')
                remaining=number(result.headers.get('x-requests-remaining'))
                if remaining is None:raise api.BudgetStop('Account remaining unavailable; no paid request sent')
                self.quota_check=dict(at=api.utcnow(),remaining=remaining,estimate=self.run_cap,
                    floor=self.floor,passed=remaining-self.run_cap>=self.floor,request_id=result.request_id)
                self.remaining=remaining
                # Last free account observation for the UI, not spend or reservations.
                api.write_json(self.data_dir/'quota.json',self.quota_check)
                self.progress(f'Account remaining: {remaining:,}; estimate: {self.run_cap:,}; floor: {self.floor:,}; '+('PASS' if self.quota_check['passed'] else 'STOP'))
            if not self.quota_check['passed']:
                raise api.BudgetStop('Run estimate would breach the 100,000-credit account floor; no paid request sent')
            return self.quota_check

    def _accept(self,envelope,path,cached,cost):
        status=envelope['status'];headers=envelope['headers'];actual=number(headers.get('x-requests-last'))
        remaining=number(headers.get('x-requests-remaining'))
        if cost:
            with self._state_mutex:
                self.charges[envelope['request_id']]=dict(request_id=envelope['request_id'],credits=actual,
                    at=envelope['captured_at'],status=status,cached=cached)
                if remaining is not None:self.remaining=min(self.remaining if self.remaining is not None else remaining,remaining)
        if status!=200:
            raise api.APIError(f'Odds API status {status}; inspect saved run response; no automatic retry',status)
        try:
            body=json.loads(envelope['body'])
            expected=api.response_cost(envelope['endpoint'],envelope['params'],body) if cost else 0
        except (ValueError,TypeError,KeyError,AttributeError,api.APIError):
            raise api.APIError('Invalid provider response; raw response retained, no automatic retry') from None
        if actual is None or remaining is None or actual!=expected or actual>cost:
            raise api.BudgetStop('Missing or inconsistent charge/quota headers; inspect run response before retry')
        if cost and remaining<self.floor:raise api.BudgetStop('Observed account remaining is below its floor')
        result=api.Result(body,status,headers,str(path),envelope['request_id'],cached)
        self.records.append(result);return result

    def request(self,path,params=None):
        params=dict(params or {})
        if any(k.lower() in ('apikey','api_key') for k in params):raise ValueError('Credentials are not request parameters')
        cost=api.estimated_cost(path,params)
        # Only proven zero-charge 429s may retry; unknown/network failures never do.
        for attempt in range(6):
            cache=self._cache_path(path,params,attempt)
            with self._identities.identity(cache):
                if cache.exists():
                    envelope=json.loads(gzip.decompress(cache.read_bytes()))
                    if envelope['endpoint']!=path or envelope['params']!=params:raise api.APIError('Raw cache identity mismatch')
                    if envelope['status']==429 and number(envelope['headers'].get('x-requests-last'))==0 and self.pool:
                        continue
                    return self._accept(envelope,cache,True,cost)
                if cost:
                    if self.resume_only:raise api.BudgetStop('Prior run has no complete cached response; review before another paid attempt')
                    self.preflight()
                with self.pool.slot() if self.pool else nullcontext():
                    if cost:
                        with self._state_mutex:
                            if self.failed:raise api.BudgetStop('Live run stopped after an uncertain or failed request')
                            # This is the displayed invocation estimate, not a persisted allowance.
                            if self.sent_estimate+cost>self.run_cap:raise api.BudgetStop('Request exceeds this run estimate; prepare a new preview')
                            self.sent_estimate+=cost
                    if self.pool:self.pool.wait_send()
                    delay=self.min_interval-(time.monotonic()-getattr(self._transport_state,'last_send',0.))
                    if delay>0:time.sleep(delay)
                    if cost and self.paid_deadline and datetime.fromisoformat(api.utcnow())>=self.paid_deadline:
                        raise api.BudgetStop('Game start reached before send; no paid request sent')
                    if self.pool:self.pool.check()
                    if cost and self.failed:raise api.BudgetStop('Live run stopped before send')
                    key=self._key if self._key is not None else api.resolve_key();self._key=key
                    status='network_error';body='';headers={}
                    self._transport_state.last_send=time.monotonic()
                    try:
                        response=self._transport().get(api.HOST+path,params=dict(params,apiKey=key),timeout=30,allow_redirects=False)
                        status=response.status_code;body=response.text.replace(key,'[REDACTED]')
                        headers={h:response.headers.get(h) for h in api.QUOTA_HEADERS}
                        if self.pool and (status==429 or response.headers.get('Retry-After') is not None):
                            headers['retry-after']=response.headers.get('Retry-After');self.pool.backoff(headers['retry-after'],status)
                    except requests.RequestException:pass
                    envelope=dict(request_id=str(uuid.uuid4()),captured_at=api.utcnow(),endpoint=path,params=params,status=status,headers=headers,body=body)
                    if cost:
                        with self._state_mutex:
                            self.charges[envelope['request_id']]=dict(request_id=envelope['request_id'],credits=number(headers.get('x-requests-last')),
                                at=envelope['captured_at'],status=status,cached=False)
                    api.atomic_write(cache,gzip.compress(json.dumps(envelope).encode()))
                    if cost and status==429 and number(headers.get('x-requests-last'))==0 and self.pool:
                        with self._state_mutex:self.sent_estimate-=cost
                        self.charges[envelope['request_id']]=dict(request_id=envelope['request_id'],credits=0,at=envelope['captured_at'],status=status,cached=False)
                        continue
                    try:return self._accept(envelope,cache,False,cost)
                    except api.APIError:
                        self.failed=True
                        if self.pool:self.pool.stop()
                        raise
        raise api.BudgetStop('Rate-limit retry limit reached; no further request sent')


def operational_client(*,research=False,**kwargs):
    """Explicit research and large campaigns retain the unchanged budgeted client."""
    return api.Client(**kwargs) if research or kwargs.get('run_cap',0)>20_000 else LiveClient(**kwargs)


def charged(client):
    return client.charged if isinstance(client,LiveClient) else client._load()['runs'].get(client.run_id,0)


def accounting(client):return client.accounting() if isinstance(client,LiveClient) else {'policy':'budgeted-research'}


@contextmanager
def failure_receipt(client,path,value):
    """A failed run still records known charges and uncertainty in its job manifest."""
    try:yield
    except Exception as error:
        current=accounting(client)
        # A later resume refusal must not erase known/unknown charges from an earlier
        # failure (for example a charged response whose raw-cache write failed).
        if Path(path).exists():
            previous=json.loads(Path(path).read_text()).get('accounting',{})
            if current.get('policy')==POLICY and previous.get('run_id')==current['run_id']:
                rows={r['request_id']:r for r in previous.get('requests',[])}
                rows.update({r['request_id']:r for r in current['requests']})
                current['requests']=list(rows.values())
                current['credits_charged']=sum(r['credits'] or 0 for r in rows.values())
                current['charges_complete']=all(r['credits'] is not None for r in rows.values())
                if current['account_check'] is None:current['account_check']=previous.get('account_check')
        api.write_json(path,dict(value,status='failed; review raw responses before retry',
            error_type=type(error).__name__,accounting=current))
        raise
