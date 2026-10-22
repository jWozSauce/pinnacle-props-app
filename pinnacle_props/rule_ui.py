"""Streamlit controls for local/URL-persisted display rules."""
import pandas as pd
import streamlit as st
from . import rules


def initialize(cloud):
    if 'display_rules' not in st.session_state:
        try:
            linked = st.query_params.get('rules') if cloud else None
            st.session_state.display_rules = rules.decode(linked) if linked else rules.active(cloud=cloud)
        except (ValueError, OSError) as exc:
            st.error('Saved display rules could not be loaded: '+str(exc))
            if st.button('Reset to default',key='repair_rules'):
                defaults=rules.load()
                if not cloud:rules.save(defaults)
                st.session_state.display_rules=defaults
                st.query_params.pop('rules',None)
                st.rerun()
            st.stop()
    if cloud:st.query_params['rules']=rules.encode(st.session_state.display_rules)
    return st.session_state.display_rules


def panel(config, cloud):
    current=initialize(cloud)
    epoch=st.session_state.get('rules_editor_epoch',0)
    st.markdown('**Betting display rules**')
    st.caption('Rules change displayed suggestions immediately after Apply. Scans and the archive keep all flagged rows. Blank market edges inherit that sport’s default.')
    if cloud:st.caption('Edits stay in this session and its URL. Bookmark the edited URL to keep them across restarts.')
    else:st.caption('Applied edits are saved on this Mac and survive restarts.')
    with st.form('rules_form'):
        tables={}
        for sport,spec in config['sports'].items():
            with st.expander(spec['label']+' rule',expanded=False):
                default=st.slider('Default minimum edge (percentage points)',0.0,100.0,
                    current['sports'][sport]['default']['min_edge'],.1,key=f'rule_min_{sport}_{epoch}')
                frame=rules.editor_rows(current,sport,spec['markets'])
                edited=st.data_editor(frame.iloc[1:].reset_index(drop=True),key=f'rule_table_{sport}_{epoch}',
                    hide_index=True,disabled=['market'],width='stretch',height=180,
                    column_config={'market':st.column_config.TextColumn('Market'),
                        'min_edge':st.column_config.NumberColumn('Minimum edge (pp)',min_value=0.,max_value=100.,step=.1),
                        'side':st.column_config.SelectboxColumn('Side',options=['inherit','both','over','under'],required=True),
                        'enabled':st.column_config.CheckboxColumn('Enabled',required=True)})
                first=frame.iloc[[0]].copy();first.loc[first.index[0],'min_edge']=default
                tables[sport]=pd.concat([first,edited],ignore_index=True)
        submitted=st.form_submit_button('Apply rules')
    if submitted:
        try:
            updated=current
            for sport,frame in tables.items():updated=rules.from_editor(updated,sport,frame)
            if not cloud:rules.save(updated)
            st.session_state.display_rules=updated
            st.session_state.rules_editor_epoch=epoch+1
            if cloud:st.query_params['rules']=rules.encode(updated)
            st.rerun()
        except (ValueError,OSError) as exc:st.error('Rules were not saved: '+str(exc))
    if not cloud and st.button('Save as default',key='save_rules_default'):
        try:
            rules.save_default(current)
            st.success('Applied rules saved as this Mac’s default. They will become the cloud default when the next release is published.')
        except (ValueError,OSError) as exc:st.error('Default was not saved: '+str(exc))
    if st.button('Reset to default',key='reset_rules'):
        try:
            defaults=rules.load()
            if not cloud:rules.save(defaults)
            st.session_state.display_rules=defaults
            st.session_state.rules_editor_epoch=epoch+1
            if cloud:st.query_params['rules']=rules.encode(defaults)
            st.rerun()
        except (ValueError,OSError) as exc:st.error('Default could not be restored: '+str(exc))
    return current
