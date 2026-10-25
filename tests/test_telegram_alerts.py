from datetime import datetime,timezone
from pathlib import Path
import copy
import json
import pytest
from pinnacle_props.telegram_alerts import Telegram,AlertError,LocalAlerts,allowed,default_policy,message,maybe_alert_scan

NOW=datetime(2026,10,21,16,tzinfo=timezone.utc)
TOKEN='12345:fixture_token_only'

class Response:
    status_code=200
    def __init__(self,result):self.result=result
    def json(self):return {'ok':True,'result':self.result}

class Session:
    def __init__(self,*responses):self.responses=list(responses);self.calls=[]
    def post(self,url,**kw):
        self.calls.append((url,kw));result=self.responses.pop(0)
        if isinstance(result,Exception):raise result
        return result

class Sender:
    def __init__(self,fail=False):self.calls=[];self.fail=fail
    def send(self,text,*,document=None):
        self.calls.append((text,document))
        if self.fail:raise AlertError('delivery uncertain')
        return {'message_id':100}

def row(**kw):
    return dict(sport='nhl',event_id='event',player='Player',market='player_assists',line=.5,
        start_time='2099-01-01T00:00:00Z',side='over',book='draftkings',price_decimal=2.2,edge=.03,stake=7.15,scan_id='scan-1',**kw)

def manifest(scan='scan-1'):
    return {'scan_id':scan,'settings':{'sport':'nhl'},'capture_time':NOW.isoformat(),'credits_charged':28}


def test_transport_no_redirects_retries_markup_or_paid_broadcast():
    session=Session(Response({'message_id':7}));bot=Telegram(TOKEN,'42',session=session)
    assert bot.send('<b>plain bet text</b>')=={'message_id':7}
    url,k=session.calls[0]
    assert url.endswith('/sendMessage') and k['allow_redirects'] is False and k['timeout']==(10,30)
    assert k['data']['allow_paid_broadcast']=='false' and 'parse_mode' not in k['data']
    assert TOKEN not in repr(bot)


@pytest.mark.parametrize('code,kind',[(429,'rate limited'),(401,'authorization failed'),(403,'authorization failed'),(500,'delivery uncertain'),(302,'delivery uncertain')])
def test_http_errors_suppress_response_and_token(code,kind):
    response=Response({'message_id':1});response.status_code=code
    s=Session(response);bot=Telegram(TOKEN,'42',session=s)
    with pytest.raises(AlertError,match=kind) as error:bot.send('test')
    assert TOKEN not in str(error.value) and len(s.calls)==1


def test_network_exception_does_not_expose_token_or_url():
    s=Session(RuntimeError('https://api.telegram.org/bot'+TOKEN))
    with pytest.raises(AlertError) as error:Telegram(TOKEN,'42',session=s).send('test')
    assert TOKEN not in str(error.value) and error.value.__suppress_context__


def test_discovery_ignores_groups_and_requires_one_private_sender():
    def update(id,kind='private'):
        return {'message':{'chat':{'id':id,'type':kind},'from':{'id':id,'is_bot':False},'text':'private message'}}
    s=Session(Response([update(42),update(42),update(-2,'group')]))
    assert Telegram(TOKEN,session=s).discover_private_chat()=='42'
    assert s.calls[0][1]['data']=={'limit':100,'timeout':0}
    for updates in ([],[update(1),update(2)],[update(42)]*100):
        with pytest.raises(AlertError,match='ambiguous'):
            Telegram(TOKEN,session=Session(Response(updates))).discover_private_chat()


@pytest.mark.parametrize('hour,minute,expected',[(2,59,True),(3,0,False),(11,59,False),(12,0,True)])
def test_default_quiet_hours_eastern(hour,minute,expected):
    assert allowed(default_policy(),'nhl',datetime(2026,10,21,hour,minute,tzinfo=timezone.utc))==expected


def test_dst_and_nonwrapping_quiet_period_and_switches():
    p=default_policy()
    assert not allowed(p,'nhl',datetime(2026,11,1,5,30,tzinfo=timezone.utc))
    assert not allowed(p,'nhl',datetime(2026,11,1,6,30,tzinfo=timezone.utc))
    p['quiet_start']='12:00';p['quiet_end']='13:00'
    assert not allowed(p,'nhl',NOW)
    p['quiet_end']='12:00';assert allowed(p,'nhl',NOW)
    for field in ('enabled','alerts'):
        q=copy.deepcopy(p);q['sports']['nhl'][field]=False;assert not allowed(q,'nhl',NOW)
    p['enabled']=False;assert not allowed(p,'nhl',NOW)
    with pytest.raises(AlertError):allowed(default_policy(),'nhl',NOW.replace(tzinfo=None))


def test_one_message_dedupes_paper_keys_across_books_and_scans(tmp_path):
    sender=Sender();alerts=LocalAlerts(tmp_path,sender);other=dict(row(),book='fanduel')
    assert alerts.scan(manifest(),[row(),other],now=NOW)['bets']==1
    assert len(sender.calls)==1
    text=sender.calls[0][0]
    for expected in ('NHL','28 credits','Player','player_assists','over 0.5','draftkings 2.2','3.00 pp','$7.15'):assert expected in text
    assert alerts.scan(manifest(),[row()],now=NOW)['status'].startswith('already')
    assert LocalAlerts(tmp_path,sender).scan(manifest('scan-2'),[row()],now=NOW)['bets']==0
    assert len(sender.calls)==1


def test_prior_paper_rows_suppress_old_but_current_round_does_not(tmp_path):
    sender=Sender();alerts=LocalAlerts(tmp_path,sender)
    old=dict(row(),scan_id='old')
    assert alerts.scan(manifest(),[row()],prior_paper=[old],now=NOW)['bets']==0
    assert alerts.scan(manifest(),[row()],prior_paper=[row()],now=NOW)['status']=='sent'


def test_quiet_and_no_new_rows_never_send_or_mutate_state(tmp_path):
    sender=Sender();alerts=LocalAlerts(tmp_path,sender)
    assert alerts.scan(manifest(),[row()],now=NOW.replace(hour=4))['status'].startswith('suppressed')
    assert alerts.scan(manifest(),[],now=NOW)['bets']==0
    assert not sender.calls and not (tmp_path/'telegram/delivery.json').exists()


def test_uncertain_send_is_persisted_and_not_retried(tmp_path):
    sender=Sender(True);alerts=LocalAlerts(tmp_path,sender)
    assert alerts.scan(manifest(),[row()],now=NOW)['status']=='delivery uncertain'
    assert alerts.scan(manifest(),[row()],now=NOW)['status'].startswith('already')
    assert alerts.scan(manifest('new'),[row()],now=NOW)['bets']==0
    assert len(sender.calls)==1
    state=json.loads((tmp_path/'telegram/delivery.json').read_text())
    assert state['attempts']['scan:scan-1']['status']=='delivery uncertain'
    assert 'Player' not in json.dumps(state) and TOKEN not in json.dumps(state)


def test_failure_once_red_and_quiet_gated(tmp_path):
    sender=Sender();alerts=LocalAlerts(tmp_path,sender)
    assert alerts.failure('scan','run-1','nhl',now=NOW)['status']=='sent'
    alerts.failure('scan','run-1','nhl',now=NOW)
    alerts.failure('scan','run-2','nhl',now=NOW.replace(hour=4))
    assert len(sender.calls)==1 and sender.calls[0][0].startswith('🔴')
    with pytest.raises(AlertError):alerts.failure('https://example.invalid/secret','x','nhl',now=NOW)


def test_large_scan_uses_one_complete_document_no_truncation(tmp_path):
    rows=[dict(row(),player='Player '+str(i)) for i in range(100)]
    sender=Sender();LocalAlerts(tmp_path,sender).scan(manifest(),rows,now=NOW)
    caption,document=sender.calls[0]
    assert len(sender.calls)==1 and len(document.decode().splitlines())==101
    assert '100 new' in caption and 'Player 99' in document.decode()
    session=Session(Response({'message_id':5}));Telegram(TOKEN,'42',session=session).send(caption,document=document)
    assert session.calls[0][0].endswith('/sendDocument')
    assert session.calls[0][1]['files']['document'][1]==document


def test_corrupt_dedupe_state_fails_closed(tmp_path):
    path=tmp_path/'telegram/delivery.json';path.parent.mkdir();path.write_text('broken')
    sender=Sender()
    with pytest.raises(AlertError):LocalAlerts(tmp_path,sender).scan(manifest(),[row()],now=NOW)
    assert not sender.calls


def test_default_review_gate_never_reads_credentials_or_sheet(tmp_path,monkeypatch):
    monkeypatch.delenv('PP_TELEGRAM_REVIEW_ENABLED',raising=False)
    monkeypatch.setattr('pinnacle_props.telegram_alerts.runtime_values',lambda:pytest.fail('secret lookup'))
    assert maybe_alert_scan(manifest(),data_dir=tmp_path)['status']=='not activated'


def test_cloud_rejects_local_dedupe_fallback(tmp_path,monkeypatch):
    from pinnacle_props.settings import ACCOUNTING_CLOUD
    monkeypatch.setenv('PP_TELEGRAM_REVIEW_ENABLED','1');token=ACCOUNTING_CLOUD.set(True)
    try:assert maybe_alert_scan(manifest(),data_dir=tmp_path)['status']=='shared alert state required'
    finally:ACCOUNTING_CLOUD.reset(token)


def test_library_debug_logging_redacts_bot_url(caplog):
    import logging
    Telegram(TOKEN,'42',session=Session())
    with caplog.at_level(logging.DEBUG,logger='urllib3.connectionpool'):
        logging.getLogger('urllib3.connectionpool').debug('POST %s','/bot'+TOKEN+'/sendMessage')
    assert TOKEN not in caplog.text and '[REDACTED]' in caplog.text


def test_enabled_scan_hook_uses_verified_snapshot_rules_and_shared_history(tmp_path,monkeypatch):
    from test_pipeline import sample_result,settings,PROFILE
    from pinnacle_props.pipeline import _save_snapshot
    from pinnacle_props.bet_store import BetStore
    from tests.test_bet_store import Spreadsheet
    from pinnacle_props import rules
    from pinnacle_props.settings import ACCOUNTING_CLOUD
    active=rules.load();active['sports']['nba']['default']['min_edge']=0
    saved,_,_=_save_snapshot(tmp_path,'sample',settings(),PROFILE,'Player_Props',
        '2026-10-07T19:00:00Z',[sample_result(tmp_path)],[],1,1,display_rules=active)
    sender=Sender();sheet=BetStore(tmp_path,spreadsheet=Spreadsheet())
    monkeypatch.setenv('PP_TELEGRAM_REVIEW_ENABLED','1')
    monkeypatch.setattr('pinnacle_props.telegram_alerts.runtime_values',lambda:{'TELEGRAM_BOT_TOKEN':TOKEN,'TELEGRAM_CHAT_ID':'42'})
    monkeypatch.setattr('pinnacle_props.telegram_alerts.Telegram',lambda *a:sender)
    monkeypatch.setattr('pinnacle_props.bet_store.BetStore',lambda *a:sheet)
    policy=default_policy();policy['quiet_start']=policy['quiet_end']='00:00'
    path=tmp_path/'jobs/config.json';path.parent.mkdir();path.write_text(json.dumps(policy))
    token=ACCOUNTING_CLOUD.set(False)
    try:
        receipt=maybe_alert_scan(saved,data_dir=tmp_path,display_rules=active)
        assert receipt['status']=='sent' and receipt['bets']==1
        assert maybe_alert_scan(saved,data_dir=tmp_path,display_rules=active)['status'].startswith('already')
        assert len(sender.calls)==1
    finally:ACCOUNTING_CLOUD.reset(token)




def test_failed_claim_write_prevents_message(tmp_path,monkeypatch):
    sender=Sender()
    def fail(*args):raise OSError('disk unavailable')
    monkeypatch.setattr('pinnacle_props.telegram_alerts.atomic_write',fail)
    with pytest.raises(OSError):LocalAlerts(tmp_path,sender).scan(manifest(),[row()],now=NOW)
    assert not sender.calls


def test_failed_receipt_write_retains_claim_without_duplicate(tmp_path,monkeypatch):
    from pinnacle_props.odds_api import atomic_write
    count=[];sender=Sender()
    def write(*args):
        count.append(1)
        if len(count)==2:raise OSError('disk unavailable')
        atomic_write(*args)
    monkeypatch.setattr('pinnacle_props.telegram_alerts.atomic_write',write)
    alerts=LocalAlerts(tmp_path,sender)
    with pytest.raises(OSError):alerts.scan(manifest(),[row()],now=NOW)
    assert LocalAlerts(tmp_path,sender).scan(manifest(),[row()],now=NOW)['status'].startswith('already')
    assert len(sender.calls)==1




def test_started_or_unknown_rows_are_not_alerted_unless_scan_opted_in(tmp_path):
    sender=Sender();alerts=LocalAlerts(tmp_path,sender)
    past=dict(row(),start_time=NOW.isoformat())
    unknown=dict(row(),start_time='bad')
    assert alerts.scan(manifest(),[past,unknown],now=NOW)['bets']==0
    assert not sender.calls and not (tmp_path/'telegram/delivery.json').exists()
    opted=manifest();opted['settings']['include_live']=True
    assert alerts.scan(opted,[past,unknown],now=NOW)['bets']==1
    assert 'game started' in sender.calls[0][0]


def test_saved_scan_alert_uses_delivery_clock_not_capture_clock(tmp_path):
    from datetime import timedelta
    sender=Sender();alerts=LocalAlerts(tmp_path,sender)
    approaching=dict(row(),start_time=(NOW+timedelta(minutes=1)).isoformat())
    assert alerts.scan(manifest(),[approaching],now=NOW+timedelta(minutes=1))['bets']==0
    assert not sender.calls
