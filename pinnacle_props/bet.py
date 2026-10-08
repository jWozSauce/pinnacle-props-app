"""Record an already placed bet locally; this module never places a bet."""
import argparse
from datetime import datetime, timezone
import math
from pathlib import Path
import pandas as pd
from .records import DATA, snapshots, flags, record_lock, csv_write

COLUMNS=['logged_at','scan_id','row','sport','event_id','book','player','market','line','side',
         'price_decimal','fair_p','edge','stake_suggested','stake_actual','note']


def read_bets(data_dir=DATA):
    path=Path(data_dir)/'bets'/'bets.csv'
    return pd.read_csv(path,keep_default_na=False) if path.exists() else pd.DataFrame(columns=COLUMNS)


def log_bet(scan_id,row,stake=None,note='',*,data_dir=DATA):
    if isinstance(row,bool) or not isinstance(row,int) or row<1: raise ValueError('Row is a one-based positive integer')
    path,manifest=snapshots(data_dir,scan_id=scan_id)[0]
    frame=flags(path,manifest)
    if row>len(frame): raise ValueError('Row is outside this saved scan')
    r=frame.iloc[row-1];side=r['side']
    amount=float(r.kellyAmount if stake is None else stake)
    if not math.isfinite(amount) or amount<=0: raise ValueError('Actual stake must be finite and positive')
    if len(note)>2000: raise ValueError('Note must be at most 2000 characters')
    record=dict(logged_at=datetime.now(timezone.utc).isoformat(),scan_id=scan_id,row=row,sport=r.sport,
                event_id=r.event_id,book=r.book,player=r.player_name,market=r.prop_type,line=r.Target_line,
                side=side,price_decimal=r['target_'+side+'_decimal'],fair_p=r['noVig'+side.title()],
                edge=r.edge,stake_suggested=r.kellyAmount,stake_actual=amount,note=note)
    with record_lock(data_dir,'bets'):
        old=read_bets(data_dir)
        if ((old.scan_id==scan_id)&(old.row==row)).any():
            raise ValueError('This saved scan row is already logged; no duplicate written')
        new=pd.DataFrame([record],columns=COLUMNS)
        csv_write(Path(data_dir)/'bets'/'bets.csv',new if old.empty else pd.concat([old,new],ignore_index=True))
    return record


def main():
    p=argparse.ArgumentParser(description='Record a bet you already placed; never places bets')
    sub=p.add_subparsers(dest='command',required=True)
    log=sub.add_parser('log'); log.add_argument('--scan',required=True); log.add_argument('--row',required=True,type=int)
    log.add_argument('--stake',type=float);log.add_argument('--note',default='')
    a=p.parse_args()
    try:
        result=log_bet(a.scan,a.row,a.stake,a.note)
        print(f"Recorded scan {result['scan_id']} row {result['row']}. No bet was placed by this software.")
    except (ValueError,OSError,KeyError) as e: p.exit(1,str(e)+'\n')


if __name__=='__main__':main()
