"""Itemised real and paper logs with official summaries and existing breakdowns."""
from pathlib import Path
import pandas as pd
import streamlit as st
from .bet_store import BetStore, session_store, canonical, duplicate_audit
from .grade import grade_all, live_table
from .live_money import breakdown
from .settlement_sources import FreeFeed
from .log_tables import overall
from .sport_filter import select_sports, filter_rows, show_sport_summary


def render(logged_tab, performance_tab, data_dir, *, store=None, feed=None, clv=None, paper_tab=None):
    if store is None:
        cache_key='pp_bet_store_'+str(data_dir)
        if cache_key not in st.session_state:st.session_state[cache_key]=BetStore(data_dir,interactive=True)
        store=st.session_state[cache_key]
    feed=feed or FreeFeed(Path(data_dir)/'settlement_cache')
    # Controls are processed before reading either panel, so both refresh together.
    refresh=False
    selections={}
    for tab,key in [(logged_tab,'logged'),(performance_tab,'performance')]:
        with tab:
            selections[key]=select_sports(key)
            refresh=st.button('Refresh log',key=key+'_refresh_log') or refresh
    if refresh:store.invalidate()
    with logged_tab:
        if st.button('Sync queued bets and migrate local log',key='sync_shared'):
            from .paper import migrate
            result=migrate(store)
            if result.get('unresolved'):st.warning('Some legacy records need review; see migration_unresolved.csv.')
            for kind in ('real','paper'):
                receipt=store.append(kind,[])
                (st.warning if receipt['queued'] else st.success)(receipt['message'])
    with performance_tab:
        if st.button('Grade pending bets (real + paper)',key='grade_pending'):
            try:
                result=grade_all(data_dir=data_dir,store=store,feed=feed)
                st.success('Updated '+str(sum(v['updated'] for v in result['logs'].values()))+' rows.')
                st.rerun()
            except (ValueError,OSError):st.error('Grading could not finish. Reload shared logs before retrying; saved records are retained.')
    raw={};frames={};warnings=[]
    for kind in ('real','paper'):
        raw[kind]=store.read(kind,raw=True);frames[kind]=canonical(raw[kind],kind)
        if store.warning:warnings.append(store.warning)
    with logged_tab:
        from .recommendation_ui import recent_panel
        recent_panel(frames['paper'],store)
    for tab,key in [(logged_tab,'logged'),(performance_tab,'performance')]:
        with tab:
            selected_raw={kind:filter_rows(f,selections[key]) for kind,f in raw.items()}
            selected={kind:filter_rows(f,selections[key]) for kind,f in frames.items()}
            show_sport_summary(frames)
            for warning in set(warnings):st.warning(warning)
            overall(selected_raw['real'],selected['real'],kind='real',key=key,clv=clv)
            st.divider()
            overall(selected_raw['paper'],selected['paper'],kind='paper',key=key,clv=clv)
            if not selected['paper'].empty:
                st.write('By round');st.dataframe(breakdown(selected['paper'],'round'),hide_index=True,width='stretch')
                st.write('ROI by edge bucket');st.dataframe(breakdown(selected['paper'],'edge_bucket'),hide_index=True,width='stretch')
    if paper_tab is not None:
        with paper_tab:
            sports=select_sports('paper')
            selected={'paper':filter_rows(frames['paper'],sports)}
            from .recommendation_ui import forms
            forms(selected['paper'],store,prefix='paper_recover')
            overall(filter_rows(raw['paper'],sports),selected['paper'],kind='paper',key='paper_tab',clv=clv)
            if not selected['paper'].empty:
                st.write('By round');st.dataframe(breakdown(selected['paper'],'round'),hide_index=True,width='stretch')
                st.write('ROI by edge bucket');st.dataframe(breakdown(selected['paper'],'edge_bucket'),hide_index=True,width='stretch')
    with performance_tab:
        with st.expander('Duplicate audit'):
            audit=pd.concat([duplicate_audit(filter_rows(raw[k],selections['performance']),k) for k in ('real','paper')],ignore_index=True)
            if audit.empty:st.caption('No duplicate keys found.')
            else:st.warning('Duplicate rows appear in the tables and CSV; metrics count each identity once.');st.dataframe(audit,hide_index=True,width='stretch')
        kind=st.radio('Performance log',['real','paper'],format_func=str.title,horizontal=True,key='performance_log')
        frame=filter_rows(frames[kind],selections['performance'])
        if frame.empty:return
        for field in ['sport','market','side','book','rule_version','edge_bucket']:
            with st.expander('By '+field.replace('_',' ')):
                st.dataframe(breakdown(frame,field),hide_index=True,width='stretch')
        settled=frame[frame.status.isin(['won','lost','push','void'])].copy()
        if not settled.empty:
            settled['profit']=pd.to_numeric(settled.profit)
            curve=settled.groupby('game_date').profit.sum().sort_index().cumsum()
            st.line_chart(curve.rename('Cumulative P/L'))
