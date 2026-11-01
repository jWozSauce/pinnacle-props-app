"""TASK-28 quote-quality rule; threshold changes require a planner ruling."""
import math

WIDE_SPREAD_PP = 12.0
EXPLANATION = "Wide spread = both sides add to more than 12 pp of vig; ignored as a thin or empty order book."


def spread_pp(over, under):
    try:
        over, under = float(over), float(under)
        if not (math.isfinite(over) and math.isfinite(under) and over > 1 and under > 1):
            return None
        return 100 * (1 / over + 1 / under - 1)
    except (TypeError, ValueError):
        return None


def is_wide(spread):
    # Ignore only floating-point roundoff at the exact 12 pp boundary.
    return spread > WIDE_SPREAD_PP + 1e-10


def quality(quote):
    """Recalculate from saved prices, without changing frozen flags or blend."""
    spread = spread_pp(quote.get('over'), quote.get('under'))
    return dict(quote, spread_pp=spread,
                wide=quote.get('venue') != 'pinnacle' and spread is not None and is_wide(spread))
