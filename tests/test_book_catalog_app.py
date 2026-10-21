from pathlib import Path
from streamlit.testing.v1 import AppTest
import pytest
from pinnacle_props import book_catalog
from pinnacle_props.pipeline import options
from pinnacle_props.settings import load_config


def fixture_catalog():
    books={k:dict(label=v,kind='sportsbook',sports={'nhl':{'markets':['player_points']}})
           for k,v in load_config()['books'].items()}
    books['extra']=dict(label='Extra Exchange',kind='exchange',sports={'nhl':{'markets':['player_points']}})
    books['absent']=dict(label='Absent Book',kind='sportsbook',sports={})
    books['pinnacle']=dict(label='Pinnacle',kind='sportsbook',sports={'nhl':{'markets':['player_points']}})
    return dict(schema_version=1,books=books,sports={'nhl':dict(status='observed')})


def test_catalog_picker_defaults_caption_disabled_books_and_reference(monkeypatch):
    monkeypatch.setenv('PP_BOOKMAKER_CATALOG_ENABLED','1')
    monkeypatch.setenv('PP_SETTLEMENT_ENABLED','0')
    monkeypatch.setattr(book_catalog,'load',fixture_catalog)
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:pytest.fail('Network on app startup'))
    app=AppTest.from_file(Path('app.py').resolve()).run(timeout=20)
    assert not app.exception
    selector=app.multiselect(key='books_nhl')
    assert selector.value==list(load_config()['books'])
    assert 'Extra Exchange' in selector.options
    assert 'Absent Book' not in selector.options
    assert app.checkbox(key='catalog_reference').disabled and app.checkbox(key='catalog_reference').value
    assert app.checkbox(key='unavailable_nhl_absent').disabled
    assert any('12 books selected ≈ 2× region cost per pull'==x.value for x in app.caption)
    selector.set_value(['extra']).run()
    assert not app.exception
    assert any('2 books selected ≈ 1× region cost per pull'==x.value for x in app.caption)
    result=options(load_config(),'nhl',books=['extra','pinnacle','extra'])
    assert result['books']==['extra'] and result['reference_book']=='pinnacle'
    with pytest.raises(ValueError):options(load_config(),'nhl',books=['absent'])


def test_explicit_catalog_opt_out_preserves_legacy_picker(monkeypatch):
    monkeypatch.setenv('PP_BOOKMAKER_CATALOG_ENABLED','0')
    with pytest.raises(ValueError):options(load_config(),'nhl',books=['extra'])
    assert options(load_config(),'nhl')['books']==list(load_config()['books'])


def test_catalog_enabled_by_default_and_keeps_defaults_without_observation(monkeypatch):
    monkeypatch.delenv('PP_BOOKMAKER_CATALOG_ENABLED',raising=False)
    data=fixture_catalog()
    for key in load_config()['books']:data['books'][key]['sports']={}
    data['sports']['nhl']['status']='deferred: budget'
    monkeypatch.setattr(book_catalog,'load',lambda:data)
    monkeypatch.setenv('PP_SETTLEMENT_ENABLED','0')
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:pytest.fail('Network on app startup'))
    app=AppTest.from_file(Path('app.py').resolve()).run(timeout=20)
    assert not app.exception and book_catalog.enabled()
    assert app.multiselect(key='books_nhl').value==list(load_config()['books'])
    assert 'Extra Exchange' in app.multiselect(key='books_nhl').options
    assert any('bookmaker discovery is deferred' in x.value for x in app.caption)
    assert options(load_config(),'nhl')['books']==list(load_config()['books'])
    assert options(load_config(),'nhl',books=['extra'])['books']==['extra']
