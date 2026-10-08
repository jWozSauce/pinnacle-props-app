"""Unrounded odds math. Formatting belongs at the display boundary."""
import math


def finite(value, name):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be a finite number') from None
    if not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    return value


def decimal(value):
    value = finite(value, 'Decimal odds')
    if value <= 1:
        raise ValueError('Decimal odds must exceed one')
    return value


def probability(value):
    value = finite(value, 'Probability')
    if not 0 <= value <= 1:
        raise ValueError('Probability must lie in [0, 1]')
    return value


def decimal_to_probability(value):
    return 1 / decimal(value)


def probability_to_decimal(value):
    value = probability(value)
    if not 0 < value < 1:
        raise ValueError('Odds require a probability strictly between zero and one')
    return 1 / value


def decimal_to_american(value):
    value = decimal(value)
    return 100 * (value - 1) if value >= 2 else -100 / (value - 1)


def american_to_decimal(value):
    if isinstance(value, str):
        value = value.replace('−', '-')
    value = finite(value, 'American odds')
    if abs(value) < 100:
        raise ValueError('American odds must have magnitude at least 100')
    return 1 + (value / 100 if value > 0 else 100 / -value)


def american_to_probability(value):
    return decimal_to_probability(american_to_decimal(value))


def format_american(value):
    """Decimal price to signed, whole American display; never feed back to sizing."""
    return f'{decimal_to_american(value):+.0f}'
