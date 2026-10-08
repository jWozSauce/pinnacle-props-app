"""Pure same-line scan. One accounting unit is one soft-book player/market/line pair.

Reference rows are inputs, not candidate bets. All raw outcome rows survive in the caller's
snapshot. Excluded pairs retain source row positions so malformed/duplicate rows remain
traceable. Time, league and Kelly settings are explicit, allowing exact replay.
"""
import math
import pandas as pd
from . import devig
from .edge import choose_side, ev_pct
from .kelly import Profile, stake
from .odds import decimal, decimal_to_american, finite, format_american

INPUT_COLUMNS = ['sport_key', 'league', 'event_id', 'book', 'market', 'player', 'side',
                 'point', 'price', 'last_update', 'commence_time', 'home_team', 'away_team']
IDENTITY = ['sport_key', 'event_id', 'market', 'player']
PAIR_KEY = IDENTITY + ['book', 'point']
LEGACY_COLUMNS = ['book', 'player_name', 'prop_type', 'Target_line', 'side', 'kellyAmount',
                  'edge', 'bet', 'league', 'game', 'date', 'away_team', 'home_team', 'PIN_line',
                  'Target_over', 'PIN_over', 'Target_under', 'PIN_under', 'noVigOver',
                  'noVigUnder', 'Target_over_p', 'Target_under_p', 'over_diffTRUE', 'under_diffTRUE']
EXTRA_COLUMNS = ['sport_key', 'event_id', 'pin_margin', 'pin_age_min', 'ev_pct',
                 'pricing_method', 'target_over_decimal', 'target_under_decimal',
                 'pin_over_decimal', 'pin_under_decimal', 'source_rows', 'best_book', 'best_price']
REASONS = ['no reference price', 'line mismatch', 'no positive side', 'below min edge',
           'margin too wide', 'live', 'duplicate line', 'incomplete pair', 'invalid price',
           'invalid identity', 'invalid timestamp']


def aware(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError('Timezone-aware timestamp required')
    return stamp.tz_convert('America/New_York')


def _pair(group):
    sides = group['side'].str.lower()
    if sides.duplicated().any():
        return None, 'duplicate line'
    if len(group) != 2 or set(sides) != {'over', 'under'}:
        return None, 'incomplete pair'
    prices = []
    for side in ('over', 'under'):
        try:
            prices.append(decimal(group.loc[sides == side, 'price'].iloc[0]))
        except ValueError:
            return None, 'invalid price'
    return prices, None


def scan(long_df, profile, *, reference_book='pinnacle', min_edge=0.0,
         max_reference_margin=None, require_line_match=True, include_live=False, now,
         exclusion_version=2):
    if exclusion_version not in (1, 2):
        raise ValueError('Unsupported exclusion classification version')
    if not isinstance(profile, Profile):
        profile = Profile(**profile)
    clock = aware(now)
    min_edge = finite(min_edge, 'Minimum edge')
    if not 0 <= min_edge <= 1:
        raise ValueError('Minimum edge must be in [0, 1]')
    if max_reference_margin is not None:
        max_reference_margin = finite(max_reference_margin, 'Maximum reference margin')
        if max_reference_margin < 0:
            raise ValueError('Maximum reference margin must be nonnegative')
    if not require_line_match:
        raise ValueError('Different-line pricing is queued; exact line matching is required')
    missing = set(INPUT_COLUMNS) - set(long_df.columns)
    if missing:
        raise ValueError('Missing scan columns: ' + ', '.join(sorted(missing)))
    df = long_df.copy().reset_index(drop=True)
    df['source_row'] = df.index
    df['side'] = df['side'].astype('string')
    df['point'] = pd.to_numeric(df['point'], errors='coerce')
    counts = dict.fromkeys(REASONS, 0)
    if exclusion_version >= 2:
        counts['one-sided market'] = 0
    flagged, excluded = [], []
    is_reference = df.book.eq(reference_book).fillna(False)
    references = df[is_reference]
    candidates = df[~is_reference]
    one_sided = set()
    if exclusion_version >= 2:
        for key, ladder in candidates.groupby(IDENTITY+['book'], sort=False, dropna=False):
            sides = set(ladder.side.str.lower().dropna())
            if sides in ({'over'}, {'under'}) and ladder.side.notna().all():
                one_sided.add(key)
    books = sorted(df.book.dropna().unique())
    for _, group in candidates.groupby(PAIR_KEY, sort=False, dropna=False):
        first = group.iloc[0]
        source = group.source_row.tolist()
        base = {c: first[c] for c in PAIR_KEY}
        base.update(source_rows=source, raw_row_count=len(group))
        reason = None
        for key in IDENTITY + ['book', 'league', 'home_team', 'away_team']:
            if pd.isna(first[key]) or str(first[key]).strip() == '' or group[key].nunique(dropna=False) != 1:
                reason = 'invalid identity'
        if not math.isfinite(first.point):
            reason = 'invalid identity'
        target, target_error = _pair(group)
        if target_error == 'incomplete pair' and tuple(first[k] for k in IDENTITY+['book']) in one_sided:
            target_error = 'one-sided market'
        reason = reason or target_error
        ref = references
        for key in IDENTITY:
            ref = ref[ref[key] == first[key]]
        if ref.empty:
            reason = reason or 'no reference price'
        same_ref = ref[ref.point == first.point]
        if not ref.empty and same_ref.empty:
            reason = reason or 'line mismatch'
        ref_prices, ref_error = _pair(same_ref)
        reason = reason or ref_error
        if reason is None:
            try:
                starts = [aware(t) for t in group.commence_time] + [aware(t) for t in same_ref.commence_time]
                updates = [aware(t) for t in same_ref.last_update]
                if any(t != starts[0] for t in starts):
                    raise ValueError('Conflicting start times')
                if any(t > clock for t in updates):
                    raise ValueError('Reference update is after capture time')
                start = starts[0]
                age = (clock - min(updates)).total_seconds() / 60
            except (ValueError, TypeError, OverflowError):
                reason = 'invalid timestamp'
        if reason is None:
            fair = devig.wpo(ref_prices)
            pin_margin = devig.margin(ref_prices)
            side, edges = choose_side(fair, target)
            index = ('over', 'under').index(side)
            if not include_live and start <= clock:
                reason = 'live'
            elif max_reference_margin is not None and pin_margin > max_reference_margin:
                reason = 'margin too wide'
            elif max(edges) <= 0:
                reason = 'no positive side'
            elif edges[index] < min_edge:
                reason = 'below min edge'
        if reason is not None:
            counts[reason] += 1
            excluded.append(dict(base, exclusion_reason=reason))
            continue
        row = dict(book=first.book, player_name=first.player, prop_type=first.market,
                   Target_line=first.point, side=side,
                   kellyAmount=stake(profile.bankroll, profile.fraction, target[index], fair[index], profile.edge_cap),
                   edge=edges[index], bet=f'{side}/{format_american(target[index])}/{fair[index]:.4f}',
                   league=first.league, game=f'{first.away_team} at {first.home_team}',
                   date=start.isoformat(), away_team=first.away_team, home_team=first.home_team,
                   PIN_line=first.point, Target_over=decimal_to_american(target[0]),
                   PIN_over=decimal_to_american(ref_prices[0]), Target_under=decimal_to_american(target[1]),
                   PIN_under=decimal_to_american(ref_prices[1]), noVigOver=fair[0], noVigUnder=fair[1],
                   Target_over_p=1/target[0], Target_under_p=1/target[1], over_diffTRUE=edges[0],
                   under_diffTRUE=edges[1], sport_key=first.sport_key, event_id=first.event_id,
                   pin_margin=pin_margin, pin_age_min=age, ev_pct=ev_pct(fair[index], target[index]),
                   pricing_method='same_line', target_over_decimal=target[0], target_under_decimal=target[1],
                   pin_over_decimal=ref_prices[0], pin_under_decimal=ref_prices[1], source_rows=source)
        comparison = df[(df.point == first.point) & (df.side.str.lower() == side)]
        for key in IDENTITY:
            comparison = comparison[comparison[key] == first[key]]
        prices = {}
        for book in books:
            offers = comparison[comparison.book == book]
            price = None
            if len(offers) == 1:
                try:
                    price = decimal(offers.iloc[0].price)
                except ValueError:
                    pass
            row['price_' + book] = price
            if price is not None and book != reference_book:
                prices[book] = price
        row['best_book'] = max(prices, key=prices.get) if prices else None
        row['best_price'] = max(prices.values()) if prices else None
        flagged.append(row)
    flagged_df = pd.DataFrame(flagged, columns=LEGACY_COLUMNS + EXTRA_COLUMNS + ['price_'+b for b in books])
    if not flagged_df.empty:
        flagged_df = flagged_df.sort_values('kellyAmount', ascending=False, kind='stable').reset_index(drop=True)
    excluded_df = pd.DataFrame(excluded, columns=PAIR_KEY+['source_rows','raw_row_count','exclusion_reason'])
    assert len(flagged_df) + sum(counts.values()) == len(candidates.groupby(PAIR_KEY, dropna=False))
    return flagged_df, excluded_df, counts


if __name__ == '__main__':
    from pinnacle_props.cli import main
    main()
