"""Public-copy-safe rule and UI acceptance tests: synthetic prices, no API access."""
from copy import deepcopy
from pathlib import Path
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest
from pinnacle_props import rules
from pinnacle_props.scan import LEGACY_COLUMNS, EXTRA_COLUMNS


def fixture_rows(sport):
    cases={
        'nba':[('player_threes',.019999),('player_threes',.02),('player_points',.039999),('player_points',.04),('player_points_rebounds_assists',.20),('player_assists',.04)],
        'nhl':[('player_assists',.02),('player_assists',.019999),('player_total_saves',.02),('player_shots_on_goal',.03),('player_shots_on_goal',.029999),('player_points',.03)],
    }.get(sport,[('player_points',.02),('player_points',.019999)])
    rows=[]
    for i,(market,edge) in enumerate(cases):
        row=dict.fromkeys(LEGACY_COLUMNS+EXTRA_COLUMNS,0.)
        row.update(book='draftkings',player_name='Fixture '+str(i),prop_type=market,Target_line=2.5,PIN_line=2.5,
            side='over' if i%2 else 'under',kellyAmount=10.,edge=edge,bet='fixture',league=sport.upper(),
            game='Away at Home',date='2099-01-01T00:00:00Z',away_team='Away',home_team='Home',
            sport_key=sport,event_id='fixture-event',best_book='draftkings',best_price=2.,pin_age_min=0.,
            pricing_method='same_line',source_rows=[0,1],target_over_decimal=2.,target_under_decimal=2.)
        rows.append(row)
    return pd.DataFrame(rows)


@pytest.fixture
def app_setup(tmp_path,monkeypatch):
    # A mocked cloud must not read the owner's local ledger or private caches.
    monkeypatch.setattr('pinnacle_props.settings.ROOT',tmp_path)
    monkeypatch.setattr(rules,'LIVE_PATH',tmp_path/'runtime/rules.json')
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:pytest.fail('App attempted network'))
    monkeypatch.setattr('pinnacle_props.odds_api.resolve_key',lambda **k:pytest.fail('App attempted credential access'))
    def replay(sport,**kwargs):
        f=fixture_rows(sport)
        m=dict(scan_id='fixture',capture_time='2026-10-09T12:00:00Z',profile_name='Fixture',event_results=[{}],credits_charged=0,
               raw_rows=len(f)*4,candidate_pairs=len(f),exclusions={})
        return m,f,pd.DataFrame()
    monkeypatch.setattr('pinnacle_props.pipeline.replay',replay)
    return tmp_path


def start():
    return AppTest.from_file(Path('app.py').resolve(),default_timeout=20)


def apply(app):
    next(b for b in app.button if b.label=='Apply rules').click().run()
    assert not app.exception


def test_adopted_clauses_hidden_count_and_show_all(app_setup):
    app=start().run()
    for sport,count in [('nba',3),('nhl',4),('nfl',1),('mlb',1),('wnba',1)]:
        app.selectbox(key='sport').select(sport).run()
        assert not app.exception
        assert app.metric[0].value==str(count)
        hidden=len(fixture_rows(sport))-count
        assert any(f'{hidden} bets hidden by the rule' in c.value for c in app.caption)
        app.checkbox(key='show_all_rules_'+sport).check().run()
        assert app.metric[0].value==str(len(fixture_rows(sport)))


def test_mac_edit_restart_and_reset(app_setup):
    app=start().run()
    app.slider(key='rule_min_nhl_0').set_value(5.)
    apply(app)
    assert rules.active()['sports']['nhl']['default']['min_edge']==5.
    assert app.metric[0].value=='2' # assists and saves overrides remain at two points
    restarted=start().run()
    assert restarted.slider(key='rule_min_nhl_0').value==5.
    assert restarted.metric[0].value=='2'
    restarted.button(key='reset_rules').click().run()
    assert not restarted.exception and restarted.metric[0].value=='4'
    assert rules.active()==rules.load()


def test_market_side_enabled_and_edge_edits_in_app(app_setup):
    app=start().run()
    frame=rules.editor_rows(rules.load(),'nhl',__import__('pinnacle_props.settings',fromlist=['load_config']).load_config()['sports']['nhl']['markets']).iloc[1:].reset_index(drop=True)
    assists=int(frame.index[frame.market=='player_assists'][0]);saves=int(frame.index[frame.market=='player_total_saves'][0])
    # AppTest exposes data-editor edits through the public session-state widget protocol.
    app.session_state['rule_table_nhl_0']={'edited_rows':{assists:{'side':'over'},saves:{'enabled':False}},'added_rows':[],'deleted_rows':[]}
    apply(app)
    saved=rules.active()
    assert saved['sports']['nhl']['markets']['player_assists']['side']=='over'
    assert saved['sports']['nhl']['markets']['player_total_saves']['enabled'] is False
    assert app.metric[0].value=='2' # both default three-point rows survive


def test_cloud_default_ignores_mac_and_url_survives_new_session(app_setup,monkeypatch):
    changed=rules.load();changed['sports']['nhl']['default']['min_edge']=9.;rules.save(changed)
    monkeypatch.setattr('pinnacle_props.deployment.deployment_settings',lambda _:dict(cloud=True,paid_enabled=False,project_cap=100000,cloud_daily_cap=2500))
    monkeypatch.setattr('pinnacle_props.access.cloud_access',lambda:None)
    app=start().run()
    assert not app.exception and app.metric[0].value=='4'
    assert not any(b.label=='Save as default' for b in app.button)
    app.slider(key='rule_min_nhl_0').set_value(5.);apply(app)
    linked=app.query_params['rules']
    restored=start();restored.query_params['rules']=linked;restored.run()
    assert not restored.exception and restored.metric[0].value=='2'
    assert rules.active()['sports']['nhl']['default']['min_edge']==9. # cloud never writes Mac state
    fresh=start().run();assert fresh.metric[0].value=='4'


def test_rule_validation_hash_and_full_precision():
    value=rules.load();same=deepcopy(value)
    assert rules.version(value)==rules.version(same)==rules.version(rules.decode(rules.encode(value)))
    frame=fixture_rows('nba');got=rules.annotate(frame,value,'nba')
    assert got.rule_pass.tolist()==[False,True,False,True,False,True]
    assert len(got)==len(frame) and 'rule_pass' not in frame
    for invalid in [float('nan'),float('inf'),-1,101,True]:
        bad=deepcopy(value);bad['sports']['nba']['default']['min_edge']=invalid
        with pytest.raises(ValueError):rules.validate(bad)
    with pytest.raises(ValueError):rules.decode('not-a-rule-link')


def test_save_default_is_explicit_and_cloud_uses_published_yaml(app_setup,monkeypatch):
    default=app_setup/'rules.yaml';default.write_text(rules.DEFAULT_PATH.read_text())
    monkeypatch.setattr(rules,'DEFAULT_PATH',default)
    app=start().run();app.slider(key='rule_min_nhl_0').set_value(5.);apply(app)
    assert rules.load()['sports']['nhl']['default']['min_edge']==3. # Apply only changes runtime
    app.button(key='save_rules_default').click().run()
    assert not app.exception and rules.load()['sports']['nhl']['default']['min_edge']==5.
    monkeypatch.setattr('pinnacle_props.deployment.deployment_settings',lambda _:dict(cloud=True,paid_enabled=False,project_cap=100000,cloud_daily_cap=2500))
    monkeypatch.setattr('pinnacle_props.access.cloud_access',lambda:None)
    fresh=start().run();assert not fresh.exception and fresh.metric[0].value=='2'


def test_search_when_no_saved_opportunity_passes_rules(app_setup,monkeypatch):
    original=fixture_rows
    monkeypatch.setitem(globals(),'fixture_rows',lambda sport:original(sport).assign(edge=0.0))
    app=start().run()
    assert not app.exception and app.metric[0].value=='0'
    app.text_input(key='search').set_value('player').run()
    assert not app.exception and app.metric[0].value=='0'
