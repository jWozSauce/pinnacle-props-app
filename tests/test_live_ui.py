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
        out.loc[pd.to_datetime(out.start_time,utc=True)>pd.Timestamp(NOW),'stage']='upcoming'
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
    assert not app.exception and app.radio(key='live_log').value=='real'
    app.radio(key='live_log').set_value('paper').run()
    assert app.tabs[1].label=='Live' and timers[-1]==60
    table=app.dataframe[0].value
    assert len(table)==1 and table.iloc[0].verdict=='🟢 covering'
    assert {'score','detail','bet','book','stake','odds','to_win','logged at','start','matchup'}<=set(table)
    assert {m.label for m in app.metric}=={'Official P/L','Projected P/L','In progress','Won / lost (incl. unofficial)','Upcoming'}
    assert app.button(key='refresh_live')


def test_empty_and_future_games_do_not_start_timer(tmp_path,monkeypatch):
    row=record(sport='nhl',start_time='2026-10-21T17:00:00Z',game_date='2026-10-21',status='pending')
    app,store,timers=setup(tmp_path,monkeypatch,[row])
    app.radio(key='live_log').set_value('paper').run()
    assert not app.exception and len(app.dataframe[0].value)==1 and timers[-1] is None
    assert next(m.value for m in app.metric if m.label=='Upcoming')=='1'
    # Stored history is retained; an upcoming bet simply has not entered Live.
    assert len(store.read('paper'))==1


def test_historical_settled_real_visible_without_live_timer(tmp_path,monkeypatch):
    app,store,timers=setup(tmp_path,monkeypatch,[])
    r=record(sport='nhl',start_time='2026-10-17T23:00:00Z',game_date='2026-10-17')
    store.append('real',[r]);old=store.read('real',reload=True).iloc[0].to_dict()
    store.grade('real',[(old,dict(old,status='won',profit=12,stat=1,graded_at='2026-10-18T04:00:00Z'))])
    app.run()
    assert not app.exception and timers[-1] is None
    assert not app.dataframe
    assert any(i.value==live_ui.EMPTY for i in app.info)
    assert next(m.value for m in app.metric if m.label=='Official P/L')=='$0.00'


def test_mixed_pending_recent_graded_card_and_full_overall(tmp_path,monkeypatch):
    from pinnacle_props import performance_ui
    store=BetStore(tmp_path,spreadsheet=Spreadsheet())
    for kind in ('real','paper'):
        rows=[record(row=i,player=f'Fixture {i}',sport='nhl',stake=10,price_decimal=3,
                     start_time='2026-10-17T23:00:00Z',game_date='2026-10-17',
                     logged_at=f'2026-10-17T12:0{i}:00Z',island_flag='ISLAND' if i==2 else '') for i in (1,2,3)]
        store.append(kind,rows)
        old=store.read(kind,reload=True).to_dict('records')
        store.grade(kind,[(old[1],dict(old[1],status='won',profit=20,stat=1,graded_at='2026-10-21T03:00:00Z')),
                          (old[2],dict(old[2],status='lost',profit=-10,stat=0,graded_at='2026-10-21T03:01:00Z'))])
    monkeypatch.setattr(live_ui,'session_store',lambda _:store)
    monkeypatch.setattr(live_ui,'utcnow',lambda:NOW)
    monkeypatch.setattr(performance_ui,'BetStore',lambda *a,**k:store)
    def live(frame,feed):
        f=frame.copy();f['stage']='live';f['verdict']='🟢 covering';f['score']='2–1';f['detail']='Fixture live';return f
    monkeypatch.setattr(live_ui,'live_table',live)
    app=AppTest.from_string(f"""import streamlit as st
from pathlib import Path
from pinnacle_props.live_ui import render_live
from pinnacle_props.performance_ui import render
a,b,c=st.tabs(['Logged','Performance','Live'])
render(a,b,Path({str(tmp_path)!r}))
with c:render_live(Path({str(tmp_path)!r}))""").run()
    assert not app.exception
    for tab in app.tabs[:2]:
        table=tab.dataframe[0].value
        assert list(table.status)==['lost','won','pending']
        assert len(table)==3 and table.iloc[1].island_flag=='ISLAND'
        assert any(m.label=='Record' and m.value=='1–1–0' for m in tab.metric)
        assert any(m.label=='Profit' and m.value=='$10.00' for m in tab.metric)
        assert any(m.label=='Staked' and m.value=='$20.00' for m in tab.metric)
        assert any(m.label=='ROI' and m.value=='+50.00%' for m in tab.metric)
        assert any('1 pending' in c.value for c in tab.caption)
        assert any(b.label=='Refresh log' for b in tab.button)
    card=app.tabs[2].dataframe[0].value
    assert list(card.verdict)==['🟢 covering','✅ won (official)','❌ lost (official)']
    assert len(card)==3 and all(v.startswith(('+','-')) for v in card.odds)
    assert any(m.label=='Official P/L' and m.value=='$10.00' for m in app.tabs[2].metric)
    assert any(m.label=='Projected P/L' and m.value=='$30.00' for m in app.tabs[2].metric)
    assert any(m.label=='Won / lost (incl. unofficial)' and m.value=='1W / 1L' for m in app.tabs[2].metric)
    app.radio(key='live_log').set_value('paper').run()
    assert not app.exception and len(app.tabs[2].dataframe[0].value)==3


def test_rolling_cutoff_and_stage_order(tmp_path,monkeypatch):
    from pinnacle_props.bet_store import HEADER
    rows=[]
    specs=[('upcoming','pending',''),('final','pending',''),('live','pending',''),
           ('official','won','2026-10-20T16:00:00Z'),('official','lost','2026-10-20T15:59:59Z'),
           ('unmatched','pending',''),('official','won','2026-10-22T16:00:00Z')]
    for i,(stage,status,graded) in enumerate(specs):
        rows.append(dict(record(row=i,player=f'Fixture {i}'),stage=stage,status=status,graded_at=graded,start_time='2026-10-22T12:00:00Z',verdict='✅ won' if stage=='final' else '—'))
    class Store:
        def read(self,kind,**kwargs):return pd.DataFrame(rows) if kind=='real' else pd.DataFrame(columns=HEADER)
    monkeypatch.setattr(live_ui,'utcnow',lambda:NOW)
    monkeypatch.setattr(live_ui,'live_table',lambda frame,feed:frame)
    out,active=live_ui.cards(Store(),None)
    assert active and list(out['real'][0].stage)==['live','decided','graded','upcoming','unmatched']
    assert len(out['real'][0])==5
