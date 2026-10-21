from pathlib import Path
import pytest
from streamlit.testing.v1 import AppTest
from pinnacle_props.deployment import deployment_settings
# Resolve repository config paths before tests redirect runtime ROOT to tmp_path.
from pinnacle_props import rules, book_catalog


def test_cloud_preserves_reserve_without_daily_policy():
    cloud=deployment_settings({'PP_DEPLOYMENT':'cloud'})
    assert cloud['project_cap']==100000 and cloud['paid_enabled'] and 'cloud_daily_cap' not in cloud
    assert deployment_settings({})['paid_enabled']
    with pytest.raises(ValueError):deployment_settings({'PP_STORE':'s3'})


def test_empty_cloud_views_open_without_key_or_odds_network(tmp_path,monkeypatch):
    monkeypatch.setattr('pinnacle_props.kelly_sheet.fetch_profiles',lambda:{})
    monkeypatch.setattr('pinnacle_props.settings.ROOT',tmp_path)
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:pytest.fail('Network on page open'))
    app=AppTest.from_file(Path('app.py').resolve(),default_timeout=15)
    app.secrets['PP_DEPLOYMENT']='cloud';app.secrets['APP_PASSPHRASE']='fixture-pass';app.run()
    app.text_input(key='_access_input').set_value('fixture-pass')
    app.button(key='access_unlock').click().run()
    assert not app.exception
    assert [t.label for t in app.tabs]==['Price comparison','Live','Kelly calculator','Logged','Performance']
    assert sum('not connected' in item.value for item in app.info)==3
    assert not any('Live refresh is not available' in item.value for item in app.info)


def test_cloud_reserve_migration_preserves_usage_and_is_idempotent(tmp_path):
    import json
    from pinnacle_props.odds_api import Client, PROJECT_CAP
    from pinnacle_props.deployment import reserve_cloud_budget
    client=Client(data_dir=tmp_path)
    state=client._load();state.update(project_cap=2_000_000,spent=170000,runs={'historical':170000},
        records=[{'request_id':'fixture','run_id':'historical','actual':170000,'timestamp':'2026-10-07T00:00:00+00:00'}])
    client._save(state)
    reserve_cloud_budget(tmp_path)
    after=client._load()
    assert after['project_cap']==1900000 and after['spent']==170000
    assert after['runs']==state['runs'] and after['records']==state['records']
    before=(tmp_path/'odds_budget.json').read_bytes()
    reserve_cloud_budget(tmp_path)
    assert (tmp_path/'odds_budget.json').read_bytes()==before


@pytest.fixture
def cloud_kelly(tmp_path,monkeypatch):
    from pinnacle_props.kelly import Profile
    monkeypatch.setenv('PP_SETTLEMENT_ENABLED','0')
    monkeypatch.setattr('pinnacle_props.settings.ROOT',tmp_path)
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:pytest.fail('Unexpected network'))
    monkeypatch.setattr('pinnacle_props.odds_api.resolve_key',lambda **k:'fixture')
    monkeypatch.setattr('pinnacle_props.pipeline.run_live',lambda *a,**k:pytest.fail('Paid scan'))
    calls=[];result={'Player_Props':Profile(1000,.25,.05)}
    def fetch():
        calls.append('free Sheet read')
        if isinstance(result.get('error'),Exception):raise result['error']
        return {k:v for k,v in result.items() if k!='error'}
    monkeypatch.setattr('pinnacle_props.kelly_sheet.fetch_profiles',fetch)
    events=[dict(id='one',commence_time='2099-01-08T18:00:00Z'),dict(id='two',commence_time='2099-01-12T18:00:00Z')]
    bound=[792]
    monkeypatch.setattr('pinnacle_props.pipeline.discover',lambda settings,**k:dict(settings=settings,events=events.copy(),bound=bound[0]))
    app=AppTest.from_file(Path('app.py').resolve(),default_timeout=15)
    app.secrets['PP_DEPLOYMENT']='cloud';app.secrets['APP_PASSPHRASE']='fixture-pass'
    return app,calls,result,events,bound,tmp_path


def unlock(app):
    app.run();app.text_input(key='_access_input').set_value('fixture-pass')
    app.button(key='access_unlock').click().run()
    assert not app.exception
    return app


def test_cloud_auto_load_once_after_unlock_and_manual_reload(cloud_kelly):
    app,calls,result,events,bound,root=cloud_kelly
    app.run();assert not calls
    unlock(app);assert len(calls)==1
    app.button(key='estimate').click().run()
    assert not app.exception and not app.button(key='refresh').disabled
    assert not app.checkbox(key='limit_dates').value
    assert any('Tick Limit game dates to scan fewer.'==x.value for x in app.caption)
    app.radio(key='view_mode').set_value('Cards').run()
    assert len(calls)==1
    app.button(key='reload_profiles').click().run()
    assert not app.exception and len(calls)==2


def test_cloud_auto_load_failure_is_explained_without_retry_loop(cloud_kelly):
    app,calls,result,events,bound,root=cloud_kelly
    result['error']=ValueError('Kelly sheet HTTP 503')
    unlock(app);app.button(key='estimate').click().run()
    assert not app.exception and len(calls)==1 and app.button(key='refresh').disabled
    assert any('Kelly settings unavailable: Kelly sheet HTTP 503'==x.value for x in app.error)
    assert any('Load Kelly profiles first.' in x.value for x in app.warning)
    app.run();assert len(calls)==1
    del result['error'];app.button(key='reload_profiles').click().run()
    assert not app.exception and len(calls)==2 and not app.button(key='refresh').disabled
    assert not app.error


def test_refresh_no_games_and_reserve_reasons(cloud_kelly):
    import json
    app,calls,result,events,bound,root=cloud_kelly
    events.clear();bound[0]=0
    unlock(app);app.button(key='estimate').click().run()
    assert app.button(key='refresh').disabled
    assert any('No upcoming games.' in x.value for x in app.warning)
    events.append(dict(id='one',commence_time='2099-01-08T18:00:00Z'));bound[0]=14
    data=root/'data';data.mkdir(exist_ok=True)
    (data/'odds_budget.json').write_text(json.dumps(dict(spent=99990,project_cap=100000,remaining=900000,reservations={})))
    app.button(key='estimate').click().run()
    assert not app.exception and app.button(key='refresh').disabled
    assert any('remaining cloud reserve (10 credits)' in x.value for x in app.warning)
    assert any('Account floor: 100,000' in x.value for x in app.caption)
    assert not any('Tick Limit game dates' in x.value for x in app.caption)


def test_estimate_above_old_daily_cap_is_enabled_even_with_legacy_ledger(cloud_kelly):
    import json
    app,calls,result,events,bound,root=cloud_kelly
    bound[0]=2501;unlock(app)
    data=root/'data';data.mkdir(exist_ok=True)
    (data/'odds_budget.json').write_text(json.dumps(dict(spent=5000,cloud_daily_cap=0,remaining=900000,reservations={})))
    app.button(key='estimate').click().run()
    assert not app.exception and not app.button(key='refresh').disabled
    assert any('2501 credits' in x.value for x in app.info)
    assert any('Tick Limit game dates' in x.value for x in app.caption)
    assert not any('daily cloud cap' in x.value.lower() for x in app.warning)


def test_cloud_ui_retains_reserve_floor_and_unreadable_state_guards(tmp_path):
    import json
    from pinnacle_props.deployment import cloud_refresh_reason
    assert cloud_refresh_reason(tmp_path,2501) is None
    assert 'reserve' in cloud_refresh_reason(tmp_path,100001)
    path=tmp_path/'odds_budget.json'
    path.write_text(json.dumps(dict(spent=5000,cloud_daily_cap=0,remaining=103000)))
    assert cloud_refresh_reason(tmp_path,3000) is None
    assert 'floor' in cloud_refresh_reason(tmp_path,3001)
    path.write_text('{broken')
    assert 'Quota record is unavailable' in cloud_refresh_reason(tmp_path,14)
