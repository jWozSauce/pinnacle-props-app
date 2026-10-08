from pathlib import Path
import pytest
from streamlit.testing.v1 import AppTest
from pinnacle_props.deployment import deployment_settings


def test_cloud_enforces_approved_temporary_daily_policy():
    cloud=deployment_settings({'PP_DEPLOYMENT':'cloud'})
    assert cloud['project_cap']==100000 and cloud['paid_enabled'] and cloud['cloud_daily_cap']==2500
    assert deployment_settings({})['paid_enabled']
    with pytest.raises(ValueError):deployment_settings({'PP_STORE':'s3'})


def test_empty_cloud_views_open_without_key_or_network(tmp_path,monkeypatch):
    monkeypatch.setattr('pinnacle_props.settings.ROOT',tmp_path)
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:pytest.fail('Network on page open'))
    app=AppTest.from_file(Path('app.py').resolve(),default_timeout=15)
    app.secrets['PP_DEPLOYMENT']='cloud';app.secrets['APP_PASSPHRASE']='fixture-pass';app.run()
    app.text_input(key='_access_input').set_value('fixture-pass')
    app.button(key='access_unlock').click().run()
    assert not app.exception
    assert [t.label for t in app.tabs]==['Price comparison','Kelly calculator','Logged','Performance']
    assert sum('not connected' in item.value for item in app.info)==3
    assert not any('Live refresh is not available' in item.value for item in app.info)


def test_cloud_reserve_migration_preserves_usage_and_is_idempotent(tmp_path):
    import json
    from pinnacle_props.odds_api import Client, PROJECT_CAP
    from pinnacle_props.deployment import reserve_cloud_budget
    client=Client(data_dir=tmp_path)
    state=client._load();state.update(project_cap=PROJECT_CAP,spent=170000,runs={'historical':170000},
        records=[{'request_id':'fixture','run_id':'historical','actual':170000,'timestamp':'2026-10-07T00:00:00+00:00'}])
    client._save(state)
    reserve_cloud_budget(tmp_path)
    after=client._load()
    assert after['project_cap']==1900000 and after['spent']==170000
    assert after['runs']==state['runs'] and after['records']==state['records']
    before=(tmp_path/'odds_budget.json').read_bytes()
    reserve_cloud_budget(tmp_path)
    assert (tmp_path/'odds_budget.json').read_bytes()==before
