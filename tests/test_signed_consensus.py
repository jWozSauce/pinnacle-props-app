import pandas as pd
import pytest
from pinnacle_props.consensus import annotate, LEGACY_FIELDS


def board(p=.54,side='over',books=3):
    row=dict(sport_key='nba',event_id='e',player_name='Example',prop_type='player_points',
             side=side,Target_line=2.5,PIN_line=2.5,book='target',edge=.05,
             noVigOver=.5,noVigUnder=.5,rule_pass=True)
    raw=[]
    for book in ['target']+['peer'+str(i) for i in range(books)]+['novig']:
        fair=.9 if book=='target' else p
        for s,q in [('over',fair),('under',1-fair)]:
            raw.append(dict(sport_key='nba',event_id='e',player='Example',market='player_points',
                            side=s,point=2.5,book=book,price=1/(q+.02)))
    return pd.DataFrame([row]),pd.DataFrame(raw)


@pytest.mark.parametrize('p,side,gap,lean',[(.54,'over',4,'with'),(.54,'under',-4,'against'),
                                        (.46,'over',-4,'against'),(.46,'under',4,'with')])
def test_signed_fair_probability_uses_bet_side_and_excludes_target(p,side,gap,lean):
    f,q=board(p,side);a=annotate(f,q).iloc[0]
    assert a.market_books_q==3 and a.sharp_books_q==1
    assert a.market_gap_pp==pytest.approx(gap) and a.sharp_gap_pp==pytest.approx(gap)
    assert a.market_edge_pp==pytest.approx(5+gap) and a.sharp_edge_pp==pytest.approx(5+gap)
    assert 'Market '+lean+' Pinnacle' in a.island_flag and 'Exchanges '+lean+' Pinnacle' in a.island_flag
    assert a.edge==.05 and a.rule_pass and a.advisory_version==2


@pytest.mark.parametrize('p,flag',[(.48999,True),(.49,False),(.49001,False),(.53,False)])
def test_final_one_point_direction_boundary(p,flag):
    f,q=board(p);a=annotate(f,q).iloc[0]
    assert ('Market against' in a.island_flag)==flag and ('Exchanges against' in a.island_flag)==flag


def test_pairs_minimum_duplicates_and_future_quotes():
    f,q=board();q=pd.concat([q,q.assign(market='player_points_alternate')],ignore_index=True)
    q['last_update']='2026-10-21T12:00:00Z'
    q.loc[(q.book=='peer2')&(q.side=='under'),'last_update']='2026-10-21T12:01:00Z'
    a=annotate(f,q,capture_time='2026-10-21T12:00:00Z').iloc[0]
    assert a.market_books_q==2 and 'Market' not in a.island_flag and a.sharp_books_q==1
    q=q[q.side=='over'];a=annotate(f,q).iloc[0]
    assert a.market_books_q==0 and a.sharp_books_q==0 and pd.isna(a.market_gap_pp)


def test_legacy_semantics_and_columns_remain_frozen():
    f,q=board(.54);a=annotate(f,q,version=1).iloc[0]
    assert a.sharp_gap_pp==pytest.approx(-4) and 'SHARP-OFF' in a.island_flag
    assert 'market_gap_pp' not in a and 'advisory_version' not in a
    assert set(a.index)-set(f.columns)==set(LEGACY_FIELDS)
