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


def test_catalog_picker_defaults_caption_unobserved_books_and_reference(monkeypatch):
    monkeypatch.setenv('PP_BOOKMAKER_CATALOG_ENABLED','1')
    monkeypatch.setenv('PP_SETTLEMENT_ENABLED','0')
    monkeypatch.setattr(book_catalog,'load',fixture_catalog)
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:pytest.fail('Network on app startup'))
    app=AppTest.from_file(Path('app.py').resolve()).run(timeout=20)
    assert not app.exception
    selector=app.multiselect(key='books_nhl')
    assert selector.value==list(load_config()['books'])
    assert 'Extra Exchange' in selector.options
    assert 'Absent Book' in selector.options
    assert app.checkbox(key='catalog_reference').disabled and app.checkbox(key='catalog_reference').value
    assert not app.checkbox(key='book_choice_nhl_absent').disabled
    assert any(x.value=='no props seen yet' for x in app.caption)
    assert any('15 books selected ≈ 2× region cost per pull'==x.value for x in app.caption)
    selector.set_value(['extra']).run()
    assert not app.exception
    assert any('5 books selected ≈ 1× region cost per pull'==x.value for x in app.caption)
    result=options(load_config(),'nhl',books=['extra','pinnacle','extra'])
    assert result['books']==['extra'] and result['reference_book']=='pinnacle'
    assert options(load_config(),'nhl',books=['absent'])['books']==['absent']


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


def test_group_search_reset_and_per_sport_url_session(monkeypatch):
    monkeypatch.setenv('PP_SETTLEMENT_ENABLED','0')
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:pytest.fail('Network on app startup'))
    catalog=fixture_catalog()
    catalog['books']['absent']['regions']=['us2']
    catalog['books']['extra']['regions']=['us_ex']
    monkeypatch.setattr(book_catalog,'load',lambda:catalog)
    app=AppTest.from_file(Path('app.py').resolve(),default_timeout=20).run()
    app.text_input(key='book_search_nhl').set_value('absent').run()
    assert not app.exception
    assert app.multiselect(key='books_nhl').value==list(load_config()['books'])
    assert not app.checkbox(key='book_choice_nhl_absent').disabled
    app.checkbox(key='book_choice_nhl_absent').check().run()
    assert 'absent' in app.multiselect(key='books_nhl').value
    # Select entire exchange group even though search hides its only entry.
    app.button(key='book_group_nhl_1').click().run()
    assert 'extra' in app.multiselect(key='books_nhl').value
    assert 'absent' in app.query_params['books_nhl']
    app.text_input(key='book_search_nhl').set_value('').run()
    assert app.checkbox(key='book_choice_nhl_extra').value
    # Per-sport query values survive Streamlit's widget cleanup.
    sport_widget=app.selectbox(key='sport')
    sport_widget.set_value('nba').run()
    app.multiselect(key='books_nba').set_value(['absent']).run()
    app.selectbox(key='sport').set_value('nhl').run()
    assert 'extra' in app.multiselect(key='books_nhl').value
    app.button(key='reset_books_nhl').click().run()
    assert app.multiselect(key='books_nhl').value==list(load_config()['books'])
    app.selectbox(key='sport').set_value('nba').run()
    assert app.multiselect(key='books_nba').value==['absent']
    app.multiselect(key='books_nba').set_value([]).run()
    assert app.multiselect(key='books_nba').value==[]
    assert app.button(key='estimate').disabled
    assert not app.exception


def test_all_documented_books_selectable_every_sport_and_cost_bound(monkeypatch):
    from pinnacle_props.consensus import request_books
    from pinnacle_props.odds_api import estimated_cost
    monkeypatch.setenv('PP_BOOKMAKER_CATALOG_ENABLED','1')
    catalog=book_catalog.load(); config=load_config()
    for sport in config['sports']:
        keys=list(book_catalog.selectable_books(catalog,sport,config['books']))
        assert set(catalog['books'])-{'pinnacle'} <= set(keys)
        assert {'betanysports','gtbets','kalshi','novig','prizepicks'} <= set(keys)
        settings=options(config,sport,books=keys)
        requested=request_books(settings)
        assert set(catalog['books']) <= set(requested)
        assert requested.count('pinnacle')==1
        expected=(len(requested)+9)//10
        assert estimated_cost('/v4/sports/'+settings['sport_key']+'/events/fixture/odds',
            {'bookmakers':','.join(requested),'markets':'player_points,player_assists'})==expected*2
        assert book_catalog.region_units(requested)==expected


def test_unobserved_selection_reaches_real_request_builder(tmp_path,monkeypatch):
    from datetime import datetime,timedelta,timezone
    from pinnacle_props import pipeline
    from pinnacle_props.kelly import Profile
    from tests.test_api import Session,Response
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:pytest.fail('Real network forbidden'))
    config=load_config();catalog=book_catalog.load()
    selected=list(book_catalog.selectable_books(catalog,'nba',config['books']))
    settings=options(config,'nba',books=selected,markets=['player_points'])
    now=datetime.now(timezone.utc)
    event=dict(id='game-full-catalog',commence_time=(now+timedelta(hours=2)).isoformat(),home_team='Home',away_team='Away')
    preview=pipeline.discover(settings,data_dir=tmp_path,key='fixture',clock=lambda:now,session=Session(Response([event])))
    assert preview['bound']==11
    empty=dict(event,sport_key='basketball_nba',bookmakers=[])
    session=Session(Response(),Response(empty,last=0))
    pipeline.run_live(preview,data_dir=tmp_path,profile=Profile(5000,.25,.04),key='fixture',session=session,paper_track=False)
    sent=[kwargs['params']['bookmakers'].split(',') for url,kwargs in session.calls if url.endswith('/odds')]
    assert len(sent)==1 and set(sent[0])==set(catalog['books'])
    assert sent[0].count('pinnacle')==1


def test_regional_duplicates_and_fresh_url_restore(monkeypatch):
    monkeypatch.setenv('PP_SETTLEMENT_ENABLED','0')
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:pytest.fail('Network on app startup'))
    app=AppTest.from_file(Path('app.py').resolve(),default_timeout=20).run()
    choices=app.multiselect(key='books_nhl')
    assert len(choices.options)==len(set(choices.options))
    assert 'Betfair Exchange · betfair_ex_au' in choices.options
    choices.set_value(['betfair_ex_au','betfair_ex_uk','betanysports','gtbets']).run()
    fresh=AppTest.from_file(Path('app.py').resolve(),default_timeout=20)
    fresh.query_params.update(app.query_params);fresh.run()
    assert fresh.multiselect(key='books_nhl').value==['betfair_ex_au','betfair_ex_uk','betanysports','gtbets']
    assert not fresh.exception
