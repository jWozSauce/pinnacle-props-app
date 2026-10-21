"""Deployment mode under TASK-25 account-floor-only live accounting."""
from .odds_api import PROJECT_CAP, Client, BudgetStop, utcnow


def deployment_settings(values):
    mode=values.get('PP_DEPLOYMENT','local')
    if mode not in ('local','cloud'):raise ValueError('Unknown deployment mode')
    if values.get('PP_STORE','local')!='local':raise ValueError('Shared storage is not configured yet')
    cloud=mode=='cloud'
    return dict(cloud=cloud,account_floor=100_000,
                paid_enabled=True)


def reserve_cloud_budget(data_dir):
    """Historical Q38 migration only; no live entry point calls this retired helper."""
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
    """The current free preflight decides, never a stale ledger or reserve."""
    return None
