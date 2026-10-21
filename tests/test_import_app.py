from streamlit.testing.v1 import AppTest


def app():
    import io
    import streamlit as st
    from pinnacle_props.import_ui import render_import
    from pinnacle_props.bet_store import BetStore
    from tests.test_bet_store import Spreadsheet
    from tests.test_bet_import import upload,Client
    from pinnacle_props.bet_import import parse,prepare
    body=io.BytesIO(upload());body.name='bets.csv'
    if 'store' not in st.session_state:
        st.session_state.store=BetStore(st.session_state.root,spreadsheet=Spreadsheet())
    store=st.session_state.store
    if st.session_state.get('seed'):
        report=prepare(parse(upload(),'bets.csv'),Client(),data_dir=st.session_state.root)
        store.append_real(report['records'])
    render_import(st.session_state.root,store,uploaded=body,client_factory=Client)


def test_preview_dedupe_no_paid_on_render(tmp_path):
    at=AppTest.from_function(app);at.session_state.root=str(tmp_path);at.run()
    assert not at.exception
    assert 'A Skater' in at.dataframe[0].value.player.tolist()
    assert any('21 credits' in x.value for x in at.markdown)
    assert at.button(key='prepare_import')
    at.session_state.seed=True;at.run()
    assert not at.exception and at.dataframe[0].value.duplicate.all()
    assert not at.button
