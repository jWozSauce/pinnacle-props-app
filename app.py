"""Phone-first local Streamlit UI. Importing the app never requests odds or resolves a key."""
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import json

import pandas as pd
import streamlit as st
from pinnacle_props.kelly import american_stake
from pinnacle_props.kelly_sheet import fetch_profiles, parse_profiles
from pinnacle_props.odds_api import APIError, resolve_key
from pinnacle_props.pipeline import discover, options, replay, run_live
from pinnacle_props.scan import LEGACY_COLUMNS
from pinnacle_props import rules
from pinnacle_props.rule_ui import initialize as initialize_rules, panel as rules_panel
from pinnacle_props.settings import ROOT, load_config
from pinnacle_props.deployment import deployment_settings
from pinnacle_props.access import cloud_access
from pinnacle_props.bet import log_bet, read_bets
from pinnacle_props.clv import report as clv_report, logged_view

st.set_page_config(page_title='Pinnacle Props',page_icon='◈',layout='wide',initial_sidebar_state='collapsed')
try:
    deployment=deployment_settings({name:st.secrets.get(name,default) for name,default in [('PP_DEPLOYMENT','local'),('PP_STORE','local')]})
except (FileNotFoundError,st.errors.StreamlitSecretNotFoundError):
    deployment=deployment_settings({})
if deployment['cloud']:cloud_access()
st.markdown('''<style>
.stApp {background:#f6f8fa;color:#14283f}
.block-container {max-width:940px;padding-top:4.5rem}
h1,h2,h3 {letter-spacing:-.025em}
[data-testid="stMetric"] {background:white;border:1px solid #e1e7ed;border-radius:12px;padding:12px}
[data-testid="stVerticalBlockBorderWrapper"] {background:white;border-radius:14px}
@media(max-width:640px){.block-container{padding:4.5rem .9rem 1rem}h1{font-size:2rem !important}}
</style>''',unsafe_allow_html=True)
from pinnacle_props.settings import ACCOUNTING_CLOUD
ACCOUNTING_CLOUD.set(deployment['cloud'])
config=load_config()
active_rules=initialize_rules(deployment['cloud'])
data_dir=ROOT/'data'
clv_preview=deployment['cloud'] or st.query_params.get('preview_clv')=='1'



def local_profiles():
    files=sorted((data_dir/'kelly').glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
    for path in files:
        try:
            return parse_profiles(json.loads(path.read_text())['body'])
        except (KeyError,ValueError):
            continue
    return {}


def key_for_live():
    # Access secrets only on an explicit live action. Local cache mode needs no key.
    try:
        secrets=st.secrets.to_dict()
    except (FileNotFoundError,st.errors.StreamlitSecretNotFoundError):
        secrets=None
    return resolve_key(streamlit_secrets=secrets)


def label_time(value):
    return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(ZoneInfo('America/New_York')).strftime('%b %d, %I:%M %p %Z')


st.caption('◈  PINNACLE PROPS')
st.title('Find the better price.')
st.write('Player props across your books, compared at the same line against Pinnacle.')
if deployment['cloud'] and not deployment['paid_enabled']:st.info('Live refresh is not available on this deployment yet.')

choices=['Auto','Cards','Table']
initial_view=st.query_params.get('view','Auto')
if initial_view not in choices:initial_view='Auto'
view_mode=st.radio('View',choices,index=choices.index(initial_view),horizontal=True,key='view_mode')
st.query_params['view']=view_mode
st.markdown('<style>.block-container{max-width:none}</style>',unsafe_allow_html=True)
if view_mode=='Auto':
    css='@media(min-width:901px){.st-key-pp_cards{display:none}} @media(max-width:900px){.st-key-pp_table{display:none}}'
elif view_mode=='Table':css='.st-key-pp_cards{display:none}'
else:css='.st-key-pp_table{display:none}'
st.markdown('<style>'+css+'</style>',unsafe_allow_html=True)


sport_names=list(config['sports'])
initial=st.query_params.get('sport','nhl')
if initial not in sport_names:initial=sport_names[0]
sport=st.selectbox('Sport',sport_names,index=sport_names.index(initial),
                   format_func=lambda x:config['sports'][x]['label'],key='sport')
st.query_params['sport']=sport

if 'profiles' not in st.session_state:
    st.session_state.profiles=local_profiles()
profiles=st.session_state.profiles
if not profiles:
    st.info('Load Kelly profiles to prepare a live scan. Saved scans retain their original settings.')
    if st.button('Load Kelly profiles',key='load_profiles'):
        try:
            st.session_state.profiles=fetch_profiles();st.rerun()
        except ValueError as e:st.error(str(e))

with st.expander('Books, markets & scan settings',expanded=False):
    query_books=st.query_params.get('books_'+sport,'').split(',')
    selected_books=[b for b in query_books if b in config['books']] or list(config['books'])
    books=st.multiselect('Books',list(config['books']),default=selected_books,
                         format_func=lambda b:config['books'][b],key='books_'+sport)
    available=config['sports'][sport]['markets']+config['sports'][sport].get('alternate_markets',[])
    query_markets=st.query_params.get('markets_'+sport,'').split(',')
    selected_markets=[m for m in query_markets if m in available] or config['sports'][sport]['markets']
    markets=st.multiselect('Markets',available,default=selected_markets,key='markets_'+sport)
    st.query_params['books_'+sport]=','.join(books)
    st.query_params['markets_'+sport]=','.join(markets)
    active_rules=rules_panel(config,deployment['cloud'])
    guard=st.checkbox('Limit Pinnacle margin',value=False,key='margin_guard')
    maximum=st.slider('Maximum Pinnacle margin (%)',0.0,50.0,10.0,.5,key='maximum')/100 if guard else None
    include_live=st.checkbox('Include live games',value=False,key='include_live')
    limit_dates=st.checkbox('Limit game dates',value=False,key='limit_dates')
    scan_dates=None
    if limit_dates:
        today=datetime.now(ZoneInfo('America/New_York')).date()
        scan_dates=st.date_input('Game dates (Eastern)',value=(today,today+timedelta(days=7)),key='scan_dates')
    stale_minutes=st.slider('Grey out quotes older than (minutes)',1,120,15,key='stale_minutes')
    names=list(profiles)
    default_profile=config['kelly_profile']
    profile_name=st.selectbox('Kelly profile',names or [default_profile],
                             index=names.index(default_profile) if default_profile in names else 0,key='profile')
    st.caption('New scan settings apply on refresh. Saved results keep the inputs captured with that scan.')
    if st.button('Reload Kelly settings',key='reload_profiles'):
        try:
            st.session_state.profiles=fetch_profiles();st.rerun()
        except ValueError as e:st.error(str(e))

try:
    settings=options(config,sport,books,markets,0.0,maximum,include_live,dates=scan_dates)
except ValueError as e:
    settings=None;st.warning(str(e))

if st.button('Estimate live refresh · free',disabled=settings is None,key='estimate',width='stretch'):
    try:
        with st.spinner('Checking available games…'):
            st.session_state.preview=discover(settings,key=key_for_live())
    except (APIError,ValueError) as e:
        st.session_state.pop('preview',None);st.error(str(e))
preview=st.session_state.get('preview')
if preview and preview['settings']==settings:
    date_counts={}
    for event in preview['events']:
        day=datetime.fromisoformat(event['commence_time'].replace('Z','+00:00')).astimezone(ZoneInfo('America/New_York')).date().isoformat()
        date_counts[day]=date_counts.get(day,0)+1
    dates=sorted(date_counts)
    span=f", {dates[0]} to {dates[-1]}" if dates else ''
    st.info(f"{len(preview['events'])} games{span} · refresh uses at most {preview['bound']} credits.")
    with st.expander('Games by date'):
        for day in dates:st.write(f"{day}: {date_counts[day]} games")
    if st.button('Refresh live prices',type='primary',disabled=not deployment['paid_enabled'] or not preview['events'] or profile_name not in profiles,
                 key='refresh',width='stretch'):
        try:
            with st.spinner('Capturing prices and comparing lines…'):
                # Fetch exactly once for this live run so sheet edits are captured, not silently stale.
                current_profiles=fetch_profiles()
                if profile_name not in current_profiles:
                    raise ValueError('Selected Kelly profile is no longer available')
                result=run_live(preview,profile=current_profiles[profile_name],profile_name=profile_name,
                                key=key_for_live(),progress=lambda _:None,project_cap=deployment['project_cap'],data_dir=data_dir,cloud_daily_cap=deployment['cloud_daily_cap'],display_rules=active_rules)
                st.session_state.profiles=current_profiles
                st.session_state['scan_'+sport]=result
                st.session_state.pop('preview',None)
                st.rerun()
        except (APIError,ValueError,KeyError) as e:st.error(str(e))

if 'scan_'+sport not in st.session_state:
    try:
        st.session_state['scan_'+sport]=replay(sport,data_dir=data_dir)
    except FileNotFoundError:
        pass
    except (ValueError,APIError) as e:
        st.error('Saved scan could not be verified: '+str(e))
result=st.session_state.get('scan_'+sport)

from pinnacle_props.settings import accounting_dir
quota_file=accounting_dir(data_dir,cloud=deployment['cloud'])/'odds_budget.json'
if quota_file.exists():
    try:
        quota=json.loads(quota_file.read_text())
        remaining=quota.get('remaining')
        st.caption(f"Account credits: {remaining:,} · This ledger used: {quota['spent']:,} / {min(quota.get('project_cap',deployment['project_cap']),deployment['project_cap']):,}" if remaining is not None else 'Account quota has not been checked.')
        if quota.get('halt') or any(not r.get('transient') for r in quota.get('reservations',{}).values()):
            st.warning('Live requests are paused for a quota or request-accounting issue. Saved scans remain available.')
        elif quota.get('reservations'):
            st.caption(f"Credits reserved for unconfirmed responses: {sum(r['estimated'] for r in quota['reservations'].values()):,}")
    except (ValueError,KeyError):st.warning('Quota record is unavailable; live requests will fail closed.')

tabs=st.tabs(['Price comparison','Kelly calculator']+(['Logged','Performance'] if clv_preview else []))
tab_prices,tab_calc=tabs[:2]
if clv_preview:
    if not deployment['cloud']:st.caption('Local bet records and pre-start CLV')
    logged=read_bets(data_dir)
with tab_prices:
    if result is None:
        st.info('No scans saved on this deployment yet. Your Mac’s saved history is not connected.' if deployment['cloud'] else 'No saved scan for this sport yet. Estimate a live refresh to begin.')
    else:
        manifest,flagged,excluded=result
        st.caption('SAVED SCAN · '+label_time(manifest['capture_time'])+' · '+manifest['profile_name'])
        st.caption('Saved prices load without an API key. Only an explicit live refresh requests new prices.')
        st.write(rules.summary(active_rules,sport))
        annotated=rules.annotate(flagged,active_rules,sport)
        hidden=int((~annotated.rule_pass).sum())
        st.caption(f'{hidden} bets hidden by the rule · {len(annotated)} saved opportunities')
        show_all=st.checkbox('Show all saved opportunities',value=False,key='show_all_rules_'+sport)
        visible=annotated if show_all else annotated[annotated.rule_pass]
        a,b,c=st.columns(3)
        a.metric('Opportunities',len(visible));b.metric('Games',len(manifest['event_results']));c.metric('Scan credits',manifest['credits_charged'])
        query=st.text_input('Filter by player, book or market',placeholder='Search these results…',key='search')
        view=visible
        with st.expander('Filter result columns'):
            filter_books=st.multiselect('Result books',sorted(flagged['book'].unique()),key='filter_books_'+sport)
            filter_markets=st.multiselect('Result markets',sorted(flagged['prop_type'].unique()),key='filter_markets_'+sport)
            filter_sides=st.multiselect('Sides',['over','under'],key='filter_sides_'+sport)
        if filter_books:view=view[view['book'].isin(filter_books)]
        if filter_markets:view=view[view['prop_type'].isin(filter_markets)]
        if filter_sides:view=view[view['side'].isin(filter_sides)]
        if query:
            mask=view[['player_name','book','prop_type']].astype(str).agg(' '.join,axis=1).str.contains(query,case=False,regex=False)
            view=view[mask]
        table_columns=LEGACY_COLUMNS+['best_book','best_price','pin_margin','pin_age_min','ev_pct']
        with st.expander('Filter individual columns'):
            columns=st.multiselect('Columns to filter',table_columns,key='column_filters_'+sport)
            for column in columns:
                text=st.text_input(column,key='column_filter_'+sport+'_'+column)
                if text:view=view[view[column].astype(str).str.contains(text,case=False,regex=False,na=False)]
        st.caption(f'{len(view)} results · largest suggested stake first')
        if view.empty:st.info('No opportunities match these filters.')
        with st.container(key='pp_table'):
            st.dataframe(view[table_columns].style.apply(
                lambda row:['color: #87929d' if row.pin_age_min>stale_minutes else '' for _ in row],axis=1),
                hide_index=True,width='stretch',height=480,column_config={
                    'kellyAmount':st.column_config.NumberColumn(format='$%.2f'),
                    **{c:st.column_config.NumberColumn(format='%.1f') for c in ['Target_line','PIN_line','pin_age_min']},
                    **{c:st.column_config.NumberColumn(format='%.0f') for c in ['Target_over','Target_under','PIN_over','PIN_under']},
                    **{c:st.column_config.NumberColumn(format='%.4f') for c in ['edge','pin_margin','noVigOver','noVigUnder','Target_over_p','Target_under_p','over_diffTRUE','under_diffTRUE']},
                    **{c:st.column_config.NumberColumn(format='%.2f') for c in ['best_price','ev_pct']}})
            st.caption('Best price is decimal odds; legacy price columns are American. Pinnacle age is measured at capture. All columns remain available by scrolling across the table.')
        with st.container(key='pp_cards'):
            count=8
            pages=max(1,(len(view)+count-1)//count)
            page=st.selectbox('Page',range(1,pages+1),key='result_page')
            for row in view.iloc[(page-1)*count:page*count].itertuples():
                with st.container(border=True):
                    age=stale_minutes<row.pin_age_min
                    st.markdown(f'### {row.player_name}')
                    price=row.Target_over if row.side=='over' else row.Target_under
                    st.write(f"{row.side.title()} {row.Target_line:g} · {row.prop_type.removeprefix('player_').replace('_',' ')}")
                    st.markdown(f"**{config['books'].get(row.book,row.book)} {price:+.0f}** · Edge {100*row.edge:.2f} pp · EV {row.ev_pct:.2f}%")
                    st.caption(f"{row.game} · {label_time(row.date)}")
                    if age:
                        st.caption(f'◷ Older quote · Pinnacle age at capture: {row.pin_age_min:.1f} min')
                    st.caption(f"Suggested stake ${row.kellyAmount:,.2f} · Pinnacle margin {100*row.pin_margin:.2f}%")
                    if clv_preview and not deployment['cloud']:
                        row_number=row.Index+1
                        record_key=manifest['scan_id']+'_'+str(row_number)
                        exists=((logged.scan_id==manifest['scan_id'])&(logged.row==row_number)).any()
                        with st.expander('Record a bet you already placed' if not exists else 'Bet recorded'):
                            st.caption(f"Saved row {row_number}. This records your action; it does not place a bet.")
                            actual=st.number_input('Actual stake ($)',min_value=0.01,value=max(.01,float(row.kellyAmount)),key='actual_'+record_key)
                            note=st.text_input('Note',key='note_'+record_key,max_chars=2000)
                            if st.button('Log bet',key='log_'+record_key,disabled=exists):
                                try:
                                    log_bet(manifest['scan_id'],row_number,actual,note,data_dir=data_dir,display_rules=active_rules)
                                    st.rerun()
                                except (ValueError,OSError) as e:st.error(str(e))
        with st.expander('Compare all books & full details'):
            if not view.empty:
                price_columns=[c for c in view if c.startswith('price_')]
                display_columns=LEGACY_COLUMNS+['best_book','best_price','pin_margin','pin_age_min','ev_pct','pricing_method']+price_columns
                display=view[display_columns].copy()
                def style_row(row):
                    styles=pd.Series('',index=row.index)
                    if row.pin_age_min>stale_minutes:styles[:]='color: #87929d'
                    best='price_'+str(row.best_book)
                    if best in styles.index:styles[best]='background-color: #d5f1e5; color: #12523e; font-weight: bold'
                    return styles
                st.dataframe(display.style.apply(style_row,axis=1),hide_index=True,width='stretch')
                st.caption('All-book price columns and best price are decimal odds. Legacy price columns are American odds. Highlight marks the best soft-book price at this exact line.')
        with st.expander('Coverage & exclusions'):
            st.write(f"{manifest['raw_rows']:,} raw outcomes · {manifest['candidate_pairs']:,} soft-book line pairs")
            st.dataframe(pd.DataFrame([{'Reason':k,'Pairs':v} for k,v in manifest['exclusions'].items() if v]),hide_index=True,width='stretch')
            st.caption('Missing or mismatched lines remain in the saved snapshot. Only complete same-line pairs can qualify.')
        st.download_button('Download results CSV',view.to_csv(index=False),'pinnacle_props.csv','text/csv',key='download')

with tab_calc:
    if not profiles:
        st.info('Load Kelly settings first.')
    else:
        calc_profile=st.selectbox('Calculator profile',list(profiles),index=list(profiles).index(profile_name) if profile_name in profiles else 0,key='calc_profile')
        profile=profiles[calc_profile]
        suffix=calc_profile
        bankroll=st.number_input('Bankroll ($)',min_value=0.0,value=profile.bankroll,key='bankroll_'+suffix)
        fraction=st.number_input('Kelly fraction',min_value=0.0,max_value=1.0,value=profile.fraction,step=.05,key='fraction_'+suffix)
        american=st.number_input('American odds',value=100.0,step=1.0,key='american')
        probability=st.number_input('Fair probability (%)',min_value=0.0,max_value=100.0,value=50.0,step=.1,key='probability')/100
        cap=st.number_input('Edge cap (percentage points)',min_value=0.0,max_value=100.0,value=profile.edge_cap*100,step=.1,key='cap_'+suffix)/100
        try:
            amount=american_stake(bankroll,fraction,american,probability,cap)
            st.metric('Suggested stake',f'${amount:,.2f}')
            if amount==0:st.caption('No bet at these inputs.')
        except ValueError as e:st.error(str(e))

if clv_preview:
    try:
        report_text,clv_frame=clv_report(data_dir=data_dir,output=None)
        with tabs[2]:
            st.subheader('Your recorded bets')
            recorded=logged_view(clv_frame,data_dir=data_dir)
            if recorded.empty:st.info('No bets recorded on this deployment. Your Mac’s bet history is not connected.' if deployment['cloud'] else 'No bets recorded. Log only bets you already placed.')
            else:
                recorded=recorded.rename(columns={'rule_version':'logged_rule_version'})
                recorded=rules.annotate(recorded,active_rules).rename(columns={'rule_pass':'fits_current_rule','rule_version':'current_rule_version'})
                recorded['logged_at']=recorded.logged_at.map(label_time)
                st.dataframe(recorded,hide_index=True,width='stretch')
                st.caption('Recorded bets remain visible even when they no longer fit the current rule. CLV is a probability difference; blank means no valid same-line close is available.')
        with tabs[3]:
            if deployment['cloud'] and clv_frame.empty:st.info('No performance history on this deployment. Your Mac’s closing prices and bet history are not connected.')
            else:st.markdown(report_text)
    except (ValueError,OSError,KeyError) as e:st.error('Local CLV records could not be verified: '+str(e))
