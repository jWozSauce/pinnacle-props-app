"""Readable, all-status bet rows shared by Live, Logged and Performance."""
import math
import pandas as pd
import streamlit as st
from .odds import decimal_to_american
from .settle import metadata, OFFICIAL
from .live_money import performance

LABELS={'won':'✅ won (official)','lost':'❌ lost (official)','push':'🟨 push (official)','void':'⚪ void (official)'}
RECORDED_FLAGS=['⚠','already_bet','same_game','island_flag','books_q','cons_edge','sharp_books_q','sharp_gap_pp','market_gap_pp','market_edge_pp','sharp_edge_pp','market_books_q','advisory_version','cons_line','pin_age_min']


def eastern(series):
    return pd.to_datetime(series,utc=True,errors='coerce',format='mixed').dt.tz_convert('America/New_York').dt.strftime('%Y-%m-%d %H:%M %Z').fillna('—')


def display(frame,*,live=False):
    f=frame.copy()
    f['bet']=f.apply(lambda r:f"{r.player} · {r.market} · {r.side} {r.line}",axis=1) if len(f) else pd.Series(dtype=str)
    f['logged at']=eastern(f.logged_at);f['start']=eastern(f.start_time)
    f['matchup']=f.away_team.astype(str)+' @ '+f.home_team.astype(str)
    def american(r):
        try:
            value=float(r.odds_american) if str(r.odds_american).strip() else decimal_to_american(r.price_decimal)
            return f'{value:+.0f}' if math.isfinite(value) and abs(value)>=100 else '—'
        except (ValueError,TypeError):return '—'
    f['odds']=f.apply(american,axis=1) if len(f) else pd.Series(dtype=str)
    f['stake']=pd.to_numeric(f.stake,errors='coerce')
    f['to_win']=(f.stake*(pd.to_numeric(f.price_decimal,errors='coerce')-1)).round(2)
    f['profit']=pd.to_numeric(f.profit,errors='coerce')
    f['import_clv_pp']=[metadata(r).get('import',{}).get('clv_pp') for r in frame.to_dict('records')]
    fields=(['verdict','bet','detail','score'] if live else ['status','bet','profit'])+['book','logged at','start','matchup','stake','odds','to_win']
    fields += [x for x in RECORDED_FLAGS if x in f]
    if not live:fields+=['edge','fair_p','clv_pp','line_moved','market_pulled','import_clv_pp','note']
    return f[[x for x in fields if x in f]]


def table(frame,*,live=False):
    st.dataframe(display(frame,live=live),hide_index=True,width='stretch',column_config={
        'stake':st.column_config.NumberColumn('stake',format='$%.2f'),
        'profit':st.column_config.NumberColumn('profit',format='$%.2f'),
        'to_win':st.column_config.NumberColumn('to_win',format='$%.2f',help='Potential win profit, excluding returned stake')})


def overall(frame,canonical_frame,*,kind,key,clv=None):
    st.subheader('Real bets' if kind=='real' else 'Paper-trade tracker (model performance)')
    m=performance(canonical_frame)
    settled=canonical_frame.status.isin(OFFICIAL)
    staked=pd.to_numeric(canonical_frame.loc[settled,'stake'],errors='coerce').sum()
    a,b,c,d=st.columns(4)
    a.metric('Record',f"{m['won']}–{m['lost']}–{m['push']}");b.metric('Staked',f'${staked:,.2f}')
    c.metric('Profit',f"${m['profit']:,.2f}");d.metric('ROI',f"{100*m['roi']:+.2f}%" if m['roi'] is not None else '—')
    st.caption(f"{m['pending']} pending · Staked covers settled rows; ROI excludes push and void stakes. Duplicate rows count once in metrics.")
    # Stable ordering retains original order for equal/unknown timestamps.
    ordered=frame.assign(_at=pd.to_datetime(frame.logged_at,utc=True,errors='coerce',format='mixed')).sort_values('_at',ascending=False,kind='stable').drop(columns='_at')
    if clv is not None and not clv.empty and not ordered.empty:
        c=clv[['scan_id','row','clv_pp','line_moved','market_pulled']].copy()
        c['row']=pd.to_numeric(c.row,errors='coerce');ordered['row']=pd.to_numeric(ordered.row,errors='coerce')
        ordered=ordered.merge(c,on=['scan_id','row'],how='left',validate='many_to_one')
    if ordered.empty:st.info('No '+kind+' bets recorded yet.')
    else:table(ordered)
    st.download_button('Download log CSV' if kind=='real' else 'Download paper log CSV',frame.to_csv(index=False),kind+'_bets.csv','text/csv',key=key+'_download_'+kind)
