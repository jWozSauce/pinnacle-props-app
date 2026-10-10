"""Versioned display rules. Never discard scan rows or change acquisition settings."""
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import zlib

import pandas as pd
import yaml
from .odds_api import atomic_write
from .settings import ROOT

DEFAULT_PATH = ROOT / 'config/rules.yaml'
LIVE_PATH = Path.home() / 'Library/Application Support/pinnacle-props/rules.json'
SPORTS = ('nba', 'nhl', 'nfl', 'mlb', 'wnba')
FIELDS = {'min_edge', 'side', 'enabled'}
LABELS = {'player_threes': 'threes', 'player_points_rebounds_assists': 'PRA',
          'player_assists': 'assists', 'player_total_saves': 'saves'}


def _entry(value, *, default=False):
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise ValueError('Rule fields must be minimum edge, side and enabled')
    value = dict(value)
    if default:
        if 'min_edge' not in value: raise ValueError('Each sport needs a default minimum edge')
        value = {'side':'both', 'enabled':True, **value}
    if 'min_edge' in value:
        n = value['min_edge']
        if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or not 0 <= n <= 100:
            raise ValueError('Minimum edge must be a finite percentage-point value from 0 to 100')
        value['min_edge'] = float(n)
    if 'side' in value and value['side'] not in ('over', 'under', 'both'):
        raise ValueError('Side must be over, under or both')
    if 'enabled' in value and not isinstance(value['enabled'], bool):
        raise ValueError('Enabled must be true or false')
    return value


def validate(value):
    if not isinstance(value, dict) or set(value) != {'version', 'sports'} or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('Unsupported rule document')
    if not isinstance(value['sports'], dict) or set(value['sports']) != set(SPORTS):
        raise ValueError('Rules must include NBA, NHL, NFL, MLB and WNBA')
    sports = {}
    for sport in SPORTS:
        spec = value['sports'][sport]
        if not isinstance(spec, dict) or set(spec) != {'default', 'markets'} or not isinstance(spec['markets'], dict):
            raise ValueError('Each sport requires default and market rules')
        markets = {}
        for market, entry in spec['markets'].items():
            if not isinstance(market,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,99}', market):
                raise ValueError('Invalid market identifier')
            markets[market] = _entry(entry)
        if len(markets) > 300: raise ValueError('Too many market rules')
        sports[sport] = dict(default=_entry(spec['default'], default=True), markets=markets)
    return dict(version=1, sports=sports)


def canonical(value):
    return json.dumps(validate(value), sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def version(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def load(path=None):
    path = Path(path) if path is not None else DEFAULT_PATH
    try:
        return validate(yaml.safe_load(path.read_text()))
    except (yaml.YAMLError, UnicodeError) as exc:
        raise ValueError('Cannot read rule document') from exc


def active(*, cloud=False):
    return load(LIVE_PATH if not cloud and LIVE_PATH.exists() else DEFAULT_PATH)


@contextmanager
def _lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix+'.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try: yield
        finally: fcntl.flock(handle, fcntl.LOCK_UN)


def save(value, path=None):
    path = Path(path) if path is not None else LIVE_PATH
    body = canonical(value) + b'\n'
    with _lock(path): atomic_write(path, body)


def save_default(value):
    value = validate(value)
    with _lock(DEFAULT_PATH):
        atomic_write(DEFAULT_PATH, yaml.safe_dump(value, sort_keys=False).encode())


def encode(value):
    return base64.urlsafe_b64encode(zlib.compress(canonical(value))).decode().rstrip('=')


def decode(text):
    if not isinstance(text, str) or len(text) > 12000: raise ValueError('Invalid rule link')
    try:
        raw = base64.b64decode(text + '='*(-len(text)%4), altchars=b'-_', validate=True)
        decoder = zlib.decompressobj()
        body = decoder.decompress(raw, 65537)
        if len(body)>65536 or not decoder.eof or decoder.unused_data: raise ValueError('Invalid rule link')
        return validate(json.loads(body))
    except (ValueError, TypeError, zlib.error, UnicodeError) as exc:
        raise ValueError('Invalid rule link; reset to the default or remove the rules parameter') from exc


def annotate(frame, value, sport=None):
    """Return every input row in its original order, with pass/version annotations."""
    value = validate(value);out = frame.copy();digest = version(value)
    markets = 'prop_type' if 'prop_type' in out else 'market'
    results = []
    for row in out.to_dict('records'):
        league = sport or row.get('sport')
        if league not in value['sports']: raise ValueError('Unknown sport for display rule')
        spec = value['sports'][league]
        rule = dict(spec['default'], **spec['markets'].get(row[markets], {}))
        edge = float(row['edge'])
        results.append(bool(rule['enabled'] and math.isfinite(edge) and edge >= rule['min_edge']/100
                            and row['side'] in ('over','under') and (rule['side']=='both' or row['side']==rule['side'])))
    if 'rule_version' in out and 'scan_rule_version' not in out:
        out['scan_rule_version']=out['rule_version']
    out['rule_pass'] = pd.Series(results,index=out.index,dtype=bool)
    out['rule_version'] = digest
    return out


def summary(value, sport):
    spec = validate(value)['sports'][sport]
    def describe(label, rule):
        rule = dict(spec['default'], **rule)
        if not rule['enabled']: return label+' off'
        return f"{label} ≥ {rule['min_edge']:g}" + ('' if rule['side']=='both' else ' '+rule['side'])
    parts = [describe(LABELS.get(m,m.removeprefix('player_').replace('_',' ')), r) for m,r in sorted(spec['markets'].items())]
    parts.append(describe('others' if parts else 'all markets',spec['default']))
    return sport.upper()+': '+', '.join(parts)+' pp'


def editor_rows(value, sport, markets):
    spec = validate(value)['sports'][sport]
    rows = [dict(market='Default / other markets', **spec['default'])]
    for market in sorted(set(markets) | set(spec['markets'])):
        override = spec['markets'].get(market, {})
        rows.append(dict(market=market, min_edge=override.get('min_edge'),
                         side=override.get('side','inherit'), enabled=override.get('enabled',spec['default']['enabled'])))
    return pd.DataFrame(rows)


def from_editor(value, sport, frame):
    value = validate(value)
    if frame.empty or frame.iloc[0].market != 'Default / other markets' or frame.market.duplicated().any():
        raise ValueError('Invalid rule table')
    default = {k:frame.iloc[0][k] for k in FIELDS}
    default['enabled'] = bool(default['enabled'])
    markets = {}
    for row in frame.iloc[1:].to_dict('records'):
        override = {}
        if not pd.isna(row['min_edge']): override['min_edge'] = float(row['min_edge'])
        if row['side'] != 'inherit': override['side'] = row['side']
        if bool(row['enabled']) != default['enabled']: override['enabled'] = bool(row['enabled'])
        if override: markets[row['market']] = override
    value['sports'][sport] = dict(default=default, markets=markets)
    return validate(value)
