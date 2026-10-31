from pathlib import Path
import json
import pandas as pd
from streamlit.testing.v1 import AppTest
from pinnacle_props import consensus


def test_peer_medians_match_direct_exclusion_for_ties_odd_even_and_absent_book():
    for n in range(1,10):
        f=pd.DataFrame({'group':['g']*n,'book':[f'b{i}' for i in range(n)],'v':[float(i//2) for i in range(n)]})
        idx=consensus.peer_index(f,['group'],'v')
        key=next(iter(idx))
        for book in [*f.book,'absent']:
            count,median=consensus.peer_value(idx,key,book)
            direct=f[f.book.ne(book)].v
            assert count==len(direct)
            assert (pd.isna(median) and direct.empty) or median==direct.median()


def test_price_tab_skips_hidden_work_and_keeps_session_documents(tmp_path,monkeypatch):
    from pinnacle_props import settings,rules,book_catalog,pipeline,performance_ui,live_ui
    from pinnacle_props.kelly import Profile
    monkeypatch.setenv('PP_SETTLEMENT_ENABLED','1')
    monkeypatch.setattr(settings,'ROOT',tmp_path)
    monkeypatch.setattr(rules,'LIVE_PATH',tmp_path/'rules.json')
    counts={'config':0,'catalog':0,'rules':0,'replay':0,'panels':0,'live':0,'clv':0}
    for obj,name,key in [(settings,'load_config','config'),(book_catalog,'load','catalog'),(rules,'load','rules')]:
        original=getattr(obj,name)
        def wrapper(*a,_fn=original,_key=key,**kw):counts[_key]+=1;return _fn(*a,**kw)
        monkeypatch.setattr(obj,name,wrapper)
    def replay(*a,**kw):counts['replay']+=1;raise FileNotFoundError()
    monkeypatch.setattr(pipeline,'replay',replay)
    def render(*a,**kw):counts['panels']+=1
    def live(*a,**kw):counts['live']+=1
    def clv(*a,**kw):counts['clv']+=1;return pd.DataFrame()
    monkeypatch.setattr(performance_ui,'render',render);monkeypatch.setattr(live_ui,'render_live',live)
    monkeypatch.setattr('pinnacle_props.clv.rebuild',clv)
    monkeypatch.setattr('pinnacle_props.recommendation_ui.linked_panel',lambda *a,**kw:None)
    monkeypatch.setattr('pinnacle_props.bet_store.session_store',lambda *a,**kw:object())
    monkeypatch.setattr('pinnacle_props.bet.read_bets',lambda *a,**kw:pd.DataFrame())
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('network')))
    app=AppTest.from_file(Path('app.py').resolve(),default_timeout=20)
    app.secrets['gcp_service_account']={'project_id':'fixture'}
    app.session_state['profiles']={'Player_Props':Profile(5000,.25,.04)}
    app.run();assert not app.exception
    assert counts['panels']==counts['live']==counts['clv']==0
    first={k:counts[k] for k in ('config','catalog','rules')}
    app.run();assert not app.exception
    assert {k:counts[k] for k in first}==first
    app.session_state['pp_tab']='Live';app.run();assert not app.exception
    assert counts['live']==1 and counts['panels']==counts['clv']==0
    app.session_state['pp_tab']='Performance';app.run();assert not app.exception
    assert counts['panels']==counts['clv']==1 and counts['live']==1
    app.session_state['pp_tab']='Price comparison';app.run();assert not app.exception
    assert counts['panels']==counts['clv']==1 and counts['live']==1
    assert counts['replay']==1
    logs=[json.loads(p.read_text()) for p in (tmp_path/'data/perf').glob('*.json')]
    assert len(logs)==5 and all(x['kind']=='render' and x['phases']['render']>=0 for x in logs)
