"""Paper-to-real forms, including saved recommendation deep links."""
import uuid
import math
import pandas as pd
import streamlit as st
from . import recommendations as rec
from .bet_duplicates import flags, contract
from .bet_store import StoreUnavailable
from .game_time import started
from .odds import american_to_decimal, decimal_to_american


def forms(frame,store,*,prefix):
    if frame.empty:return
    valid=[]
    for i,r in frame.iterrows():
        try:contract(r);uuid.UUID(r['row_id']);decimal_to_american(r['price_decimal']);amount=float(r['stake']);assert math.isfinite(amount) and amount>0;valid.append(i)
        except (ValueError,TypeError,AttributeError,AssertionError):pass
    if len(valid)!=len(frame):st.warning('Some older paper rows lack a verified contract or price. Import their receipt to recover them.')
    frame=frame.loc[valid]
    if frame.empty:return
    keys=store.real_keys();marks=flags(frame,keys)
    query=st.text_input('Find a recommendation',key=prefix+'_find',placeholder='Player, book or market') if len(frame)>10 else ''
    chosen=frame.copy();chosen['_flag']=marks
    if query:chosen=chosen[chosen[['player','book','market']].astype(str).agg(' '.join,axis=1).str.contains(query,case=False,regex=False)]
    # Paginate without removing any historical paper row from reach.
    pages=max(1,(len(chosen)+9)//10)
    page=st.selectbox('Recommendation page',range(1,pages+1),key=prefix+'_page') if pages>1 else 1
    for r in chosen.iloc[(page-1)*10:page*10].to_dict('records'):
        ident=prefix+'_'+r['row_id'];flag=r['_flag']
        title=f"{r['player']} · {r['side']} {r['line']} · {r['market'].removeprefix('player_').replace('_',' ')} · {r['book']}"
        with st.expander((flag+' · ' if flag else '')+title,expanded=prefix=='linked'):
            from .venue_ui import panel
            panel(r,saved=True)
            st.caption(f"{r['away_team']} @ {r['home_team']} · saved {r['logged_at']} · {r['status']}")
            st.caption('Record a bet you already placed. The saved recommendation and its model edge stay frozen; enter your actual fill below.')
            a,b=st.columns(2)
            stake=a.number_input('Actual stake ($)',min_value=.01,value=max(.01,round(float(r['stake']),2)),step=.01,key=ident+'_stake')
            odds=b.number_input('Actual odds (American)',value=float(decimal_to_american(r['price_decimal'])),step=1.,key=ident+'_odds')
            note=st.text_input('Note',key=ident+'_note')
            confirm=st.checkbox('I already placed this bet',key=ident+'_confirm')
            if started(r['start_time']):st.caption('This game has started. Logging records your earlier action; it does not place a bet.')
            disabled=not confirm or keys is None or flag.startswith('🔴')
            if keys is None:st.warning('Already-bet check unavailable; logging is blocked until the shared log can be read.')
            if st.button('Log as real bet',key=ident+'_log',disabled=disabled):
                try:
                    sub=st.session_state.setdefault(ident+'_submission',str(uuid.uuid4()))
                    result=rec.log_paper(store,r['row_id'],stake,american_to_decimal(odds),note=note,submission_id=sub)
                    st.session_state['bet_receipt']=result['message']
                    (st.warning if result.get('queued') else st.success)(result['message'])
                    store.cache.clear();st.rerun()
                except (ValueError,StoreUnavailable):st.error('Bet could not be logged. Check the odds, stake and shared-log connection; no automatic retry.')


def recent_panel(frame,store,*,now=None):
    st.subheader('Recent recommendations')
    selected=rec.recent(frame,now=now)
    st.caption('Last 36 hours · newest first · available across devices even after the scan is gone.')
    if selected.empty:st.info('No paper-tracked recommendations in the last 36 hours.')
    else:forms(selected,store,prefix='recent')


def linked_panel(store):
    wanted=st.query_params.get('recommendation')
    if not wanted:return
    if len(wanted)!=64 or any(c not in '0123456789abcdef' for c in wanted):
        st.warning('This recommendation link is invalid.');return
    rows=store.read('paper',reload=True)
    selected=rows[rows.apply(rec.token,axis=1)==wanted] if not rows.empty else rows
    st.subheader('Saved recommendation')
    if len(selected)!=1:st.warning('This recommendation is not available in the shared paper log. Refresh the log or import your receipt.');return
    forms(selected,store,prefix='linked')
