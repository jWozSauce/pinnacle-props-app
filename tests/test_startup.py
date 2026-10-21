import json
from datetime import datetime,timezone
import pytest
from pinnacle_props import startup
from pinnacle_props.bet_store import settlement_unavailable_reason

@pytest.mark.parametrize('environment,secrets,reason',[
 ('0',{'gcp_service_account':{}},'process environment'),
 (None,{'PP_SETTLEMENT_ENABLED':False,'gcp_service_account':{}},'Streamlit secrets'),
 (None,{},'configuration is absent'),
 (None,{'gcp_service_account':{}},''),
])
def test_reason(monkeypatch,environment,secrets,reason):
    import streamlit as st
    monkeypatch.delenv('PP_SETTLEMENT_ENABLED',raising=False)
    if environment:monkeypatch.setenv('PP_SETTLEMENT_ENABLED',environment)
    monkeypatch.setattr(st,'secrets',secrets)
    actual=settlement_unavailable_reason()
    assert reason in actual
    assert bool(actual)==bool(reason)


def test_scheduled_failure_claims_before_send_and_deduplicates(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(startup,'settlement_unavailable_reason',lambda:'fixture disabled')
    calls=[]
    def send(message):
        receipts=list((tmp_path/'settlement-startup').glob('*.json'))
        assert any(json.loads(p.read_text()).get('telegram')=='attempt claimed' for p in receipts)
        calls.append(message);return 'delivery uncertain; no automatic retry'
    monkeypatch.setattr(startup,'_telegram',send)
    for _ in range(2):assert not startup.check_settlement('grade',data_dir=tmp_path,scheduled=True,now=datetime(2026,10,21,tzinfo=timezone.utc))
    assert len(calls)==1
    assert capsys.readouterr().err.count('CRITICAL:')==2
    report=json.loads((tmp_path/'settlement-startup/grade-latest.json').read_text())
    assert report['reason']=='fixture disabled'


def test_healthy_startup_does_not_alert_or_write(tmp_path,monkeypatch):
    monkeypatch.setattr(startup,'settlement_unavailable_reason',lambda:'')
    monkeypatch.setattr(startup,'_telegram',lambda _:pytest.fail('unexpected alert'))
    assert startup.check_settlement('grade',data_dir=tmp_path,scheduled=True)
    assert not list(tmp_path.iterdir())


def test_transport_exception_is_redacted(monkeypatch):
    import requests
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN','fixture-token');monkeypatch.setenv('TELEGRAM_CHAT_ID','fixture-chat')
    def fail(*a,**k):raise ValueError('private transport detail')
    monkeypatch.setattr(requests,'post',fail)
    assert startup._telegram('fixture')=='delivery uncertain; no automatic retry'
