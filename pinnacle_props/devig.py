"""Fair-probability estimators, independent of comparison and staking.

WPO is algebraically additive for every n. Production supports two-way props only;
all invalid probabilities raise, and no alternate estimator is selected silently.
"""
import math
from .odds import decimal


def _raw(odds):
    values = [1 / decimal(o) for o in odds]
    if len(values) < 2:
        raise ValueError('At least two outcomes are required')
    return values


def margin(odds):
    return math.fsum(_raw(odds)) - 1


def wpo(odds):
    raw = _raw(odds)
    adjustment = (math.fsum(raw) - 1) / len(raw)
    result = [p - adjustment for p in raw]
    if any(not math.isfinite(p) or not 0 < p < 1 for p in result):
        raise ValueError('WPO produced an invalid probability')
    if len(raw) != 2:
        raise ValueError('Only two-way WPO is authorised for this scanner')
    return result


def basic(odds):
    raw = _raw(odds)
    return [p / math.fsum(raw) for p in raw]


def power(odds):
    raw = _raw(odds)
    lo, hi = 0.0, 1.0
    while math.fsum(p**hi for p in raw) > 1:
        hi *= 2
        if not math.isfinite(hi):
            raise ValueError('Could not bracket the power exponent')
    for _ in range(100):
        mid = (lo + hi) / 2
        if math.fsum(p**mid for p in raw) > 1:
            lo = mid
        else:
            hi = mid
    return [p ** ((lo + hi) / 2) for p in raw]
