from dataclasses import asdict
import gzip
import json
from pathlib import Path
import pandas as pd
import pytest
from pinnacle_props.kelly import Profile
from pinnacle_props.kelly_sheet import parse_profiles
from pinnacle_props.odds_api import Result, APIError
from pinnacle_props.pipeline import _save_snapshot, replay, run_live, options, scan_lock
from pinnacle_props.settings import load_config
from test_api import Session, Response, payload

PROFILE=Profile(5000,.25,.04)


def sample_result(tmp_path):
    p=payload()
    p['commence_time']='2099-01-01T00:00:00Z'
    for book in p['bookmakers']:
        if book['key']=='draftkings':book['markets'][0]['outcomes'][0]['price']=2.2
    path=tmp_path/'raw'/'fixture.json.gz';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps({'status':200,'request_id':'fixture','body':json.dumps(p)}).encode()))
    return Result(p,200,{},str(path),'fixture')


def settings():
    return options(load_config(),'nba',books=['draftkings'],markets=['player_points'])


def test_snapshot_replay_is_offline_and_input_frozen(tmp_path,monkeypatch):
    result=sample_result(tmp_path)
    expected,flagged,excluded=_save_snapshot(tmp_path,'fixture',settings(),PROFILE,'Player_Props',
                                           '2026-10-07T19:00:00Z',[result],[{'event_id':'game-1','status':200}],1,1)
    assert len(flagged)==1 and excluded.empty
    monkeypatch.setattr('pinnacle_props.pipeline.fetch_profiles',lambda:pytest.fail('Replay read the live sheet'))
    monkeypatch.setattr('pinnacle_props.odds_api.resolve_key',lambda:pytest.fail('Replay resolved a key'))
    got,flagged2,excluded2=replay('nba','fixture',data_dir=tmp_path)
    pd.testing.assert_frame_equal(flagged,flagged2)
    assert got['profile']==asdict(PROFILE) and got['credits_charged']==1
    assert replay('nba',data_dir=tmp_path)[0]['scan_id']=='fixture'
    with pytest.raises(FileExistsError):
        _save_snapshot(tmp_path,'fixture',settings(),PROFILE,'Player_Props','2026-10-07T19:00:00Z',[result],[],1,1)
    Path(result.cache_path).write_bytes(b'tampered')
    with pytest.raises(ValueError,match='digest'):replay('nba','fixture',data_dir=tmp_path)


def test_live_pipeline_and_replay_share_exact_calculation(tmp_path):
    result=sample_result(tmp_path)
    preview={'events':[{'id':'game-1','commence_time':'2099-01-01T00:00:00Z'}],'bound':1,'settings':settings()}
    session=Session(Response(),Response(result.payload,last=1))
    manifest,flagged,excluded=run_live(preview,data_dir=tmp_path,profile=PROFILE,key='test-key',session=session,progress=lambda _:None)
    assert manifest['credits_charged']==1 and manifest['candidate_pairs']==1
    assert manifest['event_results'][0]['status']==200
    _,again,_=replay('nba',manifest['scan_id'],data_dir=tmp_path)
    pd.testing.assert_frame_equal(flagged,again)
    raw=pd.read_parquet(tmp_path/'snapshots'/'nba'/(manifest['scan_id']+'.parquet'))
    assert len(raw)==4 and raw.scan_id.nunique()==1


def test_excluded_mismatches_retained_in_raw_snapshot(tmp_path):
    result=sample_result(tmp_path)
    for outcome in result.payload['bookmakers'][1]['markets'][0]['outcomes']:outcome['point']=25.5
    m,flags,excluded=_save_snapshot(tmp_path,'mismatch',settings(),PROFILE,'Player_Props','2026-10-07T19:00:00Z',[result],[],1,1)
    raw=pd.read_parquet(tmp_path/'snapshots'/'nba'/'mismatch.parquet')
    assert flags.empty and len(excluded)==1
    assert raw.loc[raw.book=='draftkings','exclusion_reason'].tolist()==['line mismatch']*2
    assert m['candidate_pairs']==m['flagged_pairs']+sum(m['exclusions'].values())


def test_profile_validation_and_duplicate_names():
    text='Sport,kelly_fraction,bankroll2,edge_limit,unused\nPlayer_Props,0.25,5000,0.04,foo\n'
    assert parse_profiles(text)['Player_Props']==PROFILE
    with pytest.raises(ValueError,match='Duplicate'):parse_profiles(text+'Player_Props,.5,1000,.1,bar\n')
    with pytest.raises(ValueError):parse_profiles(text.replace('5000','-1'))
    with pytest.raises(ValueError):parse_profiles('<html>error</html>')


def test_scan_lock_and_options_validation(tmp_path):
    with scan_lock(tmp_path):
        with pytest.raises(APIError):
            with scan_lock(tmp_path):pass
    with pytest.raises(ValueError):options(load_config(),'nba',books=['unknown'])
    with pytest.raises(ValueError):options(load_config(),'nba',markets=['unconfigured'])


def test_formatted_sheet_bankroll_preserves_source_value():
    csv='Sport,kelly_fraction,bankroll2,edge_limit\nPlayer_Props,.25,"5,000",.04\n'
    assert parse_profiles(csv)['Player_Props']==PROFILE
    with pytest.raises(ValueError,match='separators'):
        parse_profiles(csv.replace('5,000','50,00'))


def test_account_floor_blocks_paid_request_before_send(tmp_path):
    from pinnacle_props.odds_api import BudgetStop
    preview={'events':[{'id':'game-1','commence_time':'2099-01-01T00:00:00Z'}],'bound':1,'settings':settings()}
    session=Session(Response(remaining=100000))
    with pytest.raises(BudgetStop,match='account floor'):
        run_live(preview,data_dir=tmp_path,profile=PROFILE,key='test-key',
                 session=session,progress=lambda _:None,project_cap=0)
    # Only the free quota preflight consumed the one mocked response.
    assert not session.results
    assert len(session.calls)==1 and session.calls[0][0].endswith("/v4/sports")
    assert not (tmp_path/'odds_budget.json').exists()


def test_discovery_all_upcoming_and_optional_eastern_dates(tmp_path):
    from datetime import datetime,timezone
    from pinnacle_props.pipeline import discover
    now=lambda:datetime(2026,10,8,3,tzinfo=timezone.utc) # Wednesday 11 PM Eastern
    events=[dict(id=ident,commence_time=start) for ident,start in [
        ('past','2026-10-08T02:00:00Z'),('thursday','2026-10-09T00:15:00Z'),
        ('sunday','2026-10-11T17:00:00Z'),('far','2026-11-20T18:00:00Z')]]
    def get(selected):
        return discover(selected,data_dir=tmp_path,session=Session(Response(events)),key='test-key',clock=now)
    setting=options(load_config(),'nfl',books=['draftkings'],markets=['player_pass_yds'])
    all_games=get(setting)
    assert [e['id'] for e in all_games['events']]==['thursday','sunday','far']
    assert all_games['bound']==3 and setting['horizon']=={'mode':'all_listed','dates':None}
    setting=options(load_config(),'nfl',books=['draftkings'],markets=['player_pass_yds'],dates=['2026-10-08','2026-10-08'])
    assert [e['id'] for e in get(setting)['events']]==['thursday']
    setting['include_live']=True;setting['horizon']['dates']=None
    assert len(get(setting)['events'])==4
    with pytest.raises(ValueError):options(load_config(),'nfl',dates=['2026-10-11','2026-10-08'])
    with pytest.raises(ValueError):options(load_config(),'nfl',dates=['2026-10-11'])


def test_selected_horizon_survives_snapshot_and_offline_replay(tmp_path):
    result=sample_result(tmp_path);selected=settings()
    selected['horizon']={'mode':'date_range','dates':['2098-12-31','2099-01-01']}
    manifest,_,_=_save_snapshot(tmp_path,'horizon',selected,PROFILE,'Player_Props','2026-10-07T19:00:00Z',[result],[],1,1)
    assert replay('nba','horizon',data_dir=tmp_path)[0]['settings']['horizon']==selected['horizon']


def test_delayed_preview_skips_started_and_missing_time_before_http(tmp_path,monkeypatch):
    from datetime import datetime,timezone
    from pinnacle_props import game_time
    monkeypatch.setattr(game_time,'utcnow',lambda:datetime(2026,10,21,2,tzinfo=timezone.utc))
    preview={'events':[{'id':'past','commence_time':'2026-10-21T02:00:00Z'},
                       {'id':'unknown'}],'bound':2,'settings':settings()}
    session=Session()
    manifest,flagged,_=run_live(preview,data_dir=tmp_path,profile=PROFILE,key='fixture',session=session,progress=lambda _:None,paper_track=False)
    assert not session.calls and flagged.empty and manifest['credits_charged']==0
    assert [r['status'] for r in manifest['event_results']]==['skipped: game started','skipped: start time unavailable']


def test_explicit_include_live_bypasses_execution_guard(tmp_path):
    result=sample_result(tmp_path);s=settings();s['include_live']=True
    preview={'events':[{'id':'game-1','commence_time':'2020-01-01T00:00:00Z'}],'bound':1,'settings':s}
    session=Session(Response(),Response(result.payload,last=1))
    manifest,_,_=run_live(preview,data_dir=tmp_path,profile=PROFILE,key='fixture',session=session,progress=lambda _:None,paper_track=False)
    assert len(session.calls)==2 and manifest['credits_charged']==1


def test_game_start_during_free_preflight_blocks_paid_transport(tmp_path,monkeypatch):
    from datetime import datetime,timezone
    from pinnacle_props import game_time
    from pinnacle_props.odds_api import BudgetStop
    before=datetime(2026,10,21,16,tzinfo=timezone.utc)
    monkeypatch.setattr(game_time,'utcnow',lambda:before)
    monkeypatch.setattr('pinnacle_props.odds_api.utcnow',lambda:before.isoformat())
    base=Session(Response())
    class AdvancingSession:
        def get(self,*args,**kwargs):
            response=base.get(*args,**kwargs)
            monkeypatch.setattr('pinnacle_props.odds_api.utcnow',lambda:'2026-10-21T16:00:01+00:00')
            return response
    preview={'events':[{'id':'game-1','commence_time':'2026-10-21T16:00:01Z'}],'bound':1,'settings':settings()}
    with pytest.raises(BudgetStop,match='start reached'):
        run_live(preview,data_dir=tmp_path,profile=PROFILE,key='fixture',session=AdvancingSession(),progress=lambda _:None,paper_track=False)
    assert len(base.calls)==1 and base.calls[0][0].endswith('/v4/sports')
    assert not (tmp_path/'odds_budget.json').exists()
