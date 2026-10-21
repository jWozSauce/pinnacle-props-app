"""Hosted refresh must never render Streamlit from odds pool workers."""
from pathlib import Path
import threading
import pandas as pd
from streamlit.testing.v1 import AppTest
from pinnacle_props.request_pool import Progress,Pool
from pinnacle_props.pipeline import run_live,options
from pinnacle_props.settings import load_config
from pinnacle_props.kelly import Profile
from test_request_pool import Transport


def test_progress_queue_drains_only_on_owner_thread():
    owner=threading.get_ident();seen=[]
    p=Progress(lambda message:seen.append((message,threading.get_ident())))
    def worker(n):p(str(n));p.drain()
    Pool(3).map(range(3),worker)
    assert seen==[]
    p.drain()
    assert sorted(m for m,t in seen)==['0','1','2'] and all(t==owner for m,t in seen)


def fixture_preview(config):
    settings=options(config,'nba',books=['draftkings'],markets=['player_points'])
    return dict(settings=settings,bound=3,events=[{'id':f'g{i}','commence_time':'2099-01-01T00:00:00Z'} for i in range(3)])


def test_cli_progress_preflight_and_workers(tmp_path,monkeypatch):
    monkeypatch.setenv('PP_CONSENSUS_ENABLED','0')
    owner=threading.get_ident();seen=[];transport=Transport()
    m,_,_=run_live(fixture_preview(load_config()),data_dir=tmp_path,profile=Profile(5000,.25,.04),
        key='test-only',session=transport,paper_track=False,progress=lambda message:seen.append((message,threading.get_ident())))
    assert all(t==owner for _,t in seen) and any('Account remaining' in m for m,_ in seen)
    assert len([c for c in transport.calls if c[0]=='v4'])==1
    assert transport.maximum>1 and m['credits_charged']==3
    assert seen[-1][0]=='Captured 3 of 3 games'


def test_app_refresh_through_real_pool_updates_caption(tmp_path,monkeypatch):
    from pinnacle_props import pipeline,rules
    from streamlit.delta_generator import DeltaGenerator
    monkeypatch.setenv('PP_CONSENSUS_ENABLED','0')
    monkeypatch.setenv('PP_SETTLEMENT_ENABLED','0')
    monkeypatch.setattr('pinnacle_props.settings.ROOT',tmp_path)
    monkeypatch.setattr(rules,'LIVE_PATH',tmp_path/'rules.json')
    monkeypatch.setattr('pinnacle_props.odds_api.resolve_key',lambda **kw:'test-only')
    profile=Profile(5000,.25,.04)
    monkeypatch.setattr('pinnacle_props.kelly_sheet.fetch_profiles',lambda **kw:{'Player_Props':profile})
    monkeypatch.setattr(pipeline,'discover',lambda settings,**kw:dict(fixture_preview(load_config()),settings=settings))
    transport=Transport();real_run=run_live
    def run(preview,**kw):return real_run(preview,session=transport,concurrency=3,**kw)
    monkeypatch.setattr(pipeline,'run_live',run)
    def no_replay(*a,**kw):raise FileNotFoundError('no snapshot')
    monkeypatch.setattr(pipeline,'replay',no_replay)
    seen=[];original=DeltaGenerator.caption
    def caption(self,message,*a,**kw):
        if str(message).startswith(('Preflight:','Account remaining:','Captured ')):
            from streamlit.runtime.scriptrunner import get_script_run_ctx
            assert get_script_run_ctx(suppress_warning=True) is not None
            seen.append(str(message))
        return original(self,message,*a,**kw)
    monkeypatch.setattr(DeltaGenerator,'caption',caption)
    app=AppTest.from_file(Path('app.py').resolve(),default_timeout=20)
    app.query_params['sport']='nba'
    app.session_state['profiles']={'Player_Props':profile}
    app.run()
    app.multiselect(key='books_nba').set_value(['draftkings']).run()
    app.multiselect(key='markets_nba').set_value(['player_points']).run()
    app.button(key='estimate').click().run()
    assert not app.exception and not app.button(key='refresh').disabled
    app.button(key='refresh').click().run()
    assert not app.exception and not app.error
    assert any(m.startswith('Account remaining:') for m in seen) and 'Captured 3 of 3 games' in seen
    assert len(transport.paid)==3 and transport.maximum>1
    assert app.session_state['scan_nba'][0]['credits_charged']==3
