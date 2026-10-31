"""Startup compatibility failures must stop safely before the access gate."""
from pathlib import Path
import pytest
from streamlit.testing.v1 import AppTest
from pinnacle_props import refresh_perf,pipeline


@pytest.mark.parametrize('failure',['old_constructor','old_phase','missing_import','old_options'])
def test_stale_runtime_stops_with_reload_message_before_access_or_data(monkeypatch,failure):
    if failure=='old_constructor':
        class Old:
            def __init__(self,root,report=None):raise AssertionError('Must not instantiate')
        monkeypatch.setattr(refresh_perf,'RefreshPerf',Old)
    elif failure=='old_phase':
        monkeypatch.setattr(refresh_perf.RefreshPerf,'phase',lambda self,name:None)
    elif failure=='missing_import':monkeypatch.delattr(refresh_perf,'RefreshPerf')
    else:monkeypatch.setattr(pipeline,'options',lambda config,sport:None)
    monkeypatch.setattr('pinnacle_props.settings.load_config',lambda:pytest.fail('Data before compatibility guard'))
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**kw:pytest.fail('Network at startup'))
    app=AppTest.from_file(Path('app.py').resolve(),default_timeout=15)
    app.secrets['PP_DEPLOYMENT']='cloud';app.secrets['APP_PASSPHRASE']='fixture-pass'
    app.run()
    assert not app.exception and not app.error
    assert [i.value for i in app.info]==['New version loading, reload in a minute.']
    assert not app.tabs and not app.button and not app.text_input
    assert not any('data-pp-build' in m.value for m in app.markdown)
