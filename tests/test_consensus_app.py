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
