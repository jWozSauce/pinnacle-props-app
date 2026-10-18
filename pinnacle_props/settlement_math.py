"""Shared pure grading and participation policies for historical and forward bets."""
import math
from .odds import decimal

class SettlementUnavailable(ValueError):
    pass


def participated(outcome, policy):
    if outcome is None:return False
    if outcome.get('participation')=='no_minutes':return False
    if policy=='row':return True
    if policy.startswith('nhl_'):
        expected='goalie' if policy=='nhl_goalie' else 'skater'
        if outcome.get('role')!=expected:return False
        value=outcome.get('time_on_ice_seconds')
    elif policy in ('mlb_pitcher','mlb_hitter'):
        prefix='pitching' if policy=='mlb_pitcher' else 'hitting'
        value=outcome.get(prefix+'_gamesPlayed')
        if outcome.get(prefix+'_present') is not True and (value is None or not math.isfinite(float(value))):return False
    else:raise ValueError('Unknown participation policy')
    if value is None or not math.isfinite(float(value)):raise SettlementUnavailable('Participation data missing')
    return float(value)>0


def grade_bet(rule, outcome, *, line, side, price, stake=1.0):
    """Grade a previously identity-matched candidate. Unmatched names stay upstream."""
    price=decimal(price);line=float(line);stake=float(stake)
    if side not in ('over','under') or not math.isfinite(line) or not math.isfinite(stake) or stake<0:
        raise ValueError('Invalid bet')
    if not participated(outcome,rule['participation']):
        return dict(result='void',stat=None,stake_attempted=stake,stake_exposed=0.0,returned=stake,profit=0.0)
    fields=rule['fields'];coefficients=rule.get('coefficients',[1]*len(fields))
    if len(fields)!=len(coefficients):raise ValueError('Settlement coefficients differ from fields')
    values=[outcome.get(field) for field in fields]
    if any(v is None or not math.isfinite(float(v)) for v in values):
        raise SettlementUnavailable('Required settlement field missing: '+','.join(fields))
    stat=math.fsum(float(v)*c for v,c in zip(values,coefficients))
    result='push' if stat==line else ('win' if (stat>line)==(side=='over') else 'loss')
    returned=stake*(price if result=='win' else 1 if result=='push' else 0)
    return dict(result=result,stat=stat,stake_attempted=stake,stake_exposed=stake,returned=returned,profit=returned-stake)


