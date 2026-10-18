"""Real-log contract warnings and fresh submission guard (NCAAF-style)."""
import math
import pandas as pd


def text(value):
    return '' if pd.isna(value) else ' '.join(str(value).strip().casefold().split())


def contract(row):
    event=text(row.get('event_id',''))
    player=text(row.get('player',row.get('player_name','')))
    market=text(row.get('market',row.get('prop_type','')))
    side=text(row.get('side',''))
    line=float(row.get('line',row.get('Target_line',math.nan)))
    if not event or not player or not market or side not in ('over','under') or not math.isfinite(line):
        raise ValueError('Real-log contract identity is incomplete')
    return event,player,market,line,side


def real_keys(frame):
    exact={contract(r) for r in frame.to_dict('records')}
    return exact,{k[0] for k in exact},{k[:2] for k in exact}


def flags(frame,keys):
    if keys is None:return ['⚪ CHECK UNAVAILABLE']*len(frame)
    exact,games,players=keys
    result=[]
    for row in frame.to_dict('records'):
        k=contract(row)
        result.append('🔴 ALREADY BET' if k in exact else '🟡 SAME GAME · same player' if k[:2] in players else
                      '🟡 SAME GAME' if k[0] in games else '')
    return result


def select_new(records,current,allow_duplicates=False):
    """Fresh-read preflight, not an atomic cross-host uniqueness transaction."""
    exact,_,_=real_keys(current);selected=[];skipped=0
    ids=set(current.row_id) if 'row_id' in current else set()
    for row in records:
        k=contract(row)
        if row['row_id'] in ids:continue  # idempotent retry of the same write
        if k in exact and not allow_duplicates:skipped+=1;continue
        selected.append(row);exact.add(k);ids.add(row['row_id'])
    return selected,skipped


def row_style(row):
    flag=str(row.get('⚠',''))
    style='background-color:#7f1d1d;color:white' if flag.startswith('🔴') else 'background-color:#92600a;color:white' if flag.startswith('🟡') else ''
    return [style]*len(row)


def index_labels(frame):
    return [(str(flag).split(' ')[0]+' ' if flag else '')+str(index+1) for index,flag in zip(frame.index,frame['⚠'])]
