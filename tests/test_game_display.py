import hashlib,json
from datetime import datetime
import pytest
from pinnacle_props.game_display import game_label,book_link,player_label
from tests.test_bet_store import record
from pinnacle_props.log_tables import display
import pandas as pd


def test_eastern_context_all_seasons_and_missing_time():
    r=dict(away_team='Houston Rockets',home_team='Boston Celtics',start_time='2026-10-25T23:30:00Z')
    assert game_label(r)=='Houston Rockets @ Boston Celtics · Sun 7:30 pm ET'
    r['start_time']='2026-12-25T23:30:00Z';assert '6:30 pm ET' in game_label(r)
    r['start_time']='2026-12-25T23:30:00';assert 'Start time unavailable' in game_label(r)
    assert 'Matchup unavailable' in game_label({})


def test_only_verified_book_host_event_links():
    assert book_link(dict(book='draftkings',event_url='https://sportsbook.draftkings.com/event/123'))
    for url in ['javascript:alert(1)','https://evil.test/event/123','https://sportsbook.draftkings.com/','https://x@sportsbook.draftkings.com/event/123']:
        assert book_link(dict(book='draftkings',event_url=url)) is None
    assert book_link(dict(book='draftkings',event_id='odds-api-id')) is None


def test_player_team_from_exact_verified_cached_box_only(tmp_path):
    box={'header':{'id':'1'},'boxscore':{'players':[{'team':{'abbreviation':'HOU'},'statistics':[{'athletes':[{'athlete':{'id':'7','displayName':'A Player'}}]}]}]}}
    body=json.dumps(box).encode();digest=hashlib.sha256(body).hexdigest();(tmp_path/(digest+'.raw')).write_bytes(body)
    row={'player':'A Player','source':json.dumps(dict(provider='espn',game_id='1',raw_key=digest))}
    assert player_label(row,tmp_path)=='A Player (HOU)'
    row['source']=row['source'].replace('"1"','"2"');assert player_label(row,tmp_path)=='A Player'


def test_game_column_immediately_after_bet():
    f=display(pd.DataFrame([record()]))
    assert list(f.columns)[list(f.columns).index('bet')+1]=='game'
    assert '@' in f.game.iloc[0] and 'ET' in f.game.iloc[0]


def test_cloud_receipt_ocr_disabled_even_with_installed_binary(monkeypatch,tmp_path):
    from pinnacle_props.settings import ACCOUNTING_CLOUD
    from pinnacle_props.receipt import ocr
    token=ACCOUNTING_CLOUD.set(True)
    try:
        with pytest.raises(ValueError,match='Mac'):ocr(tmp_path/'receipt.png')
    finally:ACCOUNTING_CLOUD.reset(token)
