"""Read-only current-stat projection and official-performance summaries."""
import math
import pandas as pd
from .settle import OFFICIAL, resolve_outcome, load_rules
from .settlement_math import grade_bet

CAPTION = ('Live tracking covers NHL and NBA only. Official P/L includes graded bets only. Projected P/L adds unofficial finals and '
           'freezes live bets at the current stat: covering earns the potential win, behind loses '
           'the stake; on the line, waiting, upcoming and unmatched contribute zero. '
           'This is not a forecast. Official grades always take precedence.')


def card_money(card):
    out=card.copy(deep=True)
    stake=pd.to_numeric(out.get('stake',pd.Series(index=out.index,dtype=float)),errors='coerce')
    price=pd.to_numeric(out.get('price_decimal',pd.Series(index=out.index,dtype=float)),errors='coerce')
    valid=stake.ge(0)&price.gt(1)&stake.map(math.isfinite)&price.map(math.isfinite)
    out['to_win']=(stake*(price-1)).where(valid).round(2)
    official=out.get('status',pd.Series('',index=out.index)).isin(OFFICIAL)
    profit=pd.to_numeric(out.get('profit',pd.Series(0.,index=out.index)),errors='coerce').fillna(0)
    stage=out.get('stage',pd.Series('',index=out.index))
    verdict=out.get('verdict',pd.Series('',index=out.index))
    out['proj_contrib']=0.
    tracked=out.get('sport',pd.Series('nba',index=out.index)).isin(['nhl','nba'])
    wins=~official & tracked & ((stage.eq('final') & verdict.eq('✅ won')) | (stage.eq('live') & verdict.eq('🟢 covering')))
    losses=~official & tracked & ((stage.eq('final') & verdict.eq('❌ lost')) | (stage.eq('live') & verdict.eq('🔴 behind')))
    out.loc[wins,'proj_contrib']=out.loc[wins,'to_win'].fillna(0)
    out.loc[losses,'proj_contrib']=-stake.where(valid,0).loc[losses]
    actual=round(float(profit[official].sum()),2)
    return out,actual,round(actual+float(out.proj_contrib.sum()),2)


def live_row(bet, game, players, *, rules=None):
    out=dict(bet,stage='unmatched',verdict='— no live feed',score='',detail='Unmatched game')
    if game is None:return out
    out['score']=game.get('score','')
    out['detail']=game.get('detail','')
    if game.get('state')=='upcoming':
        out.update(stage='upcoming',verdict='⏳ upcoming');return out
    if game.get('state') not in ('live','final'):return out
    player,reason=resolve_outcome(bet,players)
    if reason:
        out.update(stage='waiting' if game['state']=='live' else 'unmatched',
                   verdict='⏳ waiting' if game['state']=='live' else '— no live feed',detail=reason)
        return out
    try:
        rule=(rules or load_rules())[bet['sport']][bet['market']]
        grade=grade_bet(rule,player,line=bet['line'],side=bet['side'],price=bet['price_decimal'],stake=bet['stake'])
    except (KeyError,ValueError):
        out.update(detail='Required stat or participation unavailable');return out
    if grade['result']=='void' and game['state']=='live':
        out.update(stage='waiting',verdict='⏳ waiting');return out
    out['stage']=game['state']
    labels={'win':'✅ won','loss':'❌ lost','push':'🟨 push','void':'⚪ void'} if game['state']=='final' else {'win':'🟢 covering','loss':'🔴 behind','push':'🟦 on the line'}
    out['verdict']=labels[grade['result']]
    out['detail']=f"{bet['market']} {grade['stat']} vs {bet['line']} · {game.get('detail','')}"
    return out


def performance(frame):
    f=frame.copy()
    if f.empty:return dict(won=0,lost=0,push=0,void=0,pending=0,profit=0.,roi=None)
    status=f.status
    profit=pd.to_numeric(f.profit,errors='coerce').fillna(0)
    stake=pd.to_numeric(f.stake,errors='coerce').fillna(0)
    settled=status.isin(OFFICIAL)
    denominator=float(stake[status.isin(['won','lost'])].sum())
    p=float(profit[settled].sum())
    return dict(**{k:int(status.eq(k).sum()) for k in ['won','lost','push','void','pending']},
                profit=round(p,2),roi=p/denominator if denominator else None)


def breakdown(frame, by):
    f=frame.copy()
    if by=='edge_bucket':
        f[by]=pd.cut(pd.to_numeric(f.edge,errors='coerce')*100,[-float('inf'),2,3,4,6,float('inf')],
                     labels=['<2','2–<3','3–<4','4–<6','≥6'],right=False)
    return pd.DataFrame([{by:name,**performance(rows)} for name,rows in f.groupby(by,observed=True)])
