"""Map editable table choices back to immutable saved-scan row identities."""
import hashlib
import math


def editor_key(scan_id, table):
    # A filter, sort, new scan or changed duplicate flag must not move a tick to
    # another contract. Stake/tick edits are not part of the original table.
    digest = hashlib.sha256((scan_id + table.to_json(date_format='iso')).encode()).hexdigest()
    return 'bet_editor_' + digest[:20]


def selected_rows(view, table, edited):
    if not edited.index.equals(table.index) or len(edited) != len(view):
        raise ValueError('Results changed; select the bets again')
    selected = []
    for position, (checked, amount) in enumerate(zip(edited['log'], edited['kellyAmount'])):
        if not checked:
            continue
        amount = float(amount)
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError('Actual stake must be finite and positive for every selected bet')
        selected.append((int(view.index[position]) + 1, amount))
    return selected
