"""Verified snapshot readers and serialized local record writes (no network)."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import json
import pandas as pd
from .odds_api import atomic_write
from .pipeline import sha
from .settings import ROOT

DATA = ROOT/'data'


def safe_id(value):
    if not value or not all(c.isalnum() or c in '_-' for c in value):
        raise ValueError('Invalid record identifier')
    return value


@contextmanager
def record_lock(data_dir, name):
    folder=Path(data_dir); folder.mkdir(parents=True,exist_ok=True)
    with (folder/(name+'.lock')).open('a') as handle:
        try: fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('Another '+name+' writer is running') from None
        try: yield
        finally: fcntl.flock(handle,fcntl.LOCK_UN)


def snapshots(data_dir=DATA, sport=None, scan_id=None):
    if sport: safe_id(sport)
    if scan_id: safe_id(scan_id)
    root=Path(data_dir)/'snapshots'
    paths=sorted((root/sport).glob('*.json') if sport else root.glob('*/*.json'))
    found=[]
    for path in paths:
        if scan_id and path.stem!=scan_id: continue
        manifest=json.loads(path.read_text())
        if manifest['scan_id']!=path.stem or manifest['settings']['sport']!=path.parent.name:
            raise ValueError('Snapshot manifest identity mismatch')
        found.append((path,manifest))
    if scan_id and len(found)!=1: raise ValueError('Scan id must identify exactly one saved scan')
    return found


def artifact(path, manifest, suffix):
    item=manifest['artifacts'][suffix]
    file=path.parent/item['name']
    if file.parent!=path.parent or item['name']!=manifest['scan_id']+suffix or sha(file)!=item['sha256']:
        raise ValueError('Snapshot artifact identity or digest mismatch')
    return pd.read_parquet(file) if suffix=='.parquet' else pd.read_csv(file)


def flags(path, manifest):
    frame=artifact(path,manifest,'_flagged.csv')
    frame.insert(0,'row',range(1,len(frame)+1))
    frame.insert(0,'scan_id',manifest['scan_id'])
    frame.insert(0,'sport',manifest['settings']['sport'])
    frame['scan_capture_time']=manifest['capture_time']
    return frame


def csv_write(path, frame):
    atomic_write(Path(path),frame.to_csv(index=False).encode())
