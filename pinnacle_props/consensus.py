"""Outcome-blind, same-snapshot advisory flags. No eligibility or stake changes.

Edges and probability disagreements are reported in percentage points. Duplicate
and alternate-key quotes cast one median vote per bookmaker and exact contract.
"""
import os
import numpy as np
import pandas as pd

VERSION=3
EXCHANGES=('novig','prophetx','polymarket','kalshi','polymarket_us')
LEGACY_FIELDS=['books_q','cons_edge','island_flag','sharp_books_q','sharp_gap_pp','cons_line','pin_age_min']
FIELDS=LEGACY_FIELDS+['market_gap_pp','market_edge_pp','sharp_edge_pp','market_books_q','advisory_version']
LEGACY_LEGEND='🏝 ISLAND: one book off market · ⚠️ PINNACLE-OFF: market disagrees with Pinnacle · 📏 LINE?: different consensus line · ⚠️ SHARP-OFF: exchanges disagree. Advisory only; Pinnacle age is shown in minutes.'
SIGNED_LEGEND='\n\n'.join([
    '🏝 ISLAND: this bet book is off the other sportsbooks at the same line.',
    '📏 LINE?: Pinnacle and the other books show different lines.',
    'Market: other sportsbooks’ fair consensus, excluding the bet book (at least three two-sided quotes).',
    'Exchanges: available exchanges’ fair consensus (at least one two-sided quote).',
    'With Pinnacle: agrees or leans further toward the bet (no more than 1 pp lower). Against Pinnacle: more than 1 pp lower toward the target book. Signed gaps and consensus edges are in full details. Advisory only; Pinnacle age stays visible.',
])

LEGEND=SIGNED_LEGEND.replace('this bet book is off the other sportsbooks at the same line.', 'this book clears the rule and at least three other sportsbooks’ fair consensus is with Pinnacle. Never combined with Market against Pinnacle.')

def market_labels(gap,count,rule_pass):
    if count<3 or not np.isfinite(gap):return []
    label=direction('Market',gap)
    return (['🏝 ISLAND'] if rule_pass and label=='Market with Pinnacle' else [])+[label]

def direction(label,gap):
    return f'{label} with Pinnacle' if gap>=-1.-1e-10 else f'{label} against Pinnacle'


def enabled():return os.environ.get('PP_CONSENSUS_ENABLED','1')!='0'


def observation_books(catalog=None):
    # Kalshi joins automatically only after a catalog refresh observes props.
    from .book_catalog import load
    catalog=(catalog if catalog is not None else load())['books']
    return [b for b in EXCHANGES if b in ('novig','prophetx','polymarket') or
            any(v.get('markets') for v in catalog.get(b,{}).get('sports',{}).values())]


def request_books(settings):
    return list(dict.fromkeys([settings['reference_book']]+settings['books']+settings.get('observation_books',[])))


def defaults(frame,version=VERSION):
    out=frame.copy()
    for key in (LEGACY_FIELDS if version==1 else FIELDS):
        if key not in out:out[key]='' if key=='island_flag' else np.nan
    return out


def peer_index(frame, keys, value):
    """Precompute every leave-one-book-out median with vectorized rank lookup.

    Input already has one vote per book. Removing a rank shifts the two median
    indices by at most one; no per-opportunity DataFrame filtering is needed.
    """
    result={}
    for key,group in frame.groupby(keys,sort=False):
        ordered=group.sort_values(value,kind='stable')
        values=ordered[value].to_numpy();n=len(values)
        rank=np.arange(n);count=n-1
        if count:
            lo=(count-1)//2;hi=count//2
            medians=(values[lo+(rank<=lo)]+values[hi+(rank<=hi)])/2
        else:medians=np.full(n,np.nan)
        result[key]=(float(np.median(values)),n,dict(zip(ordered.book,medians)))
    return result


def peer_value(index,key,book):
    found=index.get(key)
    if found is None:return 0,np.nan
    median,count,without=found
    return (count-1,without[book]) if book in without else (count,median)


def annotate(flagged,raw,*,reference='pinnacle',island_pp=1.,off_pp=3.,capture_time=None,version=VERSION):
    if 'scan_id' in raw and raw.scan_id.nunique(dropna=False)>1:
        raise ValueError('Consensus requires one snapshot')
    if version not in (1,2,3):raise ValueError('Unsupported advisory version')
    out=defaults(flagged,version)
    if version>=2:out['advisory_version']=version
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
    needed=pd.MultiIndex.from_arrays([out.sport_key,out.event_id,out.player_name,out.prop_type.str.removesuffix('_alternate')])
    q=q[pd.MultiIndex.from_frame(q[keys]).isin(needed)]
    q['implied']=1/q.price
    votes=q.groupby(keys+['point','side','book'],sort=False).implied.median().reset_index()
    soft=votes[~votes.book.isin([reference,*EXCHANGES])]
    groups=peer_index(soft,keys+['point','side'],'implied')
    # Each book casts one line vote, regardless of ladder size or repeated outcomes.
    lines=q[~q.book.isin([reference,*EXCHANGES])].groupby(keys+['book'],sort=False).point.median().reset_index()
    line_groups=peer_index(lines,keys,'point')
    sharp=votes[votes.book.isin(EXCHANGES)].pivot(index=keys+['point','book'],columns='side',values='implied')
    sharp_groups={}
    if {'over','under'}.issubset(sharp.columns):
        sharp=sharp.dropna(subset=['over','under']).copy()
        sharp['fair_over']=sharp.over-(sharp.over+sharp.under-1)/2
        sharp=sharp[sharp.fair_over.gt(0)&sharp.fair_over.lt(1)].reset_index()
        sharp_groups={k:(len(g),float(g.fair_over.median())) for k,g in sharp.groupby(keys+['point'],sort=False)}
    market_groups={}
    if version>=2:
        market=soft.pivot(index=keys+['point','book'],columns='side',values='implied')
        if {'over','under'}.issubset(market.columns):
            market=market.dropna(subset=['over','under']).copy()
            market['fair_over']=market.over-(market.over+market.under-1)/2
            market=market[market.fair_over.gt(0)&market.fair_over.lt(1)].reset_index()
            market_groups=peer_index(market,keys+['point'],'fair_over')
    values={field:out[field].tolist() for field in (LEGACY_FIELDS if version==1 else FIELDS)}
    for pos,r in enumerate(out.itertuples(index=False)):
        key=(r.sport_key,r.event_id,r.player_name,str(r.prop_type).removesuffix('_alternate'))
        fair=float(getattr(r,'noVig'+r.side.title()));labels=[]
        n,median=peer_value(groups,(*key,r.Target_line,r.side),r.book)
        edge=100*(fair-median) if n else np.nan
        values['books_q'][pos]=n;values['cons_edge'][pos]=edge
        if version<3 and n>=3:
            if bool(getattr(r,'rule_pass',False)) and edge<island_pp:labels.append('🏝 ISLAND')
            if version==1 and edge>=off_pp:labels.append('⚠️ PINNACLE-OFF')
        if version>=2:
            count,p=peer_value(market_groups,(*key,r.Target_line),r.book)
            values['market_books_q'][pos]=count
            if count:
                p=p if r.side=='over' else 1-p
                gap=100*(p-fair);target_edge=100*float(r.edge)+gap
                values['market_gap_pp'][pos]=gap;values['market_edge_pp'][pos]=target_edge
                if version>=3:labels.extend(market_labels(gap,count,bool(getattr(r,'rule_pass',False))))
                elif count>=3:labels.append(direction('Market',gap))
        count,line=peer_value(line_groups,key,r.book)
        if count:
            values['cons_line'][pos]=line
            if line!=float(r.PIN_line):labels.append('📏 LINE?')
        count,p=sharp_groups.get((*key,r.Target_line),(0,np.nan));values['sharp_books_q'][pos]=count
        if count:
            p=p if r.side=='over' else 1-p
            gap=100*(fair-p) if version==1 else 100*(p-fair)
            values['sharp_gap_pp'][pos]=gap
            if version>=2:values['sharp_edge_pp'][pos]=100*float(r.edge)+gap
            if version>=2:labels.append(direction('Exchanges',gap))
            elif abs(gap)>=3.-1e-10:labels.append('⚠️ SHARP-OFF')
        values['island_flag'][pos]=' · '.join(labels)
    for field,items in values.items():out[field]=pd.Series(items,index=out.index,dtype=out[field].dtype)
    return out
