"""Read-only public Constants CSV; validation prevents silent profile fallback."""
from io import StringIO
import re
import uuid
from pathlib import Path
import pandas as pd
import requests
from .kelly import Profile

def sheet_number(value):
    if isinstance(value,str) and ',' in value:
        if not re.fullmatch(r'[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?',value.strip()):
            raise ValueError('Malformed thousands separators in Kelly profile')
        return float(value.replace(',',''))
    return value


DEFAULT_URL='https://docs.google.com/spreadsheets/d/173vZBHQ31stOXKOuHzjIFH2BtMMWFbLjbzolZlqPNeo/gviz/tq?tqx=out:csv&sheet=Constants'


def parse_profiles(text):
    try:
        frame=pd.read_csv(StringIO(text))
    except Exception:
        raise ValueError('Kelly source did not return readable CSV') from None
    required={'Sport','kelly_fraction','bankroll2','edge_limit'}
    if not required.issubset(frame.columns):
        raise ValueError('Kelly CSV lacks required profile columns')
    # Blank trailing spreadsheet rows are not profiles; count populated names only.
    frame=frame[frame.Sport.notna() & frame.Sport.astype(str).str.strip().ne('')]
    if frame.Sport.duplicated().any():
        raise ValueError('Duplicate Kelly profile names')
    profiles={}
    for row in frame.itertuples():
        profiles[str(row.Sport)]=Profile(sheet_number(row.bankroll2),sheet_number(row.kelly_fraction),sheet_number(row.edge_limit))
    if not profiles:
        raise ValueError('Kelly CSV contains no profiles')
    return profiles


def fetch_profiles(url=DEFAULT_URL,session=None,cache_dir=None):
    try:
        response=(session or requests).get(url,timeout=30)
    except requests.RequestException:
        raise ValueError('Kelly sheet unavailable; no replacement defaults were invented') from None
    if response.status_code!=200:
        raise ValueError(f'Kelly sheet HTTP {response.status_code}')
    from .odds_api import ROOT, utcnow, write_json
    target=Path(cache_dir) if cache_dir is not None else ROOT/'data'/'kelly'
    write_json(target/(str(uuid.uuid4())+'.json'),{'captured_at':utcnow(),'body':response.text})
    return parse_profiles(response.text)
