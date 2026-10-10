"""Single door to The Odds API: durable reservations, raw cache, bounded spending.

Cost rule adapted from Josh's cbb/odds_api.py; standalone with no sibling runtime import.
Provider contract: https://the-odds-api.com/liveapi/guides/v4/#get-event-odds
Local runtime: five transient retries with bounded backoff; ten consecutive failures halt.
Unknown transient charges remain reserved against all caps; other unknown requests block paid work.
The cross-process lock covers this project's requests; other projects share the account
but not this lock, so provider headers are observations, not an account-wide guarantee.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import csv
import fcntl
import gzip
import hashlib
import io
import json
import math
import os
import re
import subprocess
import time
import threading
import uuid

import pandas as pd
import requests
from dotenv import dotenv_values
from .scan import INPUT_COLUMNS
from .settings import accounting_dir

ROOT = Path(__file__).resolve().parents[1]
HOST = 'https://api.the-odds-api.com'
PROJECT_CAP = 2_600_000
ACCOUNT_FLOOR = 100_000
CLOSE_DAILY_CAP = 2_500
CLOUD_DAILY_CAP = 2_500
QUOTA_HEADERS = ('x-requests-remaining', 'x-requests-used', 'x-requests-last')


class APIError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class TransientError(APIError):
    pass


class BudgetStop(APIError):
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + str(uuid.uuid4()) + '.tmp')
    try:
        with tmp.open('xb') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, indent=2, allow_nan=False) + '\n').encode())


def resolve_key(root=ROOT, streamlit_secrets=None):
    """Read-only resolution; shared RTF decoding stays in memory. Never echo candidates."""
    key = os.environ.get('ODDS_API_KEY')
    if not key:
        key = dotenv_values(Path(root) / '.env').get('ODDS_API_KEY')
    if not key and streamlit_secrets is not None:
        try:
            key = streamlit_secrets.get('ODDS_API_KEY')
        except (KeyError, FileNotFoundError):
            pass
    doc = Path(root).parent / 'The Odds API.rtfd'
    if not key and doc.exists():
        try:
            result = subprocess.run(['textutil', '-convert', 'txt', '-stdout', str(doc)],
                                    capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                match = re.search(r'(?<![0-9a-fA-F])[0-9a-f]{32}(?![0-9a-fA-F])', result.stdout)
                if match:
                    key = match.group(0)
        except (OSError, subprocess.SubprocessError):
            pass
    if not key or not isinstance(key, str) or not key.strip():
        raise APIError('No Odds API credential found. Put ODDS_API_KEY in the local .env or Streamlit secrets.')
    return key.strip()


def resolve_agent_key(root=ROOT):
    """Scheduled runtime uses the owner's .env only; no shared-document fallback."""
    path=Path(root)/'.env'
    key=dotenv_values(path).get('ODDS_API_KEY') if path.is_file() else None
    if not isinstance(key,str) or not key.strip():
        raise APIError('Launch-agent credential unavailable: set ODDS_API_KEY in the project .env; no requests sent')
    return key.strip()


def close_day_spend(state, day):
    """Committed and unresolved close spend on one Eastern calendar date."""
    total=0
    for record in state['records']:
        if record['run_id'].startswith('close-'):
            date=datetime.fromisoformat(record['timestamp'].replace('Z','+00:00')).astimezone(ZoneInfo('America/New_York')).date().isoformat()
            if date==day:
                total+=record['actual'] if record['actual'] is not None else record['estimated']
    return total


def _items(value):
    return sorted(set(x.strip() for x in str(value or '').split(',') if x.strip()))


def estimated_cost(path, params):
    path = path.rstrip('/')
    if path == '/v4/sports' or re.fullmatch(r'/v4/sports/[a-z_]+/events', path):
        return 0
    historical = path.startswith('/v4/historical/')
    if historical:
        historical_time(params.get('date'))
        if re.fullmatch(r'/v4/historical/sports/[a-z_]+/events', path):
            return 1
    if not re.fullmatch(r'/v4/(historical/)?sports/[a-z_]+/events/[A-Za-z0-9_-]+/odds', path):
        raise ValueError('Only authorised discovery and event-odds endpoints are supported')
    markets = _items(params.get('markets'))
    books = _items(params.get('bookmakers'))
    regions = math.ceil(len(books)/10) if books else len(_items(params.get('regions')))
    if not markets or regions == 0:
        raise ValueError('Explicit nonempty markets and books or regions required')
    return len(markets) * regions * (10 if historical else 1)


def returned_cost(payload, params):
    if not isinstance(payload, dict) or not isinstance(payload.get('bookmakers'), list):
        raise APIError('Malformed event odds response')
    markets = {m['key'] for b in payload['bookmakers'] for m in b.get('markets', [])}
    if not markets.issubset(set(_items(params.get('markets')))):
        raise APIError('Response contains unrequested markets')
    books = _items(params.get('bookmakers'))
    regions = math.ceil(len(books)/10) if books else len(_items(params.get('regions')))
    return len(markets) * regions


def historical_time(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z',value):
        raise ValueError('Historical timestamp must be UTC ISO seconds')
    stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
    if stamp < datetime(2023,5,3,5,30,tzinfo=timezone.utc):
        raise ValueError('Historical player-prop date predates provider coverage')
    return stamp


def response_cost(path,params,payload):
    historical=path.startswith('/v4/historical/')
    sport=path.split('/')[4 if historical else 3]
    if historical:
        if not isinstance(payload,dict):
            raise APIError('Historical response missing wrapper')
        requested=historical_time(params['date'])
        if historical_time(payload.get('timestamp'))>requested:
            raise APIError('Historical snapshot is after requested cutoff')
        if payload.get('next_timestamp') and historical_time(payload['next_timestamp'])<=requested:
            raise APIError('Historical response is not the latest snapshot at cutoff')
        payload=payload['data']
        if path.endswith('/events'):
            if not isinstance(payload,list) or any(not isinstance(e,dict) or
                not e.get('id') or e.get('sport_key')!=sport or not e.get('commence_time') for e in payload):
                raise APIError('Historical events schema invalid')
            return int(bool(payload))
    if payload.get('id')!=path.split('/')[-2] or payload.get('sport_key')!=sport:
        raise APIError('Response event identity mismatch')
    return returned_cost(payload,params)*(10 if historical else 1)


@dataclass
class Result:
    payload: object
    status: int
    headers: dict
    cache_path: str
    request_id: str
    cached: bool = False


class Client:
    def __init__(self, *, data_dir=ROOT/'data', run_id=None, run_cap=0,
                 project_cap=PROJECT_CAP, floor=ACCOUNT_FLOOR, key=None, session=None,
                 min_interval=1.0, paid_deadline=None, staged_run=False, lock_timeout=None, cloud_daily_cap=None, transient_retries=None):
        self._state_mutex = threading.RLock()
        self._lock_state = threading.local()
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.accounting_dir = accounting_dir(self.data_dir, cloud=cloud_daily_cap is not None)
        self.accounting_dir.mkdir(parents=True, exist_ok=True)
        self.transient_retries = (self.accounting_dir != self.data_dir) if transient_retries is None else transient_retries
        self.run_id = run_id or str(uuid.uuid4())
        if not re.fullmatch(r'[A-Za-z0-9_-]+', self.run_id):
            raise ValueError('Invalid run id')
        if not 0 <= run_cap <= (PROJECT_CAP if staged_run else 20_000) or not 0 <= project_cap <= PROJECT_CAP or floor < ACCOUNT_FLOOR:
            raise ValueError('Budget arguments exceed authorised bounds')
        self.run_cap, self.project_cap, self.floor = int(run_cap), int(project_cap), int(floor)
        if cloud_daily_cap is not None and (type(cloud_daily_cap) is not int or not 0<=cloud_daily_cap<=CLOUD_DAILY_CAP):
            raise ValueError('Cloud daily cap exceeds authorised bound')
        self.cloud_daily_cap = cloud_daily_cap
        self._key = key
        self.session = session or requests.Session()
        self.min_interval = max(0.0, min_interval)
        self.last_send = 0.0
        self.records = []
        self.quota_checked = False
        self.quota_time = 0.0
        self._quota_nonce = str(uuid.uuid4())
        self.lock_timeout = (180 if self.run_id.startswith('close-') else 0) if lock_timeout is None else lock_timeout
        if not 0<=self.lock_timeout<=180:raise ValueError('Lock timeout outside allowed range')
        self.paid_deadline = datetime.fromisoformat(paid_deadline.replace('Z','+00:00')) if paid_deadline else None
        if self.paid_deadline and self.paid_deadline.tzinfo is None:raise ValueError('Paid deadline requires timezone')

    @contextmanager
    def _locked(self, timeout=None):
        # Nested state reads/writes use the existing request lock; threads do not
        # inherit another thread's ownership of it.
        with self._state_mutex:
            if getattr(self._lock_state, 'held', False):
                yield
                return
            with self._file_locked(timeout):
                self._lock_state.held = True
                try: yield
                finally: self._lock_state.held = False

    @contextmanager
    def _file_locked(self, timeout=None):
        with (self.accounting_dir/'odds.lock').open('a') as lock:
            start=time.monotonic();limit=self.lock_timeout if timeout is None else timeout
            waited=False
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB);break
                except BlockingIOError:
                    elapsed=time.monotonic()-start
                    if elapsed>=limit:
                        if waited:print(f'{utcnow()} API lock timeout after {elapsed:.3f}s; no request sent',flush=True)
                        raise BudgetStop('Another project API request is running; no request sent') from None
                    if not waited:print(f'{utcnow()} Waiting for project API lock, limit {limit:g}s',flush=True)
                    waited=True;time.sleep(min(2,limit-elapsed))
            if waited:print(f'{utcnow()} API lock acquired after {time.monotonic()-start:.3f}s',flush=True)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _load(self):
        with Client._locked(self, timeout=180):
            return self._load_unlocked()

    def _load_unlocked(self):
        path = self.accounting_dir/'odds_budget.json'
        if self.accounting_dir != self.data_dir and not (self.accounting_dir/'migration.json').exists():
            raise BudgetStop('Local runtime accounting migration required; no requests sent')
        deadline = time.monotonic() + 5
        while True:
            try:
                content = path.read_text()
                break
            except FileNotFoundError:
                if not (self.accounting_dir/'odds_usage.csv').exists():
                    return {'version':1,'spent':0,'remaining':None,'quota_at':None,'halt':None,
                            'reservations':{},'runs':{},'records':[]}
                if time.monotonic() >= deadline:
                    raise BudgetStop('Budget state missing beside an existing ledger after 5 seconds; reconcile before continuing') from None
                time.sleep(min(0.1, max(0, deadline-time.monotonic())))
        try:
            state = json.loads(content)
            assert state['version']==1 and state['spent']>=0
            assert isinstance(state['records'],list) and isinstance(state['reservations'],dict)
            assert state['spent'] == sum(r['actual'] or 0 for r in state['records'])
            assert set(state['reservations']) == {r['request_id'] for r in state['records'] if r['actual'] is None}
            return state
        except (ValueError, KeyError, AssertionError, TypeError):
            raise BudgetStop('Budget state invalid; no request sent') from None

    def _save(self, state):
        with Client._locked(self, timeout=180):
            self._save_unlocked(state)

    def _save_unlocked(self, state):
        write_json(self.accounting_dir/'odds_budget.json', state)
        fields = ['request_id','run_id','timestamp','endpoint','estimated','actual','remaining',
                  'status','cache_path','outcome']
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(state['records'])
        atomic_write(self.accounting_dir/'odds_usage.csv', output.getvalue().encode())
        if self.accounting_dir != self.data_dir:
            # Reports only: a sync rollback of these copies cannot alter spending.
            for name in ('odds_budget.json','odds_usage.csv'):
                try:
                    atomic_write(self.data_dir/name,(self.accounting_dir/name).read_bytes())
                    (self.data_dir/name).chmod(0o444)
                except OSError:
                    print('Accounting snapshot export failed; authoritative runtime ledger retained',flush=True)

    def _cache_path(self, path, params, attempt=0):
        identity = json.dumps([self.run_id,path,params,self._quota_nonce if path=='/v4/sports' else None],sort_keys=True).encode()
        if attempt:identity += ('retry:'+str(attempt)).encode()
        digest = hashlib.sha256(identity).hexdigest()
        parts = path.split('/')
        sport = parts[4] if path.startswith('/v4/historical/') else (parts[3] if len(parts)>3 else 'sports')
        # Recover the original capture date when resuming across midnight.
        existing = list((self.data_dir/'raw'/sport).glob('*/'+self.run_id+'/'+digest+'.json.gz'))
        if len(existing)>1:
            raise APIError('Duplicate raw request caches need reconciliation')
        day = datetime.now(ZoneInfo('America/New_York')).date().isoformat()
        return existing[0] if existing else self.data_dir/'raw'/sport/day/self.run_id/(digest+'.json.gz')

    def _decode(self, envelope, cache_path, cached=False):
        status = envelope['status']
        if status != 200:
            error = APIError(f'Odds API request failed (status {status}); see the status ledger',status)
            error.request_id = envelope['request_id']
            raise error
        try:
            payload = json.loads(envelope['body'])
        except (ValueError, TypeError):
            raise APIError('Odds API returned invalid JSON; response cached') from None
        result = Result(payload,status,envelope['headers'],str(cache_path),envelope['request_id'],cached)
        self.records.append(result)
        return result

    def _skippable_event_error(self,path,status):
        return False  # Opt-in historical research policy; live behavior stays unchanged.

    def _cached(self, cache_path):
        envelope = json.loads(gzip.decompress(cache_path.read_bytes()))
        state = self._load();records = state['records']
        matching = [r for r in records if r['request_id']==envelope['request_id']]
        if self.transient_retries and len(matching)==1 and matching[0]['outcome'].startswith('transient'):
            if state['halt']:raise BudgetStop('API client halted: '+state['halt'])
            error=TransientError('Transient provider response; see status ledger',envelope['status'])
            error.request_id=envelope['request_id'];error.cached=True;raise error
        accounted_error=(len(matching)==1 and matching[0]['outcome']=='error'
            and self._skippable_event_error(envelope['endpoint'],envelope['status'])
            and isinstance(matching[0]['actual'],int) and 0<=matching[0]['actual']<=matching[0]['estimated']
            and not state['halt'] and envelope['request_id'] not in state['reservations'])
        if len(matching)!=1 or (matching[0]['outcome']!='ok' and not accounted_error):
            raise APIError('Cached request did not pass accounting; review the ledger before reuse')
        return self._decode(envelope,cache_path,True)

    def request(self, path, params=None):
        for attempt in range(6 if self.transient_retries else 1):
            try:return self._request_once(path,params,attempt=attempt)
            except TransientError as exc:
                self.quota_checked=False
                if attempt==5:raise
                if not getattr(exc,'cached',False):time.sleep([2,4,8,16,30][attempt])

    def _request_once(self, path, params=None, *, attempt=0):
        params = dict(params or {})
        if any(k.lower() in ('apikey','api_key') for k in params):
            raise ValueError('Credentials must not be supplied in request parameters')
        cost = estimated_cost(path, params)
        cache_path = self._cache_path(path,params,attempt)
        with self._locked():
            if cache_path.exists():
                return self._cached(cache_path)
        if cost and (not self.quota_checked or time.monotonic()-self.quota_time > 60):
            self._quota_nonce = str(uuid.uuid4())
            self.request('/v4/sports')
        key = self._key if self._key is not None else resolve_key()
        self._key = key  # Invocation-local memory only; never persist or expose it.
        with self._locked():
            # A different process may have completed this exact request in the meantime.
            if cache_path.exists():
                return self._cached(cache_path)
            state = self._load()
            if cost and self.paid_deadline and datetime.fromisoformat(utcnow())>=self.paid_deadline:
                raise BudgetStop('Close start reached while waiting; no paid request sent')
            persisted_cap=state.get('project_cap',500_000)
            if type(persisted_cap) is not int or not 0<=persisted_cap<=PROJECT_CAP:
                raise BudgetStop('Invalid persisted project cap')
            if self.cloud_daily_cap is not None:
                cap=state.setdefault('cloud_daily_cap',self.cloud_daily_cap)
                if type(cap) is not int or not 0<=cap<=CLOUD_DAILY_CAP:
                    raise BudgetStop('Invalid persisted cloud daily cap')
                day=datetime.fromisoformat(utcnow()).astimezone(ZoneInfo('America/New_York')).date().isoformat()
                spent=0
                for record in state['records']:
                    record_day=datetime.fromisoformat(record['timestamp'].replace('Z','+00:00')).astimezone(ZoneInfo('America/New_York')).date().isoformat()
                    if record_day==day:
                        spent+=record['actual'] if record['actual'] is not None else record['estimated']
                if cost and spent+cost>min(cap,self.cloud_daily_cap):
                    raise BudgetStop('Cloud daily credit cap would be exceeded; no paid request sent')
            if self.run_id.startswith('close-'):
                cap=state.setdefault('close_daily_cap',CLOSE_DAILY_CAP)
                if not isinstance(cap,int) or not 0<=cap<=CLOSE_DAILY_CAP:
                    raise BudgetStop('Invalid persisted close daily cap')
                day=datetime.fromisoformat(utcnow()).astimezone(ZoneInfo('America/New_York')).date().isoformat()
                if cost and close_day_spend(state,day)+cost>cap:
                    raise BudgetStop('Daily close credit cap would be exceeded; no paid request sent')
            if state['halt']:
                raise BudgetStop('API client halted: '+state['halt'])
            if cost:
                if any(not r.get('transient') for r in state['reservations'].values()):
                    raise BudgetStop('Unresolved request reservation; reconcile before paid work')
                reserved=sum(r['estimated'] for r in state['reservations'].values())
                run_reserved=sum(r['estimated'] for r in state['reservations'].values() if r['run_id']==self.run_id)
                if state['remaining'] is None or state['remaining']-reserved-cost < self.floor:
                    raise BudgetStop('Unknown quota or account floor would be breached; no paid request sent')
                if state['spent']+reserved+cost > min(self.project_cap,persisted_cap):
                    raise BudgetStop('Project credit cap would be exceeded; no paid request sent')
                if state['runs'].get(self.run_id,0)+run_reserved+cost > self.run_cap:
                    raise BudgetStop('Run credit cap would be exceeded; no paid request sent')
            request_id = str(uuid.uuid4())
            record = dict(request_id=request_id,run_id=self.run_id,timestamp=utcnow(),endpoint=path,
                          estimated=cost,actual=None,remaining=state['remaining'],status='pending',
                          cache_path=str(cache_path),outcome='reserved')
            state['records'].append(record)
            state['reservations'][request_id]={'estimated':cost,'run_id':self.run_id}
            self._save(state)
            delay = self.min_interval - (time.monotonic()-self.last_send)
            if delay > 0:
                time.sleep(delay)
            self.last_send = time.monotonic()
            status, body, headers = 'network_error', '', {}
            try:
                response = self.session.get(HOST+path,params=dict(params,apiKey=key),timeout=30,
                                            allow_redirects=False)
                status = response.status_code
                body = response.text.replace(key,'[REDACTED]')
                headers = {h:response.headers.get(h) for h in QUOTA_HEADERS}
            except requests.RequestException:
                # Exception strings often contain the credential-bearing URL. Never persist them.
                pass
            envelope = {'request_id':request_id,'captured_at':utcnow(),'endpoint':path,
                        'params':params,'status':status,'headers':headers,'body':body}
            atomic_write(cache_path,gzip.compress(json.dumps(envelope).encode()))
            record.update(status=status,outcome='error')
            parsed_headers = {}
            for name,value in headers.items():
                try:
                    number = float(value)
                    if math.isfinite(number) and number>=0 and number.is_integer():
                        parsed_headers[name] = int(number)
                except (ValueError, TypeError):
                    pass
            actual = parsed_headers.get('x-requests-last')
            remaining = parsed_headers.get('x-requests-remaining')
            transient=self.transient_retries and status not in (401,403,429) and (
                status=='network_error' or isinstance(status,int) and 500<=status<600
                or actual is None or remaining is None)
            used=parsed_headers.get('x-requests-used');previous_used=state.get('provider_used')
            # Zero unexplained counter delta proves pending attempts under this
            # anchor were uncharged. Positive/shared-account deltas stay reserved.
            if used is not None and previous_used is not None and actual is not None and used-previous_used==actual:
                resolved=[]
                for rid,reservation in list(state['reservations'].items()):
                    if reservation.get('transient') and reservation.get('quota_anchor')==previous_used:
                        row=next(r for r in state['records'] if r['request_id']==rid)
                        row.update(actual=0,outcome='transient_no_charge');state['reservations'].pop(rid);resolved.append(rid)
                if resolved:state.setdefault('transient_reconciliations',[]).append(dict(at=utcnow(),request_ids=resolved,used_before=previous_used,used_after=used,known_charge=actual))
            if used is not None:state['provider_used']=used
            if remaining is not None:
                state.update(remaining=remaining,quota_at=utcnow());record['remaining']=remaining
            if actual is not None:
                state['spent'] += actual
                state['runs'][self.run_id] = state['runs'].get(self.run_id,0)+actual
                state['reservations'].pop(request_id)
                record['actual']=actual
            elif transient:
                state['reservations'][request_id].update(transient=True,quota_anchor=previous_used)
            else:state['halt']='charge headers unavailable; reservation retained'
            if transient:
                record['outcome']='transient'
                state['consecutive_transient_failures']=state.get('consecutive_transient_failures',0)+1
                if state['consecutive_transient_failures']>=10:state['halt']='ten consecutive transient provider failures'
            if actual is not None and actual > cost:state['halt']='actual charge exceeded preflight bound'
            if status in (401,403,429):state['halt']=f'HTTP {status}; no automatic retry'
            if status != 200 and not transient:
                state['halt']=state['halt'] or (None if status in (404,422) or self._skippable_event_error(path,status) else 'request failed; review before retry')
            if cost and remaining is None and not transient:state['halt']='remaining quota header unavailable'
            if remaining is not None and remaining < self.floor and cost:state['halt']='account balance is below its floor'
            if status == 200 and not transient:
                try:
                    payload=json.loads(body);expected=response_cost(path,params,payload) if cost else 0
                    if actual!=expected:state['halt']='charge does not match returned-market billing'
                    else:
                        record['outcome']='ok'
                        if cost:state['consecutive_transient_failures']=0
                except (ValueError,TypeError,KeyError,AttributeError,APIError):
                    state['halt']='response schema/JSON invalid; inspect cached response'
            self._save(state)
            if state['halt']:
                raise BudgetStop('API client halted: '+state['halt']) from None
            if transient:
                error=TransientError('Transient provider response; see status ledger',status)
                error.request_id=request_id;raise error
            if path == '/v4/sports':
                self.quota_checked = True
                self.quota_time = time.monotonic()
            return self._decode(envelope,cache_path)

    def sports(self):
        return self.request('/v4/sports')

    def events(self, sport):
        return self.request(f'/v4/sports/{sport}/events')

    def event_odds(self,sport,event_id,markets,books):
        return self.request(f'/v4/sports/{sport}/events/{event_id}/odds',
                            {'markets':','.join(sorted(set(markets))),
                             'bookmakers':','.join(sorted(set(books))),'oddsFormat':'decimal'})


def normalize(payload):
    if not isinstance(payload,dict) or not isinstance(payload.get('bookmakers'),list):
        raise APIError('Expected an event with a bookmakers list')
    for key in ('id','sport_key','commence_time','home_team','away_team'):
        if not isinstance(payload.get(key),str) or not payload[key]:
            raise APIError('Event missing required identity/time fields')
    rows=[]
    try:
        for book in payload['bookmakers']:
            for market in book['markets']:
                for outcome in market['outcomes']:
                    rows.append(dict(sport_key=payload['sport_key'],league=payload.get('sport_title') or payload['sport_key'],
                                     event_id=payload['id'],book=book['key'],market=market['key'],
                                     player=outcome.get('description'),side=outcome.get('name'),
                                     point=outcome.get('point'),price=outcome.get('price'),
                                     last_update=market.get('last_update'),commence_time=payload['commence_time'],
                                     home_team=payload['home_team'],away_team=payload['away_team']))
    except (KeyError,TypeError):
        raise APIError('Malformed bookmaker, market or outcome structure') from None
    return pd.DataFrame(rows,columns=INPUT_COLUMNS)


def coverage(payload, markets, books):
    present={b['key']:{m['key']:len(m['outcomes']) for m in b['markets']} for b in payload['bookmakers']}
    return [dict(book=b,market=m,outcomes=present.get(b,{}).get(m,0),
                 returned=m in present.get(b,{})) for b in books for m in markets]
