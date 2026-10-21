"""Frozen forward-test records and one-time legacy migration."""
from datetime import datetime, timezone
from pathlib import Path
import json
import uuid
import pandas as pd
from .bet_store import BetStore, HEADER
from .records import DATA, snapshots, flags
from .odds import decimal_to_american
from . import rules


def frozen(manifest, r, row, *, stake=None, note='', paper=False, display_rules=None):
    amount=float(r['kellyAmount'] if stake is None else stake)
    if not pd.notna(amount) or amount<=0 or amount==float('inf'):raise ValueError('Stake must be finite and positive')
    side=r['side'];price=float(r['target_'+side+'_decimal'])
    start=pd.Timestamp(r['date'])
    if start.tzinfo is None:raise ValueError('Frozen bet needs a timezone-aware start')
    record=dict.fromkeys(HEADER,'')
    active=display_rules if display_rules is not None else manifest.get('rules',rules.active())
    record.update(row_id=str(uuid.uuid4()),round=manifest['scan_id'] if paper else '',logged_at=datetime.now(timezone.utc).isoformat(),
                  sport=manifest['settings']['sport'],game_date=start.tz_convert('America/New_York').date().isoformat(),
                  start_time=start.tz_convert('UTC').isoformat(),event_id=r['event_id'],away_team=r['away_team'],home_team=r['home_team'],
                  player=r['player_name'],market=r['prop_type'],line=float(r['Target_line']),side=side,book=r['book'],
                  price_decimal=price,odds_american=decimal_to_american(price),stake=amount,edge=float(r['edge']),
                  fair_p=float(r['noVig'+side.title()]),scan_id=manifest['scan_id'],row=int(row),
                  rule_version=rules.version(active),status='pending',note=note)
    from .consensus import FIELDS
    record.update({k:r.get(k,'') for k in FIELDS})
    return record


def track_scan(scan_id, *, data_dir=DATA, display_rules=None, store=None, include_started=False):
    path,manifest=snapshots(data_dir,scan_id=scan_id)[0]
    frame=flags(path,manifest)
    active=display_rules if display_rules is not None else rules.active()
    annotated=rules.annotate(frame,active,manifest['settings']['sport'])
    from .game_time import suggestions
    annotated,_,_=suggestions(annotated,show_started=include_started)
    records=[frozen(manifest,r,i+1,paper=True,display_rules=active) for i,r in annotated.iterrows()
             if r['rule_pass'] and float(r['kellyAmount'])>0]
    return (store or BetStore(data_dir)).append('paper',records)


def migrate(store):
    """Keep the old CSV untouched. A persisted migration UUID makes every retry idempotent."""
    path=store.root/'bets'/'bets.csv'
    if not path.exists():return {'written':0,'queued':0,'unresolved':0}
    from .records import record_lock
    from .odds_api import atomic_write
    # Persist assigned UUIDs BEFORE sending anything, without changing the original CSV.
    with record_lock(store.root,'migration-identities'):
        identity_path=store.root/'bets'/'migration_row_ids.json'
        identities=json.loads(identity_path.read_text()) if identity_path.exists() else {}
        for old in pd.read_csv(path,keep_default_na=False).to_dict('records'):
            key=json.dumps([old['scan_id'],int(old['row'])])
            if key not in identities:identities[key]=str(uuid.uuid4())
        atomic_write(identity_path,json.dumps(identities,sort_keys=True).encode())
    records=[];unresolved=[]
    for old in pd.read_csv(path,keep_default_na=False).to_dict('records'):
        try:
            source,m=snapshots(store.root,scan_id=old['scan_id'])[0]
            record=frozen(m,flags(source,m).iloc[int(old['row'])-1],int(old['row']),stake=old['stake_actual'],note=old.get('note',''))
            record['row_id']=identities[json.dumps([old['scan_id'],int(old['row'])])]
            # Preserve the actual log-time beliefs, including older rule versions.
            for field in ('logged_at','price_decimal','fair_p','edge','rule_version'):
                if field in old:record[field]=old[field]
            record['odds_american']=decimal_to_american(record['price_decimal'])
            records.append(record)
        except (ValueError,KeyError,IndexError,FileNotFoundError):unresolved.append(old)
    result=store.append('real',records)
    if unresolved:
        from .records import csv_write
        csv_write(store.root/'bets'/'migration_unresolved.csv',pd.DataFrame(unresolved))
    result['unresolved']=len(unresolved)
    return result
