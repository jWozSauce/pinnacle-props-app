"""Pending bets and the rolling last day of official grades; free feeds only."""
from pathlib import Path
import pandas as pd
import streamlit as st
from .bet_store import session_store,canonical
from .grade import live_table
from .live_money import card_money,CAPTION
from .settlement_sources import FreeFeed
from .game_time import utcnow
from .settle import OFFICIAL
from .log_tables import LABELS,table
from .sport_filter import select_sports, filter_rows

EMPTY='No bets to track yet. Pending bets and bets graded in the last 24 hours appear here; live scores cover NHL and NBA.'
ORDER={'live':0,'waiting':0,'decided':1,'graded':2,'upcoming':3,'unmatched':4,'untracked':4}


def cards(store,feed,sports=None):
    now=pd.Timestamp(utcnow());cutoff=now-pd.Timedelta(days=1)
    output={};active=False
    for kind in ('real','paper'):
        frame=canonical(store.read(kind,reload=True),kind)
        if sports is not None:frame=filter_rows(frame,sports)
        graded=pd.to_datetime(frame.graded_at,utc=True,errors='coerce',format='mixed')
        corrected=pd.to_datetime(frame.regraded_at,utc=True,errors='coerce',format='mixed')
        recent=(graded.ge(cutoff)&graded.le(now))|(corrected.ge(cutoff)&corrected.le(now))
        frame=frame[frame.status.eq('pending')|(frame.status.isin(OFFICIAL)&recent)].copy()
        card,official,projected=card_money(live_table(frame,feed))
        # Card money consumes the live module's final stage before display relabeling.
        if 'stage' not in card:card['stage']='unmatched'
        if 'verdict' not in card:card['verdict']='— no live feed'
        done=card.status.isin(OFFICIAL)
        card.loc[done,'stage']='graded';card.loc[done,'verdict']=card.loc[done,'status'].map(LABELS)
        card['stage']=card.stage.replace({'final':'decided'})
        active=active or bool(card.stage.isin(['live','waiting']).any())
        card=card.assign(_order=card.stage.map(ORDER).fillna(4),_start=pd.to_datetime(card.start_time,utc=True,errors='coerce',format='mixed')).sort_values(['_order','_start'],kind='stable').drop(columns=['_order','_start'])
        output[kind]=(card,official,projected)
    return output,active


def render_live(data_dir,*,store=None,feed=None):
    store=store if store is not None else session_store(data_dir)
    feed=feed if feed is not None else FreeFeed(Path(data_dir)/'settlement_cache')
    sports=select_sports('live')
    initial,active=cards(store,feed,sports)
    @st.fragment(run_every=60 if active else None)
    def panel():
        current,current_active=cards(store,feed,sports)
        if current_active!=active:st.rerun(scope='app')
        st.subheader('Live')
        if store.warning:st.warning(store.warning)
        kind=st.radio('Track bets',['real','paper'],index=0,format_func=lambda k:f'{k.title()} ({len(current[k][0])})',horizontal=True,key='live_log')
        card,official,projected=current[kind]
        a,b=st.columns(2);a.metric('Official P/L',f'${official:,.2f}');b.metric('Projected P/L',f'${projected:,.2f}')
        st.caption(CAPTION)
        a,b,c=st.columns(3)
        a.metric('In progress',int(card.stage.isin(['live','waiting']).sum()))
        done=card[card.stage.isin(['decided','graded'])]
        b.metric('Won / lost (incl. unofficial)',f"{int(done.verdict.str.startswith('✅').sum())}W / {int(done.verdict.str.startswith('❌').sum())}L")
        c.metric('Upcoming',int(card.stage.eq('upcoming').sum()))
        st.button('Refresh now',key='refresh_live')
        st.caption('All pending bets plus bets graded in the last 24 hours; times are Eastern.')
        if active:st.caption('Updates every 60 seconds while a tracked game is in progress.')
        if card.empty:st.info(EMPTY)
        else:
            table(card,live=True)
            if kind=='paper':
                from .recommendation_ui import forms
                forms(card,store,prefix='live_recover')
    panel()
