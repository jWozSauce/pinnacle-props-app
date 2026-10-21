"""Outcome-blind, same-snapshot advisory flags. No eligibility or stake changes.

Edges and probability disagreements are reported in percentage points. Duplicate
and alternate-key quotes cast one median vote per bookmaker and exact contract.
"""
import os
import numpy as np
import pandas as pd

VERSION=2
EXCHANGES=('novig','prophetx','polymarket','kalshi','polymarket_us')
LEGACY_FIELDS=['books_q','cons_edge','island_flag','sharp_books_q','sharp_gap_pp','cons_line','pin_age_min']
FIELDS=LEGACY_FIELDS+['market_gap_pp','market_edge_pp','sharp_edge_pp','market_books_q','advisory_version']
LEGACY_LEGEND='🏝 ISLAND: one book off market · ⚠️ PINNACLE-OFF: market disagrees with Pinnacle · 📏 LINE?: different consensus line · ⚠️ SHARP-OFF: exchanges disagree. Advisory only; Pinnacle age is shown in minutes.'
LEGEND='\n\n'.join([
    '🏝 ISLAND: this bet book is off the other sportsbooks at the same line.',
    '📏 LINE?: Pinnacle and the other books show different lines.',
    'Market: other sportsbooks’ fair consensus, excluding the bet book (at least three two-sided quotes).',
    'Exchanges: available exchanges’ fair consensus (at least one two-sided quote).',
    'With Pinnacle: agrees or leans further toward the bet (no more than 1 pp lower). Against Pinnacle: more than 1 pp lower toward the target book. Signed gaps and consensus edges are in full details. Advisory only; Pinnacle age stays visible.',
])

def direction(label,gap):
    return f'{label} with Pinnacle' if gap>=-1.-1e-10 else f'{label} against Pinnacle'


def enabled():return os.environ.get('PP_CONSENSUS_ENABLED','1')!='0'


def observation_books():
    # Kalshi joins automatically only after a catalog refresh observes props.
    from .book_catalog import load
    catalog=load()['books']
    return [b for b in EXCHANGES if b in ('novig','prophetx','polymarket') or
            any(v.get('markets') for v in catalog.get(b,{}).get('sports',{}).values())]


def request_books(settings):
    return list(dict.fromkeys([settings['reference_book']]+settings['books']+settings.get('observation_books',[])))


def defaults(frame,version=VERSION):
    out=frame.copy()
    for key in (LEGACY_FIELDS if version==1 else FIELDS):
        if key not in out:out[key]='' if key=='island_flag' else np.nan
    return out


def annotate(flagged,raw,*,reference='pinnacle',island_pp=1.,off_pp=3.,capture_time=None,version=VERSION):
    if 'scan_id' in raw and raw.scan_id.nunique(dropna=False)>1:
        raise ValueError('Consensus requires one snapshot')
    if version not in (1,2):raise ValueError('Unsupported advisory version')
    out=defaults(flagged,version)
    if version==2:out['advisory_version']=2
    if out.empty:return out
    q=raw.copy();q['market']=q.market.str.removesuffix('_alternate')
    q['side']=q.side.str.lower()
    for col in ('point','price'):q[col]=pd.to_numeric(q[col],errors='coerce')
    q=q[np.isfinite(q.point)&np.isfinite(q.price)&q.price.gt(1)&q.side.isin(['over','under'])]
    if capture_time is not None and 'last_update' in q:
        stamp=pd.to_datetime(q.last_update,utc=True,errors='coerce')
        q=q[stamp.notna() & stamp.le(pd.Timestamp(capture_time))]
    keys=['sport_key','event_id','player','market']
    q=q.dropna(subset=keys+['book'])
    q['implied']=1/q.price
    votes=q.groupby(keys+['point','side','book'],sort=False).implied.median().reset_index()
    soft=votes[~votes.book.isin([reference,*EXCHANGES])]
    groups={k:g for k,g in soft.groupby(keys+['point','side'],sort=False)}
    # Each book casts one line vote, regardless of ladder size or repeated outcomes.
    lines=q[~q.book.isin([reference,*EXCHANGES])].groupby(keys+['book'],sort=False).point.median().reset_index()
    line_groups={k:g for k,g in lines.groupby(keys,sort=False)}
    sharp=votes[votes.book.isin(EXCHANGES)].pivot(index=keys+['point','book'],columns='side',values='implied')
    sharp_groups={}
    if {'over','under'}.issubset(sharp.columns):
        sharp=sharp.dropna(subset=['over','under']).copy()
        sharp['fair_over']=sharp.over-(sharp.over+sharp.under-1)/2
        sharp=sharp[sharp.fair_over.gt(0)&sharp.fair_over.lt(1)].reset_index()
        sharp_groups={k:g for k,g in sharp.groupby(keys+['point'],sort=False)}
    market_groups={}
    if version==2:
        market=soft.pivot(index=keys+['point','book'],columns='side',values='implied')
        if {'over','under'}.issubset(market.columns):
            market=market.dropna(subset=['over','under']).copy()
            market['fair_over']=market.over-(market.over+market.under-1)/2
            market=market[market.fair_over.gt(0)&market.fair_over.lt(1)].reset_index()
            market_groups={k:g for k,g in market.groupby(keys+['point'],sort=False)}
    for idx,r in out.iterrows():
        key=(r.sport_key,r.event_id,r.player_name,str(r.prop_type).removesuffix('_alternate'))
        fair=float(r['noVig'+r.side.title()]);labels=[]
        peers=groups.get((*key,r.Target_line,r.side))
        n=0;edge=np.nan
        if peers is not None:
            peers=peers[peers.book.ne(r.book)];n=len(peers)
            if n:edge=100*(fair-peers.implied.median())
        out.at[idx,'books_q']=n;out.at[idx,'cons_edge']=edge
        if n>=3:
            if bool(r.get('rule_pass',False)) and edge<island_pp:labels.append('🏝 ISLAND')
            if version==1 and edge>=off_pp:labels.append('⚠️ PINNACLE-OFF')
        if version==2:
            peers=market_groups.get((*key,r.Target_line))
            out.at[idx,'market_books_q']=0
            if peers is not None:
                peers=peers[peers.book.ne(r.book)]
                out.at[idx,'market_books_q']=len(peers)
                if len(peers):
                    p=float(peers.fair_over.median());p=p if r.side=='over' else 1-p
                    gap=100*(p-fair);target_edge=100*float(r.edge)+gap
                    out.at[idx,'market_gap_pp']=gap;out.at[idx,'market_edge_pp']=target_edge
                    if len(peers)>=3:labels.append(direction('Market',gap))
        peers=line_groups.get(key)
        if peers is not None:
            peers=peers[peers.book.ne(r.book)]
            if not peers.empty:
                line=float(peers.point.median());out.at[idx,'cons_line']=line
                if line!=float(r.PIN_line):labels.append('📏 LINE?')
        peers=sharp_groups.get((*key,r.Target_line));out.at[idx,'sharp_books_q']=0
        if peers is not None and not peers.empty:
            p=peers.fair_over.median();p=p if r.side=='over' else 1-p
            gap=100*(fair-p) if version==1 else 100*(p-fair)
            out.at[idx,'sharp_books_q']=len(peers);out.at[idx,'sharp_gap_pp']=gap
            if version==2:out.at[idx,'sharp_edge_pp']=100*float(r.edge)+gap
            if version==2:labels.append(direction('Exchanges',gap))
            elif abs(gap)>=3.-1e-10:labels.append('⚠️ SHARP-OFF')
        out.at[idx,'island_flag']=' · '.join(labels)
    return out
