from pathlib import Path
import json
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest
from pinnacle_props import bet_duplicates as bd
from pinnacle_props.bet_selection import editor_key, selected_rows
from pinnacle_props.bet_store import BetStore
from tests.test_bet_store import Spreadsheet, record
from tests.test_rules_app import fixture_rows


@pytest.fixture
def checked_app(tmp_path,monkeypatch):
    from pinnacle_props import rules
    import pinnacle_props.bet as bet
    import pinnacle_props.paper as paper
    monkeypatch.delenv('PP_SETTLEMENT_ENABLED',raising=False)
    monkeypatch.setattr('pinnacle_props.settings.ROOT',tmp_path)
    monkeypatch.setattr(rules,'LIVE_PATH',tmp_path/'rules.json')
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:pytest.fail('No network in acceptance fixture'))
    store=BetStore(tmp_path/'data',spreadsheet=Spreadsheet())
    monkeypatch.setattr('pinnacle_props.bet_store.BetStore',lambda *a,**k:store)
    monkeypatch.setattr('pinnacle_props.bet_store.session_store',lambda *a,**k:store)
    monkeypatch.setattr(paper,'BetStore',lambda *a,**k:store)
    monkeypatch.setattr('pinnacle_props.performance_ui.render',lambda *a,**k:None)
    monkeypatch.setattr('pinnacle_props.clv.rebuild',lambda **k:pd.DataFrame())
    frame=fixture_rows('nhl')
    frame['sport']='nhl'
    manifest=dict(scan_id='fixture',settings={'sport':'nhl'},capture_time='2026-10-09T12:00:00Z',profile_name='Fixture',event_results=[],credits_charged=0,raw_rows=len(frame)*4,candidate_pairs=len(frame),exclusions={})
    monkeypatch.setattr('pinnacle_props.pipeline.replay',lambda *a,**k:(manifest,frame,pd.DataFrame()))
    for module in (bet,paper):
        monkeypatch.setattr(module,'snapshots',lambda *a,**k:[(tmp_path/'fixture.json',manifest)])
        monkeypatch.setattr(module,'flags',lambda *a,**k:frame.copy())
    app=AppTest.from_file(Path('app.py').resolve(),default_timeout=20)
    app.secrets['gcp_service_account']={'project_id':'fixture'}
    app.run();assert not app.exception
    return app,store,frame


def edit(app,rows):
    table=next(t for t in app.dataframe if 'log' in t.value.columns)
    key=editor_key('fixture',table.value)
    app.session_state[key]={'edited_rows':rows,'added_rows':[],'deleted_rows':[]}
    return table


def test_selected_rows_reach_guarded_store_with_edited_stakes(checked_app):
    app,store,frame=checked_app
    table=edit(app,{0:{'log':True,'kellyAmount':12.34},1:{'log':True,'kellyAmount':7.89}})
    config=json.loads(table.proto.columns)
    assert not config['log'].get('disabled',False)
    assert not config['kellyAmount'].get('disabled',False)
    assert config['_index']['disabled'] is True
    for column in table.value.columns:
        if column not in ('log','kellyAmount'):assert config[column]['disabled'] is True
    app.button(key='log_selected').click().run()
    assert not app.exception
    rows=store.read('real',reload=True)
    assert rows.player.tolist()==['Fixture 0','Fixture 2']
    assert [float(v) for v in rows.stake]==[12.34,7.89]
    assert [int(v) for v in rows.row]==[1,3]  # visible positions differ from saved positions
    assert any('2 logged; 0 queued; 0 exact duplicates skipped' in i.value for i in app.info)
    assert not next(t.value for t in app.dataframe if 'log' in t.value.columns)['log'].any()


def test_empty_selection_warns_without_real_writes(checked_app):
    app,store,_=checked_app
    app.button(key='log_selected').click().run()
    assert not app.exception
    assert any('Tick the log box on the bets you placed' in w.value for w in app.warning)
    assert store.read('real',reload=True).empty


def test_exact_duplicate_skipped_then_explicit_override(checked_app):
    app,store,_=checked_app
    store.append_real([record(event_id='fixture-event',player='Fixture 0',market='player_assists',line=2.5,side='under')])
    app.button(key='reload_real_check').click().run()
    table=edit(app,{0:{'log':True,'kellyAmount':15.25}})
    assert str(table.value.index[0]).startswith('🔴') and table.value.iloc[0]['⚠']=='🔴 ALREADY BET'
    app.button(key='log_selected').click().run()
    assert not app.exception and len(store.read('real',reload=True))==1
    assert any('1 exact duplicates skipped' in i.value for i in app.info)
    app.checkbox(key='allow_real_duplicates').check().run()
    edit(app,{0:{'log':True,'kellyAmount':15.25}})
    app.button(key='log_selected').click().run()
    assert not app.exception
    rows=store.read('real',reload=True)
    assert len(rows)==2 and float(rows.iloc[-1].stake)==15.25


def test_paper_uses_rules_and_suggested_stakes_not_real_selection(checked_app):
    app,store,_=checked_app
    edit(app,{0:{'log':True,'kellyAmount':99.0}})
    assert app.button(key='paper_all').label=='Bet ALL 4 qualifying (paper round)'
    app.button(key='paper_all').click().run()
    assert not app.exception
    paper=store.read('paper',reload=True)
    assert len(paper)==4 and set(float(v) for v in paper.stake)=={10.}
    assert store.read('real',reload=True).empty


def test_filtered_selection_and_batch_stake_validation(checked_app):
    app,store,_=checked_app
    edit(app,{0:{'log':True,'kellyAmount':99.}})
    app.text_input(key='search').set_value('Fixture 3').run()
    assert not next(t.value for t in app.dataframe if 'log' in t.value.columns)['log'].any()
    edit(app,{0:{'log':True,'kellyAmount':6.25}})
    app.button(key='log_selected').click().run()
    row=store.read('real',reload=True).iloc[0]
    assert row.player=='Fixture 3' and int(row.row)==4 and float(row.stake)==6.25
    view=pd.DataFrame({'kellyAmount':[10.,10.]},index=[0,3]);table=view.copy();table.insert(0,'log',False)
    edited=table.copy();edited['log']=True;edited.loc[3,'kellyAmount']=float('nan')
    with pytest.raises(ValueError,match='finite and positive'):selected_rows(view,table,edited)


def test_fresh_guard_blocks_write_after_display_read_succeeds(checked_app):
    app,store,_=checked_app
    edit(app,{0:{'log':True,'kellyAmount':12.}})
    store.spreadsheet.worksheet=lambda _:(_ for _ in ()).throw(RuntimeError('offline'))
    app.button(key='log_selected').click().run()
    assert not app.exception
    assert any('CHECK UNAVAILABLE' in e.value for e in app.error)
    assert not any((store.root/'bets').glob('*outbox.csv'))


def test_card_path_retains_same_real_record_schema(checked_app):
    app,store,_=checked_app
    app.radio(key='view_mode').set_value('Cards').run()
    app.number_input(key='actual_fixture_1').set_value(23.45).run()
    app.button(key='log_fixture_1').click().run()
    assert not app.exception
    rows=store.read('real',reload=True)
    assert len(rows)==1 and rows.iloc[0].player=='Fixture 0'
    assert float(rows.iloc[0].stake)==23.45 and int(rows.iloc[0].row)==1


def test_settlement_enabled_with_zero_rule_passing_rows(checked_app):
    app,store,frame=checked_app
    frame['edge']=0.0
    app.run()
    assert not app.exception
    assert app.metric[0].value=='0'
    assert any('No opportunities match these filters.' in item.value for item in app.info)
    app.text_input(key='search').set_value('player').run()
    assert not app.exception and app.metric[0].value=='0'


@pytest.mark.parametrize('blank',[None,float('nan'),'',0])
def test_blank_or_nonstring_duplicate_flags_are_safe(checked_app,monkeypatch,blank):
    app,store,frame=checked_app
    monkeypatch.setattr(bd,'flags',lambda visible,keys:[blank]*len(visible))
    app.run()
    assert not app.exception and app.metric[0].value=='4'
    assert not any('already in your bet log' in item.value for item in app.warning)
    table=next(t.value for t in app.dataframe if 'log' in t.value.columns)
    assert not table['log'].any() and len(table)==4
