"""Offline bookmaker catalog, explicit bounded discovery, and canonical selection."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import gzip
import json
import os

from .settings import ROOT
from .odds_api import BudgetStop, write_json

CATALOG = ROOT / 'config/bookmaker_catalog.json'
REGIONS = ('us', 'us2', 'us_ex', 'us_dfs', 'eu', 'uk', 'au', 'ca', 'fr', 'se', 'fi')
CAP = 600


def enabled():
    """Accepted catalog defaults on; explicit zero retains the legacy picker."""
    return os.environ.get('PP_BOOKMAKER_CATALOG_ENABLED', '1') != '0'


def load(path=CATALOG):
    data = json.loads(Path(path).read_text())
    if data.get('schema_version') != 1 or not isinstance(data.get('books'), dict):
        raise ValueError('Invalid bookmaker catalog')
    return data


def canonical_books(books, reference='pinnacle'):
    return list(dict.fromkeys([reference] + list(books)))


def region_units(books, reference='pinnacle'):
    return (len(canonical_books(books, reference)) + 9) // 10


def cost_caption(books, reference='pinnacle'):
    count = len(canonical_books(books, reference))
    return f'{count} books selected ≈ {region_units(books, reference)}× region cost per pull'


def sport_books(catalog, sport):
    """Only observed player-prop books can be selected; no future coverage claim."""
    return {key: book for key, book in catalog['books'].items()
            if key != 'pinnacle' and book.get('sports', {}).get(sport, {}).get('markets')}



def selectable_books(catalog, sport, defaults):
    """Observed books plus Josh's preserved eleven defaults (20:27 EDT ruling)."""
    selected = {key: catalog['books'].get(key, dict(label=label, sports={}))
                for key, label in defaults.items()}
    selected.update(sport_books(catalog, sport))
    return selected


def build_catalog(definitions, preview, client, *, receipt_dir, cap=CAP):
    """Resume the same client run from immutable raw caches; never exceed its cap.

    The caller must log the bound and approve this explicit operation. Sparse
    leagues go first; actual response charges determine whether later pulls fit.
    A budget stop leaves complete receipts for earlier sports, never a false zero.
    """
    if not 0 < cap <= CAP or client.run_cap > cap:
        raise ValueError('Catalog cap must not exceed 600 credits')
    folder = Path(receipt_dir)
    folder.mkdir(parents=True, exist_ok=True)
    books = {b['key']: dict(b, sports={}) for b in definitions}
    catalog = dict(schema_version=1, discovered_at=datetime.now(timezone.utc).isoformat(),
                   documentation='https://the-odds-api.com/sports-odds-data/bookmaker-apis.html',
                   regions=list(REGIONS), run_id=client.run_id, cap=cap,
                   books=books, sports={})
    total = 0
    stopped = False
    for sport in ('wnba', 'mlb', 'nhl', 'nba', 'nfl'):
        spec = preview[sport]
        event = spec['event']
        if not event:
            catalog['sports'][sport] = dict(status='deferred: no listed future games')
            continue
        if stopped:
            catalog['sports'][sport] = dict(status='deferred: run stopped before request')
            continue
        params = dict(markets=','.join(spec['markets']), regions=','.join(REGIONS), oddsFormat='decimal')
        path = f"/v4/sports/{spec['sport_key']}/events/{event['id']}/odds"
        try:
            result = client.request(path, params)
        except BudgetStop:
            catalog['sports'][sport] = dict(status='deferred: next request exceeds remaining budget or another budget guard')
            stopped = True
            continue
        raw = Path(result.cache_path).read_bytes()
        envelope_hash = hashlib.sha256(raw).hexdigest()
        receipt = dict(request_id=result.request_id, raw_sha256=envelope_hash,
                       captured_at=json.loads(gzip.decompress(raw))['captured_at'],
                       event_sha256=hashlib.sha256(event['id'].encode()).hexdigest(),
                       regions=list(REGIONS), requested_markets=spec['markets'],
                       credits=int(result.headers['x-requests-last']))
        total += receipt['credits']
        catalog['sports'][sport] = dict(status='observed', receipt=receipt)
        for book in result.payload['bookmakers']:
            key = book['key']
            # Provider additions are visible, but unknown classification is not guessed.
            entry = books.setdefault(key, dict(key=key, label=book['title'], regions=[],
                                               kind='unclassified', sports={}))
            markets = sorted({m['key'] for m in book['markets'] if m.get('outcomes')})
            if markets:
                entry['sports'][sport] = dict(markets=markets, receipt=receipt['request_id'])
        write_json(folder / f'{sport}-receipt.json', receipt)
    catalog['credits'] = total
    catalog['complete'] = all(v['status'] in ('observed', 'deferred: no listed future games')
                              for v in catalog['sports'].values())
    return catalog
