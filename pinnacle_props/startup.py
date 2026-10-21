"""Fixed, secret-safe settlement startup diagnostics for scheduled jobs."""
from datetime import datetime, timezone
import hashlib
import json
import os
import sys
from .bet_store import settlement_unavailable_reason
from .odds_api import atomic_write
from .records import record_lock
from .settings import accounting_dir


def _telegram(text):
    """One bounded attempt; callers persist the claim before transport."""
    try:
        token=os.environ.get('TELEGRAM_BOT_TOKEN')
        chat=os.environ.get('TELEGRAM_CHAT_ID')
        if not token or not chat:
            import streamlit as st
            token=token or st.secrets.get('TELEGRAM_BOT_TOKEN')
            chat=chat or st.secrets.get('TELEGRAM_CHAT_ID')
        if not token or not chat:return 'not configured'
        import requests
        response=requests.post('https://api.telegram.org/bot'+str(token)+'/sendMessage',
                               json={'chat_id':chat,'text':text},timeout=(10,20))
        return 'sent' if response.status_code==200 and response.json().get('ok') is True else 'delivery failed'
    except Exception:
        return 'delivery uncertain; no automatic retry'


def check_settlement(job, *, data_dir, scheduled=False, now=None):
    if job not in ('grade','paper-validation'):raise ValueError('Unknown settlement job')
    reason=settlement_unavailable_reason()
    if not reason:return True
    message=f'CRITICAL: Pinnacle Props {job} cannot start: {reason}. No grading or scan attempted.'
    print(message,file=sys.stderr,flush=True)
    if not scheduled:return False
    now=now or datetime.now(timezone.utc)
    report={'at':now.isoformat(),'job':job,'status':'settlement unavailable','reason':reason}
    try:
        with record_lock(data_dir,'settlement-startup'):
            folder=accounting_dir(data_dir)/'settlement-startup'
            ident=hashlib.sha256((job+'|'+reason+'|'+now.date().isoformat()).encode()).hexdigest()
            attempt=folder/(ident+'.json')
            if attempt.exists():
                report['telegram']='already attempted today; inspect receipt'
            else:
                # Claim before any request; an uncertain response must never resend.
                atomic_write(attempt,json.dumps(dict(report,telegram='attempt claimed')).encode())
                report['telegram']=_telegram(message)
                atomic_write(attempt,json.dumps(report).encode())
                # Missing configuration did not send anything; permit a later configured attempt.
                if report['telegram']=='not configured':attempt.unlink()
            atomic_write(folder/(job+'-latest.json'),json.dumps(report).encode())
    except Exception:
        print('CRITICAL: Startup failure evidence or alert could not be recorded; no job retry.',file=sys.stderr,flush=True)
    return False
