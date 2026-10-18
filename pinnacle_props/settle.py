"""Pure forward settlement. No network, Sheet writes, or odds acquisition."""
from datetime import datetime, timezone
from difflib import SequenceMatcher
import json
import math
import pandas as pd
import yaml
from .identity import name_key, identity_table
from .settlement_math import grade_bet, participated, SettlementUnavailable
from .settings import ROOT

OFFICIAL = {'won', 'lost', 'push', 'void'}


def load_rules():
    return yaml.safe_load((ROOT/'config'/'settlement.yaml').read_text())


def match_game(bet, games):
    """Orientation tolerant, +/-1 Eastern date; ambiguous fixtures stay pending.

    Commence time disambiguates doubleheaders only when exactly one start matches.
    A fuzzy runner-up is not silently discarded even if its score is a bit lower.
    """
    candidates=[]
    for game in games:
        if abs((pd.Timestamp(game['game_date'])-pd.Timestamp(bet['game_date'])).days)>1: continue
        ratios=[]
        for home,away in [('home_team','away_team'),('away_team','home_team')]:
            ratios.append(sum(SequenceMatcher(None,name_key(game[a]),name_key(bet[b])).ratio()
                              for a,b in [(home,'home_team'),(away,'away_team')])/2)
        if max(ratios)>.75: candidates.append(game)
    # Duplicate scoreboard appearances (a rescheduled game) are the same identity.
    candidates=list({str(g['id']):g for g in candidates}.values())
    if len(candidates)==1:return candidates[0], ''
    if len(candidates)>1 and bet.get('start_time'):
        exact=[g for g in candidates if g.get('start_time') and
               abs((pd.Timestamp(g['start_time'])-pd.Timestamp(bet['start_time'])).total_seconds())<60]
        if len(exact)==1:return exact[0], ''
    return None, 'ambiguous game' if candidates else 'unmatched game'


def resolve_outcome(bet, players, identities=None):
    """Exact normalized identity, optionally a source-specific identity table.

    An absent player is not automatically a DNP: the live/final roster must identify
    the athlete or an existing unique source identity must establish the absence.
    """
    table=identity_table(pd.DataFrame(players,columns=['player_id','player_name']),[bet['player']])
    ids=set(table.loc[table.status=='matched','player_id'].dropna().astype(str))
    matches=[p for p in players if str(p['player_id']) in ids]
    if (table.status=='ambiguous').any():return None,'ambiguous player'
    ids={str(p['player_id']) for p in matches}
    if len(ids)>1 or len(matches)>1: return None,'ambiguous player'
    if len(matches)==1:return matches[0],''
    if identities is not None and not identities.empty:
        found=identities[(identities.name_key==name_key(bet['player'])) & (identities.status=='matched')]
        ids=set(found.player_id.dropna().astype(str))
        if len(ids)==1:
            matches=[p for p in players if str(p['player_id']) in ids]
            if len(matches)==1:return matches[0],''
            # Identity-table absence alone is insufficient: incomplete boxes happen.
    return None,'unmatched player; participation cannot be confirmed'


def metadata(row):
    try:return json.loads(row.get('source') or '{}')
    except (ValueError,TypeError):return {}


def due(row, now):
    if row.get('status')=='pending':return True
    if row.get('status') not in OFFICIAL or not row.get('graded_at'):return False
    meta=metadata(row)
    if row.get('sport')=='nfl' and meta.get('provider')=='espn':return True
    return not meta.get('checked_24h') and pd.Timestamp(now)-pd.Timestamp(row['graded_at'])>=pd.Timedelta(hours=24)


def settle_row(bet, game, players, *, rules=None, now=None, identities=None):
    now=now or datetime.now(timezone.utc).isoformat()
    result=dict(bet)
    if game is None:return result, 'unmatched game'
    state=game.get('state')
    if state in ('postponed','cancelled'):
        if pd.Timestamp(now)-pd.Timestamp(bet['start_time'])<pd.Timedelta(days=7):
            return result,'postponed/cancelled; awaiting play for seven days'
        grade={'result':'void','profit':0.,'stat':None}
        reason='postponed/cancelled for seven days'
    elif state!='final':return result,'game not final'
    else:
        outcome,reason=resolve_outcome(bet,players,identities)
        if reason:return result,reason
        try:
            rule=(rules or load_rules())[bet['sport']][bet['market']]
            grade=grade_bet(rule,outcome,line=bet['line'],side=bet['side'],price=bet['price_decimal'],stake=bet['stake'])
        except (KeyError,ValueError) as exc:
            return result,'settlement unavailable: '+('unsupported market' if isinstance(exc,KeyError) else str(exc))
        reason='source final; '+('nonparticipation' if grade['result']=='void' else 'box score')
    status={'win':'won','loss':'lost'}.get(grade['result'],grade['result'])
    oldmeta=metadata(bet)
    meta={'provider':game['provider'],'game_id':str(game['id']),'raw_key':game.get('raw_key',''),
          'checked_at':now, 'checked_24h':bool(bet.get('graded_at') and pd.Timestamp(now)-pd.Timestamp(bet['graded_at'])>=pd.Timedelta(hours=24))}
    stat='' if grade['stat'] is None else float(grade['stat'])
    profit=round(float(grade['profit']),2)
    changed=bet.get('status') in OFFICIAL and (bet['status']!=status or
             float(bet.get('profit') or 0)!=profit or str(bet.get('stat','')) not in (str(stat),format(stat,'.15g') if isinstance(stat,float) else ''))
    note=str(bet.get('note',''))
    if changed:
        prior={h:bet.get(h,'') for h in ('status','profit','stat','graded_at','regraded_at','source')}
        note += '\nPrior grade: '+json.dumps(prior,sort_keys=True)+'\nCorrection: '+reason
    elif status=='void' and bet.get('status')=='pending':note+='\nVoid: '+reason
    result.update(status=status,profit=profit,stat=stat,graded_at=bet.get('graded_at') or now,
                  regraded_at=now if changed else bet.get('regraded_at',''),source=json.dumps(meta,sort_keys=True),note=note)
    return result,reason
