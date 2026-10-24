from datetime import datetime,timedelta,timezone
from pathlib import Path
import pytest
from streamlit.testing.v1 import AppTest
from pinnacle_props import pipeline, rules, book_catalog
from pinnacle_props.settings import load_config
from pinnacle_props.kelly import Profile
from tests.test_api import Session,Response,payload

NOW=datetime(2026,10,31,16,tzinfo=timezone.utc) # Spans the Eastern DST fall-back.


def listing():
    return [dict(id=name,commence_time=(NOW+timedelta(hours=hours)).isoformat(),
                 home_team='Home',away_team='Away')
            for name,hours in [('near1',2),('near2',20),('later',60*24)]]


def discover(settings,tmp_path,events=None):
    return pipeline.discover(settings,data_dir=tmp_path,key='fixture',
                             session=Session(Response(events or listing())),clock=lambda:NOW)


def price_session(events):
    # Route by event identity: parallel workers must not consume an ordered queue.
    responses={}
    for event in events:
        body=payload();body.update(event);responses[event['id']]=Response(body,last=1)
    class Prices:
        def __init__(self):self.calls=[]
        def get(self,url,**kwargs):
            self.calls.append((url,kwargs))
            if url.endswith('/v4/sports'):return Response()
            return responses[url.split('/events/')[1].split('/')[0]]
    return Prices()


@pytest.mark.parametrize('sport',['nba','nhl','nfl','mlb','wnba'])
def test_all_sports_window_estimate_override_and_eastern_dates(tmp_path,sport):
    config=load_config();config['sports'][sport]['scan_window_hours']=72;market=config['sports'][sport]['markets'][0]
    settings=pipeline.options(config,sport,books=['draftkings'],markets=[market])
    p=discover(settings,tmp_path)
    assert [e['id'] for e in p['events']]==['near1','near2'] and p['bound']==2
    assert [e['id'] for e in p['later_events']]==['later']
    assert datetime.fromisoformat(p['window_end'])-NOW==timedelta(hours=72)
    extra=dict(id='boundary',commence_time=(NOW+timedelta(hours=72)).isoformat())
    outside=dict(id='outside',commence_time=(NOW+timedelta(hours=72,seconds=1)).isoformat())
    assert [e['id'] for e in discover(settings,tmp_path,[extra,outside])['events']]==['boundary']
    config['sports'][sport]['scan_window_hours']=168
    extended=pipeline.options(config,sport,books=['draftkings'],markets=[market])
    assert len(discover(extended,tmp_path,[extra,outside])['events'])==2
    narrow=pipeline.options(config,sport,books=['draftkings'],markets=[market],dates=['2026-10-31','2026-10-31'])
    assert [e['id'] for e in discover(narrow,tmp_path)['events']]==['near1']
    distant_date=(NOW+timedelta(days=60)).date().isoformat()
    settings=pipeline.options(config,sport,dates=[distant_date,distant_date])
    assert not discover(settings,tmp_path)['events']
    settings=pipeline.options(config,sport,dates=[distant_date,distant_date],include_beyond_window=True)
    assert [e['id'] for e in discover(settings,tmp_path)['events']]==['later']


@pytest.mark.parametrize('hours',[0,-1,True,float('inf'),float('nan'),'72'])
def test_invalid_config_window_fails(hours):
    config=load_config();config['scan_window_hours']=hours
    with pytest.raises(ValueError,match='scan_window_hours'):pipeline.options(config,'nba')


def test_app_estimate_requests_and_ephemeral_opt_in(tmp_path,monkeypatch):
    from pinnacle_props import game_time
    profile=Profile(5000,.25,.04)
    monkeypatch.setattr('pinnacle_props.settings.ROOT',tmp_path)
    monkeypatch.setattr('pinnacle_props.odds_api.resolve_key',lambda **kw:'fixture')
    monkeypatch.setattr('pinnacle_props.kelly_sheet.fetch_profiles',lambda:{'Player_Props':profile})
    monkeypatch.setattr(game_time,'utcnow',lambda:NOW)
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:pytest.fail('Real network forbidden'))
    real_discover=pipeline.discover
    def free(settings,**kw):
        return real_discover(settings,data_dir=tmp_path,key='fixture',session=Session(Response(listing())),clock=lambda:NOW)
    monkeypatch.setattr(pipeline,'discover',free)
    requested=[];real_run=pipeline.run_live
    def run(preview,**kw):
        session=price_session(preview['events'])
        result=real_run(preview,data_dir=tmp_path,profile=profile,key='fixture',session=session,progress=lambda _:None,paper_track=False)
        requested.append(sorted(url.split('/events/')[1].split('/')[0] for url,params in session.calls if '/odds' in url))
        return result
    monkeypatch.setattr(pipeline,'run_live',run)
    app=AppTest.from_file(Path('app.py').resolve(),default_timeout=20)
    app.session_state['profiles']={'Player_Props':profile}
    app.query_params['sport']='nba';app.run()
    assert not app.exception, [e.message for e in app.exception]
    app.multiselect(key='books_nba').set_value(['draftkings']).run()
    app.multiselect(key='markets_nba').set_value(['player_points']).run()
    assert not app.checkbox(key='include_beyond_window').value
    app.button(key='estimate').click().run()
    assert any('2 games in the next 72 h (1 more listed later, not scanned)' in i.value for i in app.info)
    app.button(key='refresh').click().run()
    assert not app.exception and requested[-1]==['near1','near2']
    app.checkbox(key='include_beyond_window').check().run()
    app.button(key='estimate').click().run()
    assert any('3 games (including games beyond 72 h)' in i.value for i in app.info)
    app.button(key='refresh').click().run()
    assert not app.exception and requested[-1]==['later','near1','near2'], [e.value for e in app.error]
    assert not app.checkbox(key='include_beyond_window').value
    assert 'include_beyond_window' not in app.query_params
    app.checkbox(key='include_beyond_window').check().run()
    app.selectbox(key='sport').select('nhl').run()
    assert not app.checkbox(key='include_beyond_window').value
    fresh=AppTest.from_file(Path('app.py').resolve(),default_timeout=20)
    fresh.query_params.update(app.query_params);fresh.run()
    assert not fresh.checkbox(key='include_beyond_window').value


def test_paper_tracking_receives_only_window_scan(tmp_path,monkeypatch):
    import json
    from pinnacle_props import game_time
    monkeypatch.setattr(game_time,'utcnow',lambda:NOW)
    monkeypatch.setattr('pinnacle_props.bet_store.enabled',lambda:True)
    captured=[]
    def track(scan_id,**kwargs):
        manifest=json.loads((tmp_path/'snapshots/nba'/(scan_id+'.json')).read_text())
        captured.extend(e['event_id'] for e in manifest['event_results'])
        return {'message':'synthetic paper tracking only'}
    monkeypatch.setattr('pinnacle_props.paper.track_scan',track)
    settings=pipeline.options(load_config(),'nba',books=['draftkings'],markets=['player_points'])
    preview=discover(settings,tmp_path)
    pipeline.run_live(preview,data_dir=tmp_path,profile=Profile(5000,.25,.04),key='fixture',
                      session=price_session(preview['events']),progress=lambda _:None,paper_track=True)
    assert captured==['near1','near2']


def test_accepted_defaults():
    config=load_config()
    for sport in config['sports']:
        assert pipeline.options(config,sport)['horizon']['hours']==(168 if sport=='nfl' else 72)
