"""Exact-point main/alternate pairing, provenance, ambiguity and frozen replay."""
from copy import deepcopy
import pandas as pd
import pytest
from pinnacle_props.scan import scan
from pinnacle_props.kelly import Profile
from pinnacle_props.pipeline import options,_save_snapshot,replay,discover
from pinnacle_props.settings import load_config
from test_pipeline import sample_result,PROFILE


def quotes(target='player_threes_alternate',reference='player_threes'):
    rows=[]
    for book,market,prices in [('pinnacle',reference,(1.9,1.9)),('draftkings',target,(2.2,1.75))]:
        for side,price in zip(('Over','Under'),prices):
            rows.append(dict(sport_key='basketball_nba',league='NBA',event_id='game-1',book=book,
                market=market,player='Player',side=side,point=2.5,price=price,
                last_update='2026-10-07T18:58:00Z',commence_time='2099-01-01T00:00:00Z',home_team='H',away_team='A'))
    return pd.DataFrame(rows)


def evaluate(frame,version=1):
    return scan(frame,Profile(5000,.25,.04),now='2026-10-07T19:00:00Z',alternate_version=version)


@pytest.mark.parametrize('target,ref',[('player_threes_alternate','player_threes'),
    ('player_threes','player_threes_alternate'),('player_threes_alternate','player_threes_alternate')])
def test_exact_pair_and_provenance(target,ref):
    raw=quotes(target,ref);before=raw.copy(deep=True)
    f,e,c=evaluate(raw)
    assert len(f)==1 and e.empty and sum(c.values())==0
    r=f.iloc[0]
    assert r.prop_type=='player_threes' and r.pricing_method=='same_line_alt'
    assert r.Target_line==r.PIN_line==2.5 and r.noVigOver==pytest.approx(.5)
    assert r.edge==pytest.approx(.5-1/2.2) and r.pin_margin==pytest.approx(2/1.9-1)
    assert r.target_market_keys==[target] and r.pin_market_keys==[ref]
    assert r.source_rows==[2,3] and r.pin_source_rows==[0,1]
    pd.testing.assert_frame_equal(raw,before)


def test_frozen_default_and_period_identity():
    f,e,c=evaluate(quotes(),None)
    assert f.empty and c['no reference price']==1
    f,e,c=evaluate(quotes('player_threes_q1_alternate'))
    assert f.empty and c['no reference price']==1
    f,e,c=evaluate(quotes('player_threes','player_threes'))
    assert f.pricing_method.tolist()==['same_line']


@pytest.mark.parametrize('book',['pinnacle','draftkings'])
def test_identical_cross_key_duplicates_collapse_and_conflicts_fail(book):
    raw=quotes('player_threes','player_threes')
    other=raw[raw.book==book].copy();other['market']='player_threes_alternate'
    both=pd.concat([raw,other],ignore_index=True)
    f,e,c=evaluate(both)
    assert len(f)==1 and e.empty and f.pricing_method.tolist()==['same_line_alt']
    assert f.price_draftkings.iloc[0]==2.2
    both.loc[4,'price']=2.4
    f,e,c=evaluate(both)
    assert f.empty and c['duplicate line']==1
    f,e,c=evaluate(pd.concat([raw,raw[raw.book==book]],ignore_index=True))
    assert f.empty and c['duplicate line']==1


def test_one_sided_mismatch_and_source_accounting():
    raw=quotes();raw.loc[raw.book=='draftkings','point']=3.5
    f,e,c=evaluate(raw)
    assert f.empty and c['line mismatch']==1 and e.source_rows.tolist()==[[2,3]]
    f,e,c=evaluate(raw.iloc[:3])
    assert f.empty and c['one-sided market']==1
    raw=quotes();raw.loc[3,'market']='player_threes'
    f,e,c=evaluate(raw)
    assert len(f)==1 and f.target_market_keys.iloc[0]==['player_threes','player_threes_alternate']


def test_market_selection_is_opt_in_and_costs_all_keys(monkeypatch,tmp_path):
    from test_api import Session,Response
    cfg=load_config();monkeypatch.setenv('PP_SAME_LINE_ALTERNATES_ENABLED','0')
    assert options(cfg,'nba')['markets']==cfg['sports']['nba']['markets']
    assert 'alternate_version' not in options(cfg,'nba')
    with pytest.raises(ValueError):options(cfg,'nba',markets=['player_threes_alternate'])
    monkeypatch.setenv('PP_SAME_LINE_ALTERNATES_ENABLED','1')
    for sport,spec in cfg['sports'].items():
        s=options(cfg,sport)
        assert s['alternate_version']==1 and s['markets']==spec['markets']+spec['alternate_markets']
        assert all(m.removesuffix('_alternate') in spec['markets'] for m in spec['alternate_markets'])
    s=options(cfg,'nba')
    from datetime import datetime,timezone
    preview=discover(s,data_dir=tmp_path,key='test-key',session=Session(Response([{'id':'game-1','commence_time':'2099-01-01T00:00:00Z'}])),clock=lambda:datetime(2098,12,31,tzinfo=timezone.utc))
    from pinnacle_props.consensus import request_books
    assert preview['bound']==len(s['markets'])*((len(request_books(s))+9)//10)


def test_alternate_snapshot_replays_after_switch_off(tmp_path,monkeypatch):
    monkeypatch.setenv('PP_SAME_LINE_ALTERNATES_ENABLED','1')
    result=sample_result(tmp_path)
    result.payload['bookmakers'][1]['markets'][0]['key']='player_points_alternate'
    # sample_result already cached before mutation; rebuild the immutable fixture cache.
    import gzip,json
    from pathlib import Path
    Path(result.cache_path).write_bytes(gzip.compress(json.dumps({'status':200,'request_id':'fixture','body':json.dumps(result.payload)}).encode()))
    s=options(load_config(),'nba',books=['draftkings'],markets=['player_points','player_points_alternate'])
    m,f,e=_save_snapshot(tmp_path,'alt',s,PROFILE,'Player_Props','2026-10-07T19:00:00Z',[result],[],2,2)
    assert f.pricing_method.tolist()==['same_line_alt']
    assert m['candidate_pairs']==len(f)+sum(m['exclusions'].values())
    monkeypatch.setenv('PP_SAME_LINE_ALTERNATES_ENABLED','0')
    again,f2,e2=replay('nba','alt',data_dir=tmp_path)
    pd.testing.assert_frame_equal(f,f2)
    assert again['settings']['alternate_version']==1
    raw=pd.read_parquet(tmp_path/'snapshots/nba/alt.parquet')
    assert set(raw.market)=={'player_points','player_points_alternate'}


def test_accepted_default_enabled(monkeypatch):
    monkeypatch.delenv('PP_SAME_LINE_ALTERNATES_ENABLED',raising=False)
    s=options(load_config(),'nba')
    assert s['alternate_version']==1 and 'player_threes_alternate' in s['markets']
