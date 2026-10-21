"""Current-clock gates for saved suggestions; immutable scan inputs stay unchanged."""
from datetime import datetime, timezone
import math
import pandas as pd


def utcnow():
    return datetime.now(timezone.utc)


def start_time(value):
    try:
        start=pd.Timestamp(value)
        if pd.isna(start) or start.tzinfo is None:raise ValueError
        return start.to_pydatetime()
    except (ValueError,TypeError,OverflowError):
        raise ValueError('Game start time is unavailable; cannot confirm pregame status') from None


def started(value, *, now=None):
    return start_time(value)<=(now if now is not None else utcnow())


def label(value, *, now=None):
    try:seconds=(start_time(value)-(now if now is not None else utcnow())).total_seconds()
    except ValueError:return 'start time unavailable'
    if seconds<=0:return 'game started'
    minutes=math.ceil(seconds/60)
    hours,minutes=divmod(minutes,60)
    text=f'starts in {hours} h {minutes} m' if hours else f'starts in {minutes} m'
    return text+(' · closing soon' if seconds<=15*60 else '')


def suggestions(frame, *, show_started=False, now=None):
    """Return a copy and exclusion counts without changing saved row identities."""
    now=now if now is not None else utcnow()
    out=frame.copy()
    out['start_status']=out['date'].map(lambda value:label(value,now=now))
    past=out.start_status.eq('game started');unknown=out.start_status.eq('start time unavailable')
    return out[~unknown & (~past | show_started)].copy(),int(past.sum()) if not show_started else 0,int(unknown.sum())
