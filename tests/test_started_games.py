from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd
import pytest
from pinnacle_props import game_time
from tests.test_bet_selection_app import checked_app, edit

NOW=datetime(2026,10,21,2,tzinfo=timezone.utc)


def test_time_boundaries_offsets_unknown_and_saved_identity():
    frame=pd.DataFrame({'date':['2026-10-20T22:00:00-04:00',
        (NOW+timedelta(minutes=15)).isoformat(),(NOW+timedelta(minutes=72)).isoformat(),
        '2026-10-22T00:00:00','bad']},index=[2,5,8,11,14])
    original=frame.copy(deep=True)
    visible,hidden,unknown=game_time.suggestions(frame,now=NOW)
    assert visible.index.tolist()==[5,8] and (hidden,unknown)==(1,2)
    assert visible.start_status.tolist()==['starts in 15 m · closing soon','starts in 1 h 12 m']
    assert len(game_time.suggestions(frame,show_started=True,now=NOW)[0])==3
    pd.testing.assert_frame_equal(frame,original)


def two_games(checked_app,monkeypatch):
    app,store,frame=checked_app
    monkeypatch.setattr(game_time,'utcnow',lambda:NOW)
    frame.drop(frame.index[2:],inplace=True)
    frame['edge']=.10
    frame['date']=[(NOW-timedelta(minutes=1)).isoformat(),(NOW+timedelta(minutes=12)).isoformat()]
    app.run()
    return app,store,frame


def test_saved_scan_default_hide_reveal_countdown_and_card_confirmation(checked_app,monkeypatch):
    app,store,frame=two_games(checked_app,monkeypatch)
    assert not app.exception and app.metric[0].value=='1'
    assert any(c.value=='1 hidden: game started' for c in app.caption)
    table=next(t.value for t in app.dataframe if 'log' in t.value.columns)
    assert len(table)==1 and 'closing soon' in table.iloc[0].start_status
    assert not any(b.key=='log_fixture_1' for b in app.button)
    app.checkbox(key='show_started_games').check().run()
    assert not app.exception and app.metric[0].value=='2'
    app.button(key='log_fixture_1').click().run()
    assert store.read('real',reload=True).empty
    assert any('Game started; confirm' in e.value for e in app.error)
    app.checkbox(key='confirm_started_fixture_1').check().run()
    app.button(key='log_fixture_1').click().run()
    assert not app.exception and len(store.read('real',reload=True))==1
    # Opt-ins do not enter URLs or persisted settings; a new session starts off.
    from streamlit.testing.v1 import AppTest
    fresh=AppTest.from_file(Path('app.py').resolve(),default_timeout=20).run()
    assert not fresh.exception
    assert not fresh.checkbox(key='show_started_games').value
    assert not fresh.checkbox(key='include_live').value


def test_checked_bets_require_confirmation_and_paper_excludes_started(checked_app,monkeypatch):
    app,store,frame=two_games(checked_app,monkeypatch)
    app.checkbox(key='show_started_games').check().run()
    edit(app,{0:{'log':True},1:{'log':True}})
    app.button(key='log_selected').click().run()
    assert not app.exception and store.read('real',reload=True).empty
    confirm=next(c for c in app.checkbox if c.label.startswith('Confirm logging 1 selected'))
    edit(app,{0:{'log':True},1:{'log':True}})
    confirm.check().run()
    edit(app,{0:{'log':True},1:{'log':True}})
    app.button(key='log_selected').click().run()
    assert not app.exception and len(store.read('real',reload=True))==2, ([e.value for e in app.error],[w.value for w in app.warning])
    assert app.button(key='paper_all').label=='Bet ALL 1 qualifying (paper round)'
    app.button(key='paper_all').click().run()
    assert not app.exception and store.read('paper',reload=True).player.tolist()==['Fixture 1']


def test_logging_rechecks_clock_after_render(checked_app,monkeypatch):
    app,store,frame=two_games(checked_app,monkeypatch)
    import pinnacle_props.bet as bet
    monkeypatch.setattr(game_time,'utcnow',lambda:NOW+timedelta(minutes=12))
    with pytest.raises(ValueError,match='Game started; confirm'):
        bet.log_bet('fixture',2,10,data_dir=store.root)
    assert store.read('real',reload=True).empty
