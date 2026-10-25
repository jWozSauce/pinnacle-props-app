"""Recover frozen recommendations without requiring the original scan directory."""
from datetime import datetime, timezone
import hashlib
import json
import math
import uuid
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import pandas as pd
from .bet_store import HEADER, key, StoreUnavailable
from .odds import decimal, decimal_to_american
from .settle import metadata

APP_URL='https://pinnacle-props-app-cphgkmky6dkynraddvdmae.streamlit.app/'


def token(row):
    return hashlib.sha256(json.dumps(key(row,'paper'),ensure_ascii=True).encode()).hexdigest()


def link(row,base=APP_URL):
    url=urlsplit(base)
    if url.scheme!='https' or not url.netloc:raise ValueError('App link must use HTTPS')
    query=dict(parse_qsl(url.query));query['recommendation']=token(row)
    return urlunsplit((url.scheme,url.netloc,url.path,urlencode(query),''))


def recent(frame,now=None,hours=36):
    now=pd.Timestamp(now or datetime.now(timezone.utc))
    dates=pd.to_datetime(frame.logged_at,utc=True,errors='coerce',format='mixed')
    return frame.assign(_at=dates).loc[dates.between(now-pd.Timedelta(hours=hours),now)].sort_values('_at',ascending=False,kind='stable').drop(columns='_at')


def from_paper(row,stake,price,*,note='',row_id=None,now=None):
    amount=float(stake);price=decimal(price)
    if not math.isfinite(amount) or amount<=0:raise ValueError('Stake must be finite and positive')
    # Preserve the source quote and model beliefs even when actual fill price differs.
    result={h:row.get(h,'') for h in HEADER}
    result.update(row_id=row_id or str(uuid.uuid4()),round='',logged_at=now or datetime.now(timezone.utc).isoformat(),
                  stake=amount,price_decimal=price,odds_american=decimal_to_american(price),status='pending',
                  profit='',stat='',graded_at='',regraded_at='',note=note)
    saved=metadata(row)
    result['source']=json.dumps({'recommendation':dict(paper_row_id=row['row_id'],paper_token=token(row),
        frozen_record={h:row.get(h,'') for h in HEADER},original_provenance=saved)},default=str,sort_keys=True)
    return result


def log_paper(store,row_id,stake,price,*,note='',submission_id=None,receipt_item=None):
    # A fresh shared read is required; never log a stale/mutated or disappeared source.
    try:
        _,values=store._worksheet('paper')
        found=[r for r in store._records(values) if r['row_id']==row_id]
        if len(found)!=1:raise ValueError('Paper identity missing or duplicated')
    except Exception:raise StoreUnavailable('Recommendation could not be verified in the shared log. Refresh and retry.') from None
    record=from_paper(found[0],stake,price,note=note,row_id=submission_id)
    if receipt_item is not None:
        from .receipt import matches
        if not matches(pd.DataFrame(found),receipt_item):raise ValueError('Recommendation changed; preview again')
        source=json.loads(record['source']);source['import']=dict(receipt_item,matched_paper_row_id=row_id)
        record['source']=json.dumps(source,sort_keys=True,default=str)
    return store.append_real([record])
