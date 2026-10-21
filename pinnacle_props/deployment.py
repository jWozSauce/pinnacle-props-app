"""Explicit deployment mode; Q38 temporarily waives lifetime cloud accounting."""
from .odds_api import PROJECT_CAP, Client, BudgetStop, utcnow


def deployment_settings(values):
    mode=values.get('PP_DEPLOYMENT','local')
    if mode not in ('local','cloud'):raise ValueError('Unknown deployment mode')
    if values.get('PP_STORE','local')!='local':raise ValueError('Shared storage is not configured yet')
    cloud=mode=='cloud'
    return dict(cloud=cloud,project_cap=100_000 if cloud else PROJECT_CAP,
                paid_enabled=True,cloud_daily_cap=2500 if cloud else None)


def reserve_cloud_budget(data_dir):
    """Q38 migration preserves all existing usage and runs under the request lock."""
    client=Client(data_dir=data_dir,lock_timeout=180)
    with client._locked():
        state=client._load()
        if state.get('project_cap') in (1_900_000,2_500_000,2_600_000,2_800_000):return
        if state.get('project_cap')!=2_000_000 or state['spent']>1_900_000 or state['halt'] or state['reservations']:
            raise BudgetStop('Unexpected ledger state; cannot reserve cloud allowance')
        state['project_cap']=1_900_000
        state.setdefault('budget_migrations',[]).append(dict(at=utcnow(),previous_persisted_cap=2_000_000,
            new_cap=1_900_000,cloud_reserve=100_000,approval='PLANS Q38 2026-10-07 21:48 EDT'))
        client._save(state)


def cloud_refresh_reason(data_dir,bound,daily_cap):
    """Read-only UI explanation; Client still enforces the authoritative guards."""
    import json
    from pathlib import Path
    from datetime import datetime
    from zoneinfo import ZoneInfo
    path=Path(data_dir)/'odds_budget.json'
    if not path.exists():
        return f'Estimated refresh exceeds the daily cloud cap ({daily_cap:,} credits). Limit game dates to scan fewer.' if bound>daily_cap else None
    try:
        state=json.loads(path.read_text())
        if state.get('halt') or state.get('reservations'):
            return 'Live requests are paused for a request-accounting issue.'
        cap=min(daily_cap,state.get('cloud_daily_cap',daily_cap))
        day=datetime.fromisoformat(utcnow()).astimezone(ZoneInfo('America/New_York')).date()
        spent=sum(r['actual'] if r['actual'] is not None else r['estimated']
                  for r in state['records']
                  if datetime.fromisoformat(r['timestamp'].replace('Z','+00:00')).astimezone(ZoneInfo('America/New_York')).date()==day)
        if spent>=cap:return 'Daily cloud cap reached.'
        if spent+bound>cap:return f'Estimated refresh exceeds the remaining daily cloud cap ({cap-spent:,} credits). Limit game dates to scan fewer.'
    except (ValueError,KeyError,TypeError,OSError):
        return 'Quota record is unavailable; live requests are paused.'
    return None
