"""Probability edge and expected profit per unit risked."""
from .odds import decimal, probability


def edge(fair_p, price):
    return probability(fair_p) - 1 / decimal(price)


def ev_pct(fair_p, price):
    return 100 * (probability(fair_p) * decimal(price) - 1)


def choose_side(fair, prices):
    edges = [edge(p, o) for p, o in zip(fair, prices, strict=True)]
    if len(edges) != 2:
        raise ValueError('Expected Over and Under')
    index = 0 if edges[0] >= edges[1] else 1
    return ('over', 'under')[index], edges
