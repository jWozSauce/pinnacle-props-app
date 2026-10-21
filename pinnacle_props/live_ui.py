"""Today's real and paper live card, using bounded free feeds only."""
from pathlib import Path
import pandas as pd
import streamlit as st
from .bet_store import session_store,canonical
from .grade import live_table
from .live_money import card_money,CAPTION
from .settlement_sources import FreeFeed
from .game_time import utcnow

EMPTY='No bets to track yet. Bets you log, and paper bets the scanner tracks, appear here from their start time and update live for NHL and NBA.'


def cards(store,feed):
    now=pd.Timestamp(utcnow());today=now.tz_convert('America/New_York').date()
    output={};active=False
    for kind in ('real','paper'):
        frame=canonical(store.read(kind,reload=True),kind)
        starts=pd.to_datetime(frame.start_time,utc=True,errors='coerce')
        frame=frame[starts.le(now)&starts.dt.tz_convert('America/New_York').dt.date.eq(today)].copy()
        card,official,projected=card_money(live_table(frame,feed))
        stages=card.get('stage',pd.Series('',index=card.index))
        active=active or bool(stages.isin(['live','waiting']).any())
        output[kind]=(card,official,projected)
    return output,active


def render_live(data_dir,*,store=None,feed=None):
    store=store if store is not None else session_store(data_dir)
    feed=feed if feed is not None else FreeFeed(Path(data_dir)/'settlement_cache')
    initial,active=cards(store,feed)
    @st.fragment(run_every=60 if active else None)
    def panel():
        current,current_active=cards(store,feed)
        # Re-register the timer when a tracked game enters/leaves live play.
        if current_active!=active:st.rerun(scope='app')
        counts={k:int(v[0].status.eq('pending').sum()) for k,v in current.items()}
        st.subheader('Live')
        if store.warning:st.warning(store.warning)
        kind=st.radio('Track bets',['real','paper'],index=1 if counts['real']==0 and counts['paper'] else 0,
            format_func=lambda k:f'{k.title()} ({counts[k]})',horizontal=True,key='live_log')
        card,official,projected=current[kind]
        a,b=st.columns(2);a.metric('Official P/L',f'${official:,.2f}');b.metric('Projected P/L',f'${projected:,.2f}')
        st.caption(CAPTION)
        st.button('Refresh now',key='refresh_live')
        if active:st.caption('Updates every 60 seconds while a tracked game is in progress.')
        pending=card[card.status.eq('pending')].copy()
        if pending.empty:st.info(EMPTY)
        else:
            pending['bet']=pending.apply(lambda r:f"{r.player} · {r.market} · {r.side} {r.line}",axis=1)
            st.dataframe(pending[['verdict','score','detail','bet','book','stake','price_decimal','to_win']],hide_index=True,width='stretch')
    panel()
