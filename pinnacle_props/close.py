"""Manual, pre-start Pinnacle capture with immutable provenance and resumable requests."""
from .live_api import operational_client, charged, accounting, failure_receipt
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import uuid
from zoneinfo import ZoneInfo
import pandas as pd
from .odds_api import APIError, normalize, write_json, resolve_agent_key
from .pipeline import sha
from .records import DATA, snapshots, artifact, record_lock, safe_id
from .scan import aware
from .settings import load_config


def utcnow(): return datetime.now(timezone.utc)


def assess_timing(listing_start,response_start,fetched_at,window):
    """Q28: capture must precede, and be within the window of, both starts."""
    if not math.isfinite(window) or window<=0:raise ValueError('Invalid close window')
    leads=[(aware(s)-aware(fetched_at)).total_seconds()/60 for s in (listing_start,response_start)]
    return dict(timing_version=2,listing_start=listing_start,response_start=response_start,
                listing_lead_minutes=leads[0],response_lead_minutes=leads[1],
                timing_valid=all(0<v<=window for v in leads))


def reassessment(path,data_dir):
    """Build a versioned assessment from immutable original metadata and raw bytes."""
    import gzip
    original=json.loads(path.read_text());raw=Path(data_dir)/original['raw_path']
    if sha(raw)!=original['raw_sha256'] or sha(path.parent/original['parquet'])!=original['sha256']:
        raise ValueError('Reassessment source digest mismatch')
    envelope=json.loads(gzip.decompress(raw.read_bytes()))
    payload=json.loads(envelope['body'])
    if payload['id']!=original['event_id'] or envelope['captured_at']!=original['fetched_at']:
        raise ValueError('Reassessment raw identity mismatch')
    return dict(original_metadata_sha256=sha(path),raw_sha256=sha(raw),
                **assess_timing(original['start'],payload['commence_time'],
                                original['fetched_at'],original['window']))


def load_close(data_dir,sport,event_id):
    folder=Path(data_dir)/'closes'/safe_id(sport)
    name=safe_id(event_id)+'.json'
    candidates=sorted(folder.glob('*/'+name))
    legacy=folder/name
    if legacy.exists():candidates.append(legacy)
    if not candidates:return None,None
    path=candidates[0]
    if len(candidates)>1:
        values=[json.loads(p.read_text()) for p in candidates]
        if len({(v['sha256'],v['fetched_at']) for v in values})!=1:
            raise ValueError('Conflicting close records for one event')
    meta=json.loads(path.read_text())
    raw=path.parent/meta['parquet']
    if raw.parent!=path.parent or sha(raw)!=meta['sha256'] or meta['event_id']!=event_id or meta['sport']!=sport:
        raise ValueError('Close identity or digest mismatch')
    assessment=path.with_name(event_id+'.timing-v2.json')
    if assessment.exists():
        revised=json.loads(assessment.read_text())
        if revised!=reassessment(path,data_dir):raise ValueError('Reassessment differs from raw evidence')
        meta=dict(meta,original_timing_valid=meta['timing_valid'],**revised)
        meta['status']=('captured' if meta.get('returned_markets',meta['markets']) else 'no Pinnacle board') if meta['timing_valid'] else 'invalid timing'
    return meta,pd.read_parquet(raw)


def prepare(sport,scan_id=None,*,data_dir=DATA,window=10,now=None):
    if not math.isfinite(window) or window<=0:raise ValueError('Window must be positive and finite')
    clock=aware(now or utcnow())
    all_scans=snapshots(data_dir,sport)
    selected=snapshots(data_dir,sport,scan_id) if scan_id else all_scans
    selected_events=set()
    for path,m in selected:
        selected_events.update(artifact(path,m,'.parquet').event_id.dropna())
    events={}
    for path,m in all_scans:
        raw=artifact(path,m,'.parquet')
        for event_id,rows in raw.groupby('event_id'):
            if event_id not in selected_events:continue
            safe_id(event_id)
            starts={aware(t).isoformat() for t in rows.commence_time}
            if len(starts)!=1:raise ValueError('Conflicting event start times in saved scan')
            start=starts.pop()
            item=events.setdefault(event_id,dict(event_id=event_id,start=start,markets=set(),sport_key=m['settings']['sport_key']))
            if item['start']!=start or item['sport_key']!=m['settings']['sport_key']:
                raise ValueError('Event identity/start changed across scans; resolve before close capture')
            item['markets'].update(m['settings']['markets'])
    jobs=[]
    for item in events.values():
        minutes=(aware(item['start'])-clock).total_seconds()/60
        meta,_=load_close(data_dir,sport,item['event_id'])
        pending=Path(data_dir)/'closes'/sport/(item['event_id']+'_job.json')
        if meta or (not 0<minutes<=window and not pending.exists()):continue
        item['markets']=sorted(item['markets']);jobs.append(item)
    return dict(sport=sport,window=window,events=jobs,bound=sum(len(e['markets']) for e in jobs))


def capture(sport,scan_id=None,*,data_dir=DATA,window=10,key=None,session=None,clock=utcnow,progress=print):
    config=load_config()
    if sport not in config['sports']:raise ValueError('Unknown configured sport')
    results=[]
    with record_lock(data_dir,'closes'):
        plan=prepare(sport,scan_id,data_dir=data_dir,window=window,now=clock())
        progress(f"Preflight: {len(plan['events'])} eligible events; at most {plan['bound']} credits; pre-start window {window:g} minutes.")
        if plan['bound']>20_000:raise ValueError('Run exceeds staging threshold')
        directory=Path(data_dir)/'closes'/sport;directory.mkdir(parents=True,exist_ok=True)
        for event in plan['events']:
            # A persisted request identity permits raw-cache recovery without a second charge.
            job_path=directory/(event['event_id']+'_job.json')
            if job_path.exists():
                job=json.loads(job_path.read_text())
                if job['event']!=event:raise ValueError('Saved close job inputs changed; reconcile before retry')
            else:
                job={'event':event,'run_id':'close-'+str(uuid.uuid4()),'window':window}
                write_json(job_path,job)
            c=operational_client(data_dir=data_dir,run_id=job['run_id'],run_cap=len(event['markets']),
                     floor=config['account_floor'],project_cap=config['project_cap'],key=key,session=session,paid_deadline=event['start'],progress=progress,resume_only=bool(job.get('attempted_at')))
            remaining=(aware(event['start'])-aware(clock())).total_seconds()/60
            cache=c._cache_path(f"/v4/sports/{event['sport_key']}/events/{event['event_id']}/odds",
                                {'markets':','.join(event['markets']),'bookmakers':'pinnacle','oddsFormat':'decimal'})
            if not cache.exists() and not 0<remaining<=job['window']:
                progress('Event left the pre-start window; no new request sent for '+event['event_id']);continue
            with failure_receipt(c,job_path,job):
                if not job.get('attempted_at'):
                    c.preflight()
                    job['attempted_at']=utcnow().isoformat();write_json(job_path,job)
                response=c.event_odds(event['sport_key'],event['event_id'],event['markets'],['pinnacle'])
                # Use the raw response capture time, including when recovering a cached request.
                import gzip
                envelope=json.loads(gzip.decompress(Path(response.cache_path).read_bytes()))
                fetched_at=envelope['captured_at']
                rows=normalize(response.payload)
                actual_start=aware(response.payload['commence_time'])
                timing=assess_timing(event['start'],response.payload['commence_time'],fetched_at,job['window'])
                valid=timing['timing_valid']
                filename=event['event_id']+'_'+aware(fetched_at).tz_convert('UTC').strftime('%Y%m%dT%H%M%S%fZ')+'.parquet'
                output=directory/filename
                if output.exists():
                    pd.testing.assert_frame_equal(pd.read_parquet(output),rows)
                else: rows.to_parquet(output,index=False)
                meta=dict(sport=sport,event_id=event['event_id'],fetched_at=fetched_at,start=event['start'],
                          markets=event['markets'],sport_key=event['sport_key'],window=job['window'],
                          **timing,status='captured' if valid else 'invalid timing',
                          parquet=filename,sha256=sha(output),request_id=response.request_id,
                          raw_path=str(Path(response.cache_path).relative_to(data_dir)),raw_sha256=sha(response.cache_path),
                          run_id=job['run_id'],bound=len(event['markets']),charged=charged(c),accounting=accounting(c))
                write_json(directory/(event['event_id']+'.json'),meta)
                results.append(meta)
                progress(f"{event['event_id']}: {meta['status']}; {meta['charged']} credits.")
    from .clv import rebuild
    rebuild(data_dir=data_dir,sport=sport)
    return results


def capture_all(*,data_dir=DATA,window=10,key=None,session=None,clock=utcnow,progress=print):
    """One bounded pass through stored sports; never discover or scan a new slate."""
    results=[]
    with record_lock(data_dir,'close-agent'):
        for sport in load_config()['sports']:
            plan=prepare(sport,data_dir=data_dir,window=window,now=clock())
            if not plan['events']:continue
            progress('Sport: '+sport)
            results.extend(capture(sport,data_dir=data_dir,window=window,key=key,
                                   session=session,clock=clock,progress=progress))
    return results


def main():
    p=argparse.ArgumentParser(description='Manually capture a pre-start Pinnacle closing-price proxy')
    p.add_argument('--sport');choice=p.add_mutually_exclusive_group(required=True)
    choice.add_argument('--all-open',action='store_true');choice.add_argument('--scan')
    choice.add_argument('--all-events',action='store_true')
    p.add_argument('--window',type=float,default=10)
    a=p.parse_args()
    if a.scan and not a.sport:p.error('--scan requires --sport')
    def log(message):
        stamp=datetime.now(ZoneInfo('America/New_York')).isoformat(timespec='seconds')
        print(stamp+' '+message,flush=True)
    log('run_started')
    try:
        key=resolve_agent_key() if not a.sport else None
        if a.all_events:
            from .close_archive import capture_all_events
            results=capture_all_events(sport=a.sport,window=a.window,progress=log,key=key)
        elif a.sport:results=capture(a.sport,a.scan,window=a.window,progress=log,key=key)
        else:results=capture_all(window=a.window,progress=log,key=key)
        log(f'run_completed captures={len(results)}'+(' idle/no new captures' if not results else ''))
    except (APIError,ValueError,OSError,KeyError) as e:
        # Do not print source lines, locals, request URLs or credentials. File/line
        # frames distinguish a File Provider read failure from a lock failure.
        if isinstance(e,OSError):
            import traceback
            frames=traceback.extract_tb(e.__traceback__)
            log('io_failure '+json.dumps({'errno':e.errno,'frames':[
                {'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in frames]}))
        log('run_failed '+str(e));p.exit(1)


if __name__=='__main__':main()
