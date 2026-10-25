"""Explicit preview, paid preparation and final guarded write for owner imports."""
import hashlib
import json
from pathlib import Path
import streamlit as st
from .bet_import import parse, preview, estimate, prepare, grade_preview, commit, run_id
from .live_api import operational_client
from .settlement_sources import FreeFeed


def render_import(data_dir,store,*,uploaded=None,client_factory=operational_client,items_override=None,prefix="bet",title="Import bets from a spreadsheet"):
    with st.expander(title):
        if items_override is None:st.caption('Upload the Bet Tracker Bets sheet (.xlsx) or its CSV. Preview first; missing historical prices stay blank; slip odds and stake remain authoritative. No bets are placed.')
        upload=uploaded if uploaded is not None else None if items_override is not None else st.file_uploader('Bet Tracker file',type=['xlsx','csv'],key='bet_import_file')
        if upload is None and items_override is None:return
        try:
            items=items_override if items_override is not None else parse(upload.getvalue(),upload.name)
            view=preview(items,store)
            todo=view[~view.duplicate].drop(columns='duplicate').to_dict('records')
            st.dataframe(view[['number','game_date','player','market','line','side','book','odds_american','stake','placed_at','duplicate']],hide_index=True,width='stretch')
            st.caption(f'{len(todo)} new · {int(view.duplicate.sum())} already imported. Unknown placement times remain blank.')
            if not todo:return
            rid=run_id(todo)
            bound=estimate(todo);st.write(f'Historical backfill estimate: at most {bound} credits (including game listings).')
            st.caption('Preparation retrieves available entry and close prices (future closes wait for capture); the next screen previews official grades before a separate write confirmation.')
            if st.button('Prepare historical backfill',key=('' if prefix=='bet' else prefix+'_')+'prepare_import',disabled=False):
                try:
                    key=st.secrets.get('ODDS_API_KEY')
                except Exception:key=None
                report=prepare(todo,client_factory(data_dir=data_dir,run_id=rid,run_cap=bound,key=key),data_dir=data_dir,store=store)
                st.session_state[prefix+'_import_prepared']=(rid,report)
            saved=st.session_state.get(prefix+'_import_prepared')
            if saved and saved[0]==rid:
                report=saved[1];feed=FreeFeed(Path(data_dir)/'settlement_cache')
                grades=grade_preview(report['records'],feed)
                st.dataframe(grades,hide_index=True,width='stretch')
                st.caption(f"Recorded historical charge: {report['credits']} credits. Owner results and source details are preserved alongside official grading.")
                if any(g['comparison']=='DIFFERS' for g in grades):st.warning('Official grades differ from the workbook. Review the differences below before importing; the workbook is never changed.')
                if any(g.get('profit_difference') not in (None,0) for g in grades):st.warning('Calculated profit differs from the workbook for some rows. Both values are retained; review payout rounding before confirming.')
                ready=st.checkbox('I confirm these already-placed bets and have reviewed any grade differences',key=prefix+'_confirm_import_'+rid)
                if st.button('Import into real log',key=('' if prefix=='bet' else prefix+'_')+'write_import',disabled=not ready):
                    result=commit(report,store,feed)
                    (st.warning if result.get('queued') else st.success)(result['message'])
                    st.session_state.pop(prefix+'_import_prepared',None)
                    st.rerun()
        except Exception:
            st.error('Import stopped. Verify the file, shared-log access and credit status. Saved evidence is retained; no automatic retry.')
