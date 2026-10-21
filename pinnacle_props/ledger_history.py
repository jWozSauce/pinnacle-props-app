"""Version-2 ledger history: immutable CSV segments, bounded active JSON.

The caller holds odds.lock. Segments are durable before a JSON pointer commits;
interrupted writes leave harmless unreferenced segments, never a lost reservation.
"""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import csv,hashlib,io,json,re
from zoneinfo import ZoneInfo

FIELDS=['request_id','run_id','timestamp','endpoint','estimated','actual','remaining','status','cache_path','outcome']


def encode(records):
    out=io.StringIO();writer=csv.DictWriter(out,fieldnames=FIELDS);writer.writeheader();writer.writerows(records)
    return out.getvalue().encode()


def decode(body):
    reader=csv.DictReader(io.StringIO(body.decode()))
    if reader.fieldnames!=FIELDS:raise ValueError('History CSV header mismatch')
    rows=[]
    for row in reader:
        for key in ('estimated','actual','remaining'):
            row[key]=int(row[key]) if row[key] else None
        if row['status'].isdigit():row['status']=int(row['status'])
        if row['actual'] is None or row['actual']<0:raise ValueError('Invalid charge cannot be archived')
        rows.append(row)
    return rows


def summary(records):
    days={}
    for r in records:
        if r['run_id'].startswith('close-'):
            day=datetime.fromisoformat(r['timestamp'].replace('Z','+00:00')).astimezone(ZoneInfo('America/New_York')).date().isoformat()
            days[day]=days.get(day,0)+r['actual']
    return dict(count=len(records),spent=sum(r['actual'] for r in records),close_days=days)


def validate(directory,state,cache):
    segments=state.get('history',[]);assert isinstance(segments,list)
    assert len({s['name'] for s in segments})==len(segments)
    assert state['archived_spent']==sum(s['spent'] for s in segments)
    assert state['archived_records']==sum(s['count'] for s in segments)
    for segment in segments:
        name=segment['name'];assert re.fullmatch(r'[a-f0-9]{64}\.csv',name)
        path=Path(directory)/'odds_history'/name;st=path.stat()
        signature=(st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns,json.dumps(segment,sort_keys=True))
        if cache.get(name)!=signature:
            body=path.read_bytes();assert hashlib.sha256(body).hexdigest()+'.csv'==name
            assert summary(decode(body))=={k:segment[k] for k in ('count','spent','close_days')}
            cache[name]=signature


def compact(directory,state,*,now=None):
    from .odds_api import atomic_write
    now=now or datetime.now(timezone.utc);cutoff=now-timedelta(days=2)
    # Do not relocate records while any sender may still reference a reservation.
    if state['reservations']:return
    archive=[];active=[]
    for row in state['records']:
        try:old=datetime.fromisoformat(row['timestamp'].replace('Z','+00:00'))<cutoff
        except (ValueError,TypeError):old=False
        (archive if old and row['actual'] is not None else active).append(row)
    if not archive:return
    body=encode(archive);name=hashlib.sha256(body).hexdigest()+'.csv'
    path=Path(directory)/'odds_history'/name
    if path.exists():assert path.read_bytes()==body
    else:atomic_write(path,body)
    state.setdefault('history',[]).append(dict(name=name,**summary(archive)))
    state['archived_spent']=state.get('archived_spent',0)+sum(r['actual'] for r in archive)
    state['archived_records']=state.get('archived_records',0)+len(archive)
    state['records']=active


def find_record(directory,state,request_id):
    found=[r for r in state['records'] if r['request_id']==request_id]
    for segment in state.get('history',[]):
        found.extend(r for r in decode((Path(directory)/'odds_history'/segment['name']).read_bytes()) if r['request_id']==request_id)
    return found


def full_records(directory,state):
    return [r for s in state.get('history',[]) for r in decode((Path(directory)/'odds_history'/s['name']).read_bytes())]+state['records']


def migrate(client):
    from .odds_api import BudgetStop,atomic_write
    with client._locked():
        state=client._load()
        if state['version']==2:return {'status':'already migrated','spent':state['spent'],'active_records':len(state['records']),'archived_records':state['archived_records']}
        if state['halt'] or state['reservations']:raise BudgetStop('Reconcile halt/reservations before ledger migration')
        assert len({r['request_id'] for r in state['records']})==len(state['records'])
        directory=client.accounting_dir
        before={name:(directory/name).read_bytes() for name in ('odds_budget.json','odds_usage.csv') if (directory/name).exists()}
        backup=directory/'ledger_backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup.mkdir(parents=True,exist_ok=False);backup.chmod(0o700)
        for name,body in before.items():atomic_write(backup/name,body)
        hashes={name:hashlib.sha256(body).hexdigest() for name,body in before.items()}
        atomic_write(backup/'provenance.json',json.dumps({'schema_from':1,'schema_to':2,'sha256':hashes,'spent':state['spent']}).encode())
        state.update(version=2,history=[],archived_spent=0,archived_records=0,
                     history_migration={'at':datetime.now(timezone.utc).isoformat(),'backup':str(backup),'sha256':hashes},
                     usage_csv_scope='active records; full history is immutable odds_history CSV segments plus active records')
        compact(directory,state)
        validate(directory,state,{})
        client._save(state);after=client._load()
        assert after['spent']==sum(r['actual'] or 0 for r in full_records(directory,after))
        return {'status':'migrated','spent':after['spent'],'active_records':len(after['records']),
                'archived_records':after['archived_records'],'json_bytes':(directory/'odds_budget.json').stat().st_size,
                'backup':str(backup),'source_sha256':hashes}
