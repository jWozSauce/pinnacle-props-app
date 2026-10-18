"""Real/paper official and live money panels, isolated for offline AppTest."""
from pathlib import Path
import pandas as pd
import streamlit as st
from .bet_store import BetStore, StoreUnavailable, canonical, duplicate_audit
from .grade import grade_all, live_table
from .live_money import card_money, performance, breakdown, CAPTION
from .settlement_sources import FreeFeed


def render(logged_tab, performance_tab, data_dir, *, store=None, feed=None, clv=None, paper_tab=None):
    if store is None:
        cache_key='pp_bet_store_'+str(data_dir)
        if cache_key not in st.session_state:st.session_state[cache_key]=BetStore(data_dir)
        store=st.session_state[cache_key]
    feed=feed or FreeFeed(Path(data_dir)/'settlement_cache')
    with logged_tab:
        st.subheader('Shared real and paper logs')
        if st.button('Reload shared logs',key='reload_shared'):
            store.cache.clear()
        if st.button('Sync queued bets and migrate local log',key='sync_shared'):
            from .paper import migrate
            result=migrate(store)
            receipts=[store.append(kind,[]) for kind in ('real','paper')]
            if result.get('unresolved'):st.warning('Some legacy records need review; see migration_unresolved.csv.')
            for receipt in receipts:
                (st.warning if receipt['queued'] else st.success)(receipt['message'])
        frames={};audits=[]
        for kind in ('real','paper'):
            frame=store.read(kind,raw=True);frames[kind]=canonical(frame,kind)
            audits.append(duplicate_audit(frame,kind))
            st.write(kind.title()+' bets')
            if store.warning:st.warning(store.warning)
            view=frame.copy()
            if clv is not None and not clv.empty and not view.empty:
                c=clv[['scan_id','row','clv_pp','line_moved','market_pulled']].copy()
                c['row']=pd.to_numeric(c.row,errors='coerce');view['row']=pd.to_numeric(view.row,errors='coerce')
                view=view.merge(c,on=['scan_id','row'],how='left',validate='many_to_one')
            st.dataframe(view,hide_index=True,width='stretch')
            st.download_button('Download '+kind+' log',frame.to_csv(index=False),kind+'_bets.csv','text/csv',key='download_'+kind)
    if paper_tab is not None:
        with paper_tab:
            paper=frames['paper']
            st.subheader('Paper-trade tracker (model performance)')
            metrics=performance(paper)
            a,b,c=st.columns(3)
            a.metric('Paper record',f"{metrics['won']}–{metrics['lost']}–{metrics['push']}")
            b.metric('Paper profit',f"${metrics['profit']:,.2f}")
            c.metric('Paper ROI',f"{100*metrics['roi']:.2f}%" if metrics['roi'] is not None else '—')
            st.caption('First-seen bets only in every metric and round; push and void stakes excluded from ROI.')
            if paper.empty:st.info('No paper rounds yet — use Bet ALL qualifying on the results view.')
            else:
                st.write('By round')
                st.dataframe(breakdown(paper,'round'),hide_index=True,width='stretch')
                st.write('ROI by edge bucket')
                st.dataframe(breakdown(paper,'edge_bucket'),hide_index=True,width='stretch')
                st.dataframe(paper,hide_index=True,width='stretch')
    with performance_tab:
        with st.expander('Duplicate audit and canonical first-seen view'):
            audit=pd.concat(audits,ignore_index=True)
            if audit.empty:st.caption('No duplicate keys found.')
            else:
                st.warning('Duplicate keys excluded from every metric; raw logs retain every row.')
                st.dataframe(audit,hide_index=True,width='stretch')
            st.caption('Paper key: event, player, market, line, side. Real key: row_id. The first stored occurrence is canonical.')
            for kind in ('real','paper'):
                st.write(kind.title()+' canonical rows')
                st.dataframe(frames[kind],hide_index=True,width='stretch')
        if st.button('Grade pending bets (real + paper)',key='grade_pending'):
            try:
                result=grade_all(data_dir=data_dir,store=store,feed=feed)
                st.success('Updated '+str(sum(v['updated'] for v in result['logs'].values()))+' rows.')
                st.rerun()
            except (ValueError,OSError):st.error('Grading could not finish. Reload shared logs before retrying; saved records are retained.')
        st.button('Refresh live status',key='refresh_live')
        st.caption(CAPTION)
        tables={}
        for column,kind in zip(st.columns(2),('real','paper')):
            card,actual,projected=card_money(live_table(frames[kind],feed))
            tables[kind]=card
            metrics=performance(frames[kind])
            with column:
                st.subheader(kind.title())
                st.metric('Official P/L',f'${actual:,.2f}')
                st.metric('Projected P/L',f'${projected:,.2f}')
                st.metric('W–L–P',f"{metrics['won']}–{metrics['lost']}–{metrics['push']}")
                st.metric('Pending',metrics['pending'])
                st.metric('ROI',f"{100*metrics['roi']:.2f}%" if metrics['roi'] is not None else '—')
                st.caption(f"{metrics['void']} voids · ROI excludes push and void stakes")
        kind=st.radio('Performance log',['real','paper'],format_func=str.title,horizontal=True,key='performance_log')
        card=tables[kind];frame=frames[kind]
        if card.empty:st.info('No '+kind+' bets recorded yet.');return
        pending=card[card.status=='pending'].copy()
        pending['bet']=pending.apply(lambda r:f"{r.player} · {r.market} · {r.side} {r.line}",axis=1)
        st.dataframe(pending[['verdict','score','detail','bet','book','stake','price_decimal','to_win','proj_contrib']],hide_index=True,width='stretch')
        for field in ['sport','market','side','book','rule_version','edge_bucket']:
            with st.expander('By '+field.replace('_',' ')):
                st.dataframe(breakdown(frame,field),hide_index=True,width='stretch')
        settled=frame[frame.status.isin(['won','lost','push','void'])].copy()
        if not settled.empty:
            settled['profit']=pd.to_numeric(settled.profit)
            curve=settled.groupby('game_date').profit.sum().sort_index().cumsum()
            st.line_chart(curve.rename('Cumulative P/L'))
        st.caption('Shared closing prices and CLV will be available when the archive connection is complete.')
