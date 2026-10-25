import io
import json
import pytest
from streamlit.testing.v1 import AppTest
from pinnacle_props import receipt
from pinnacle_props.bet_store import BetStore
from pinnacle_props.settle import metadata
from tests.test_bet_store import Spreadsheet
from tests.test_recommendations import paper

TEXT='''BETONLINE
Bet ID fixture-slip-1
Accepted Date 10/24/2026
Description A Player (HOU) Under 1.5 three point field goals made
Status Pending
Risk $8.61
To Win $13.09
'''


def test_extract_values_and_no_invented_timestamp():
    r=receipt.parse_text(TEXT)
    assert r['player']=='A Player' and r['market']=='player_threes' and r['book']=='betonlineag'
    assert r['stake']==8.61 and r['price_decimal']==2.52 and r['odds_american']==152
    assert r['side']=='under' and r['line']==1.5 and 'placed_at' not in r
    item=receipt.item(dict(r,sport='nba',away_team='Houston Rockets',home_team='Boston Celtics'),b'receipt','r.pdf')
    assert item['placed_at']=='' and item['receipt_fields']['to_win']==13.09
    with pytest.raises(ValueError):receipt.parse_text(TEXT+'\nOver 20.5 points')
    assert 'book' not in receipt.parse_text(TEXT.replace('BETONLINE','unknown logo'))


def test_matching_is_exact_contract_and_book():
    import pandas as pd
    r=paper(player='A Player',game_date='2026-10-24',market='player_threes',line=1.5,side='under',book='betonlineag')
    f=receipt.parse_text(TEXT)
    assert receipt.matches(pd.DataFrame([r]),f)==[r]
    for k,v in [('book','draftkings'),('line',2.5),('side','over'),('game_date','2026-10-25'),('player','B Player')]:
        assert receipt.matches(pd.DataFrame([r]),dict(f,**{k:v}))==[]


def test_image_local_ocr_and_limits(tmp_path,monkeypatch):
    from PIL import Image
    image=Image.new('RGB',(10,10));b=io.BytesIO();image.save(b,format='PNG')
    seen=[]
    monkeypatch.setattr(receipt,'ocr',lambda path:seen.append(path.exists()) or TEXT)
    assert receipt.parse_text(receipt.extract(b.getvalue(),'s.png'))['book']=='betonlineag' and seen==[True]
    with pytest.raises(ValueError):receipt.extract(b'x'*5_000_001,'r.pdf')
    with pytest.raises(ValueError):receipt.extract(b'x','r.exe')


def test_pdf_embedded_text_without_ocr(monkeypatch):
    from unittest.mock import Mock
    import pypdf
    monkeypatch.setattr(pypdf,'PdfReader',lambda _:Mock(is_encrypted=False,pages=[Mock(extract_text=lambda:TEXT)]))
    monkeypatch.setattr(receipt,'ocr',lambda _:(_ for _ in ()).throw(AssertionError('not needed')))
    assert receipt.parse_text(receipt.extract(b'fake pdf','r.pdf'))['stake']==8.61


def receipt_app():
    import io
    import streamlit as st
    from pinnacle_props.receipt_ui import render_receipt
    body=io.BytesIO(b'fixture-receipt');body.name='receipt.pdf'
    render_receipt(st.session_state.root,st.session_state.store,uploaded=body)


def test_receipt_preview_then_explicit_guarded_write(tmp_path,monkeypatch):
    monkeypatch.setattr(receipt,'extract',lambda *a:TEXT)
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:(_ for _ in ()).throw(AssertionError('network forbidden')))
    store=BetStore(tmp_path,spreadsheet=Spreadsheet())
    r=paper(player='A Player',game_date='2026-10-24',market='player_threes',line=1.5,side='under',book='betonlineag');store.append('paper',[r])
    at=AppTest.from_function(receipt_app);at.session_state.root=str(tmp_path);at.session_state.store=store;at.run()
    assert not at.exception and not at.error and store.read('real').empty
    button=next(b for b in at.button if b.label=='Import receipt into real log');assert button.disabled
    at.checkbox[0].check().run();next(b for b in at.button if b.label=='Import receipt into real log').click().run()
    assert not at.exception and not at.error
    saved=store.read('real',reload=True).iloc[0].to_dict()
    assert float(saved['stake'])==8.61 and float(saved['price_decimal'])==2.52
    assert metadata(saved)['import']['receipt_fields']['slip_id']=='fixture-slip-1'
    assert any('already in the real log' in w.value for w in at.warning)


def test_unmatched_receipt_editable_and_no_paid_on_preview(tmp_path,monkeypatch):
    monkeypatch.setattr(receipt,'extract',lambda *a:TEXT)
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:(_ for _ in ()).throw(AssertionError('network forbidden')))
    store=BetStore(tmp_path,spreadsheet=Spreadsheet());at=AppTest.from_function(receipt_app)
    at.session_state.root=str(tmp_path);at.session_state.store=store;at.run()
    next(x for x in at.selectbox if x.label=='Receipt sport').set_value('nba')
    next(x for x in at.text_input if x.label=='Away team').set_value('Houston Rockets')
    next(x for x in at.text_input if x.label=='Home team').set_value('Boston Celtics');at.run()
    assert not at.exception and not at.error and store.read('real').empty
    assert at.button(key='receipt_prepare_import')
    assert any('11 credits' in x.value for x in at.markdown)
