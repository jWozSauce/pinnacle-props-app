from streamlit.testing.v1 import AppTest
from pinnacle_props.bet_store import BetStore
from pinnacle_props import performance_ui, live_ui
from tests.test_bet_store import Spreadsheet, record


def choose(app, view, sport):
    app.checkbox(key=f'pp_{view}_sports_all').uncheck().run()
    app.checkbox(key=f'pp_{view}_sports_{sport}').check().run()
    assert not app.exception


def fixture_store(tmp_path):
    store=BetStore(tmp_path,spreadsheet=Spreadsheet())
    for kind in ('real','paper'):
        rows=[record(sport=sport,player=sport.upper()+str(i),row=i,stake=10,price_decimal=3,
                     game_date='2026-10-21',start_time='2026-10-21T15:00:00Z',edge=.04)
              for i,sport in enumerate(('nhl','nba','nhl'))]
        store.append(kind,rows)
        old=store.read(kind,reload=True).to_dict('records')
        store.grade(kind,[(old[0],dict(old[0],status='won',profit=20,graded_at='2026-10-21T16:00:00Z')),
                          (old[1],dict(old[1],status='lost',profit=-10,graded_at='2026-10-21T16:00:00Z'))])
    return store


def test_overall_paper_tables_chart_and_url(tmp_path,monkeypatch):
    store=fixture_store(tmp_path)
    monkeypatch.setattr(performance_ui,'BetStore',lambda *a,**k:store)
    script=f'''import streamlit as st
from pathlib import Path
from pinnacle_props.performance_ui import render
a,b,c=st.tabs(['Logged','Performance','Paper'])
render(a,b,Path({str(tmp_path)!r}),paper_tab=c)
'''
    app=AppTest.from_string(script).run()
    choose(app,'performance','nhl')
    tab=app.tabs[1]
    assert [(m.label,m.value) for m in tab.metric][:4]==[('Record','1–0–0'),('Staked','$10.00'),('Profit','$20.00'),('ROI','+200.00%')]
    summary=tab.dataframe[0].value.set_index('Sport')
    assert len(summary)==5 and summary.loc['NBA','Real profit']==-10
    assert summary.loc['NHL','Paper profit']==20 and summary.loc['NHL','Real pending']==1
    itemised=[d.value for d in tab.dataframe if 'bet' in d.value]
    assert len(itemised)==2 and all(len(f)==2 and f.bet.str.startswith('NHL').all() for f in itemised)
    assert app.query_params['performance_sports']=='nhl'
    assert any('1 pending' in c.value for c in tab.caption)
    sport_breakdown=next(d.value for d in tab.dataframe if 'sport' in d.value)
    assert list(sport_breakdown.sport)==['nhl']
    # Chart protobuf data contains the filtered cumulative profit.
    assert len(tab.get('vega_lite_chart'))==1
    from streamlit import dataframe_util
    chart=tab.get('vega_lite_chart')[0].proto
    chart_frames=[dataframe_util.convert_arrow_bytes_to_pandas_df(d.data.data) for d in chart.datasets]
    assert any(20 in f.select_dtypes('number').to_numpy() for f in chart_frames)
    choose(app,'paper','nhl')
    paper=app.tabs[2]
    assert next(m.value for m in paper.metric if m.label=='Profit')=='$20.00'
    assert all(set(d.value.profit)=={20.} for d in paper.dataframe if 'pending' in d.value)
    app.radio(key='performance_log').set_value('paper').run()
    assert app.checkbox(key='pp_performance_sports_nhl').value
    # A fresh browser session restores URL state, without carrying session_state.
    fresh=AppTest.from_string(script)
    fresh.query_params.update(app.query_params)
    fresh.run()
    assert not fresh.exception and fresh.checkbox(key='pp_performance_sports_nhl').value
    assert not fresh.checkbox(key='pp_performance_sports_nba').value
    assert next(m.value for m in fresh.tabs[1].metric if m.label=='Profit')=='$20.00'
    app.checkbox(key='pp_performance_sports_nhl').uncheck().run()
    assert app.query_params['performance_sports']=='none'
    assert not any('bet' in d.value for d in app.tabs[1].dataframe)
    assert next(m.value for m in app.tabs[1].metric if m.label=='Profit')=='$0.00'
    app.checkbox(key='pp_performance_sports_all').check().run()
    assert next(m.value for m in app.tabs[1].metric if m.label=='Profit')=='$10.00'
    assert app.query_params['performance_sports']=='all'


def test_live_filter_official_projected_counts_and_rows(tmp_path,monkeypatch):
    from datetime import datetime,timezone
    store=fixture_store(tmp_path)
    monkeypatch.setattr(live_ui,'session_store',lambda _:store)
    monkeypatch.setattr(live_ui,'utcnow',lambda:datetime(2026,10,21,18,tzinfo=timezone.utc))
    def live(frame,feed):
        f=frame.copy();f['stage']='live';f['verdict']='🟢 covering';f['detail']='fixture';f['score']='2–1';return f
    monkeypatch.setattr(live_ui,'live_table',live)
    app=AppTest.from_string(f'''from pinnacle_props.live_ui import render_live
from pathlib import Path
render_live(Path({str(tmp_path)!r}))''').run()
    choose(app,'live','nhl')
    assert next(m.value for m in app.metric if m.label=='Official P/L')=='$20.00'
    assert next(m.value for m in app.metric if m.label=='Projected P/L')=='$40.00'
    assert next(m.value for m in app.metric if m.label=='In progress')=='1'
    assert len(app.dataframe[0].value)==2 and app.dataframe[0].value.bet.str.startswith('NHL').all()
    assert app.query_params['live_sports']=='nhl'
    app.radio(key='live_log').set_value('paper').run()
    assert next(m.value for m in app.metric if m.label=='Projected P/L')=='$40.00'
    app.checkbox(key='pp_live_sports_nhl').uncheck().run()
    assert not app.exception and not app.dataframe
    assert next(m.value for m in app.metric if m.label=='Official P/L')=='$0.00'


def test_session_fallback_and_unrelated_url_preserved():
    script="""import streamlit as st
from pinnacle_props.sport_filter import select_sports
select_sports('performance')
"""
    app=AppTest.from_string(script)
    app.query_params['sport']='nba';app.run()
    choose(app,'performance','nhl')
    assert app.query_params['sport']=='nba'
    del app.query_params['performance_sports']
    app.run()
    assert app.checkbox(key='pp_performance_sports_nhl').value
    assert not app.checkbox(key='pp_performance_sports_nba').value
