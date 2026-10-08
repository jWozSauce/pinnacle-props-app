from pathlib import Path
from streamlit.testing.v1 import AppTest
from pinnacle_props.pipeline import replay


def test_app_saved_scan_starts_without_credential_or_network(monkeypatch):
    monkeypatch.delenv('ODDS_API_KEY',raising=False)
    monkeypatch.setattr('pinnacle_props.odds_api.resolve_key',lambda **_:(_ for _ in ()).throw(AssertionError('Resolved key in cache mode')))
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Network in cache mode')))
    app=AppTest.from_file(Path('app.py').resolve(),default_timeout=15).run()
    assert not app.exception
    assert app.title[0].value=='Find the better price.'
    assert app.selectbox(key='sport').value=='nhl'
    if list(Path('data/snapshots/nhl').glob('*.json')):
        expected=replay('nhl')[1]
        assert app.metric[0].value==str(len(expected))
        app.text_input(key='search').set_value('no-such-player').run()
        assert not app.exception
        assert any('No opportunities' in x.value for x in app.info)
    app.selectbox(key='sport').select('nba').run()
    assert not app.exception


def test_calculator_floors_negative_edge(monkeypatch):
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Network')))
    app=AppTest.from_file(Path('app.py').resolve(),default_timeout=15).run()
    if not app.number_input: return  # Clean checkout has no private Kelly profile cache.
    app.number_input(key='american').set_value(100).run()
    app.number_input(key='probability').set_value(40).run()
    assert not app.exception
    assert app.metric[-1].value=='$0.00'


def test_responsive_view_switch_persists_in_url(monkeypatch):
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Network')))
    app=AppTest.from_file(Path('app.py').resolve(),default_timeout=15)
    app.run()
    assert not app.exception and app.radio(key='view_mode').value=='Auto'
    assert [t.label for t in app.tabs]==['Price comparison','Kelly calculator']
    for mode in ['Table','Cards','Auto']:
        app.radio(key='view_mode').set_value(mode).run()
        assert not app.exception and app.query_params['view']==mode
    if list(Path('data/snapshots/nhl').glob('*.json')):
        app.multiselect(key='column_filters_nhl').set_value(['player_name']).run()
        app.text_input(key='column_filter_nhl_player_name').set_value('no-such-player').run()
        assert not app.exception
        assert any('No opportunities' in x.value for x in app.info)
