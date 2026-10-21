"""Read-only status totals from completed/failed run manifests, never a spend ledger."""
from datetime import datetime
from pathlib import Path
import json


def since(data_dir,after):
    cutoff=datetime.fromisoformat(after.replace('Z','+00:00'))
    if cutoff.tzinfo is None:raise ValueError('Status cutoff requires timezone')
    root=Path(data_dir);requests={};legacy={};unknown=set();issues=[]
    for directory in ('snapshots','incomplete_scans','closes','imports','catalog'):
        for path in sorted((root/directory).rglob('*.json')):
            if path.is_symlink():continue
            # Legacy discovery/day summaries are not per-run charge receipts.
            if directory=='closes' and path.parent==root/'closes'/'manifests':continue
            try:
                if getattr(path.stat(),'st_flags',0)&0x40000000:
                    raise OSError('Offline File Provider manifest')
                value=json.loads(path.read_text())
                if not isinstance(value,dict):continue
                accounting=value.get('accounting',{})
                if accounting.get('policy')=='account-floor-v1':
                    for row in accounting['requests']:
                        if datetime.fromisoformat(row['at'].replace('Z','+00:00'))<=cutoff:continue
                        rid=row['request_id'];credits=row['credits']
                        if credits is None:unknown.add(rid);continue
                        if type(credits) is not int or credits<0:raise ValueError('Invalid charge evidence')
                        if rid in requests and requests[rid]!=credits:raise ValueError('Conflicting charge evidence')
                        requests[rid]=credits;unknown.discard(rid)
                elif value.get('scan_id') and 'credits_charged' in value:
                    if datetime.fromisoformat(value['capture_time'].replace('Z','+00:00'))>cutoff:
                        legacy[value['scan_id']]=value['credits_charged']
                elif value.get('run_id') and 'charged' in value and value.get('fetched_at'):
                    if datetime.fromisoformat(value['fetched_at'].replace('Z','+00:00'))>cutoff:
                        legacy[value['run_id']]=value['charged']
            except (ValueError,KeyError,TypeError,AttributeError,OSError):issues.append(str(path.relative_to(root)))
    # Cache recovery may repeat a request in more than one manifest; count its ID once.
    return dict(credits=sum(requests.values())+sum(legacy.values()),requests=len(requests),
                unknown_charges=len(unknown-set(requests)),issues=issues,complete=not unknown-set(requests) and not issues)


def main():
    import argparse
    from .settings import ROOT
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--after',required=True,help='Previous status timestamp with timezone')
    parser.add_argument('--data-dir',type=Path,default=ROOT/'data')
    args=parser.parse_args()
    report=since(args.data_dir,args.after)
    quota=args.data_dir/'quota.json'
    report['last_free_check']=json.loads(quota.read_text()) if quota.exists() else None
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
