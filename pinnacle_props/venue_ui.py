"""Per-bet expandable quote details, shared by scan and recovery views."""
import json
import pandas as pd
import streamlit as st
from .venue_details import finite,saved_details
from .odds import decimal_to_american
from .consensus import EXCHANGES,direction


NAMES={'novig':'Novig','prophetx':'ProphetX','polymarket':'Polymarket','kalshi':'Kalshi','polymarket_us':'Polymarket US'}

def verdict(v,book):
    if not v['same_line']:return 'Different line'
    if v['venue']=='pinnacle':return 'Reference'
    if v['venue']==book:return 'Your bet'
    if not finite(v.get('gap_pp')):return 'No two-sided quote'
    return direction('',float(v['gap_pp'])).strip().capitalize()


def venue_summary(venues,book):
    same={v['venue']:v for v in venues if v['same_line']}
    exchange=[]
    for name in EXCHANGES:
        if name=='polymarket_us' and name not in same:continue
        v=same.get(name);label=verdict(v,book).split()[0].lower() if v and finite(v.get('gap_pp')) else 'no quote'
        exchange.append(NAMES[name]+' '+label)
    market=[verdict(v,book) for name,v in same.items() if name not in ('pinnacle',book,*EXCHANGES)]
    return 'Exchanges: '+', '.join(exchange)+f" · Market: {market.count('With pinnacle')} with, {market.count('Against pinnacle')} against"


def display_rows(rows,book):
    display=[]
    for v in rows:
        price=v.get('price');american=f'{decimal_to_american(price):+.0f}' if finite(price) and float(price)>1 else '—'
        display.append({'Venue':v['venue'],'Verdict':verdict(v,book).replace('pinnacle','Pinnacle'),
            'Line':v['line'],'Price (American)':american,
            'Implied probability (%)':100/float(price) if finite(price) and float(price)>1 else None,
            'Fair probability (%)':100*v['fair_p'] if v.get('fair_p') is not None else None,
            'Gap from Pinnacle (pp)':v.get('gap_pp'),'Quote age (min)':v.get('age_min'),
            'Price (decimal)':price,'Over (decimal)':v.get('over'),'Under (decimal)':v.get('under')})
    return pd.DataFrame(display)


def second_line(edge,amount):
    if finite(edge) and finite(amount):st.caption(f'Blended (Pinnacle + exchanges): edge {float(edge):+.2f} pp, stake ${float(amount):,.2f}')
    else:st.caption('Blended (Pinnacle + exchanges): no exchange quote')


def panel(row,*,saved=False):
    if saved:
        provenance=saved_details(row);venues=provenance.get('venues',[])
        player=row['player'];market=row['market'];line=row['line'];side=row['side']
        edge=row.get('edge');amount=provenance.get('suggested_stake',row.get('stake'))
    else:
        player=row['player_name'];market=row['prop_type'];line=row['Target_line'];side=row['side']
        edge=row.get('edge');amount=row.get('kellyAmount')
        try:venues=json.loads(row.get('venue_quotes','[]'))
        except (ValueError,TypeError):venues=[]
    st.write(f'{player} · {market} · {side} {line}')
    if finite(edge) and finite(amount):st.write(f'Pinnacle: edge {100*float(edge):+.2f} pp, stake ${float(amount):,.2f}')
    second_line(row.get('blend_edge_pp'),row.get('blend_stake'))
    st.caption('Blended suggestion uses Pinnacle plus Novig/ProphetX at this exact line. Pinnacle remains primary for recommendations and logging.')
    explanations={'ISLAND':'this book clears the rule and at least three other books agree with Pinnacle',
        'LINE?':'other books show a different line; those quotes are separated below',
        'Market with Pinnacle':'other sportsbooks agree with or lean further toward this bet',
        'Market against Pinnacle':'other sportsbooks give this side a lower probability',
        'Exchanges with Pinnacle':'exchange consensus agrees with or leans further toward this bet',
        'Exchanges against Pinnacle':'exchange consensus gives this side a lower probability'}
    flags=str(row.get('island_flag',''))
    for label,meaning in explanations.items():
        if label in flags:st.caption(label+': '+meaning)
    if not venues:st.info('Venue quotes were not saved with this older recommendation. Missing prices are not reconstructed.');return
    st.caption(venue_summary(venues,row['book']))
    def table(rows):
        st.dataframe(display_rows(rows,row['book']),hide_index=True,width='stretch')

    same=[v for v in venues if v['same_line']];other=[v for v in venues if not v['same_line']]
    if same:table(same)
    if other:st.divider();st.caption('Different lines · gaps compare different thresholds, not the same contract');table(other)


def expand(row,*,saved=False,title='Compare venues'):
    with st.expander(title):panel(row,saved=saved)


def table_rows(frame):
    """Native HTML disclosure rows: the chevron opens directly inside its table row."""
    from html import escape
    pages=max(1,(len(frame)+19)//20)
    page=st.selectbox('Table page',range(1,pages+1),key='venue_table_page') if pages>1 else 1
    chunks=['<style>.venue-row{border-bottom:1px solid #87929d55;padding:.55rem}.venue-row summary{cursor:pointer}.venue-panel{overflow-x:auto;padding:.6rem}.venue-panel table{border-collapse:collapse;white-space:nowrap}.venue-panel th,.venue-panel td{padding:.3rem .6rem;text-align:right}.venue-panel th:first-child,.venue-panel td:first-child{text-align:left}</style>']
    for _,r in frame.iloc[(page-1)*20:page*20].iterrows():
        price=r.get('target_'+r.side+'_decimal');american=f'{decimal_to_american(price):+.0f}' if finite(price) and float(price)>1 else '—'
        summary=f"{r.player_name} · {r.side} {r.Target_line:g} · {r.prop_type.removeprefix('player_')} · {r.book} {american} · edge {100*r.edge:+.2f} pp · stake ${r.kellyAmount:.2f}"
        try:venues=json.loads(r.get('venue_quotes','[]'))
        except (ValueError,TypeError):venues=[]
        chunks.append('<details class="venue-row"><summary>'+escape(summary)+'</summary><div class="venue-panel">')
        chunks.append(f'<p>Pinnacle: edge {100*r.edge:+.2f} pp, stake ${r.kellyAmount:.2f}</p>')
        blend=f"edge {float(r.blend_edge_pp):+.2f} pp, stake ${float(r.blend_stake):.2f}" if finite(r.get('blend_edge_pp')) and finite(r.get('blend_stake')) else 'no exchange quote'
        chunks.append('<p>Blended (Pinnacle + exchanges): '+blend+'</p>')
        chunks.append('<p>'+escape(venue_summary(venues,r.book))+'</p>')
        chunks.append('<p>'+escape(str(r.get('island_flag','')))+'</p><p>With Pinnacle = at least Pinnacle minus 1 pp; against = more than 1 pp lower toward your bet book. LINE? = different thresholds below. ISLAND = this book clears the rule and at least three other books agree with Pinnacle.</p>')
        for same in (True,False):
            selected=[v for v in venues if v['same_line']==same]
            if not selected:continue
            if not same:chunks.append('<hr><p>Different lines · gaps compare different thresholds, not the same contract</p>')
            display=display_rows(selected,r.book)
            chunks.append(display.to_html(index=False,border=0,escape=True,na_rep='—',float_format=lambda v:f'{v:.2f}'))
        if not venues:chunks.append('<p>Venue quotes unavailable in this saved scan.</p>')
        chunks.append('</div></details>')
    st.html(''.join(chunks))
