"""Bounded, cached public box scores. These clients cannot call a paid odds host."""
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit
import hashlib
import io
import json
import time
import urllib.request
import pandas as pd
from .odds_api import atomic_write
from .settle import match_game

ESPN = {'nfl':'football/nfl','nhl':'hockey/nhl','mlb':'baseball/mlb','nba':'basketball/nba','wnba':'basketball/wnba'}
HOSTS={'site.api.espn.com','api-web.nhle.com','statsapi.mlb.com','github.com'}

class FeedUnavailable(ValueError):pass


def eastern(value):return pd.Timestamp(value).tz_convert('America/New_York').date().isoformat()


def number(value):
    try:
        n=float(value)
        return n if pd.notna(n) and abs(n)!=float('inf') else None
    except (ValueError,TypeError):return None


def seconds(value):
    try:
        m,s=map(int,str(value).split(':'));return 60*m+s if 0<=s<60 else None
    except (ValueError,TypeError):return None


def outs(value):
    """Baseball 5.2 means seventeen outs, never 5.2*3."""
    parts=str(value).split('.')
    if len(parts)>2 or not parts[0].isdigit():return None
    rem=parts[1] if len(parts)==2 else '0'
    return int(parts[0])*3+int(rem) if rem in ('0','1','2') else None


class FreeFeed:
    def __init__(self,root,*,clock=time.time,fetch=None,max_requests=200):
        self.root=Path(root);self.clock=clock;self.fetch=fetch;self.max_requests=max_requests
        self.calls=0;self.last=0.;self.memo={};self.failed_hosts=set()

    def get(self,url,params=None,*,ttl=60,binary=False):
        u=urlsplit(url)
        if u.scheme!='https' or u.hostname not in HOSTS or u.query or u.username:
            raise FeedUnavailable('Unapproved free source')
        if any('key' in str(k).lower() or 'token' in str(k).lower() for k in (params or {})):
            raise FeedUnavailable('Credentials forbidden in public source requests')
        if u.hostname in self.failed_hosts:raise FeedUnavailable('Source already failed this run; no retry')
        request={'url':url,'params':params or {}}
        key=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
        latest=self.root/(key+'.latest.json')
        now=self.clock()
        if key in self.memo and now-self.memo[key][0]<ttl:
            raw,digest=self.memo[key][1:]
        else:
            info=json.loads(latest.read_text()) if latest.exists() else None
            if info and now-info['time']<ttl:
                raw=(self.root/(info['sha256']+'.raw')).read_bytes();digest=info['sha256']
                if hashlib.sha256(raw).hexdigest()!=digest:raise FeedUnavailable('Public cache integrity failed')
            else:
                if self.calls>=self.max_requests:raise FeedUnavailable('Free-source request bound reached')
                time.sleep(max(0,.5-(time.monotonic()-self.last)))
                self.calls+=1
                try:
                    full=url+('?' + urlencode(params) if params else '')
                    request_obj=urllib.request.Request(full,headers={'User-Agent':'Mozilla/5.0 PinnacleProps/1.0'}) if u.hostname=='api-web.nhle.com' else full
                    # urllib's default redirect handler follows GET 307 redirects.
                    raw=self.fetch(full) if self.fetch else urllib.request.urlopen(request_obj,timeout=30).read()
                except Exception:
                    self.last=time.monotonic()
                    self.failed_hosts.add(u.hostname)
                    # Do not persist exception text/headers and never auto retry.
                    raise FeedUnavailable('Public source unavailable; no automatic retry') from None
                self.last=time.monotonic();digest=hashlib.sha256(raw).hexdigest()
                self.root.mkdir(parents=True,exist_ok=True)
                atomic_write(self.root/(digest+'.raw'),raw)
                meta={'request':request,'time':now,'sha256':digest}
                atomic_write(self.root/(key+'-'+str(int(now*1000))+'.json'),json.dumps(meta).encode())
                atomic_write(latest,json.dumps(meta).encode())
            self.memo[key]=(now,raw,digest)
        return (raw if binary else json.loads(raw)),digest

    def espn_games(self,sport,day):
        payload,key=self.get('https://site.api.espn.com/apis/site/v2/sports/'+ESPN[sport]+'/scoreboard',{'dates':day.replace('-',''),'limit':1000})
        return espn_games(payload,key)

    def espn_box(self,sport,game):
        payload,key=self.get('https://site.api.espn.com/apis/site/v2/sports/'+ESPN[sport]+'/summary',{'event':game['id']})
        competitions=payload.get('header',{}).get('competitions',[])
        if str(payload.get('header',{}).get('id'))!=str(game['id']) or len(competitions)!=1:
            raise FeedUnavailable('ESPN summary identity mismatch')
        # Finality comes from this exact box response, not a stale scoreboard.
        current=espn_game(dict(id=game['id'],date=competitions[0].get('date',game['start_time']),
                              competitions=competitions,status=competitions[0]['status']),key)
        return current,espn_players(payload,sport)

    def games(self,sport,day,*,official=False):
        if not official or sport not in ('nhl','mlb'):return self.espn_games(sport,day)
        if sport=='nhl':
            # The score endpoint has nicknames only; schedule carries full team identities.
            value,key=self.get('https://api-web.nhle.com/v1/schedule/'+day)
            return [nhl_game(g,key) for d in value['gameWeek'] if d['date']==day for g in d['games']]
        value,key=self.get('https://statsapi.mlb.com/api/v1/schedule',{'sportId':1,'date':day})
        return [mlb_game(g,key) for d in value['dates'] for g in d['games']]

    def box(self,sport,game,*,official=False):
        if not official or sport not in ('nhl','mlb'):return self.espn_box(sport,game)
        if sport=='nhl':
            v,key=self.get(f"https://api-web.nhle.com/v1/gamecenter/{game['id']}/boxscore")
            if str(v['id'])!=str(game['id']):raise FeedUnavailable('NHL identity mismatch')
            current=dict(game,state=nhl_state(v['gameState']),raw_key=key)
            players=nhl_players(v)
            # Power-play points are derived from goal/assist identities in official plays.
            play,pkey=self.get(f"https://api-web.nhle.com/v1/gamecenter/{game['id']}/play-by-play")
            if str(play['id'])!=str(game['id']):raise FeedUnavailable('NHL play identity mismatch')
            if nhl_state(play.get('gameState'))!=current['state']:raise FeedUnavailable('NHL play finality mismatch')
            roster={str(r['playerId']):r for r in play.get('rosterSpots',[])}
            for p in players:
                r=roster.get(p['player_id'])
                if not r:raise FeedUnavailable('NHL full player identity unavailable')
                p['player_name']=r['firstName']['default']+' '+r['lastName']['default']
            # Blocked shots from the official blocked-shot play identity.
            blocked={p['player_id']:0 for p in players}
            for event in play.get('plays',[]):
                if event.get('typeDescKey')=='blocked-shot':
                    pid=str(event.get('details',{}).get('blockingPlayerId',''))
                    if pid in blocked:blocked[pid]+=1
            landing,lkey=self.get(f"https://api-web.nhle.com/v1/gamecenter/{game['id']}/landing")
            if str(landing['id'])!=str(game['id']):raise FeedUnavailable('NHL landing identity mismatch')
            if nhl_state(landing.get('gameState'))!=current['state']:raise FeedUnavailable('NHL landing finality mismatch')
            scoring=landing.get('summary',{}).get('scoring')
            if scoring is None:raise FeedUnavailable('NHL scoring inventory absent')
            goal_count=sum(len(period.get('goals',[])) for period in scoring if period.get('periodDescriptor',{}).get('periodType')!='SO')
            if goal_count!=sum(p.get('goals',0) for p in players):raise FeedUnavailable('NHL scoring inventory incomplete')
            pp={p['player_id']:0 for p in players}
            for period in scoring:
                if period.get('periodDescriptor',{}).get('periodType')=='SO':continue
                for goal in period.get('goals',[]):
                    if goal.get('strength')=='pp':
                        for pid in [str(goal.get('playerId',''))]+[str(a['playerId']) for a in goal.get('assists',[])]:
                            if pid in pp:pp[pid]+=1
            for p in players:
                p['blocked_shots']=blocked[p['player_id']]
                p['power_play_points']=pp[p['player_id']]
            current['raw_key']=key+':'+pkey+':'+lkey
            return current,players
        v,key=self.get(f"https://statsapi.mlb.com/api/v1.1/game/{game['id']}/feed/live")
        if str(v['gamePk'])!=str(game['id']):raise FeedUnavailable('MLB identity mismatch')
        current=dict(game,state=mlb_state(v['gameData']['status']),raw_key=key)
        return current,mlb_players(v)


def state(status):
    t=status.get('type',{})
    name=t.get('name','').lower()
    if 'postpon' in name:return 'postponed'
    if 'cancel' in name:return 'cancelled'
    if t.get('completed') is True and t.get('state')=='post':return 'final'
    return {'in':'live','pre':'upcoming'}.get(t.get('state'),'unmatched')


def espn_game(e,key=''):
    c=e['competitions'][0];teams={v['homeAway']:v for v in c['competitors']}
    if set(teams)!={'home','away'}:raise FeedUnavailable('ESPN teams ambiguous')
    status=e.get('status',c.get('status',{}))
    return dict(id=str(e['id']),provider='espn',raw_key=key,start_time=e['date'],game_date=eastern(e['date']),
                home_team=teams['home']['team']['displayName'],away_team=teams['away']['team']['displayName'],
                home_code=teams['home']['team'].get('abbreviation',''),away_code=teams['away']['team'].get('abbreviation',''),
                state=state(status),score=f"{teams['away'].get('score','')}–{teams['home'].get('score','')}",
                detail=status.get('type',{}).get('shortDetail',''))


def espn_games(payload,key=''):
    if len(payload.get('events',[]))>=1000:raise FeedUnavailable('ESPN scoreboard truncated')
    return [espn_game(e,key) for e in payload.get('events',[]) if len(e.get('competitions',[]))==1]


def espn_players(payload,sport):
    rows={}
    for team in payload.get('boxscore',{}).get('players',[]):
        categories={s.get('name') for s in team.get('statistics',[])}
        team_ids=set()
        for block in team.get('statistics',[]):
            labels=block.get('labels',[]);category=block.get('name')
            for a in block.get('athletes',[]):
                athlete=a.get('athlete',{});pid=str(athlete.get('id',''))
                if not pid or not athlete.get('displayName'):continue
                r=rows.setdefault(pid,dict(player_id=pid,player_name=athlete['displayName'],team_id=str(team['team']['id'])))
                if r['player_name']!=athlete['displayName'] or r['team_id']!=str(team['team']['id']):
                    raise FeedUnavailable('ESPN conflicting athlete identity')
                team_ids.add(pid)
                if a.get('didNotPlay') or athlete.get('scratched'):
                    r['participation']='no_minutes';continue
                values=a.get('stats',[])
                if len(values)!=len(labels):continue
                stats=dict(zip(labels,values))
                if sport in ('nba','wnba'):
                    if stats.get('MIN') == '--':r['participation']='no_minutes'
                    for label,field in {'PTS':'points','REB':'rebounds','AST':'assists','TO':'turnovers','STL':'steals','BLK':'blocks'}.items():r[field]=number(stats.get(label))
                    for label,field in {'FG':'field_goals','3PT':'threes','FT':'free_throws'}.items():
                        pair=str(stats.get(label,'')).split('-')
                        if len(pair)==2:r[field+'_made']=number(pair[0]);r[field+'_attempted']=number(pair[1])
                elif sport=='nfl':
                    mappings={'passing':{'YDS':'passing_yards','TD':'passing_tds','INT':'passing_interceptions'},
                              'rushing':{'YDS':'rushing_yards','TD':'rushing_tds','CAR':'carries'},
                              'receiving':{'YDS':'receiving_yards','TD':'receiving_tds','REC':'receptions'},
                              'defensive':{'SOLO':'def_tackles_solo','SACKS':'def_sacks'}}
                    for label,field in mappings.get(category,{}).items():r[field]=number(stats.get(label))
                    if category=='passing':
                        pair=str(stats.get('C/ATT','')).split('/')
                        if len(pair)==2:r['completions']=number(pair[0]);r['attempts']=number(pair[1])
                    if category=='defensive' and number(stats.get('TOT')) is not None and number(stats.get('SOLO')) is not None:
                        r['def_tackle_assists']=number(stats['TOT'])-number(stats['SOLO'])
                    if category=='kicking':
                        for label,field in [('FG','fg_made'),('XP','pat_made')]:r[field]=number(str(stats.get(label,'')).split('/')[0])
                elif sport=='nhl':
                    r['role']='goalie' if category=='goalies' else 'skater'
                    r['time_on_ice_seconds']=seconds(stats.get('TOI'))
                    for label,field in {'G':'goals','A':'assists','S':'shots_on_goal','SV':'total_saves','BS':'blocked_shots','PPP':'power_play_points'}.items():
                        if label in stats:r[field]=number(stats[label])
                elif sport=='mlb':
                    pitching='IP' in labels;prefix='pitching' if pitching else 'hitting'
                    r[prefix+'_present']=True;r[prefix+'_gamesPlayed']=1
                    mapping={'H':'hits','BB':'baseOnBalls','K':'strikeOuts','HR':'homeRuns','R':'runs','RBI':'rbi','ER':'earnedRuns','2B':'doubles','3B':'triples','SB':'stolenBases','TB':'totalBases'}
                    for label,field in mapping.items():
                        if label in stats:r[prefix+'_'+field]=number(stats[label])
                    if pitching:r['pitching_outs']=outs(stats.get('IP'))
        if sport=='nfl':
            # A player listed in one complete category has zero in absent categories
            # only when those category tables are present in this same team box.
            zero={'passing':['passing_yards','passing_tds','passing_interceptions','completions','attempts'],
                  'rushing':['rushing_yards','rushing_tds','carries'],'receiving':['receiving_yards','receiving_tds','receptions'],
                  'kicking':['fg_made','pat_made'],'defensive':['def_tackles_solo','def_tackle_assists','def_sacks']}
            for pid in team_ids:
                for cat,fields in zero.items():
                    if cat in categories:
                        for field in fields:rows[pid].setdefault(field,0.)
    if sport in ('mlb','nhl'):
        supplement_plays(rows,payload,sport)
    return list(rows.values())


def supplement_plays(rows,payload,sport):
    """Use recorded ESPN play identities only when they reconcile to the box.

    Extra-base hits need the full batting hit inventory to agree. Ambiguous steal
    movement stays missing rather than assigning a steal to every runner on base.
    """
    plays=payload.get('plays',[])
    if not isinstance(plays,list) or not plays:return
    if sport=='nhl':
        goals={pid:0 for pid in rows};pp={pid:0 for pid in rows};valid=True
        for p in plays:
            if p.get('type',{}).get('text')!='Goal':continue
            participants=p.get('participants',[])
            scorer=[str(a['athlete']['id']) for a in participants if a.get('type')=='scorer']
            if len(scorer)!=1 or scorer[0] not in goals:valid=False;continue
            goals[scorer[0]]+=1
            strength=p.get('strength',{}).get('text')
            if not strength:valid=False
            if strength=='Power Play':
                for a in participants:
                    pid=str(a['athlete']['id'])
                    if a.get('type') in ('scorer','assist','assister') and pid in pp:pp[pid]+=1
        if valid and all(goals[pid]==r.get('goals') for pid,r in rows.items() if r.get('role')=='skater'):
            for pid,r in rows.items():r['power_play_points']=pp[pid]
        return
    counts={pid:{'Single':0,'Double':0,'Triple':0,'Home Run':0} for pid in rows}
    seen=set();valid=True
    for p in plays:
        kind=p.get('type',{}).get('text')
        if kind not in ('Single','Double','Triple','Home Run'):continue
        hitters=[str(a['athlete']['id']) for a in p.get('participants',[]) if a.get('type')=='batter']
        atbat=p.get('atBatId')
        if len(hitters)!=1 or hitters[0] not in counts or not atbat or atbat in seen:valid=False;continue
        seen.add(atbat);counts[hitters[0]][kind]+=1
    complete=valid and all(sum(counts[pid].values())==r.get('hitting_hits') for pid,r in rows.items() if r.get('hitting_present'))
    if complete:
        for pid,r in rows.items():
            if r.get('hitting_present'):
                c=counts[pid];r['hitting_doubles']=c['Double'];r['hitting_triples']=c['Triple']
                r['hitting_totalBases']=c['Single']+2*c['Double']+3*c['Triple']+4*c['Home Run']
    steals={pid:0 for pid in rows};positions={};half=None;valid=True
    for p in plays:
        next_half=(p.get('period',{}).get('number'),p.get('period',{}).get('type'))
        if next_half!=half:positions={};half=next_half
        current={str(p[b]['athlete']['id']):i for i,b in enumerate(('onFirst','onSecond','onThird'),1) if isinstance(p.get(b),dict) and p[b].get('athlete',{}).get('id')}
        if p.get('type',{}).get('text')=='Stolen Base':
            moving=[pid for pid,base in current.items() if pid in positions and base>positions[pid]]
            if not moving:valid=False
            for pid in moving:
                if pid not in steals:valid=False
                else:steals[pid]+=1
        positions=current
    starts_at_first=plays[0].get('type',{}).get('text')=='Start Inning' and plays[0].get('period',{}).get('number')==1
    if valid and complete and starts_at_first:
        for pid,r in rows.items():
            if r.get('hitting_present'):r['hitting_stolenBases']=steals[pid]


def nhl_state(v):return 'final' if v in ('OFF','FINAL') else 'live' if v in ('LIVE','CRIT') else 'postponed' if v=='PPD' else 'upcoming'


def nhl_game(g,key=''):
    def name(t):
        if not all(t.get(k,{}).get('default') for k in ('placeName','commonName')):
            raise FeedUnavailable('NHL full team identity unavailable')
        return ' '.join(t[k]['default'] for k in ('placeName','commonName'))
    return dict(id=str(g['id']),provider='nhl',raw_key=key,start_time=g['startTimeUTC'],game_date=eastern(g['startTimeUTC']),
                home_team=name(g['homeTeam']),away_team=name(g['awayTeam']),state=nhl_state(g['gameState']))


def nhl_players(v):
    rows=[]
    for team in v['playerByGameStats'].values():
        for group,athletes in team.items():
            for a in athletes:
                name=a.get('name',{}).get('default','')
                # Box score name can be an initial; full roster identities are required.
                roster=next((r for r in v.get('rosterSpots',[]) if r['playerId']==a['playerId']),None)
                if roster:name=roster['firstName']['default']+' '+roster['lastName']['default']
                r=dict(player_id=str(a['playerId']),player_name=name,role='goalie' if group=='goalies' else 'skater',time_on_ice_seconds=seconds(a.get('toi')))
                for source,target in {'goals':'goals','assists':'assists','sog':'shots_on_goal','saves':'total_saves','blockedShots':'blocked_shots'}.items():
                    if source in a:r[target]=number(a[source])
                rows.append(r)
    return rows


def mlb_state(s):
    text=s.get('detailedState','').lower()
    if 'cancel' in text:return 'cancelled'
    if 'postpon' in text or 'suspend' in text:return 'postponed'
    return 'final' if s.get('abstractGameState')=='Final' else 'live' if s.get('abstractGameState')=='Live' else 'upcoming'


def mlb_game(g,key=''):
    return dict(id=str(g['gamePk']),provider='mlb',raw_key=key,start_time=g['gameDate'],game_date=eastern(g['gameDate']),
                home_team=g['teams']['home']['team']['name'],away_team=g['teams']['away']['team']['name'],state=mlb_state(g['status']))


def mlb_players(v):
    rows=[]
    for team in v['liveData']['boxscore']['teams'].values():
        for player in team['players'].values():
            p=player['person'];r=dict(player_id=str(p['id']),player_name=p['fullName'])
            for role,source in [('hitting','batting'),('pitching','pitching')]:
                stats=player.get('stats',{}).get(source,{})
                r[role+'_present']=bool(stats);r[role+'_gamesPlayed']=1 if stats else 0
                for k,val in stats.items():
                    if isinstance(val,(int,float)):r[role+'_'+k]=val
                if role=='pitching' and 'inningsPitched' in stats:r['pitching_outs']=outs(stats['inningsPitched'])
                if role=='hitting' and stats and all(k in stats for k in ('hits','doubles','triples','homeRuns')):
                    r['hitting_totalBases']=stats['hits']+stats['doubles']+2*stats['triples']+3*stats['homeRuns']
            rows.append(r)
    return rows


def nflverse_box(feed, game):
    """Weekly reconciliation only after that exact final game exists in both releases."""
    base='https://github.com/nflverse/nflverse-data/releases/download/'
    schedule,skey=feed.get(base+'schedules/games.parquet',ttl=21600,binary=True)
    frame=pd.read_parquet(io.BytesIO(schedule))
    codes={'WSH':'WAS','LA':'LA','LAR':'LA','JAC':'JAX'}
    h=codes.get(game.get('home_code'),game.get('home_code'));a=codes.get(game.get('away_code'),game.get('away_code'))
    rows=frame[(frame.home_team==h)&(frame.away_team==a)&frame.home_score.notna()&frame.away_score.notna()]
    rows=rows[(pd.to_datetime(rows.gameday)-pd.Timestamp(game['game_date'])).abs()<=pd.Timedelta(days=1)]
    if len(rows)!=1:return None
    match=rows.iloc[0]
    raw,key=feed.get(base+f'stats_player/stats_player_week_{int(match.season)}.parquet',ttl=21600,binary=True)
    stats=pd.read_parquet(io.BytesIO(raw))
    kind='REG' if match.game_type=='REG' else 'POST'
    stats=stats[(stats.season==int(match.season))&(stats.week==int(match.week))&(stats.season_type==kind)&stats.team.isin([h,a])]
    if stats.empty:return None
    records=[]
    for r in stats.to_dict('records'):
        if pd.isna(r.get('player_id')) or pd.isna(r.get('player_display_name')):continue
        r.update(player_id=str(r['player_id']),player_name=r['player_display_name'])
        records.append(r)
    return dict(game,provider='nflverse',id=str(match.game_id),raw_key=key+':'+skey),records
