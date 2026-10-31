"""Frozen, outcome-blind venue comparisons; Pinnacle remains the primary model."""
import json
import math
import pandas as pd
from .consensus import EXCHANGES
from .devig import wpo
from .kelly import stake,Profile

BLEND_FIELDS=['blend_edge_pp','blend_stake']
BLEND_VENUES=('novig','prophetx')


def finite(value):
    try:return math.isfinite(float(value))
    except (TypeError,ValueError):return False


def quotes(raw,capture_time):
    """One complete pair per venue/line; prefer main market over alternate copies."""
    groups={}
    if raw.empty:return groups
    f=raw.copy();f['base']=f.market.str.removesuffix('_alternate');f['alternate']=f.market.str.endswith('_alternate')
    f['side']=f.side.str.lower();f['point']=pd.to_numeric(f.point,errors='coerce');f['price']=pd.to_numeric(f.price,errors='coerce')
    f['stamp']=pd.to_datetime(f.last_update,utc=True,errors='coerce')
    f=f[f.point.map(finite)&f.price.map(finite)&f.price.gt(1)&f.side.isin(['over','under'])&f.stamp.notna()&f.stamp.le(pd.Timestamp(capture_time))]
    keys=['sport_key','event_id','player','base']
    # Select main over alternate once, then aggregate both sides in one pass.
    pair=keys+['book','point']
    f=f[f.alternate.eq(f.groupby(pair,sort=False).alternate.transform('min'))]
    prices=f.groupby(pair+['side'],sort=False).price.agg(['first','nunique'])
    prices=prices['first'].where(prices['nunique'].eq(1)).unstack('side').reindex(columns=['over','under'])
    stamps=f.groupby(pair,sort=False).stamp.min()
    joined=prices.join(stamps).reset_index()
    captured=pd.Timestamp(capture_time)
    for row in joined.itertuples(index=False,name=None):
        sport,event,player,market,book,line,over,under,stamp=row
        over=float(over) if pd.notna(over) else None
        under=float(under) if pd.notna(under) else None
        if over is None and under is None:continue
        fair=None
        if over is not None and under is not None:
            try:fair=float(wpo([over,under])[0])
            except ValueError:pass
        groups.setdefault((sport,event,player,market),[]).append(dict(venue=book,line=float(line),
            over=over,under=under,fair_over=fair,age_min=float((captured-stamp).total_seconds()/60)))
    return groups


def enrich(flagged,raw,profile,capture_time):
    out=flagged.copy();groups=quotes(raw,capture_time)
    for name in BLEND_FIELDS:out[name]=float('nan')
    out['venue_quotes']='[]'
    quote_cache={}
    for i,r in out.iterrows():
        ident=(r.sport_key,r.event_id,r.player_name,str(r.prop_type).removesuffix('_alternate'))
        rows=groups.get(ident,[]);p=float(r['noVig'+r.side.title()]);line=float(r.Target_line)
        cache_key=(*ident,line,r.side,p)
        if cache_key not in quote_cache:
            rendered=[];exchange=[]
            for v in rows:
                prob=v['fair_over'] if r.side=='over' else 1-v['fair_over'] if v['fair_over'] is not None else None
                price=v[r.side]
                item=dict(v,price=price,fair_p=prob,gap_pp=100*(prob-p) if prob is not None else None,same_line=v['line']==line)
                rendered.append(item)
                if v['venue'] in BLEND_VENUES and v['line']==line and prob is not None:exchange.append(prob)
            rendered.sort(key=lambda v:(not v['same_line'],0 if v['venue']=='pinnacle' else 1 if v['venue'] in EXCHANGES else 2,EXCHANGES.index(v['venue']) if v['venue'] in EXCHANGES else -(v['price'] or 0),v['venue'],v['line']))
            quote_cache[cache_key]=(json.dumps(rendered,sort_keys=True,allow_nan=False),exchange)
        encoded,exchange=quote_cache[cache_key]
        if exchange:
            blended=(p+sum(exchange)/len(exchange))/2
            out.at[i,'blend_edge_pp']=100*(blended-1/float(r['target_'+r.side+'_decimal']))
            out.at[i,'blend_stake']=stake(profile.bankroll,profile.fraction,float(r['target_'+r.side+'_decimal']),blended,profile.edge_cap)
        out.at[i,'venue_quotes']=encoded
    return out


def saved_details(row):
    """Read new source provenance, or the original source of a recovered paper bet."""
    from .settle import metadata
    source=metadata(row);rec=source.get('recommendation',{})
    if 'venues' in rec:return rec
    original=rec.get('original_provenance',{}).get('recommendation',{})
    if original:return original
    imported=source.get('import',{}).get('paper_record',{})
    if imported:return saved_details(imported)
    return {}
