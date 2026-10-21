"""Explicit deployment mode; Q38 temporarily waives lifetime cloud accounting."""
from .odds_api import PROJECT_CAP, Client, BudgetStop, utcnow


def deployment_settings(values):
    mode=values.get('PP_DEPLOYMENT','local')
    if mode not in ('local','cloud'):raise ValueError('Unknown deployment mode')
    if values.get('PP_STORE','local')!='local':raise ValueError('Shared storage is not configured yet')
    cloud=mode=='cloud'
    return dict(cloud=cloud,project_cap=100_000 if cloud else PROJECT_CAP,
                paid_enabled=True)


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


def cloud_refresh_reason(data_dir,bound):
    """Read-only reserve/floor explanation; Client rechecks fresh quota before send."""
    import json
    from pathlib import Path
    path=Path(data_dir)/'odds_budget.json'
    state={}
    try:
        if path.exists():state=json.loads(path.read_text())
        if state.get('halt') or state.get('reservations'):
            return 'Live requests are paused for a request-accounting issue.'
        persisted=state.get('project_cap',100000);spent=state.get('spent',0)
        if type(persisted) is not int or persisted<0 or type(spent) is not int or spent<0:
            raise ValueError('Invalid ledger')
        cap=min(100000,persisted)
        if spent+bound>cap:
            return f'Estimated refresh exceeds the remaining cloud reserve ({max(0,cap-spent):,} credits). Limit game dates to scan fewer.'
        remaining=state.get('remaining')
        if remaining is not None:
            if type(remaining) is not int or remaining<0:raise ValueError('Invalid quota')
            if remaining-bound<100000:return 'Estimated refresh would breach the 100,000-credit account floor.'
    except (ValueError,KeyError,TypeError,OSError):
        return 'Quota record is unavailable; live requests are paused.'
    return None
