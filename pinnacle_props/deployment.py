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
        if state.get('project_cap') in (1_900_000,2_500_000,2_600_000):return
        if state.get('project_cap')!=2_000_000 or state['spent']>1_900_000 or state['halt'] or state['reservations']:
            raise BudgetStop('Unexpected ledger state; cannot reserve cloud allowance')
        state['project_cap']=1_900_000
        state.setdefault('budget_migrations',[]).append(dict(at=utcnow(),previous_persisted_cap=2_000_000,
            new_cap=1_900_000,cloud_reserve=100_000,approval='PLANS Q38 2026-10-07 21:48 EDT'))
        client._save(state)
