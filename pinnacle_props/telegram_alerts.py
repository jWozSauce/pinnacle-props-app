"""Opt-in Telegram delivery. No credentials, network, or writes at import time.

Local delivery state supports one Mac writer during the interim. Hosted/droplet
activation must supply shared state before claiming cross-host deduplication.
"""
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import csv
import hashlib
import io
import json
import math
import logging
import os
import re
import requests
from .bet_store import key as paper_key
from .odds_api import atomic_write
from .records import record_lock
from .settings import accounting_dir
from .game_time import started,label


class AlertError(ValueError):
    def __init__(self,kind):
        self.kind=kind
        super().__init__('Telegram '+kind+'; details suppressed')


class _RedactTelegramURL(logging.Filter):
    def filter(self,record):
        text=record.getMessage()
        if '/bot' in text:
            record.msg=re.sub(r'/bot[0-9]+:[A-Za-z0-9_-]+','/bot[REDACTED]',text)
            record.args=()
        return True


class Telegram:
    def __init__(self,token,chat_id=None,*,session=None):
        if not isinstance(token,str) or not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+',token):
            raise AlertError('configuration invalid')
        if chat_id is not None and not re.fullmatch(r'-?[0-9]+',str(chat_id)):
            raise AlertError('configuration invalid')
        logger=logging.getLogger('urllib3.connectionpool')
        if not any(isinstance(f,_RedactTelegramURL) for f in logger.filters):
            logger.addFilter(_RedactTelegramURL())
        self._token=token;self._chat=str(chat_id) if chat_id is not None else None
        self._session=session or requests.Session()
        # Never inherit a requests retry adapter for message delivery.
        if session is None:
            self._session.mount('https://',requests.adapters.HTTPAdapter(max_retries=0))

    def __repr__(self):return '<Telegram credentials redacted>'

    def _request(self,method,data,*,files=None):
        if method not in ('sendMessage','sendDocument','getUpdates'):raise AlertError('method invalid')
        try:
            response=self._session.post('https://api.telegram.org/bot'+self._token+'/'+method,
                data=data,files=files,timeout=(10,30),allow_redirects=False)
            status=response.status_code
            if status==429:raise AlertError('rate limited')
            if status in (401,403):raise AlertError('authorization failed')
            if not 200<=status<300:raise AlertError('delivery uncertain')
            payload=response.json()
            if payload.get('ok') is not True:raise AlertError('request rejected')
            return payload['result']
        except AlertError:raise
        except Exception:raise AlertError('delivery uncertain') from None

    def send(self,text,*,document=None):
        if self._chat is None:raise AlertError('chat not configured')
        params={'chat_id':self._chat,'allow_paid_broadcast':'false','protect_content':'true'}
        if document is None:
            if not 0<len(text.encode('utf-16-le'))//2<=4096:raise AlertError('message too large')
            params.update(text=text,link_preview_options=json.dumps({'is_disabled':True}))
            result=self._request('sendMessage',params)
        else:
            if len(document)>10*1024**2 or len(text.encode('utf-16-le'))//2>1024:
                raise AlertError('attachment too large')
            params['caption']=text
            result=self._request('sendDocument',params,files={'document':('new-bets.csv',document,'text/csv')})
        if not isinstance(result.get('message_id'),int):raise AlertError('delivery uncertain')
        return {'message_id':result['message_id']}

    def discover_private_chat(self):
        # A single onboarding read, not a polling service. Do not acknowledge or
        # discard updates, disable webhooks, or select an arbitrary conversation.
        updates=self._request('getUpdates',{'limit':100,'timeout':0})
        if not isinstance(updates,list) or len(updates)>=100:raise AlertError('chat discovery ambiguous')
        chats=set()
        for update in updates:
            message=update.get('message',{});chat=message.get('chat',{});sender=message.get('from',{})
            if chat.get('type')=='private' and sender.get('is_bot') is False and chat.get('id')==sender.get('id'):
                chats.add(str(chat['id']))
        if len(chats)!=1:raise AlertError('chat discovery ambiguous')
        return chats.pop()


def default_policy():
    return {'version':1,'enabled':True,'quiet_start':'23:00','quiet_end':'08:00',
            'sports':{s:{'enabled':True,'alerts':True} for s in ('nfl','nhl','mlb','nba','wnba')}}


def validate_policy(policy):
    if policy.get('version')!=1 or type(policy.get('enabled')) is not bool:raise AlertError('policy invalid')
    for name in ('quiet_start','quiet_end'):
        if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',str(policy.get(name,''))):raise AlertError('policy invalid')
    if set(policy.get('sports',{}))!=set(default_policy()['sports']):raise AlertError('policy invalid')
    for values in policy['sports'].values():
        if any(type(values.get(k)) is not bool for k in ('enabled','alerts')):raise AlertError('policy invalid')
    return policy


def allowed(policy,sport,now):
    validate_policy(policy)
    if now.tzinfo is None:raise AlertError('clock invalid')
    if sport not in policy['sports']:raise AlertError('sport invalid')
    options=policy['sports'][sport]
    if not policy['enabled'] or not options['enabled'] or not options['alerts']:return False
    minute=now.astimezone(ZoneInfo('America/New_York')).strftime('%H:%M')
    start,end=policy['quiet_start'],policy['quiet_end']
    # Equal endpoints mean no quiet period, explicitly documented in the UI.
    quiet=(start<=minute<end) if start<end else (minute>=start or minute<end) if start>end else False
    return not quiet


def key_hash(row):
    return hashlib.sha256(json.dumps(paper_key(row,'paper'),ensure_ascii=True).encode()).hexdigest()


def _safe_cell(value):
    text=' '.join(str(value).split())
    # Documents can be opened in spreadsheets; formula-like names stay literal.
    return "'"+text if text.startswith(('=','+','-','@')) else text


def message(manifest,rows,*,now=None):
    when=datetime.fromisoformat(manifest['capture_time'].replace('Z','+00:00'))
    if when.tzinfo is None:raise AlertError('capture time invalid')
    when=when.astimezone(ZoneInfo('America/New_York')).strftime('%b %d %H:%M %Z')
    header=f"{manifest['settings']['sport'].upper()} · {when} · {int(manifest['credits_charged'])} credits\n{len(rows)} new rule-passing bets"
    from .recommendations import link
    from .game_display import game_label,player_label,book_link
    columns=['recommendation_url','sport','player','game','book_event_url','market','line','side','book','price_decimal','edge_points','suggested_stake','start_status']
    clean=[];lines=[]
    for row in rows:
        numbers=[float(row[k]) for k in ('line','price_decimal','edge','stake')]
        if not all(math.isfinite(x) for x in numbers) or numbers[1]<=1 or numbers[3]<=0:raise AlertError('bet invalid')
        line,price,edge,stake=numbers
        cleaned={k:_safe_cell(row[k]) for k in ('sport','player','market','side','book')}
        cleaned['player']=_safe_cell(player_label(row))
        cleaned.update(game=_safe_cell(game_label(row)),book_event_url=book_link(row) or '')
        cleaned.update(recommendation_url=link(row),line=line,price_decimal=price,edge_points=100*edge,suggested_stake=stake,start_status=label(row.get('start_time'),now=now));clean.append(cleaned)
        lines.append(f"{cleaned['player']} · {cleaned['game']} · {cleaned['market']} · {cleaned['side']} {line:g} | {cleaned['book']} {price:g} | edge {100*edge:.2f} pp | stake ${stake:.2f} | {cleaned['start_status']}\nLog this bet: {cleaned['recommendation_url']}")
    text=header+'\n\n'+'\n'.join(lines)
    if len(text.encode('utf-16-le'))//2<=4096:return text,None
    out=io.StringIO();writer=csv.DictWriter(out,fieldnames=columns);writer.writeheader();writer.writerows(clean)
    return header+'\nFull bet list attached.',out.getvalue().encode()


class LocalAlerts:
    """Persist a claim before HTTP; uncertain sends need manual reconciliation.

    This intentionally cannot promise exactly-once delivery after a process death.
    Prior attempted bet keys are consumed to prevent a second scan duplicating them.
    """
    def __init__(self,data_dir,transport,policy=None):
        self.data_dir=Path(data_dir);self.root=accounting_dir(data_dir)/'telegram'
        self.transport=transport;self.policy=validate_policy(policy or default_policy())

    def _load(self):
        path=self.root/'delivery.json'
        if not path.exists():return {'version':1,'seen':[],'attempts':{}}
        try:
            state=json.loads(path.read_text())
            assert state['version']==1 and isinstance(state['seen'],list) and isinstance(state['attempts'],dict)
            return state
        except Exception:raise AlertError('delivery state invalid') from None

    def _save(self,state):
        atomic_write(self.root/'delivery.json',json.dumps(state,sort_keys=True).encode())

    def _deliver(self,state,ident,text,document,keys,now):
        if ident in state['attempts']:return {'status':'already attempted; inspect receipt'}
        state['seen']=sorted(set(state['seen'])|set(keys))
        state['attempts'][ident]={'status':'delivery uncertain','at':now.isoformat(),'bets':len(keys)}
        self._save(state)
        try:
            result=self.transport.send(text,document=document)
            state['attempts'][ident].update(status='sent',**result)
        except AlertError as exc:
            state['attempts'][ident]['status']=exc.kind
        except Exception:
            state['attempts'][ident]['status']='delivery uncertain'
        self._save(state)
        return dict(state['attempts'][ident])

    def scan(self,manifest,rows,*,prior_paper=(),now=None):
        now=now or datetime.now(timezone.utc);sport=manifest['settings']['sport']
        if not allowed(self.policy,sport,now):return {'status':'suppressed by scan/alert policy'}
        ident='scan:'+str(manifest['scan_id'])
        with record_lock(self.data_dir,'telegram-alerts'):
            state=self._load()
            if ident in state['attempts']:return {'status':'already attempted; inspect receipt'}
            prior={key_hash(r) for r in prior_paper if r.get('scan_id')!=manifest['scan_id']}
            seen=set(state['seen'])|prior;selected=[];keys=[]
            for row in rows:
                try:past=started(row.get('start_time'),now=now)
                except ValueError:continue
                if past and manifest['settings'].get('include_live') is not True:continue
                k=key_hash(row)
                if k not in seen:seen.add(k);keys.append(k);selected.append(row)
            if not selected:return {'status':'no new rule-passing bets','bets':0}
            text,document=message(manifest,selected,now=now)
            return self._deliver(state,ident,text,document,keys,now)

    def failure(self,job,run_id,sport,*,now=None):
        now=now or datetime.now(timezone.utc)
        if not allowed(self.policy,sport,now):return {'status':'suppressed by scan/alert policy'}
        # Never interpolate exception messages, URLs, credentials or arbitrary job text.
        if job not in ('scan','close','grade','paper-validation'):raise AlertError('job invalid')
        ident='failure:'+job+':'+hashlib.sha256(str(run_id).encode()).hexdigest()
        with record_lock(self.data_dir,'telegram-alerts'):
            return self._deliver(self._load(),ident,f'🔴 {sport.upper()} {job} failed. Open Jobs for details.',None,[],now)

    def test(self,sport,request_id,*,now=None):
        now=now or datetime.now(timezone.utc)
        if not allowed(self.policy,sport,now):return {'status':'suppressed by scan/alert policy'}
        with record_lock(self.data_dir,'telegram-alerts'):
            ident='test:'+hashlib.sha256(str(request_id).encode()).hexdigest()
            return self._deliver(self._load(),ident,'Pinnacle Props · Telegram test alert',None,[],now)


def review_enabled():
    # Internal acceptance switch. No extra credential required of Josh for release.
    return os.environ.get('PP_TELEGRAM_REVIEW_ENABLED')=='1'


def runtime_values():
    # Resolve only the two requested secrets, only on an enabled delivery path.
    names=('TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID');values={n:os.environ.get(n) for n in names}
    if not all(values.values()):
        try:
            import streamlit as st
            for name in names:
                if not values[name]:values[name]=st.secrets.get(name)
        except Exception:pass
    if not all(values.values()):raise AlertError('not configured')
    return values


def maybe_alert_scan(manifest,*,data_dir,display_rules=None):
    if not review_enabled():return {'status':'not activated'}
    # Cross-host shared claims are not yet implemented; no ephemeral cloud fallback.
    from .settings import ACCOUNTING_CLOUD
    if ACCOUNTING_CLOUD.get():return {'status':'shared alert state required'}
    try:
        from .records import snapshots,flags
        from .paper import frozen
        from .bet_store import BetStore
        from . import rules
        policy_path=accounting_dir(data_dir)/'jobs/config.json'
        policy=json.loads(policy_path.read_text()) if policy_path.exists() else default_policy()
        if not allowed(policy,manifest['settings']['sport'],datetime.now(timezone.utc)):
            return {'status':'suppressed by scan/alert policy'}
        values=runtime_values()
        path,saved=snapshots(data_dir,scan_id=manifest['scan_id'])[0]
        active=display_rules if display_rules is not None else rules.active()
        frame=rules.annotate(flags(path,saved),active,manifest['settings']['sport'])
        rows=[frozen(saved,r,i+1,paper=True,display_rules=active) for i,r in frame.iterrows()
              if r['rule_pass'] and float(r['kellyAmount'])>0]
        if not rows:return {'status':'no new rule-passing bets','bets':0}
        store=BetStore(data_dir);_,contents=store._worksheet('paper')
        prior=store._records(contents)
        if store._path('paper','outbox').exists():
            from .bet_store import _read
            prior+=_read(store._path('paper','outbox'))
        transport=Telegram(values['TELEGRAM_BOT_TOKEN'],values['TELEGRAM_CHAT_ID'])
        return LocalAlerts(data_dir,transport,policy).scan(saved,rows,prior_paper=prior)
    except Exception:
        # A notification failure never asks callers to repeat a paid scan.
        return {'status':'alert preparation failed; no scan retry'}


def maybe_alert_failure(job,run_id,sport,*,data_dir):
    if not review_enabled():return {'status':'not activated'}
    from .settings import ACCOUNTING_CLOUD
    if ACCOUNTING_CLOUD.get():return {'status':'shared alert state required'}
    try:
        path=accounting_dir(data_dir)/'jobs/config.json'
        policy=json.loads(path.read_text()) if path.exists() else default_policy()
        if not allowed(policy,sport,datetime.now(timezone.utc)):
            return {'status':'suppressed by scan/alert policy'}
        values=runtime_values()
        return LocalAlerts(data_dir,Telegram(values['TELEGRAM_BOT_TOKEN'],values['TELEGRAM_CHAT_ID']),policy).failure(job,run_id,sport)
    except Exception:return {'status':'alert preparation failed; no job retry'}
