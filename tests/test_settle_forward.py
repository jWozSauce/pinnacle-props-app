from copy import deepcopy
import json
from pathlib import Path
import pandas as pd
import pytest
from pinnacle_props.settle import settle_row, due, match_game, load_rules
from pinnacle_props.live_money import card_money, live_row, performance, breakdown
from pinnacle_props.settlement_sources import espn_players, espn_game, mlb_players, outs, FreeFeed, FeedUnavailable

FIX=Path(__file__).parent/'fixtures'/'settlement'


def bet(**kwargs):
    row=dict(sport='nba',player='A Player',market='player_points',line=21.5,side='over',price_decimal=2.5,stake=20,
             status='pending',profit='',stat='',graded_at='',regraded_at='',source='',note='User note',game_date='2026-10-09',
             start_time='2026-10-09T23:00:00Z',home_team='Boston Celtics',away_team='Houston Rockets',scan_id='s',row=1)
    row.update(kwargs);return row


def game(**kwargs):
    row=dict(id='123',provider='espn',state='final',game_date='2026-10-09',start_time='2026-10-09T23:00:00Z',home_team='Boston Celtics',away_team='Houston Rockets')
    row.update(kwargs);return row

@pytest.mark.parametrize('sport,market,fields,line,side,status,stat',[
 ('nba','player_points_rebounds_assists',dict(points=20,rebounds=7,assists=5),31.5,'over','won',32),
 ('wnba','player_points',dict(points=18),18,'under','push',18),
 ('nfl','player_kicking_points',dict(fg_made=2,pat_made=3),9.5,'over','lost',9),
 ('nfl','player_rush_reception_yds',dict(rushing_yards=60,receiving_yards=15),74.5,'over','won',75),
 ('nhl','player_total_saves',dict(role='goalie',time_on_ice_seconds=3600,total_saves=31),30.5,'over','won',31),
 ('nhl','player_points',dict(role='skater',time_on_ice_seconds=900,goals=1,assists=2),3,'under','push',3),
 ('mlb','pitcher_outs',dict(pitching_present=True,pitching_gamesPlayed=1,pitching_outs=17),16.5,'under','lost',17),
 ('mlb','batter_singles',dict(hitting_present=True,hitting_gamesPlayed=1,hitting_hits=4,hitting_doubles=1,hitting_triples=0,hitting_homeRuns=1),1.5,'over','won',2),
])
def test_hand_grades(sport,market,fields,line,side,status,stat):
    row=bet(sport=sport,market=market,line=line,side=side)
    player=dict(player_id='p',player_name='A Player',**fields)
    before=deepcopy((row,player));result,reason=settle_row(row,game(),[player],now='2026-10-10T05:00:00Z')
    assert result['status']==status and result['stat']==stat
    assert result['profit']=={'won':30,'lost':-20,'push':0}[status]
    assert (row,player)==before

@pytest.mark.parametrize('sport,market,extra',[
 ('nba','player_points',{'participation':'no_minutes'}),('wnba','player_points',{'participation':'no_minutes'}),
 ('nfl','player_pass_yds',{'participation':'no_minutes'}),('nhl','player_total_saves',{'role':'goalie','time_on_ice_seconds':0}),
 ('mlb','pitcher_outs',{'pitching_present':False,'pitching_gamesPlayed':0})])
def test_void(sport,market,extra):
    r,_=settle_row(bet(sport=sport,market=market),game(),[dict(player_id='p',player_name='A Player',**extra)])
    assert r['status']=='void' and r['profit']==0


def test_missing_and_ambiguous_never_void():
    row=bet();p=dict(player_id='1',player_name='A Player')
    for players in ([],[p],[p,dict(p,player_id='2')]):
        result,reason=settle_row(row,game(),players)
        assert result==row and reason
    assert match_game(row,[game(id='1'),game(id='2')])[0] is None
    assert match_game(row,[game(home_team='Houston Rockets',away_team='Boston Celtics')])[0]
    assert match_game(row,[game(game_date='2026-10-11')])[0] is None
    assert match_game(row,[game(),game(id='2',start_time='2026-10-10T02:00:00Z')])[0]['id']=='123'


def test_postponement_finality_and_correction():
    r=bet();p=dict(player_id='1',player_name='A Player',points=22)
    assert settle_row(r,game(state='live'),[p])[0]==r
    assert settle_row(r,game(state='postponed'),[],now='2026-10-10T23:00:00Z')[0]==r
    assert settle_row(r,game(state='postponed'),[],now='2026-10-17T23:00:00Z')[0]['status']=='void'
    graded,_=settle_row(r,game(),[p],now='2026-10-10T05:00:00Z')
    assert not due(graded,'2026-10-11T04:59:59Z') and due(graded,'2026-10-11T05:00:00Z')
    corrected,_=settle_row(graded,game(),[dict(p,points=21)],now='2026-10-11T05:01:00Z')
    assert corrected['status']=='lost' and corrected['profit']==-20 and 'Prior grade:' in corrected['note']
    assert corrected['graded_at']==graded['graded_at'] and corrected['regraded_at']
    assert not due(corrected,'2026-10-12T06:00:00Z')


def test_ncaaf_mixed_card_decimal():
    stakes=[50,30,20,55,20,40,100,100,25,50,60,30,45]
    americans=[150,-110,120,-110,150,-200,-110,200,120,100,-120,-110,110]
    statuses=['won','lost','push']+['pending']*10
    stages=['final']*7+['live','upcoming','unmatched','live','live','waiting']
    verdicts=['❌ lost','✅ won','✅ won','✅ won','✅ won','❌ lost','🟨 push','🟢 covering','⏳ upcoming','— no live feed','🔴 behind','🟦 on the line','⏳ waiting']
    card=pd.DataFrame(dict(stake=stakes,price_decimal=[1+a/100 if a>0 else 1+100/-a for a in americans],status=statuses,stage=stages,verdict=verdicts,profit=[80,-30,0]+[0]*10))
    original=card.copy(deep=True);out,official,projected=card_money(card)
    assert official==50 and projected==230
    assert out.to_win.tolist()==[75,27.27,24,50,30,20,90.91,200,30,50,50,27.27,49.5]
    assert out.proj_contrib.tolist()==[0,0,0,50,30,-40,0,200,0,0,-60,0,0]
    pd.testing.assert_frame_equal(card,original)

@pytest.mark.parametrize('sport',['nba','wnba','nfl','nhl','mlb'])
def test_recorded_box_parsers(sport):
    payload=json.loads((FIX/(sport+'_espn.json')).read_text());players=espn_players(payload,sport)
    assert len(players)>10 and all(p['player_id'] and p['player_name'] for p in players)
    c=payload['header']['competitions'][0]
    g=espn_game(dict(id=payload['header']['id'],date=c['date'],competitions=[c],status=c['status']))
    assert g['state']=='final'


def test_live_stages_from_recorded_box_with_explicit_synthetic_states():
    payload=json.loads((FIX/'nba_espn.json').read_text());players=espn_players(payload,'nba');p=next(p for p in players if p.get('points',0)>0)
    b=bet(player=p['player_name'],line=p['points']-.5)
    for state,verdict in [('upcoming','⏳ upcoming'),('live','🟢 covering'),('final','✅ won')]:
        assert live_row(b,game(state=state),players)['verdict']==verdict
    assert live_row(b,None,[])['stage']=='unmatched'
    assert live_row(b,game(state='live'),[])['stage']=='waiting'
    assert live_row(dict(b,line=p['points']),game(state='live'),players)['verdict']=='🟦 on the line'
    assert live_row(dict(b,line=p['points']+1),game(state='live'),players)['verdict']=='🔴 behind'
    assert live_row(dict(b,line=p['points']),game(),players)['verdict']=='🟨 push'
    assert live_row(dict(b,line=p['points']+1),game(),players)['verdict']=='❌ lost'
    assert live_row(b,game(),[dict(p,participation='no_minutes')])['verdict']=='⚪ void'


def test_recorded_hand_checks():
    nfl=espn_players(json.loads((FIX/'nfl_espn.json').read_text()),'nfl')
    j=next(p for p in nfl if p['player_name']=='Daniel Jones');assert j['completions']==19 and j['attempts']==34 and j['passing_yards']==143
    t=next(p for p in nfl if p['player_name']=='Jonathan Taylor');assert t['rushing_yards']==95 and t['rushing_tds']==2
    k=next(p for p in nfl if p['player_name']=='Spencer Shrader');assert 3*k['fg_made']+k['pat_made']==12
    h=espn_players(json.loads((FIX/'nhl_espn.json').read_text()),'nhl')
    p=next(p for p in h if p['player_name']=='Philipp Grubauer');assert p['total_saves']==37
    p=next(p for p in h if p['player_name']=='Matty Beniers');assert p['shots_on_goal']==2
    m=espn_players(json.loads((FIX/'mlb_espn.json').read_text()),'mlb')
    p=next(p for p in m if p['player_name']=='Parker Messick');assert p['pitching_outs']==7
    official=mlb_players(json.loads((FIX/'mlb_official.json').read_text()));assert official
    assert outs('5.2')==17 and outs('5.3') is None


def test_feed_cache_and_paid_host_rejection(tmp_path):
    calls=[];clock=[100.]
    feed=FreeFeed(tmp_path,clock=lambda:clock[0],fetch=lambda u:calls.append(u) or b'{"events":[]}')
    assert feed.espn_games('nba','2026-10-10')==[]
    feed.espn_games('nba','2026-10-10');assert len(calls)==1
    clock[0]+=61;feed.espn_games('nba','2026-10-10');assert len(calls)==2
    with pytest.raises(FeedUnavailable):feed.get('https://api.the-odds-api.com/v4/sports')
    assert len(calls)==2


def test_roi_and_edges():
    f=pd.DataFrame([dict(status=s,stake=10,profit=p,edge=e) for s,p,e in [('won',10,.02),('lost',-10,.03),('push',0,.04),('void',0,.06),('pending','',.01)]])
    assert performance(f)['roi']==0
    assert set(breakdown(f,'edge_bucket').edge_bucket)=={'<2','2–<3','3–<4','4–<6','≥6'}


def test_zero_minutes_is_not_dnp_and_extra_base_hits_match_recorded_plays():
    payload=json.loads((FIX/'nba_espn.json').read_text());block=payload['boxscore']['players'][0]['statistics'][0]
    block['athletes'][0]['stats'][block['labels'].index('MIN')]='0'
    p=espn_players(payload,'nba')[0]
    assert p.get('participation')!='no_minutes'
    baseball=espn_players(json.loads((FIX/'mlb_espn.json').read_text()),'mlb')
    # Recorded CLE–CHW game: Meidroth and Bazzana each doubled once.
    p=next(p for p in baseball if p['player_name']=='Chase Meidroth');assert p['hitting_doubles']==1
    p=next(p for p in baseball if p['player_name']=='Travis Bazzana');assert p['hitting_doubles']==1


def test_ambiguous_game_does_not_select_first_and_failed_feed_not_retried(tmp_path):
    row=bet(start_time='2026-10-09T22:00:00Z')
    assert match_game(row,[game(),game(id='b')])[0] is None
    calls=[]
    def fail(url):calls.append(url);raise OSError('do not expose provider error')
    feed=FreeFeed(tmp_path,fetch=fail)
    for day in ('2026-10-09','2026-10-10'):
        with pytest.raises(FeedUnavailable):feed.espn_games('nba',day)
    assert len(calls)==1


def test_real_nhl_official_schedule_and_complete_box(tmp_path):
    from pinnacle_props.settlement_sources import FreeFeed,FeedUnavailable
    from pinnacle_props.settle import match_game
    payload=json.loads((Path('tests/fixtures/settlement')/'nhl_official.json').read_text())
    def fetch(url):
        kind='schedule' if '/schedule/' in url else url.rsplit('/',1)[-1]
        return json.dumps(payload[kind]).encode()
    feed=FreeFeed(tmp_path,fetch=fetch)
    games=feed.games('nhl','2026-10-08',official=True)
    g,players=feed.box('nhl',games[0],official=True)
    assert g['home_team']=='Boston Bruins' and g['away_team']=='Utah Mammoth'
    assert match_game(dict(game_date='2026-10-08',home_team='Boston Bruins',away_team='Utah Mammoth'),games)[0]['id']=='2026020056'
    byname={p['player_name']:p for p in players}
    assert byname['David Pastrnak']['goals']==2
    assert byname['David Pastrnak']['assists']==2
    assert byname['David Pastrnak']['power_play_points']==3
    assert byname['Jeremy Swayman']['total_saves']==36
    assert byname['Will Borgen']['blocked_shots']==4
    assert byname['Clayton Keller']['shots_on_goal']==4
    assert len(players)==40
    payload['landing']['summary']['scoring']=[]
    with pytest.raises(FeedUnavailable,match='inventory incomplete'):
        FreeFeed(tmp_path/'partial',fetch=fetch).box('nhl',games[0],official=True)
