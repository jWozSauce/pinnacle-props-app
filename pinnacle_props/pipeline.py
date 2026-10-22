"""Acquisition, immutable snapshots and explicit cache replay around the pure scan."""
from .live_api import operational_client, charged, accounting, failure_receipt
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date,datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import fcntl
import gzip
import hashlib
import json
import uuid

import pandas as pd
from .kelly import Profile
from .kelly_sheet import fetch_profiles
from .odds_api import APIError, BudgetStop, coverage, normalize, write_json
from .scan import scan, INPUT_COLUMNS
from .settings import ROOT, load_config, alternate_pairing_enabled, scan_markets
from . import book_catalog, consensus


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def options(config,sport,books=None,markets=None,min_edge=None,max_reference_margin=None,include_live=False,dates=None):
    if sport not in config['sports']:
        raise ValueError('Unknown configured sport')
    settings=config['sports'][sport]
    allowed_books = (book_catalog.selectable_books(book_catalog.load(), sport, config['books'])
                     if book_catalog.enabled() else config['books'])
    defaults = [b for b in config['books'] if b in allowed_books]
    selected_books=list(dict.fromkeys(books if books is not None else defaults))
    selected_books=[b for b in selected_books if b != config['reference_book'] and (not consensus.enabled() or b not in consensus.EXCHANGES)]
    if not selected_books or any(b not in allowed_books for b in selected_books):
        raise ValueError('Choose at least one configured soft book')
    selected_markets=list(dict.fromkeys(markets if markets is not None else scan_markets(settings)))
    allowed=scan_markets(settings)
    if not selected_markets or any(m not in allowed for m in selected_markets):
        raise ValueError('Choose at least one configured market')
    selected_dates=None
    if dates is not None:
        if len(dates)!=2:raise ValueError('Choose both From and To dates')
        selected_dates=[date.fromisoformat(str(d)).isoformat() for d in dates]
        if selected_dates[0]>selected_dates[1]:raise ValueError('From date must not follow To date')
    return dict(horizon={'mode':'date_range' if selected_dates else 'all_listed','dates':selected_dates},sport=sport,sport_key=settings['key'],league=settings['label'],
                reference_book=config['reference_book'],books=selected_books,markets=selected_markets,
                **({'alternate_version':1} if alternate_pairing_enabled() else {}),
                min_edge=config['min_edge'] if min_edge is None else min_edge,
                max_reference_margin=max_reference_margin,include_live=include_live,
                **({'advisory_version':consensus.VERSION,'observation_books':consensus.observation_books()} if consensus.enabled() else {}))


@contextmanager
def scan_lock(data_dir):
    data_dir=Path(data_dir);data_dir.mkdir(parents=True,exist_ok=True)
    from .settings import lock_dir
    locks=lock_dir(data_dir);locks.mkdir(parents=True,exist_ok=True)
    with (locks/'scan.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise BudgetStop('Another scan is in progress; no duplicate scan started') from None
        try:
            yield
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)


def discover(settings,*,data_dir=ROOT/'data',session=None,key=None,clock=lambda:datetime.now(timezone.utc)):
    """Free preparation for a user-reviewable cost estimate. Contains no credentials."""
    c=operational_client(data_dir=data_dir,run_cap=0,session=session,key=key)
    result=c.events(settings['sport_key'])
    if not isinstance(result.payload,list):
        raise APIError('Events endpoint did not return a list')
    now=clock();day=now.astimezone(ZoneInfo('America/New_York')).date()
    dates=settings.get('horizon',{}).get('dates')
    try:
        events=[]
        for e in result.payload:
            start=datetime.fromisoformat(e['commence_time'].replace('Z','+00:00'))
            if start.tzinfo is None:raise ValueError('Naive event start')
            event_day=start.astimezone(ZoneInfo('America/New_York')).date().isoformat()
            if not settings['include_live'] and start<=now:continue
            if dates and not dates[0]<=event_day<=dates[1]:continue
            events.append(e)
    except (KeyError,TypeError,ValueError):
        raise APIError('Events list has an invalid start time') from None
    ids=[e['id'] for e in events]
    if len(ids)!=len(set(ids)):
        raise APIError('Events endpoint returned duplicate event IDs')
    books=consensus.request_books(settings)
    bound=len(events)*len(settings['markets'])*((len(books)+9)//10)
    return {'events':events,'bound':bound,'discovered_at':datetime.now(timezone.utc).isoformat(),
            'date':day.isoformat(),'event_cache':result.cache_path,'settings':settings}


def _evaluate(frame,settings,profile,capture_time,exclusion_version=2):
    if settings.get('advisory_version') not in (None,1,2,3):raise ValueError('Unsupported advisory version')
    return scan(frame,profile,reference_book=settings['reference_book'],min_edge=settings['min_edge'],
                max_reference_margin=settings['max_reference_margin'],include_live=settings['include_live'],
                now=capture_time,exclusion_version=exclusion_version,alternate_version=settings.get('alternate_version'),
                observation_books=consensus.EXCHANGES if settings.get('advisory_version') else ())


def _frame(results):
    frames=[normalize(r.payload) for r in results]
    return pd.concat(frames,ignore_index=True) if frames else pd.DataFrame(columns=INPUT_COLUMNS)


def _save_snapshot(data_dir,scan_id,settings,profile,profile_name,capture_time,results,event_results,
                   bound,charged,mode='live',display_rules=None,run_accounting=None):
    frame=_frame(results)
    flagged,excluded,counts=_evaluate(frame,settings,profile,capture_time)
    from . import rules
    from .settings import ACCOUNTING_CLOUD
    display_rules=rules.validate(display_rules) if display_rules is not None else rules.active(cloud=ACCOUNTING_CLOUD.get())
    flagged=rules.annotate(flagged,display_rules,settings['sport'])
    if settings.get('advisory_version'):
        flagged=consensus.annotate(flagged,frame,reference=settings['reference_book'],capture_time=capture_time,version=settings['advisory_version'])
    raw=frame.copy()
    if settings.get('advisory_version'):raw['kind']=raw.book.map(lambda b:'exchange' if b in consensus.EXCHANGES else 'sportsbook')
    raw['scan_id']=scan_id
    reasons=[None]*len(raw)
    for row in excluded.itertuples():
        for index in row.source_rows:reasons[index]=row.exclusion_reason
    raw['exclusion_reason']=pd.Series(reasons,dtype='string')
    raw.loc[raw.book==settings['reference_book'],'exclusion_reason']='reference input'
    directory=Path(data_dir)/'snapshots'/settings['sport'];directory.mkdir(parents=True,exist_ok=True)
    base=directory/scan_id
    suffixes=['.parquet','_flagged.csv','_excluded.csv','_coverage.csv','.json']
    if any(Path(str(base)+s).exists() for s in suffixes):
        raise FileExistsError('Snapshot id already exists; no artifacts overwritten')
    cov=[]
    allbooks=consensus.request_books(settings)
    for result in results:
        cov.extend(dict(event_id=result.payload['id'],**row) for row in coverage(result.payload,settings['markets'],allbooks))
    # Failed responses still have one coverage row per requested book/market.
    for event in event_results:
        if event['status']!=200:
            cov.extend(dict(event_id=event['event_id'],book=b,market=m,outcomes=0,returned=False)
                       for b in allbooks for m in settings['markets'])
    raw.to_parquet(str(base)+'.parquet',index=False)
    flagged.to_csv(str(base)+'_flagged.csv',index=False)
    excluded.to_csv(str(base)+'_excluded.csv',index=False)
    pd.DataFrame(cov,columns=['event_id','book','market','outcomes','returned']).to_csv(str(base)+'_coverage.csv',index=False)
    manifest={'schema_version':1,'exclusion_version':2,'scan_id':scan_id,'mode':mode,'settings':settings,'profile':asdict(profile),
              'profile_name':profile_name,'capture_time':capture_time,'upper_bound':bound,'credits_charged':charged,
              'event_results':event_results,'raw_responses':[{'path':str(Path(r.cache_path).relative_to(data_dir)),
                  'sha256':sha(r.cache_path),'request_id':r.request_id} for r in results],
              'raw_rows':len(frame),'flagged_pairs':len(flagged),'excluded_pairs':len(excluded),'exclusions':counts,
              'candidate_pairs':len(flagged)+len(excluded),
              'rules':display_rules,'rule_version':rules.version(display_rules),
              'artifacts':{s:{'name':scan_id+s,'sha256':sha(str(base)+s)} for s in suffixes if s!='.json'}}
    if run_accounting is not None:manifest['accounting']=run_accounting
    # Manifest is the completion marker; a partial output never qualifies for replay.
    write_json(str(base)+'.json',manifest)
    return manifest,flagged,excluded


def run_live(preview,*,data_dir=ROOT/'data',profile=None,profile_name=None,key=None,session=None,progress=print,project_cap=None,display_rules=None,paper_track=True,concurrency=None):
    from . import rules
    from .settings import ACCOUNTING_CLOUD
    display_rules=rules.validate(display_rules) if display_rules is not None else rules.active(cloud=ACCOUNTING_CLOUD.get())
    config=load_config()
    settings=preview['settings']
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    scan_id=stamp+'-'+str(uuid.uuid4())[:8]
    profile_name=profile_name or config['kelly_profile']
    profile=profile or fetch_profiles()[profile_name]
    from .request_pool import Pool,workers,caller_progress
    progress=caller_progress(progress)
    pool=Pool(workers(config,'scans',concurrency))
    with scan_lock(data_dir):
        c=operational_client(data_dir=data_dir,run_id=scan_id,run_cap=preview['bound'],
                 floor=config['account_floor'],project_cap=min(config['project_cap'],project_cap) if project_cap is not None else config['project_cap'],key=key,session=session,pool=pool,progress=progress)
        results=[];statuses=[]
        progress(f"Preflight: {len(preview['events'])} events; upper bound {preview['bound']} credits.")
        if preview['bound']>20_000:
            raise BudgetStop('Run exceeds the 20,000-credit staging threshold')
        def fetch(event):
            from .game_time import started,start_time
            if not settings.get('include_live',False):
                try:skip=started(event.get('commence_time'))
                except ValueError:return None,{'event_id':event['id'],'status':'skipped: start time unavailable'}
                if skip:return None,{'event_id':event['id'],'status':'skipped: game started'}
            deadline=None if settings.get('include_live',False) else start_time(event['commence_time'])
            with c.deadline(deadline):
                try:
                    result=c.event_odds(settings['sport_key'],event['id'],settings['markets'],consensus.request_books(settings))
                    return result,{'event_id':event['id'],'status':result.status,'request_id':result.request_id}
                except APIError as e:
                    if e.status not in (404,422):raise
                    return None,{'event_id':event['id'],'status':e.status}
        # Free whole-run check and its UI update precede every worker submission.
        with failure_receipt(c,Path(data_dir)/'incomplete_scans'/(scan_id+'.json'),{'preview':preview}):
            from .game_time import started
            eligible=settings.get('include_live',False) and bool(preview['events'])
            for event in preview['events']:
                try:eligible=eligible or not started(event.get('commence_time'))
                except ValueError:pass
            if eligible and preview['bound']:c.preflight()
        try:
            finished,errors=pool.map(preview['events'],fetch,
                progress=lambda done,total:progress(f'Captured {done} of {total} games'))
        finally:progress.drain()
        for event,value,error in zip(preview['events'],finished,errors):
            if error is not None:
                statuses.append({'event_id':event['id'],'status':'unfetched or failed: '+type(error).__name__})
            else:
                response,status=value;statuses.append(status)
                if response is not None:results.append(response)
        if any(errors):
            write_json(Path(data_dir)/'incomplete_scans'/(scan_id+'.json'),
                       {'preview':preview,'event_results':statuses,'state':'stopped','rate_limit':pool.rate_limit,'accounting':accounting(c)})
            raise next(e for e in errors if e is not None)
        run_charge=charged(c)
        now=datetime.now(timezone.utc).isoformat()
        with failure_receipt(c,Path(data_dir)/'incomplete_scans'/(scan_id+'.json'),{'preview':preview,'event_results':statuses}):
            result = _save_snapshot(Path(data_dir),scan_id,settings,profile,profile_name,now,results,statuses,preview['bound'],run_charge,display_rules=display_rules,run_accounting=accounting(c))
        from .bet_store import enabled
        if enabled() and paper_track:
            from .paper import track_scan
            receipt=track_scan(scan_id,data_dir=data_dir,display_rules=display_rules,include_started=settings.get('include_live',False))
            if progress: progress(receipt['message'])
        return result


def manifest_path(sport,scan_id=None,data_dir=ROOT/'data'):
    directory=Path(data_dir)/'snapshots'/sport
    if scan_id:
        if not all(c.isalnum() or c in '_-' for c in scan_id):
            raise ValueError('Invalid scan id')
        path=directory/(scan_id+'.json')
        if not path.is_file():
            raise FileNotFoundError('Requested scan manifest not found')
        return path
    paths=sorted(directory.glob('*.json'))
    if not paths:
        raise FileNotFoundError('No completed snapshot exists for this sport')
    return paths[-1]


def replay(sport,scan_id=None,*,data_dir=ROOT/'data'):
    path=manifest_path(sport,scan_id,data_dir)
    manifest=json.loads(path.read_text())
    if manifest['schema_version']!=1:
        raise ValueError('Unsupported snapshot schema')
    frames=[]
    for raw in manifest['raw_responses']:
        cache=Path(data_dir)/raw['path']
        if not cache.resolve().is_relative_to(Path(data_dir).resolve()) or sha(cache)!=raw['sha256']:
            raise ValueError('Raw response path/digest mismatch')
        envelope=json.loads(gzip.decompress(cache.read_bytes()))
        if envelope['status']!=200 or envelope['request_id']!=raw['request_id']:
            raise ValueError('Raw response identity mismatch')
        frames.append(normalize(json.loads(envelope['body'])))
    frame=pd.concat(frames,ignore_index=True) if frames else pd.DataFrame(columns=INPUT_COLUMNS)
    flagged,excluded,counts=_evaluate(frame,manifest['settings'],Profile(**manifest['profile']),manifest['capture_time'],manifest.get('exclusion_version',1))
    if 'rules' in manifest:
        from . import rules
        if rules.version(manifest['rules'])!=manifest.get('rule_version'):raise ValueError('Saved rule version mismatch')
        flagged=rules.annotate(flagged,manifest['rules'],manifest['settings']['sport'])
    if manifest['settings'].get('advisory_version'):
        flagged=consensus.annotate(flagged,frame,reference=manifest['settings']['reference_book'],capture_time=manifest['capture_time'],version=manifest['settings']['advisory_version'])
    for suffix,df in [('_flagged.csv',flagged),('_excluded.csv',excluded)]:
        if hashlib.sha256(df.to_csv(index=False).encode()).hexdigest()!=manifest['artifacts'][suffix]['sha256']:
            raise ValueError('Replay differs from the saved '+suffix+' artifact')
    if counts!=manifest['exclusions']:
        raise ValueError('Replay exclusions differ from the saved snapshot')
    return manifest,flagged,excluded
