"""Offline same-line CLV and descriptive reports; no settlement or forecast claim."""
import argparse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
from .bet import read_bets
from .close import load_close
from .devig import wpo
from .odds import decimal
from .records import DATA, snapshots, flags, csv_write, record_lock
from .settings import ROOT
from .scan import aware

COLUMNS=['sport','scan_id','row','scan_capture_time','event_id','book','player','market','line','side',
         'price_decimal','bet_implied_p','edge','close_over_decimal','close_under_decimal',
         'close_fair_p','clv_pp','closing_line','line_moved','market_pulled','status','close_fetched_at','rule_version','rule_pass']


def evaluate(frame,closes):
    output=[]
    for r in frame.itertuples():
        price=decimal(getattr(r,'target_'+r.side+'_decimal'))
        row=dict.fromkeys(COLUMNS)
        row.update(sport=r.sport,scan_id=r.scan_id,row=r.row,event_id=r.event_id,book=r.book,
                   scan_capture_time=r.scan_capture_time,rule_version=getattr(r,'rule_version','legacy-unrecorded'),
                   rule_pass=getattr(r,'rule_pass',None),
                   player=r.player_name,market=r.prop_type,line=r.Target_line,side=r.side,
                   price_decimal=price,bet_implied_p=1/price,edge=r.edge,line_moved=False,
                   market_pulled=False,status='awaiting close')
        meta,raw=closes.get(r.event_id,(None,None))
        if meta:
            row['close_fetched_at']=meta['fetched_at']
            if not meta['timing_valid']:row['status']='invalid timing'
            elif aware(meta['fetched_at'])<aware(r.scan_capture_time):row['status']='close predates scan'
            elif r.prop_type not in meta['markets']:row['status']='market not captured'
            else:
                matches=raw[(raw.book=='pinnacle')&(raw.market==r.prop_type)&(raw.player==r.player_name)].copy()
                matches['point']=pd.to_numeric(matches.point,errors='coerce')
                lines=sorted(pd.to_numeric(matches.point,errors='coerce').dropna().unique())
                row['closing_line']=';'.join(format(v,'.15g') for v in lines)
                exact=matches[matches.point==r.Target_line]
                if matches.empty:
                    row.update(status='market pulled',market_pulled=True)
                elif exact.empty and lines:
                    row.update(status='line moved',line_moved=True)
                elif len(exact)!=2 or set(exact.side.str.lower())!={'over','under'}:
                    row['status']='invalid closing pair'
                else:
                    try:
                        if any(aware(t)>aware(meta['fetched_at']) for t in exact.last_update):
                            raise ValueError('Closing update after capture')
                        over=decimal(exact.loc[exact.side.str.lower()=='over','price'].iloc[0])
                        under=decimal(exact.loc[exact.side.str.lower()=='under','price'].iloc[0])
                        fair=wpo([over,under])[('over','under').index(r.side)]
                        row.update(status='matched',close_over_decimal=over,close_under_decimal=under,
                                   close_fair_p=fair,clv_pp=fair-1/price)
                    except ValueError:row['status']='invalid closing pair'
        output.append(row)
    return pd.DataFrame(output,columns=COLUMNS)


def rebuild(*,data_dir=DATA,sport=None):
    all_frames=[]
    with record_lock(data_dir,'clv'):
        for path,m in snapshots(data_dir,sport):
            f=flags(path,m)
            closes={e:load_close(data_dir,m['settings']['sport'],e) for e in f.event_id.unique()}
            result=evaluate(f,closes)
            assert len(result)==m['flagged_pairs']
            csv_write(Path(data_dir)/'clv'/m['settings']['sport']/(m['scan_id']+'_clv.csv'),result)
            all_frames.append(result)
    return pd.concat(all_frames,ignore_index=True) if all_frames else pd.DataFrame(columns=COLUMNS)


def logged_view(clv,*,data_dir=DATA):
    bets=read_bets(data_dir)
    if bets.empty:return bets
    return bets.merge(clv[['scan_id','row','clv_pp','status','line_moved','market_pulled']],
                      on=['scan_id','row'],how='left',validate='one_to_one')


def summary(frame,key):
    lines=['| '+key+' | Rows | Matched | Mean CLV (pp) | Positive share | Mean scan edge (pp) | Line-moved share |',
           '|---|---:|---:|---:|---:|---:|---:|']
    for name,rows in frame.groupby(key,sort=True):
        closed=rows[rows.status.isin(['matched','line moved','market pulled'])]
        matched=rows[rows.status=='matched']
        value=lambda v:'—' if pd.isna(v) else f'{v:.3f}'
        mean=value(matched.clv_pp.mean()*100)
        positive=value((matched.clv_pp>0).mean()*100) if len(matched) else '—'
        moved=value(closed.line_moved.mean()*100) if len(closed) else '—'
        label=str(name).replace('|','/').replace('\n',' ')
        lines.append(f'| {label} | {len(rows)} | {len(matched)} | {mean} | {positive} | {value(rows.edge.mean()*100)} | {moved} |')
    return '\n'.join(lines)


def report(*,data_dir=DATA,output=ROOT/'docs'/'CLV_REPORT.md'):
    frame=rebuild(data_dir=data_dir)
    bets=read_bets(data_dir)
    logged=frame.merge(bets[['scan_id','row']],on=['scan_id','row'],how='inner',validate='one_to_one')
    now=datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M %Z')
    body=[f'# Closing-line value report\n\nGenerated {now}.',
          'Descriptive, pre-start Pinnacle capture within the requested window; this is a closing-price proxy, not a guarantee of the final quote. No bets are placed or settled by this report.',
          'CSV clv_pp is a probability difference; displayed pp values multiply it by 100. Mean CLV and positive share use matched rows only. Line-moved share uses matched, moved and pulled rows; pending/invalid captures are excluded. Missingness is reported below. Repeated scans count separately, so rows are not independent observations.',
          '## Capture status\n\n'+ ('No flagged rows stored.' if frame.empty else '\n'.join(f'- {k}: {v}' for k,v in frame.status.value_counts().items()))]
    for title,data in [('All flagged rows',frame),('Logged bets',logged)]:
        body.append('## '+title)
        if data.empty:body.append('No rows.');continue
        for key in ['sport','market','book','scan_id']:body.append('### By '+key+'\n\n'+summary(data,key))
    body.append('## Validation\n\nSee docs/CLV_LIVE_VALIDATION.md for the dated live sample, seeded independent hand-checks and scope limits. See docs/TASK5_CLV.md for the capture runbook. This regenerated report does not expand the validated sample.')
    text='\n\n'.join(body)+'\n'
    if output is not None:
        from .odds_api import atomic_write
        atomic_write(Path(output),text.encode())
    return text,frame


def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['report']);p.parse_args()
    report();print('Wrote docs/CLV_REPORT.md from local records; no network requests.')


if __name__=='__main__':main()
