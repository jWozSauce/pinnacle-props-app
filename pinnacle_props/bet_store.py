"""Restricted two-tab Sheet store with atomic appends and an ephemeral local outbox.

No credential discovery outside Streamlit secrets. Provider errors are deliberately
redacted. The outbox is retained after acknowledgement to recover a lost/raced append.
"""
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import uuid
import time
import pandas as pd
from .records import DATA, csv_write, record_lock
from .odds_api import atomic_write

TABS = {'real': 'PP_Bet_Log', 'paper': 'PP_Paper_Log'}
HEADER = 'round logged_at sport game_date start_time event_id away_team home_team player market line side book price_decimal odds_american stake edge fair_p scan_id row rule_version status profit stat graded_at regraded_at source note row_id'.split()
GRADING = HEADER[21:28]
FROZEN = HEADER[:21] + ['row_id']
SHEET_ID = '173vZBHQ31stOXKOuHzjIFH2BtMMWFbLjbzolZlqPNeo'

class StoreUnavailable(ValueError):
    pass


def enabled():
    """Acceptance switch. Merely importing or opening the old app has no new writes."""
    if 'PP_SETTLEMENT_ENABLED' in os.environ:return os.environ['PP_SETTLEMENT_ENABLED']=='1'
    try:
        import streamlit as st
        return st.secrets.get('PP_SETTLEMENT_ENABLED',False) is True
    except Exception:return False


def session_store(data_dir=DATA):
    """Reuse the UI's session read cache; CLI invocations get an independent store."""
    import streamlit as st
    from streamlit.runtime.scriptrunner import get_script_run_ctx
    if get_script_run_ctx(suppress_warning=True) is None:return BetStore(data_dir)
    key='pp_bet_store_'+str(data_dir)
    if key not in st.session_state:st.session_state[key]=BetStore(data_dir)
    return st.session_state[key]


def connect():
    try:
        import streamlit as st
        import gspread
        from google.oauth2.service_account import Credentials
        info = dict(st.secrets['gcp_service_account'])
        info['private_key'] = info['private_key'].replace('\\n', '\n')
        creds = Credentials.from_service_account_info(info, scopes=['https://www.googleapis.com/auth/spreadsheets'])
        client = gspread.authorize(creds)
        client.set_timeout((10, 30))
        return client.open_by_key(st.secrets.get('CONSTANTS_SHEET_ID', SHEET_ID))
    except Exception:
        raise StoreUnavailable('Shared logs unavailable. Check service-account setup and Sheet access; details suppressed.') from None


def key(row, kind):
    if kind == 'paper':
        return (str(row['event_id']), str(row['player']), str(row['market']),
                format(float(row['line']), '.15g'), str(row['side']).lower())
    if kind != 'real': raise ValueError('Unknown log kind')
    return (str(row['row_id']),)


def canonical(frame, kind):
    """First physical occurrence wins. Preserve raw rows for the visible audit."""
    if frame.empty: return frame.copy()
    keys = frame.apply(lambda r: key(r, kind), axis=1)
    return frame.loc[~keys.duplicated()].copy()


def duplicate_audit(frame, kind):
    groups = {}
    for r in frame.to_dict('records'):
        groups.setdefault(key(r, kind), []).append(r)
    return pd.DataFrame([dict(log=kind, key=json.dumps(k), count=len(rows),
                              canonical_row_id=rows[0]['row_id'])
                         for k, rows in groups.items() if len(rows)>1],
                        columns=['log','key','count','canonical_row_id'])


def _frame(rows):
    return pd.DataFrame(rows, columns=HEADER).fillna('')


def _read(path):
    return pd.read_csv(path, keep_default_na=False, dtype=str).to_dict('records') if path.exists() else []


class BetStore:
    def __init__(self, data_dir=DATA, *, spreadsheet=None, connector=connect):
        self.root = Path(data_dir)
        self.spreadsheet = spreadsheet
        self.connector = connector
        self.cache = {}
        self.warning = ''
        self.real_check = None
        self.real_check_at = 0.0

    def _path(self, kind, suffix):
        if kind not in TABS: raise ValueError('Only real and paper logs are writable')
        return self.root / 'bets' / f'{kind}_{suffix}.csv'

    def _worksheet(self, kind):
        if kind not in TABS: raise ValueError('Forbidden worksheet')
        if self.spreadsheet is None: self.spreadsheet = self.connector()
        try:
            ws = self.spreadsheet.worksheet(TABS[kind])
        except Exception as exc:
            # Never create on authentication, network, quota, or unknown failures.
            if type(exc).__name__ != 'WorksheetNotFound': raise
            ws = self.spreadsheet.add_worksheet(title=TABS[kind], rows=5000, cols=len(HEADER))
        values = ws.get_all_values()
        if not any(any(str(cell) for cell in row) for row in values):
            ws.update([HEADER], 'A1:AC1', value_input_option='RAW')
            values = [HEADER]
        if values[0] != HEADER: raise StoreUnavailable('Shared log header mismatch; no rows changed')
        return ws, values

    @staticmethod
    def _records(values):
        return [dict(zip(HEADER, r + [''] * (len(HEADER)-len(r)))) for r in values[1:]]

    def read(self, kind, *, reload=False, raw=False):
        if kind in self.cache and not reload:
            return self.cache[kind].copy() if raw else canonical(self.cache[kind],kind)
        self.warning = ''
        try:
            _, values = self._worksheet(kind)
            records = self._records(values)
            csv_write(self._path(kind, 'mirror'), _frame(records))
        except Exception:
            self.warning = 'Shared logs unavailable; showing the last local copy and queued rows.'
            records = _read(self._path(kind, 'mirror'))
        seen = {key(r, kind) for r in records}
        for r in _read(self._path(kind, 'outbox')):
            if key(r, kind) not in seen:
                records.append(r); seen.add(key(r, kind))
        self.cache[kind] = _frame(records)
        return self.cache[kind].copy() if raw else canonical(self.cache[kind],kind)

    def real_keys(self, *, reload=False):
        """Display-only TTL; never return mirrors as a successful duplicate check."""
        from .bet_duplicates import real_keys
        if not reload and self.real_check is not None and time.monotonic()-self.real_check_at<120:
            return self.real_check
        try:
            _,values=self._worksheet('real')
            rows=self._records(values)
            rows.extend(_read(self._path('real','outbox')))
            result=real_keys(_frame(rows))
        except Exception:
            self.real_check=None
            return None
        self.real_check=result;self.real_check_at=time.monotonic()
        return result

    def append_real(self, records, *, allow_duplicates=False):
        """User submissions must freshly verify the shared real log before queueing.

        No stale local fallback. The provider's atomic append prevents row loss;
        as in NCAAF, the duplicate preflight is not a cross-host transaction.
        """
        from .bet_duplicates import select_new
        try:
            _,values=self._worksheet('real')
            current=self._records(values)+_read(self._path('real','outbox'))
            selected,skipped=select_new(records,_frame(current),allow_duplicates)
        except Exception:
            self.real_check=None
            raise StoreUnavailable('CHECK UNAVAILABLE: real logging blocked until a fresh shared-log read succeeds.') from None
        if selected:result=self.append('real',selected)
        else:result={'written':0,'queued':0,'message':'No new real bets logged'}
        result['skipped']=skipped
        if skipped:result['message']+=f'; skipped {skipped} already logged bet(s). Allow re-logging to override.'
        self.real_check=None
        return result

    def append(self, kind, records):
        """Queue before network; idempotent retry after a lost HTTP acknowledgement."""
        with record_lock(self.root, 'sheet-'+kind):
            path = self._path(kind, 'outbox')
            queued = _read(path)
            seen = {key(r, kind) for r in queued}
            for r in records:
                try:
                    if uuid.UUID(str(r.get('row_id',''))).version != 4: raise ValueError()
                except (ValueError,AttributeError): raise ValueError('New rows require a frozen uuid4 row_id') from None
                if key(r, kind) not in seen:
                    if r.get('status') != 'pending': raise ValueError('New bets must be pending')
                    queued.append({h:r.get(h, '') for h in HEADER}); seen.add(key(r, kind))
            csv_write(path, _frame(queued))
            try:
                ws, values = self._worksheet(kind)
                remote = self._records(values)
                seen = {key(r, kind) for r in remote}
                row_ids = {r['row_id'] for r in remote}
                missing = [r for r in queued if r['row_id'] not in row_ids and key(r, kind) not in seen]
                if missing:
                    # The server chooses the next row atomically, including blank-leading rows.
                    # String values keep notes beginning with '=' literal, never formulas.
                    self.spreadsheet.batch_update({'requests':[{'appendCells':{
                        'sheetId':ws.id, 'fields':'userEnteredValue',
                        'rows':[{'values':[{'userEnteredValue':{'stringValue':str(r.get(h,''))}}
                                           for h in HEADER]} for r in missing]}}]})
                    verified = self._records(ws.get_all_values())
                    have = {r['row_id'] for r in verified}
                    if any(r['row_id'] not in have for r in missing):
                        raise StoreUnavailable('Append verification failed; queue retained')
                    remote = verified
                csv_write(self._path(kind, 'mirror'), _frame(remote))
                self.cache.pop(kind, None); self.warning = ''
                if kind=='real':self.real_check=None
                return {'written': len(missing), 'queued': 0, 'message': 'Shared log synchronized'}
            except Exception:
                self.cache.pop(kind, None)
                self.warning = 'Shared log unavailable or changed; rows queued on this deployment only. Download the log and retry sync before closing; a cloud rebuild can lose this queue.'
                return {'written': 0, 'queued': len(queued), 'message': self.warning}

    def grade(self, kind, changes):
        """Only V:AB may change. Compare full old row before grading/regrading.

        Corrections are TASK-17 D's specific exception to pending-only updates.
        Prior values live in the same Sheet row's note as well as a local journal.
        """
        with record_lock(self.root, 'sheet-'+kind):
            try:
                ws, values = self._worksheet(kind)
                remote = self._records(values)
                updates=[]
                for old, new in changes:
                    candidates=[(i,r) for i,r in enumerate(remote) if r['row_id']==old['row_id']]
                    if len(candidates)!=1: raise StoreUnavailable('Grading identity is absent or duplicated')
                    i, current=candidates[0]
                    if any(str(current[h]) != str(old.get(h,'')) for h in HEADER):
                        raise StoreUnavailable('Row changed since grading read; reload before retry')
                    if any(str(new.get(h,'')) != str(old.get(h,'')) for h in FROZEN):
                        raise ValueError('Acquisition fields are immutable')
                    changed_grade=new['status']!=old['status'] or any(str(new.get(h,''))!=str(old.get(h,'')) and not (str(new.get(h,'')) and str(old.get(h,'')) and float(new[h])==float(old[h])) for h in ('profit','stat'))
                    if current['status'] != 'pending' and changed_grade and not new.get('regraded_at'):
                        raise ValueError('Correction needs a regrade timestamp')
                    updates.append({'range':f'V{i+2}:AB{i+2}', 'values':[[new.get(h,'') for h in GRADING]]})
                if updates:
                    if ws.get_all_values()!=values: raise StoreUnavailable('Sheet changed before grading')
                    # Explicit last-moment identity check. No stored row index crosses calls.
                    locations=[next(i+2 for i,r in enumerate(remote) if r['row_id']==old['row_id'])
                               for old,_ in changes]
                    identities=ws.batch_get([f'AC{i}:AC{i}' for i in locations])
                    if len(identities)!=len(changes) or any(
                            cells!=[[old['row_id']]] for cells,(old,_) in zip(identities,changes)):
                        raise StoreUnavailable('Row moved before grading; reload before retry')
                    journal=self.root/'bets'/'grade_journal'
                    journal.mkdir(parents=True,exist_ok=True)
                    import uuid
                    atomic_write(journal/(str(uuid.uuid4())+'.json'),json.dumps(changes,default=str).encode())
                    ws.batch_update(updates,value_input_option='RAW')
                self.cache.pop(kind,None)
                return len(updates)
            except (ValueError,StoreUnavailable): raise
            except Exception:
                raise StoreUnavailable('Grading write unavailable; reload to reconcile. No automatic retry.') from None

    def backup(self, day):
        for kind in TABS:
            _,values=self._worksheet(kind)
            path=self.root/'sheets_backup'/f'{day}_{kind}.csv'
            if not path.exists(): csv_write(path,_frame(self._records(values)))
