import pandas as pd
import pytest
from pinnacle_props import bet_duplicates as bd
from pinnacle_props.bet_store import BetStore,StoreUnavailable
from tests.test_bet_store import Spreadsheet,record as base_record

def record(**kwargs):
    return base_record(**dict({"event_id":"event"},**kwargs))


def test_exact_contract_same_player_same_game_and_other_game():
    a=record(event_id='event',player='A Player',market='player_points',line=21.5,side='over')
    frame=pd.DataFrame([dict(a,book='another',line='21.50'),dict(a,market='player_assists'),dict(a,player='Other'),dict(a,event_id='new')])
    assert bd.flags(frame,bd.real_keys(pd.DataFrame([a])))==['🔴 ALREADY BET','🟡 SAME GAME · same player','🟡 SAME GAME','']
    assert bd.flags(frame,None)==['⚪ CHECK UNAVAILABLE']*4


def test_fresh_guard_skips_duplicates_in_batch_and_explicit_override(tmp_path):
    sheet=Spreadsheet();store=BetStore(tmp_path,spreadsheet=sheet)
    a=record();b=record(book='other')
    assert store.append_real([a,b])['skipped']==1
    assert store.append_real([record()])['written']==0
    assert store.append_real([record()],allow_duplicates=True)['written']==1
    assert len(store.read('real',reload=True))==2
    assert store.append_real([a])['written']==0


def test_fresh_read_ignores_cached_empty_and_blocks_unavailable(tmp_path):
    sheet=Spreadsheet();store=BetStore(tmp_path,spreadsheet=sheet)
    assert store.real_keys()==(set(),set(),set())
    other=BetStore(tmp_path/'other',spreadsheet=sheet);other.append_real([record()])
    assert store.append_real([record()])['skipped']==1
    sheet.worksheet=lambda _:(_ for _ in ()).throw(RuntimeError('sensitive provider details'))
    assert store.real_keys(reload=True) is None
    with pytest.raises(StoreUnavailable,match='CHECK UNAVAILABLE'):
        store.append_real([record(event_id='different')],allow_duplicates=True)
    assert not (tmp_path/'bets/real_outbox.csv').exists()


def test_paper_rows_never_trigger_flags_and_display_cache_expires(tmp_path,monkeypatch):
    sheet=Spreadsheet();store=BetStore(tmp_path,spreadsheet=sheet)
    store.append('paper',[record()]);now=[0.0]
    monkeypatch.setattr('pinnacle_props.bet_store.time.monotonic',lambda:now[0])
    assert bd.flags(pd.DataFrame([record()]),store.real_keys())==['']
    other=BetStore(tmp_path/'other',spreadsheet=sheet);other.append_real([record()])
    assert bd.flags(pd.DataFrame([record()]),store.real_keys())==['']
    now[0]=121
    assert bd.flags(pd.DataFrame([record()]),store.real_keys())==['🔴 ALREADY BET']
    assert '🔴' in bd.index_labels(pd.DataFrame({'⚠':['🔴 ALREADY BET']}))[0]
