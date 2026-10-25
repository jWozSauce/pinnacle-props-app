import re
import uuid
from types import SimpleNamespace
import pandas as pd
import pytest
from pinnacle_props.bet_store import BetStore, HEADER, BASE_HEADER, StoreUnavailable,column_name
from pinnacle_props.settle import settle_row
from tests.test_settle_forward import bet,game

class WorksheetNotFound(Exception):pass

class Worksheet:
    def __init__(self):self.values=[];self.writes=[];self.row_count=5000;self.lose_reply=False;self.id=0
    def get_all_values(self):return [list(r) for r in self.values]
    def update(self,values,range_name,value_input_option):
        assert value_input_option=='RAW'
        m=re.fullmatch(r'([A-Z]+)(\d+):([A-Z]+)(\d+)',range_name);assert m
        col=lambda s:sum((ord(c)-64)*26**i for i,c in enumerate(s[::-1]))-1
        start=int(m[2])-1;c=col(m[1])
        for i,r in enumerate(values,start):
            while len(self.values)<=i:self.values.append(['']*len(HEADER))
            self.values[i][c:c+len(r)]=[str(v) for v in r]
        self.writes.append(range_name)
        if self.lose_reply:self.lose_reply=False;raise RuntimeError('unknown acknowledgement')
    def batch_update(self,data,value_input_option):
        for r in data:self.update(r['values'],r['range'],value_input_option)
    def batch_get(self,ranges):return [[[self.values[int(re.search(r'\d+',r)[0])-1][28]]] for r in ranges]
    def add_rows(self,n):self.row_count+=n

class Spreadsheet:
    def __init__(self):self.tabs={};self.access=[]
    def worksheet(self,name):
        self.access.append(name)
        if name not in self.tabs:raise WorksheetNotFound()
        return self.tabs[name]
    def batch_update(self,body):
        for req in body['requests']:
            append=req['appendCells'];assert append['fields']=='userEnteredValue'
            ws=next(w for w in self.tabs.values() if w.id==append['sheetId'])
            for row in append['rows']:
                ws.values.append([c['userEnteredValue']['stringValue'] for c in row['values']])
            ws.writes.append('AppendCells')
            if ws.lose_reply:ws.lose_reply=False;raise RuntimeError('unknown acknowledgement')
    def add_worksheet(self,title,rows,cols):
        assert title in ('PP_Bet_Log','PP_Paper_Log');self.tabs[title]=Worksheet();self.tabs[title].id=len(self.tabs);return self.tabs[title]


def record(**kwargs):
    r=dict(dict.fromkeys(HEADER,''),**bet(**kwargs),round='')
    r['row_id']=str(uuid.uuid4());return r


def test_explicit_ranges_blank_leading_cells_dedupe_and_regrade(tmp_path):
    sheet=Spreadsheet();store=BetStore(tmp_path,spreadsheet=sheet)
    a=record();b=record(row=2)
    store.append('real',[a]);store.append('real',[b]);store.append('real',[a])
    ws=sheet.tabs['PP_Bet_Log'];assert ws.writes==['A1:'+column_name(len(HEADER))+'1','AppendCells','AppendCells']
    old=store.read('real',reload=True).to_dict('records')[0]
    new,_=settle_row(old,game(),[dict(player_id='p',player_name='A Player',points=22)],now='2026-10-10T05:00:00Z')
    store.grade('real',[(old,new)]);assert ws.writes[-1]=='V2:AB2'
    old=store.read('real',reload=True).to_dict('records')[0]
    new,_=settle_row(old,game(),[dict(player_id='p',player_name='A Player',points=21)],now='2026-10-11T05:00:00Z')
    store.grade('real',[(old,new)]);assert 'Prior grade:' in ws.values[1][27]
    assert ws.values[1][:21]==[str(a[h]) for h in HEADER[:21]]
    assert set(sheet.access)=={'PP_Bet_Log'}


def test_outbox_timeout_retry_and_read_failure(tmp_path):
    sheet=Spreadsheet();store=BetStore(tmp_path,spreadsheet=sheet)
    store.append('real',[]);sheet.tabs['PP_Bet_Log'].lose_reply=True
    assert store.append('real',[record()])['queued']==1
    assert store.append('real',[])['queued']==0
    assert len(store.read('real',reload=True))==1
    bad=BetStore(tmp_path,connector=lambda:(_ for _ in ()).throw(RuntimeError('sensitive text')))
    assert bad.append('real',[record(row=2)])['queued']==2
    assert len(bad.read('real'))==2 and 'sensitive text' not in bad.warning
    assert store.append('real',[])['written']==1


def test_paper_first_seen_across_books_and_scans(tmp_path):
    store=BetStore(tmp_path,spreadsheet=Spreadsheet())
    store.append('paper',[record(event_id='e',book='b1'),record(event_id='e',book='b2',scan_id='other',stake=99)])
    frame=store.read('paper');assert len(frame)==1 and float(frame.iloc[0].stake)==20


def test_header_mismatch_and_immutable_cells(tmp_path):
    sheet=Spreadsheet();store=BetStore(tmp_path,spreadsheet=sheet);store.append('real',[record()])
    old=store.read('real').to_dict('records')[0];new=dict(old,stake=900,status='won')
    with pytest.raises(ValueError):store.grade('real',[(old,new)])
    sheet.tabs['PP_Bet_Log'].values[0]=['wrong']
    assert store.append('real',[record(row=2)])['queued']==2
    assert len(sheet.tabs['PP_Bet_Log'].values)==2
    with pytest.raises(ValueError):store.read('Constants')


def test_two_hosts_interleaving_preserves_both_rows(tmp_path):
    sheet=Spreadsheet();a=BetStore(tmp_path/'a',spreadsheet=sheet);b=BetStore(tmp_path/'b',spreadsheet=sheet)
    a.append('real',[])
    first=record(note='=not a formula');second=record(row=2)
    append=sheet.batch_update
    def race(body):
        sheet.batch_update=append
        assert b.append('real',[second])['written']==1
        return append(body)
    sheet.batch_update=race
    assert a.append('real',[first])['written']==1
    rows=a.read('real',reload=True)
    assert set(rows.row_id)=={first['row_id'],second['row_id']}
    assert rows.iloc[1]['note']=='=not a formula'
    assert a.append('real',[])['written']==b.append('real',[])['written']==0


def test_paper_concurrent_duplicate_is_audited_and_not_counted(tmp_path):
    from pinnacle_props.bet_store import canonical,duplicate_audit
    from pinnacle_props.live_money import performance
    sheet=Spreadsheet();a=BetStore(tmp_path/'a',spreadsheet=sheet);b=BetStore(tmp_path/'b',spreadsheet=sheet)
    a.append('paper',[])
    first=record();second=record(book='other')
    append=sheet.batch_update
    def race(body):
        sheet.batch_update=append;b.append('paper',[second]);return append(body)
    sheet.batch_update=race;a.append('paper',[first])
    raw=a.read('paper',reload=True,raw=True)
    assert len(raw)==2 and len(a.read('paper'))==1
    audit=duplicate_audit(raw,'paper')
    assert audit.iloc[0]['count']==2 and audit.iloc[0].canonical_row_id==second['row_id']
    assert performance(canonical(raw,'paper'))['pending']==1


def test_grade_relocates_uuid_and_rejects_last_moment_move(tmp_path):
    sheet=Spreadsheet();store=BetStore(tmp_path,spreadsheet=sheet)
    store.append('real',[record(),record(row=2)])
    old=store.read('real').to_dict('records')[0];new=dict(old,source='checked')
    ws=sheet.tabs['PP_Bet_Log'];ws.values[1:]=ws.values[1:][::-1]
    store.grade('real',[(old,new)]);assert ws.writes[-1]=='V3:AB3'
    old=store.read('real',reload=True).to_dict('records')[1];new=dict(old,source='again')
    ws.batch_get=lambda *a:[['different-row']]
    writes=list(ws.writes)
    with pytest.raises(StoreUnavailable):store.grade('real',[(old,new)])
    assert ws.writes==writes


def test_real_identity_is_uuid_not_scan_row(tmp_path):
    store=BetStore(tmp_path,spreadsheet=Spreadsheet())
    store.append('real',[record(),record()])
    assert len(store.read('real'))==2


def test_google_empty_sheet_response_is_empty_row(tmp_path):
    sheet=Spreadsheet();ws=sheet.add_worksheet('PP_Bet_Log',5000,len(HEADER));ws.values=[[]]
    store=BetStore(tmp_path,spreadsheet=sheet)
    assert store.append('real',[record()])['written']==1
    assert ws.values[0]==HEADER and len(store.read('real'))==1


@pytest.mark.parametrize('environment,secret,credential,expected', [
    (None, None, True, True), (None, True, True, True),
    ('1', None, True, True), ('true', None, True, True),
    (None, 'anything', True, True), (None, False, True, False),
    (None, ' false ', True, False), ('0', True, True, False),
    ('FALSE', True, True, False), ('1', False, True, False),
    (None, None, False, False), ('1', None, False, False),
])
def test_settlement_defaults_and_explicit_opt_out(monkeypatch,environment,secret,credential,expected):
    import streamlit as st
    from pinnacle_props.bet_store import enabled
    monkeypatch.delenv('PP_SETTLEMENT_ENABLED',raising=False)
    if environment is not None:monkeypatch.setenv('PP_SETTLEMENT_ENABLED',environment)
    secrets={}
    if credential:secrets['gcp_service_account']={'project_id':'fixture'}
    if secret is not None:secrets['PP_SETTLEMENT_ENABLED']=secret
    monkeypatch.setattr(st,'secrets',secrets)
    assert enabled() is expected


def test_settlement_missing_secret_file_is_disabled(monkeypatch):
    import streamlit as st
    from pinnacle_props.bet_store import enabled
    class Missing:
        def __contains__(self,key):raise FileNotFoundError('missing fixture')
    monkeypatch.delenv('PP_SETTLEMENT_ENABLED',raising=False)
    monkeypatch.setattr(st,'secrets',Missing())
    assert enabled() is False
