"""Incremental free-source settlement, live cards and local archive grading."""
from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import pandas as pd
from .bet_store import BetStore, enabled, HEADER
from .records import DATA, record_lock, snapshots, flags, csv_write
from .settle import due, settle_row, match_game, metadata, OFFICIAL
from .settlement_sources import FreeFeed, FeedUnavailable, nflverse_box
from .live_money import live_row
from .paper import frozen, migrate


class Resolver:
    def __init__(self,feed,*,official=False):
        self.feed=feed;self.official=official;self.catalog={};self.boxes={};self.errors={}

    def resolve(self,bet):
        sport=bet['sport'];day=pd.Timestamp(bet['game_date'])
        games=[]
        for offset in (-1,0,1):
            date=(day+pd.Timedelta(days=offset)).date().isoformat();key=(sport,date)
            if key in self.errors:raise FeedUnavailable(self.errors[key])
            if key not in self.catalog:
                try:self.catalog[key]=self.feed.games(sport,date,official=self.official)
                except FeedUnavailable:
                    self.errors[key]='Source unavailable; no retry in this run'
                    raise
            games.extend(self.catalog[key])
        game,reason=match_game(bet,games)
        if not game:return None,[],reason
        if game['state'] not in ('live','final') or (self.official and game['state']!='final'):
            return game,[],''
        key=(sport,game['id'])
        if key not in self.boxes:
            result=self.feed.box(sport,game,official=self.official)
            if self.official and sport=='nfl' and result[0]['state']=='final':
                try:
                    reconciled=nflverse_box(self.feed,result[0])
                    # A partial weekly release is not permission to void absent players.
                    if reconciled:
                        from .identity import name_key
                        weekly={name_key(p['player_name']):p for p in reconciled[1]}
                        self.boxes[key+('weekly',)]=reconciled
                except (FeedUnavailable,ValueError,KeyError):pass
            self.boxes[key]=result
        result=self.boxes[key]
        weekly=self.boxes.get(key+('weekly',))
        if weekly:
            from .identity import name_key
            matches=[p for p in weekly[1] if name_key(p['player_name'])==name_key(bet['player'])]
            if len(matches)==1:return weekly[0],weekly[1],''
        return *result,''


def live_table(frame,feed):
    resolver=Resolver(feed)
    output=[]
    for bet in frame.to_dict('records'):
        if bet.get('status') in OFFICIAL:
            output.append(dict(bet,stage='official',verdict=bet['status'],score='',detail='Official grade'));continue
        if bet['sport'] not in ('nhl','nba'):
            start=pd.to_datetime(bet.get('start_time'),utc=True,errors='coerce')
            upcoming=pd.notna(start) and start>pd.Timestamp.now(tz='UTC')
            output.append(dict(bet,stage='upcoming' if upcoming else 'untracked',
                               verdict='⏳ upcoming' if upcoming else '— not tracked live',
                               score='',detail='Live tracking is available for NHL and NBA; official grading covers every sport.'))
            continue
        try:game,players,reason=resolver.resolve(bet)
        except (FeedUnavailable,ValueError,KeyError):game,players,reason=None,[],'Free live source unavailable'
        row=live_row(bet,game,players)
        if reason:row['detail']=reason
        output.append(row)
    return pd.DataFrame(output) if output else frame.copy()


def changes_for(frame,resolver,now):
    changes=[];reasons=[]
    for bet in frame.to_dict('records'):
        if not due(bet,now):continue
        try:
            game,players,reason=resolver.resolve(bet)
            if game:new,reason=settle_row(bet,game,players,now=now)
            else:new=dict(bet)
        except (FeedUnavailable,ValueError,KeyError):new,reason=dict(bet),'Official source unavailable; retained previous status'
        if new==bet and bet['status']=='pending':
            # Never parse or truncate the owner's note to store our own status.
            new['source']=json.dumps(dict(metadata(bet),pending_reason=reason),sort_keys=True)
        if new!=bet:changes.append((bet,new))
        reasons.append(dict(scan_id=bet['scan_id'],row=bet['row'],status=new['status'],reason=reason))
    return changes,reasons


def grade_all(*,data_dir=DATA,store=None,feed=None,now=None,backup=False,capture_live=False):
    now=now or datetime.now(timezone.utc).isoformat()
    store=store or BetStore(data_dir)
    feed=feed or FreeFeed(Path(data_dir)/'settlement_cache')
    with record_lock(data_dir,'grading'):
        migration=migrate(store)
        for kind in ('real','paper'):store.append(kind,[])
        frames={kind:store.read(kind,reload=True) for kind in ('real','paper')}
        resolver=Resolver(feed,official=True)
        report={'at':now,'migration':migration,'logs':{}}
        for kind,frame in frames.items():
            changes,reasons=changes_for(frame,resolver,now)
            written=store.grade(kind,changes) if changes else 0
            report['logs'][kind]={'updated':written,'rows':reasons}
        if capture_live:
            # Headless evidence for the same NHL/NBA live cards shown in the hosted app.
            # FreeFeed caches every original response before parsing.
            from .live_money import card_money
            report['live']={}
            for kind in frames:
                card,actual,projected=card_money(live_table(store.read(kind,reload=True),feed))
                report['live'][kind]={'official_profit':actual,'projected_profit':projected,
                                      'rows':card.to_dict('records')}
        if backup:store.backup(pd.Timestamp(now).tz_convert('America/New_York').date().isoformat())
        report['source_failures']=sum('source unavailable' in row['reason'].lower() for item in report['logs'].values() for row in item['rows'])
        path=Path(data_dir)/'grading_runs'/pd.Timestamp(now).strftime('%Y%m%dT%H%M%S%fZ.json')
        from .odds_api import atomic_write
        atomic_write(path,json.dumps(report,default=str,indent=2).encode())
        return report


def grade_archive(*,data_dir=DATA,feed=None,now=None):
    """Every flagged snapshot row, independent of paper rules. Resume per scan/row."""
    now=now or datetime.now(timezone.utc).isoformat()
    feed=feed or FreeFeed(Path(data_dir)/'settlement_cache',max_requests=1000)
    resolver=Resolver(feed,official=True)
    count=0
    with record_lock(data_dir,'archive-grading'):
        for path,manifest in snapshots(data_dir):
            target=Path(data_dir)/'settled'/manifest['settings']['sport']/(manifest['scan_id']+'.csv')
            if target.exists():frame=pd.read_csv(target,keep_default_na=False,dtype=str)
            else:
                f=flags(path,manifest)
                frame=pd.DataFrame([frozen(manifest,r,i+1,stake=max(0.01,float(r.kellyAmount)),paper=True) for i,r in f.iterrows()],columns=HEADER)
                # Archive analysis keeps the actual suggested stake, including zero.
                if len(frame):frame['stake']=f.kellyAmount.to_numpy()
            changes,_=changes_for(frame,resolver,now)
            by_key={(str(old['scan_id']),str(old['row'])):new for old,new in changes}
            rows=[by_key.get((str(r['scan_id']),str(r['row'])),r) for r in frame.to_dict('records')]
            csv_write(target,pd.DataFrame(rows,columns=HEADER));count+=len(rows)
    return count


def in_window(now):
    t=pd.Timestamp(now).tz_convert('America/New_York')
    return (t.minute in (0,30) and (t.hour>=17 or t.hour<3 or (t.hour==3 and t.minute==0))) or (t.hour==5 and t.minute==30)


def main():
    p=argparse.ArgumentParser(description='Free-source grading; never requests odds')
    p.add_argument('--scheduled',action='store_true');p.add_argument('--archive',action='store_true');p.add_argument('--backup',action='store_true')
    p.add_argument('--capture-live',action='store_true',help='Retain real NHL/NBA live-card evidence alongside grades')
    a=p.parse_args();now=datetime.now(timezone.utc).isoformat()
    if not enabled():p.exit(2,'Settlement is disabled or service-account configuration is absent.\n')
    if a.scheduled and not in_window(now):return
    try:
        if a.archive:print(json.dumps({'archive_rows':grade_archive(now=now)}))
        else:
            result=grade_all(now=now,capture_live=a.capture_live or a.scheduled,backup=a.backup or (a.scheduled and pd.Timestamp(now).tz_convert('America/New_York').hour==5))
            print(json.dumps({'at':now,'updated':{k:v['updated'] for k,v in result['logs'].items()},'source_failures':result['source_failures']}))
            if result['source_failures']:p.exit(1,'Some official sources unavailable; affected rows remain pending or retain prior grades.\n')
    except Exception:
        p.exit(1,'Grading failed; previous results retained. Check local run evidence and source availability.\n')

if __name__=='__main__':main()
