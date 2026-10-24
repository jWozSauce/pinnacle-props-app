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
    """Observed prop coverage only; this is not the selectable-book universe."""
    return {key: book for key, book in catalog['books'].items()
            if key != 'pinnacle' and book.get('sports', {}).get(sport, {}).get('markets')}



def selectable_books(catalog, sport, defaults):
    """Every documented book, plus preserved defaults, regardless of observation."""
    selected = {key: catalog['books'].get(key, dict(label=label, sports={}))
                for key, label in defaults.items()}
    selected.update({k: v for k, v in catalog['books'].items() if k != 'pinnacle'})
    return selected


GROUPS = ('US sportsbooks', 'US exchanges and prediction markets',
          'Offshore', 'Europe and elsewhere')
OFFSHORE = {'betonlineag', 'betus', 'bovada', 'lowvig', 'mybookieag',
            'betanysports', 'gtbets', 'everygame'}
DOCUMENTATION = 'https://the-odds-api.com/sports-odds-data/bookmaker-apis.html'


def clean_label(value):
    return ' '.join(value.replace('(opens new window)', '').split())


def book_group(key, book):
    regions = book.get('regions', [])
    if key in OFFSHORE: return GROUPS[2]
    if 'us_ex' in regions: return GROUPS[1]
    if any(r in regions for r in ('us', 'us2', 'us_dfs')): return GROUPS[0]
    return GROUPS[3]


def render_picker(catalog, sport, defaults, query_books):
    """Offline grouped controls backed by one canonical per-sport selection."""
    import streamlit as st
    selectable = selectable_books(catalog, sport, defaults)
    state_key = 'books_' + sport
    if state_key not in st.session_state:
        st.session_state[state_key] = [k for k in query_books if k in selectable]
    # Canonical state survives hiding a book through search or collapsing a group.
    def set_books(values):
        st.session_state[state_key] = list(dict.fromkeys(values))
    def toggle(key, widget):
        values = list(st.session_state[state_key])
        set_books(values + [key] if st.session_state[widget] else [k for k in values if k != key])
    def select_group(keys):
        set_books(list(st.session_state[state_key]) + keys)
    st.button('Reset to default eleven', key='reset_books_' + sport,
              on_click=set_books, args=(list(defaults),))
    search = st.text_input('Search bookmakers', key='book_search_' + sport,
                           placeholder='Name or API key').strip().casefold()
    for index, group in enumerate(GROUPS):
        members = sorted((k for k, v in selectable.items() if book_group(k, v) == group),
                         key=lambda k: (clean_label(selectable[k]['label']).casefold(), k))
        visible = [k for k in members if search in (k + ' ' + clean_label(selectable[k]['label'])).casefold()]
        with st.expander(f'{group} · {len(members)} books', expanded=bool(search and visible)):
            st.button('Select all in group', key=f'book_group_{sport}_{index}',
                      on_click=select_group, args=(members,), disabled=not members,
                      help='Selects the entire group, including books hidden by search.')
            if not visible: st.caption('No matching bookmakers.')
            for key in visible:
                widget = f'book_choice_{sport}_{key}'
                st.session_state[widget] = key in st.session_state[state_key]
                st.checkbox(clean_label(selectable[key]['label']) + ' · ' + key, key=widget,
                            on_change=toggle, args=(key, widget))
                if not selectable[key].get('sports', {}).get(sport, {}).get('markets'):
                    st.caption('no props seen yet')
    from collections import Counter
    labels = {key: clean_label(book['label']) for key, book in selectable.items()}
    counts = Counter(labels.values())
    return st.multiselect('Selected books', list(selectable), key=state_key,
                          format_func=lambda key: labels[key] + (' · ' + key if counts[labels[key]] > 1 else ''))


def documentation_definitions(html):
    """Parse documented table columns, merging keys that occur in several regions."""
    from html.parser import HTMLParser
    import re
    class Tables(HTMLParser):
        def __init__(self):
            super().__init__(); self.tables=[]; self.table=None; self.row=None; self.cell=None
        def handle_starttag(self, tag, attrs):
            if tag == 'table': self.table=[]
            elif tag == 'tr' and self.table is not None: self.row=[]
            elif tag in ('td', 'th') and self.row is not None: self.cell=[]
        def handle_data(self, data):
            if self.cell is not None: self.cell.append(data)
        def handle_endtag(self, tag):
            if tag in ('td', 'th') and self.cell is not None:
                self.row.append(clean_label(''.join(self.cell))); self.cell=None
            elif tag == 'tr' and self.row is not None:
                self.table.append(self.row); self.row=None
            elif tag == 'table' and self.table is not None:
                self.tables.append(self.table); self.table=None
    parser=Tables(); parser.feed(html)
    books={}
    for table in parser.tables:
        if not table or 'Bookmaker key' not in table[0]: continue
        headers=table[0]
        try: columns=[headers.index(k) for k in ('Region key','Bookmaker key','Bookmaker')]
        except ValueError as exc: raise ValueError('Bookmaker documentation table changed') from exc
        for row in table[1:]:
            if not row: continue
            if len(row) <= max(columns): raise ValueError('Incomplete bookmaker documentation row')
            region,key,label=[row[i] for i in columns]
            if not all(re.fullmatch(r'[a-z0-9_]+', v) for v in (region,key)) or not label:
                raise ValueError('Invalid bookmaker documentation row')
            kind='dfs' if region=='us_dfs' else 'exchange' if region=='us_ex' or key.startswith('betfair_ex_') else 'sportsbook'
            entry=books.setdefault(key,dict(key=key,label=label,regions=[],kind=kind))
            if region not in entry['regions']: entry['regions'].append(region)
    if not books or 'pinnacle' not in books: raise ValueError('No complete bookmaker documentation catalog')
    return list(books.values())


def merge_documentation(catalog, definitions, *, source_sha256):
    """Update definitions without fabricating or discarding dated prop observations."""
    from copy import deepcopy
    updated=deepcopy(catalog)
    for definition in definitions:
        key=definition['key']
        updated['books'].setdefault(key,dict(sports={})).update(definition)
    updated['documentation']=DOCUMENTATION
    updated['definitions_refreshed_at']=datetime.now(timezone.utc).isoformat()
    updated['definitions_sha256']=source_sha256
    updated['regions']=sorted({r for d in definitions for r in d['regions']})
    return updated


def build_catalog(definitions, preview, client, *, receipt_dir, cap=CAP):
    """Resume the same client run from immutable raw caches; never exceed its cap.

    The caller must log the bound and approve this explicit operation. Sparse
    leagues go first; actual response charges determine whether later pulls fit.
    A budget stop leaves complete receipts for earlier sports, never a false zero.
    """
    if not 0 < cap or client.run_cap > cap:
        raise ValueError('Catalog requires a positive reviewed estimate')
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
    from .live_api import accounting,charged
    catalog['accounting']=accounting(client)
    catalog['credits'] = charged(client)
    catalog['complete'] = all(v['status'] in ('observed', 'deferred: no listed future games')
                              for v in catalog['sports'].values())
    return catalog
