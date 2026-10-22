"""Declarative sport, market and book settings."""
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]
ACCOUNTING_PROJECT_ROOT=ROOT


def load_config(path=ROOT/'config/scanner.yaml'):
    with Path(path).open() as f:
        config=yaml.safe_load(f)
    if not isinstance(config,dict) or not config.get('sports') or not config.get('books'):
        raise ValueError('Scanner configuration missing sports or books')
    return config


def accounting_dir(data_dir, *, cloud=False):
    """Q48: only the real local workspace spends from the non-synced ledger.

    Isolated fixture/custom stores and the cloud retain their own accounting.
    """
    data_dir=Path(data_dir)
    if not (cloud or ACCOUNTING_CLOUD.get()) and data_dir.resolve()==(ACCOUNTING_PROJECT_ROOT/'data').resolve():
        return Path.home()/'Library/Application Support/pinnacle-props'
    return data_dir


from contextvars import ContextVar
ACCOUNTING_CLOUD=ContextVar('accounting_cloud',default=False)


def lock_dir(data_dir):
    path=Path(data_dir)
    if not ACCOUNTING_CLOUD.get() and path.resolve().is_relative_to((ACCOUNTING_PROJECT_ROOT/'data').resolve()):
        return accounting_dir(ACCOUNTING_PROJECT_ROOT/'data')/'locks'/path.resolve().relative_to((ACCOUNTING_PROJECT_ROOT/'data').resolve())
    return path


def close_manifest_dir(data_dir):
    """Mutable close state follows the Q48 ledger, after an explicit migration.

    Never import a Dropbox report back over authoritative operational state.
    Fixture and cloud stores retain their existing isolated paths.
    """
    data_dir=Path(data_dir)
    runtime=accounting_dir(data_dir)
    if runtime!=data_dir:
        target=runtime/'close-manifests'
        if not (target/'migration.json').exists():
            raise ValueError('Close manifest migration required; no requests sent')
        return target
    return data_dir/'closes/manifests'


def alternate_pairing_enabled():
    """Q71 accepted: main and alternate quotes pair by exact canonical contract."""
    import os
    return os.environ.get('PP_SAME_LINE_ALTERNATES_ENABLED', '1') == '1'


def scan_markets(spec):
    return spec['markets'] + (spec.get('alternate_markets', []) if alternate_pairing_enabled() else [])
