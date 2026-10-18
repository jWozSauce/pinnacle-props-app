"""Exact normalized player identities; collisions never auto-resolve."""
import unicodedata
import pandas as pd

def name_key(value):
    folded = unicodedata.normalize('NFKD',str(value)).casefold()
    return ''.join(c for c in folded if c.isalnum())


def identity_table(outcomes, names):
    # No fuzzy acceptance. Normalized collisions remain ambiguous, not guessed.
    rows=[]
    pool=outcomes[['player_id','player_name']].drop_duplicates().copy()
    pool['name_key']=pool.player_name.map(name_key)
    for name in sorted(set(names)):
        matches=pool[pool.name_key==name_key(name)]
        ids=sorted(set(matches.player_id.astype(str)))
        rows.append({'description':name,'name_key':name_key(name),'player_id':ids[0] if len(ids)==1 else None,
                     'status':'matched' if len(ids)==1 else 'ambiguous' if ids else 'unmatched',
                     'candidate_ids':'|'.join(ids)})
    return pd.DataFrame(rows,columns=['description','name_key','player_id','status','candidate_ids'])


