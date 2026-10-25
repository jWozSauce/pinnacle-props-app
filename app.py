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
from pinnacle_props import rules, game_time
from pinnacle_props.rule_ui import initialize as initialize_rules, panel as rules_panel
from pinnacle_props.settings import ROOT, load_config, scan_markets
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
from pinnacle_props.bet_store import enabled as settlement_enabled, session_store
from pinnacle_props import bet_duplicates as bd
settlement_preview=settlement_enabled()
clv_preview=settlement_preview or deployment['cloud'] or st.query_params.get('preview_clv')=='1'



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


if 'bet_receipt' in st.session_state:st.info(st.session_state.pop('bet_receipt'))
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
    css='@media(min-width:901px){.st-key-pp_cards{display:none}}'
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
    st.session_state.profiles={} if deployment['cloud'] else local_profiles()

def reload_profiles():
    try:
        st.session_state.profiles=fetch_profiles()
        st.session_state.pop('kelly_error',None)
        return True
    except ValueError as e:
        st.session_state.kelly_error='Kelly settings unavailable: '+str(e)
        return False

if deployment['cloud'] and not st.session_state.get('kelly_auto_attempted'):
    st.session_state.kelly_auto_attempted=True
    reload_profiles()
profiles=st.session_state.profiles
if not profiles:
    st.info('Load Kelly profiles to prepare a live scan. Saved scans retain their original settings.')
    if st.button('Load Kelly profiles',key='load_profiles'):
        if reload_profiles():st.rerun()

with st.expander('Books, markets & scan settings',expanded=False):
    from pinnacle_props import book_catalog, consensus
    catalog=book_catalog.load() if book_catalog.enabled() else None
    selectable=book_catalog.selectable_books(catalog,sport,config['books']) if catalog else config['books']
    book_labels={k:book_catalog.clean_label(b['label']) for k,b in selectable.items()} if catalog else config['books']
    query_name='books_'+sport
    query_books=st.query_params[query_name].split(',') if query_name in st.query_params else list(config['books'])
    selected_books=[b for b in query_books if b in selectable]
    books=(book_catalog.render_picker(catalog,sport,config['books'],selected_books) if catalog else
           st.multiselect('Books',list(selectable),default=selected_books,
                          format_func=lambda b:config['books'][b],key=query_name))
    if catalog:
        st.checkbox('Pinnacle · required reference',value=True,disabled=True,key='catalog_reference')
        st.caption(book_catalog.cost_caption(books+(consensus.observation_books() if consensus.enabled() else []),config['reference_book']))
        st.caption('Billing uses unique markets returned × bookmaker bands (10 books per band). Empty books still count toward bands but add no new returned markets; they can increase the charge for markets returned by other books. An entirely empty response costs zero.')
        if consensus.enabled():st.caption('Novig, ProphetX and Polymarket are captured for comparison; they are not bettable. Cost includes these observation venues, even when not selected.')
        st.caption('All catalog books are selectable for every sport. Grey notes describe the dated discovery sample, not guaranteed coverage. US sportsbooks also includes DFS pick’em providers.')
        st.caption('Exchange quotes use back prices only. DFS pick’em prices are provider-implied equivalents.')
        status=catalog['sports'].get(sport,{}).get('status','discovery not completed')
        if status!='observed':st.caption(config['sports'][sport]['label']+' bookmaker discovery is deferred; every catalog book remains selectable.')
    available=scan_markets(config['sports'][sport])
    query_markets=st.query_params.get('markets_'+sport,'').split(',')
    selected_markets=[m for m in query_markets if m in available] or available
    markets=st.multiselect('Markets',available,default=selected_markets,key='markets_'+sport)
    st.query_params['books_'+sport]=','.join(books)
    st.query_params['markets_'+sport]=','.join(markets)
    active_rules=rules_panel(config,deployment['cloud'])
    guard=st.checkbox('Limit Pinnacle margin',value=False,key='margin_guard')
    maximum=st.slider('Maximum Pinnacle margin (%)',0.0,50.0,10.0,.5,key='maximum')/100 if guard else None
    paper_track=st.checkbox('Paper-track rule-passing bets on every refresh',value=True,key='paper_track') if settlement_preview else False
    include_live=st.checkbox('Include live games',value=False,key='include_live')
    window_hours=config['sports'][sport].get('scan_window_hours',config.get('scan_window_hours',72))
    if st.session_state.pop('_reset_scan_window',False) or st.session_state.get('_scan_window_sport')!=sport:
        st.session_state['include_beyond_window']=False
    st.session_state['_scan_window_sport']=sport
    include_beyond_window=st.checkbox(f'Include games beyond {window_hours:g} hours',value=False,key='include_beyond_window')
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
        if reload_profiles():st.rerun()

try:
    settings=options(config,sport,books,markets,0.0,maximum,include_live,dates=scan_dates,include_beyond_window=include_beyond_window)
except ValueError as e:
    settings=None;st.warning(str(e))

if st.button('Estimate live refresh · free',disabled=settings is None,key='estimate',width='stretch'):
    try:
        with st.spinner('Checking available games…'):
            st.session_state.preview=discover(settings,key=key_for_live())
    except (APIError,ValueError) as e:
        st.session_state.pop('preview',None);st.error(str(e))
preview=st.session_state.get('preview')
if st.session_state.get('kelly_error') and not (preview and preview['settings']==settings):
    st.error(st.session_state.kelly_error)
if preview and preview['settings']==settings:
    date_counts={}
    for event in preview['events']:
        day=datetime.fromisoformat(event['commence_time'].replace('Z','+00:00')).astimezone(ZoneInfo('America/New_York')).date().isoformat()
        date_counts[day]=date_counts.get(day,0)+1
    dates=sorted(date_counts)
    later=len(preview.get('later_events',[]))
    window=f'in the next {window_hours:g} h ({later} more listed later, not scanned)' if not include_beyond_window else f'(including games beyond {window_hours:g} h)'
    st.info(f"{len(preview['events'])} games {window} · refresh uses at most {preview['bound']} credits.")
    if dates and (datetime.fromisoformat(dates[-1])-datetime.fromisoformat(dates[0])).days>=3:
        st.caption('Tick Limit game dates to scan fewer.')
    with st.expander('Games by date'):
        for day in dates:st.write(f"{day}: {date_counts[day]} games")
    if preview.get('later_events'):
        with st.expander('Later listings · outside the default window'):
            for event in preview['later_events']:
                st.write(f"{label_time(event['commence_time'])} · {event.get('away_team','')} @ {event.get('home_team','')}")
    if st.session_state.get('kelly_error'):st.error(st.session_state.kelly_error)
    refresh_reasons=[]
    if not deployment['paid_enabled']:refresh_reasons.append('Live refresh is unavailable on this deployment.')
    if not preview['events']:refresh_reasons.append('No upcoming games.')
    if profile_name not in profiles:refresh_reasons.append('Load Kelly profiles first.')
    if deployment['cloud']:
        from pinnacle_props.deployment import cloud_refresh_reason
        reason=cloud_refresh_reason(data_dir,preview['bound'])
        if reason:refresh_reasons.append(reason)
    if refresh_reasons:st.warning(' '.join(refresh_reasons))
    if st.button('Refresh live prices',type='primary',disabled=bool(refresh_reasons),
                 key='refresh',width='stretch'):
        try:
            with st.spinner('Capturing prices and comparing lines…'):
                # Fetch exactly once for this live run so sheet edits are captured, not silently stale.
                current_profiles=fetch_profiles()
                if profile_name not in current_profiles:
                    raise ValueError('Selected Kelly profile is no longer available')
                scan_messages=[]
                progress_text=st.empty()
                import threading,logging
                scan_thread=threading.get_ident()
                def report_scan(message):
                    if threading.get_ident()!=scan_thread:
                        logging.getLogger(__name__).warning('Off-thread scan progress suppressed')
                        return
                    scan_messages.append(message);progress_text.caption(message)
                result=run_live(preview,profile=current_profiles[profile_name],profile_name=profile_name,
                                key=key_for_live(),progress=report_scan,data_dir=data_dir,display_rules=active_rules,paper_track=paper_track)
                st.session_state.pop('pp_bet_store_'+str(data_dir),None)
                if settlement_preview and paper_track:
                    st.session_state['bet_receipt']=scan_messages[-1] if scan_messages else 'Paper tracking finished; check shared logs.'
                st.session_state.profiles=current_profiles
                st.session_state['scan_'+sport]=result
                st.session_state.pop('preview',None)
                st.session_state['_reset_scan_window']=True
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

quota_file=Path(data_dir)/'quota.json'
st.caption('Account floor: 100,000 credits · Fresh balance check before each paid run.')
if quota_file.exists():
    try:
        quota=json.loads(quota_file.read_text())
        remaining=quota.get('remaining',quota.get('x-requests-remaining'))
        if remaining is not None:st.caption(f"Account remaining: {int(remaining):,} credits (last free check).")
    except (ValueError,KeyError,TypeError):st.caption('Account remaining unavailable; the next run will check before spending.')
else:st.caption('Account remaining: not yet checked. The next run checks it for free.')

if settlement_preview:
    from pinnacle_props.recommendation_ui import linked_panel
    linked_panel(session_store(data_dir))

tabs=st.tabs(['Price comparison','Live','Kelly calculator']+((['Logged','Performance','Paper'] if settlement_preview else ['Logged','Performance']) if clv_preview else []))
tab_prices,tab_live,tab_calc=tabs[:3]
if clv_preview:
    if not deployment['cloud']:st.caption('Local bet records and pre-start CLV')
    logged=read_bets(data_dir)
with tab_prices:
    if result is None:
        st.info('No scans saved on this deployment yet. Your Mac’s saved history is not connected.' if deployment['cloud'] else 'No saved scan for this sport yet. Estimate a live refresh to begin.')
    else:
        manifest,flagged,excluded=result
        from pinnacle_props.venue_ui import expand as venue_expand, second_line
        if 'venue_quotes' not in flagged and manifest.get('profile'):
            from pinnacle_props.records import snapshots,artifact
            from pinnacle_props.venue_details import enrich
            from pinnacle_props.kelly import Profile
            try:
                saved_path=snapshots(data_dir,scan_id=manifest['scan_id'])[0][0]
                flagged=enrich(flagged,artifact(saved_path,manifest,'.parquet'),Profile(**manifest['profile']),manifest['capture_time'])
            except (ValueError,OSError,KeyError,IndexError):pass
        real_keys=None;allow_duplicates=bool(st.session_state.get('allow_real_duplicates',False))
        if settlement_preview:
            bet_store=session_store(data_dir)
            reload_check=st.button('Refresh already-bet check',key='reload_real_check')
            real_keys=bet_store.real_keys(reload=reload_check)
        st.caption('SAVED SCAN · '+label_time(manifest['capture_time'])+' · '+manifest['profile_name'])
        st.caption('Saved prices load without an API key. Only an explicit live refresh requests new prices.')
        st.write(rules.summary(active_rules,sport))
        from pinnacle_props import consensus
        flagged=consensus.defaults(flagged)
        annotated=rules.annotate(flagged,active_rules,sport)
        if manifest.get('settings',{}).get('advisory_version'):st.caption(consensus.LEGEND if manifest['settings']['advisory_version']>=3 else consensus.SIGNED_LEGEND if manifest['settings']['advisory_version']==2 else consensus.LEGACY_LEGEND)
        if manifest.get('settings',{}).get('advisory_version') in (1,2):st.caption('Saved scan uses the earlier ISLAND definition. Refresh prices to apply the current labels.')
        hidden=int((~annotated.rule_pass).sum())
        st.caption(f'{hidden} bets hidden by the rule · {len(annotated)} saved opportunities')
        show_all=st.checkbox('Show all saved opportunities',value=False,key='show_all_rules_'+sport)
        show_started=st.checkbox('Show started games',value=False,key='show_started_games')
        visible,started_count,unknown_count=game_time.suggestions(annotated if show_all else annotated[annotated.rule_pass],show_started=show_started)
        st.caption(f'{started_count} hidden: game started')
        if unknown_count:st.caption(f'{unknown_count} hidden: start time unavailable')
        if settlement_preview:
            visible['⚠']=pd.Series(bd.flags(visible,real_keys),index=visible.index,dtype='string').fillna('')
            exact=int(visible['⚠'].str.startswith('🔴',na=False).sum());same=int(visible['⚠'].str.startswith('🟡',na=False).sum())
            if real_keys is None:st.warning('⚪ CHECK UNAVAILABLE. Real logging requires a successful fresh shared-log read.')
            elif exact or same:st.warning(f'{exact} bets below are already in your bet log · {same} more are on games you already have a real bet on')
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
        if query and not view.empty:
            mask=view[['player_name','book','prop_type']].astype(str).agg(' '.join,axis=1).str.contains(query,case=False,regex=False)
            view=view[mask]
        advisory_columns=['island_flag','books_q','market_books_q','sharp_books_q'] if manifest.get('settings',{}).get('advisory_version') else []
        advisory_details=advisory_columns+['cons_edge','market_gap_pp','market_edge_pp','sharp_gap_pp','sharp_edge_pp','advisory_version'] if advisory_columns else []
        table_columns=['start_status']+advisory_details+LEGACY_COLUMNS+['best_book','best_price','pin_margin','pin_age_min','ev_pct','pricing_method']
        with st.expander('Filter individual columns'):
            columns=st.multiselect('Columns to filter',table_columns,key='column_filters_'+sport)
            for column in columns:
                text=st.text_input(column,key='column_filter_'+sport+'_'+column)
                if text:view=view[view[column].astype(str).str.contains(text,case=False,regex=False,na=False)]
        st.caption(f'{len(view)} results · largest suggested stake first')
        if view.empty:st.info('No opportunities match these filters.')
        with st.container(key='pp_cards'):
            count=8
            pages=max(1,(len(view)+count-1)//count)
            page=st.selectbox('Page',range(1,pages+1),key='result_page')
            st.markdown('<style>[class*="st-key-pp_card_red_"]{background:#7f1d1d!important;color:white!important}[class*="st-key-pp_card_amber_"]{background:#92600a!important;color:white!important}[class*="st-key-pp_card_red_"] *,[class*="st-key-pp_card_amber_"] *{color:white!important}</style>',unsafe_allow_html=True)
            for row in view.iloc[(page-1)*count:page*count].itertuples():
                flag=view.loc[row.Index,'⚠'] if settlement_preview else ''
                tone='red' if flag.startswith('🔴') else 'amber' if flag.startswith('🟡') else 'plain'
                with st.container(border=True,key=f'pp_card_{tone}_{row.Index}'):
                    if flag:st.write(flag)
                    if row.island_flag:
                        st.write(row.island_flag)
                        st.caption(f'Pinnacle age at capture: {row.pin_age_min:.1f} min · other books: {row.books_q:g}')
                    age=stale_minutes<row.pin_age_min
                    st.markdown(f"### {flag.split(' ')[0]+' ' if flag else ''}{row.player_name}")
                    price=row.Target_over if row.side=='over' else row.Target_under
                    st.write(f"{row.side.title()} {row.Target_line:g} · {row.prop_type.removeprefix('player_').replace('_',' ')}")
                    st.markdown(f"**{book_labels.get(row.book,row.book)} {price:+.0f}** · Edge {100*row.edge:.2f} pp · EV {row.ev_pct:.2f}%")
                    st.caption(f"{row.game} · {label_time(row.date)} · {row.start_status}")
                    if age:
                        st.caption(f'◷ Older quote · Pinnacle age at capture: {row.pin_age_min:.1f} min')
                    st.caption(f"Suggested stake ${row.kellyAmount:,.2f} · Pinnacle margin {100*row.pin_margin:.2f}%")
                    second_line(getattr(row,'blend_edge_pp',None),getattr(row,'blend_stake',None))
                    venue_expand(view.loc[row.Index])
                    if row.pricing_method=='same_line_alt':st.caption('Same line · alternate market')
                    if clv_preview and (settlement_preview or not deployment['cloud']):
                        row_number=row.Index+1
                        record_key=manifest['scan_id']+'_'+str(row_number)
                        exists=not settlement_preview and ((logged.scan_id==manifest['scan_id'])&(logged.row==row_number)).any()
                        with st.expander('Record a bet you already placed' if not exists else 'Bet recorded'):
                            st.caption(f"Saved row {row_number}. This records your action; it does not place a bet.")
                            actual=st.number_input('Actual stake ($)',min_value=0.01,value=max(.01,float(row.kellyAmount)),key='actual_'+record_key)
                            note=st.text_input('Note',key='note_'+record_key,max_chars=2000)
                            confirm_started=False
                            if game_time.started(row.date):
                                confirm_started=st.checkbox('Confirm logging a bet on a started game',value=False,key='confirm_started_'+record_key)
                            if st.button('Log bet',key='log_'+record_key,disabled=exists or (settlement_preview and (real_keys is None or (flag.startswith('🔴') and not allow_duplicates)))):
                                try:
                                    receipt=log_bet(manifest['scan_id'],row_number,actual,note,data_dir=data_dir,display_rules=active_rules,allow_duplicates=allow_duplicates,confirm_started=confirm_started)
                                    st.session_state.pop('pp_bet_store_'+str(data_dir),None)
                                    st.session_state['bet_receipt']=receipt.get('storage',{}).get('message','Bet recorded')
                                    st.rerun()
                                except (ValueError,OSError) as e:st.error(str(e))
        with st.container(key='pp_table'):
            from pinnacle_props.venue_ui import table_rows
            table_rows(view)
            with st.expander('Select bets and full data table'):
                table=view[(['⚠'] if settlement_preview else [])+table_columns].copy()
                if settlement_preview:
                    table.index=bd.index_labels(view)
                    table.insert(0,'log',False)
                column_config={
                    '_index':st.column_config.TextColumn('Saved row',disabled=True),
                    'log':st.column_config.CheckboxColumn('log',default=False),
                    'kellyAmount':st.column_config.NumberColumn('Stake ($)',format='$%.2f',min_value=0.01,step=0.01,help='Starts at the Kelly suggestion; enter the amount you actually bet.'),
                    **{c:st.column_config.NumberColumn(format='%.1f') for c in ['Target_line','PIN_line','pin_age_min']},
                    **{c:st.column_config.NumberColumn(format='%.0f') for c in ['Target_over','Target_under','PIN_over','PIN_under']},
                    **{c:st.column_config.NumberColumn(format='%.4f') for c in ['edge','pin_margin','noVigOver','noVigUnder','Target_over_p','Target_under_p','over_diffTRUE','under_diffTRUE']},
                    **{c:st.column_config.NumberColumn(format='%.2f') for c in ['best_price','ev_pct']}}
                styled=table.style.apply(
                    lambda row:bd.row_style(row) if settlement_preview and row.get('⚠','') else ['color: #87929d' if row.pin_age_min>stale_minutes else '' for _ in row],axis=1)
                if settlement_preview:
                    from pinnacle_props.bet_selection import editor_key, selected_rows
                    edited=st.data_editor(styled,key=editor_key(manifest['scan_id'],table),
                        disabled=[c for c in table.columns if c not in ('log','kellyAmount')],
                        hide_index=False,width='stretch',height=480,column_config=column_config)
                else:
                    st.dataframe(styled,hide_index=True,width='stretch',height=480,column_config=column_config)
                st.caption('Best price is decimal odds; legacy price columns are American. Pinnacle age is measured at capture. All columns remain available by scrolling across the table.')
        if settlement_preview:
            allow_duplicates=st.checkbox('Allow re-logging bets marked 🔴 ALREADY BET',value=False,key='allow_real_duplicates')
            action_real,action_paper,action_download=st.columns(3)
            selected_confirmation=False
            try:
                chosen=selected_rows(view,table,edited)
                started_picks=[r for r,amount in chosen if game_time.started(flagged.iloc[r-1]['date'])]
            except ValueError:
                started_picks=[]
            if started_picks:
                import hashlib
                confirmation_key=hashlib.sha256(json.dumps([manifest['scan_id'],chosen]).encode()).hexdigest()[:20]
                selected_confirmation=st.checkbox(f'Confirm logging {len(started_picks)} selected bets on started games',value=False,key='confirm_started_selected_'+confirmation_key)
            selected_action=action_real.button('Log selected bets',type='primary',key='log_selected',disabled=real_keys is None)
            paper_candidates,_,_=game_time.suggestions(annotated)
            qualifying=int((paper_candidates.rule_pass & (paper_candidates.kellyAmount>0)).sum())
            paper_action=action_paper.button(f'Bet ALL {qualifying} qualifying (paper round)',key='paper_all',disabled=qualifying==0)
            action_download.download_button('Download bet card',view.to_csv(index=False),'pinnacle_props.csv','text/csv',key='download')
            if selected_action:
                written=queued=skipped=0
                try:
                    picks=selected_rows(view,table,edited)
                    if not picks:
                        st.warning('Tick the log box on the bets you placed')
                    elif started_picks and not selected_confirmation:
                        st.warning('Confirm logging the selected bets on started games first')
                    else:
                        for row_number,amount in picks:
                            receipt=log_bet(manifest['scan_id'],row_number,amount,data_dir=data_dir,
                                            display_rules=active_rules,allow_duplicates=allow_duplicates,confirm_started=selected_confirmation and row_number in started_picks)['storage']
                            written+=receipt['written'];queued+=receipt['queued'];skipped+=receipt.get('skipped',0)
                        st.session_state.pop('pp_bet_store_'+str(data_dir),None)
                        st.session_state['bet_receipt']=f'Real bets: {written} logged; {queued} queued; {skipped} exact duplicates skipped.'
                        st.session_state.pop(editor_key(manifest['scan_id'],table),None)
                        st.rerun()
                except (ValueError,OSError) as e:
                    st.error(f'{written} logged; {queued} queued; {skipped} skipped before stop. '+str(e))
            if paper_action:
                from pinnacle_props.paper import track_scan
                receipt=track_scan(manifest['scan_id'],data_dir=data_dir,display_rules=active_rules)
                st.session_state.pop('pp_bet_store_'+str(data_dir),None)
                (st.warning if receipt['queued'] else st.success)(receipt['message'])
        with st.expander('Coverage & exclusions'):
            st.write(f"{manifest['raw_rows']:,} raw outcomes · {manifest['candidate_pairs']:,} soft-book line pairs")
            st.dataframe(pd.DataFrame([{'Reason':k,'Pairs':v} for k,v in manifest['exclusions'].items() if v]),hide_index=True,width='stretch')
            st.caption('Missing or mismatched lines remain in the saved snapshot. Only complete same-line pairs can qualify.')
        if not settlement_preview:st.download_button('Download results CSV',view.to_csv(index=False),'pinnacle_props.csv','text/csv',key='download')

with tab_live:
    from pinnacle_props.live_ui import render_live, EMPTY
    if settlement_preview:render_live(data_dir)
    else:st.info(EMPTY)

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

if settlement_preview:
    from pinnacle_props.performance_ui import render
    try:
        from pinnacle_props.clv import rebuild
        clv_frame=rebuild(data_dir=data_dir)
    except (ValueError,OSError,KeyError):clv_frame=None
    render(tabs[3],tabs[4],data_dir,clv=clv_frame,paper_tab=tabs[5])
    with tabs[3]:
        from pinnacle_props.import_ui import render_import
        from pinnacle_props.bet_store import session_store
        render_import(data_dir,session_store(data_dir))
        from pinnacle_props.receipt_ui import render_receipt
        render_receipt(data_dir,session_store(data_dir))
elif clv_preview:
    try:
        report_text,clv_frame=clv_report(data_dir=data_dir,output=None)
        with tabs[3]:
            st.subheader('Your recorded bets')
            recorded=logged_view(clv_frame,data_dir=data_dir)
            if recorded.empty:st.info('No bets recorded on this deployment. Your Mac’s bet history is not connected.' if deployment['cloud'] else 'No bets recorded. Log only bets you already placed.')
            else:
                recorded=recorded.rename(columns={'rule_version':'logged_rule_version'})
                recorded=rules.annotate(recorded,active_rules).rename(columns={'rule_pass':'fits_current_rule','rule_version':'current_rule_version'})
                recorded['logged_at']=recorded.logged_at.map(label_time)
                st.dataframe(recorded,hide_index=True,width='stretch')
                st.caption('Recorded bets remain visible even when they no longer fit the current rule. CLV is a probability difference; blank means no valid same-line close is available.')
        with tabs[4]:
            if deployment['cloud'] and clv_frame.empty:st.info('No performance history on this deployment. Your Mac’s closing prices and bet history are not connected.')
            else:st.markdown(report_text)
    except (ValueError,OSError,KeyError) as e:st.error('Local CLV records could not be verified: '+str(e))
