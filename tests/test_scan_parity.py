"""Golden digests captured from accepted 71eec4c before the indexing rewrite."""
from pathlib import Path
import hashlib,json,random
import pandas as pd
import pytest
from pinnacle_props.scan import scan,INPUT_COLUMNS
from pinnacle_props.kelly import Profile


def scenario(seed):
    rng=random.Random(seed);rows=[]
    for i in range(12):
        for book in ('pinnacle','a','b','novig'):
            for side in ('Over','Under'):
                rows.append(dict(sport_key='basketball_nba',league='NBA',event_id=f'game-{i%3}',book=book,
                    market=f'player_type_{i%2}',player=f'Player {i}',side=side,point=10.5+i,
                    price=round(rng.uniform(1.5,2.8),2),last_update='2026-10-07T18:58:00Z',
                    commence_time='2026-10-08T00:00:00Z',home_team='Home',away_team='Away'))
    # Order, malformed pairs, invalid identity/timestamps, and multiple lines.
    rows.extend(dict(rows[i]) for i in rng.sample(range(len(rows)),4))
    for i in rng.sample(range(len(rows)),8):
        key,value=rng.choice([('book',None),('player',''),('side',None),('side','Yes'),('point',float('nan')),
          ('price','bad'),('price',1),('last_update','2026-10-09T00:00:00Z'),('commence_time','bad'),('point',11.5)])
        rows[i][key]=value
    rng.shuffle(rows)
    return pd.DataFrame(rows,columns=INPUT_COLUMNS),dict(reference_book='pinnacle',min_edge=.01 if seed%2 else 0,
        max_reference_margin=.08 if seed%3==0 else None,now='2026-10-07T19:00:00Z',
        observation_books=('novig',),exclusion_version=1+seed%2)


def fingerprints(result):
    f,e,c=result
    return {'flagged':hashlib.sha256(f.to_csv(index=False).encode()).hexdigest(),
            'excluded':hashlib.sha256(e.to_csv(index=False).encode()).hexdigest(),'counts':c}


@pytest.mark.parametrize('seed',range(16))
def test_accepted_output_order_and_classification(seed):
    expected=json.loads((Path(__file__).parent/'fixtures/scan_parity.json').read_text())
    frame,kw=scenario(seed)
    assert fingerprints(scan(frame,Profile(5000,.25,.04),**kw))==expected[str(seed)]
