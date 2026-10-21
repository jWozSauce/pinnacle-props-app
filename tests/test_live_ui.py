from datetime import datetime,timezone
from pathlib import Path
import pandas as pd
from streamlit.testing.v1 import AppTest
from pinnacle_props import live_ui
from pinnacle_props.bet_store import BetStore
from tests.test_bet_store import Spreadsheet,record

NOW=datetime(2026,10,21,16,tzinfo=timezone.utc)


def setup(tmp_path,monkeypatch,rows):
    store=BetStore(tmp_path,spreadsheet=Spreadsheet())
    if rows:store.append('paper',rows)
    monkeypatch.setattr(live_ui,'session_store',lambda _:store)
    monkeypatch.setattr(live_ui,'utcnow',lambda:NOW)
    def live(frame,feed):
        out=frame.copy();out['stage']='live';out['verdict']='🟢 covering'
        out['score']='2–1';out['detail']='assists 1 vs 0.5 · P2';return out
    monkeypatch.setattr(live_ui,'live_table',live)
    timers=[];original=live_ui.st.fragment
    def fragment(*a,**kw):timers.append(kw.get('run_every'));return original(*a,**kw)
    monkeypatch.setattr(live_ui.st,'fragment',fragment)
    app=AppTest.from_string(f'''import streamlit as st
from pinnacle_props.live_ui import render_live
from pathlib import Path
a,b=st.tabs(['Price comparison','Live'])
with b:render_live(Path({str(tmp_path)!r}))''').run()
    return app,store,timers


def test_started_paper_row_and_sixty_second_refresh(tmp_path,monkeypatch):
    row=record(sport='nhl',start_time='2026-10-21T15:00:00Z',game_date='2026-10-21',status='pending')
    app,store,timers=setup(tmp_path,monkeypatch,[row])
    assert not app.exception and app.radio(key='live_log').value=='paper'
    assert app.tabs[1].label=='Live' and timers[-1]==60
    table=app.dataframe[0].value
    assert len(table)==1 and table.iloc[0].verdict=='🟢 covering'
    assert {'score','detail','bet','book','stake','price_decimal','to_win'}<=set(table)
    assert {m.label for m in app.metric}=={'Official P/L','Projected P/L'}
    assert app.button(key='refresh_live')


def test_empty_and_future_games_do_not_start_timer(tmp_path,monkeypatch):
    row=record(sport='nhl',start_time='2026-10-21T17:00:00Z',game_date='2026-10-21',status='pending')
    app,store,timers=setup(tmp_path,monkeypatch,[row])
    assert not app.exception and not app.dataframe and timers[-1] is None
    assert any(i.value==live_ui.EMPTY for i in app.info)
    # Stored history is retained; an upcoming bet simply has not entered Live.
    assert len(store.read('paper'))==1
