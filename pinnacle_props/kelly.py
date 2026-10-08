"""Fractional Kelly with the legacy probability-edge cap, using decimal prices."""
from dataclasses import dataclass
from .odds import american_to_decimal, decimal, finite, probability


@dataclass(frozen=True)
class Profile:
    bankroll: float
    fraction: float
    edge_cap: float

    def __post_init__(self):
        for key in ('bankroll', 'fraction', 'edge_cap'):
            object.__setattr__(self, key, finite(getattr(self, key), key))
        if self.bankroll < 0 or not 0 <= self.fraction <= 1 or not 0 <= self.edge_cap <= 1:
            raise ValueError('Bankroll must be nonnegative; fraction and edge cap in [0, 1]')


def stake(bankroll, fraction, decimal_odds, fair_p, edge_cap):
    profile = Profile(bankroll, fraction, edge_cap)
    odds = decimal(decimal_odds)
    p = min(probability(fair_p), 1 / odds + profile.edge_cap)
    return max(0.0, (odds * p - 1) / (odds - 1)) * profile.bankroll * profile.fraction


def american_stake(bankroll, fraction, american_odds, fair_p, edge_cap):
    return stake(bankroll, fraction, american_to_decimal(american_odds), fair_p, edge_cap)
