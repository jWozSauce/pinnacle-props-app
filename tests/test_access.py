from pathlib import Path
import pytest
from streamlit.testing.v1 import AppTest


def cloud(monkeypatch,secret='fixture-pass'):
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:pytest.fail('Network at access gate'))
    app=AppTest.from_file(Path('app.py').resolve(),default_timeout=15)
    app.secrets['PP_DEPLOYMENT']='cloud'
    if secret is not None:app.secrets['APP_PASSPHRASE']=secret
    return app


def test_cloud_missing_secret_fails_closed_before_config_or_records(monkeypatch):
    monkeypatch.setattr('pinnacle_props.settings.load_config',lambda:pytest.fail('Scanner configuration before gate'))
    app=cloud(monkeypatch,None).run()
    assert not app.exception and len(app.error)==1
    assert not app.tabs and not app.selectbox and not app.metric and not app.button


def test_locked_page_has_only_prompt_and_cannot_unlock_via_url(monkeypatch):
    monkeypatch.setattr('pinnacle_props.settings.load_config',lambda:pytest.fail('Scanner configuration before gate'))
    app=cloud(monkeypatch);app.query_params['unlocked']='true';app.query_params['preview_clv']='1';app.run()
    assert not app.exception and [b.label for b in app.button]==['Unlock']
    assert [t.label for t in app.text_input]==['Passphrase']
    assert not app.tabs and not app.title and not app.caption and not app.metric and not app.selectbox


def test_correct_passphrase_unlock_and_lock_clear_session(monkeypatch,tmp_path):
    monkeypatch.setattr('pinnacle_props.settings.ROOT',tmp_path)
    app=cloud(monkeypatch).run()
    app.text_input(key='_access_input').set_value('fixture-pass');app.button(key='access_unlock').click().run()
    assert not app.exception and len(app.tabs)==4
    assert 'fixture-pass' not in str(app.query_params)
    assert app.session_state['_access_input']==''
    app.button(key='access_lock').click().run()
    assert not app.exception and not app.tabs and not app.selectbox


def test_five_failures_lock_for_sixty_seconds(monkeypatch,tmp_path):
    monkeypatch.setattr('pinnacle_props.settings.ROOT',tmp_path)
    now=[100.0];monkeypatch.setattr('pinnacle_props.access.clock',lambda:now[0])
    app=cloud(monkeypatch).run()
    for _ in range(5):
        app.text_input(key='_access_input').set_value('wrong');app.button(key='access_unlock').click().run()
    assert not app.exception and app.button(key='access_unlock').disabled and not app.tabs
    now[0]=159.9;app.run();assert app.button(key='access_unlock').disabled
    now[0]=160.1;app.run();assert not app.button(key='access_unlock').disabled
    app.text_input(key='_access_input').set_value('fixture-pass');app.button(key='access_unlock').click().run()
    assert not app.exception and len(app.tabs)==4
