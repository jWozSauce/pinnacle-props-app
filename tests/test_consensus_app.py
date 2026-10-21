from pathlib import Path
import pandas as pd
from streamlit.testing.v1 import AppTest
from test_rules_app import fixture_rows,app_setup


def test_flags_legend_and_observation_cost_visible(app_setup,monkeypatch):
    monkeypatch.setenv('PP_CONSENSUS_ENABLED','1')
    def saved(sport,**kwargs):
        f=fixture_rows(sport)
        f['island_flag']='🏝 ISLAND · ⚠️ SHARP-OFF';f['books_q']=3;f['cons_edge']=0.
        f['sharp_books_q']=1;f['sharp_gap_pp']=4.
        m=dict(scan_id='fixture',capture_time='2026-10-09T12:00:00Z',profile_name='Fixture',
            settings={'advisory_version':1},event_results=[{}],credits_charged=0,
            raw_rows=len(f)*4,candidate_pairs=len(f),exclusions={})
        return m,f,pd.DataFrame()
    monkeypatch.setattr('pinnacle_props.pipeline.replay',saved)
    app=AppTest.from_file(Path('app.py').resolve(),default_timeout=20).run()
    assert not app.exception
    assert any('ISLAND:' in c.value and 'SHARP-OFF:' in c.value for c in app.caption)
    assert any('15 books selected ≈ 2×' in c.value for c in app.caption)
    assert 'Novig' not in app.multiselect(key='books_nhl').options
    assert any('island_flag' in d.value.columns for d in app.dataframe)
    assert any('🏝 ISLAND · ⚠️ SHARP-OFF' in m.value for m in app.markdown)


def test_signed_flags_edges_and_legend_visible(app_setup,monkeypatch):
    def saved(sport,**kwargs):
        f=fixture_rows(sport)
        f['island_flag']='🏝 ISLAND · Exchanges with Pinnacle · Market against Pinnacle'
        f['sharp_gap_pp']=4.;f['sharp_edge_pp']=9.;f['market_gap_pp']=-6.;f['market_edge_pp']=-1.
        f['advisory_version']=2
        m=dict(scan_id='fixture',capture_time='2026-10-09T12:00:00Z',profile_name='Fixture',
               settings={'advisory_version':2},event_results=[{}],credits_charged=0,
               raw_rows=len(f)*4,candidate_pairs=len(f),exclusions={})
        return m,f,pd.DataFrame()
    monkeypatch.setattr('pinnacle_props.pipeline.replay',saved)
    app=AppTest.from_file(Path('app.py').resolve(),default_timeout=20).run()
    assert not app.exception
    assert any('With Pinnacle:' in c.value and '1 pp' in c.value for c in app.caption)
    assert any('Exchanges with Pinnacle' in m.value for m in app.markdown)
    assert any('Market against Pinnacle' in m.value for m in app.markdown)
    assert any({'market_gap_pp','sharp_edge_pp'}.issubset(d.value.columns) for d in app.dataframe)

    tables=[d.value for d in app.dataframe if 'island_flag' in d.value]
    assert 'sharp_gap_pp' not in tables[0] and 'market_gap_pp' not in tables[0]
    assert not any('+4.0 pp' in m.value or '-6.0 pp' in m.value for m in app.markdown)
