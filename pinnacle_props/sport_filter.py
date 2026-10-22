"""Per-screen sport selections persisted in shareable URLs and session state."""
import pandas as pd
import streamlit as st
from .live_money import performance
from .settle import OFFICIAL

SPORTS=('nfl','nhl','nba','mlb','wnba')


def select_sports(view):
    param=view+'_sports'; saved='pp_'+param; prefix=saved+'_'
    encoded=st.query_params.get(param)
    if encoded is not None:
        choices=SPORTS if encoded=='all' else tuple(s for s in SPORTS if s in encoded.lower().split(','))
    else:
        choices=st.session_state.get(saved,SPORTS)
    st.session_state[saved]=tuple(choices)
    st.session_state[prefix+'all']=len(choices)==len(SPORTS)
    for sport in SPORTS:st.session_state[prefix+sport]=sport in choices

    def changed(sport):
        if sport=='all':
            for name in SPORTS:st.session_state[prefix+name]=st.session_state[prefix+'all']
        selected=tuple(s for s in SPORTS if st.session_state[prefix+s])
        st.session_state[saved]=selected
        st.session_state[prefix+'all']=len(selected)==len(SPORTS)
        st.query_params[param]='all' if len(selected)==len(SPORTS) else ','.join(selected) or 'none'

    st.caption('Filter sports')
    columns=st.columns(3)
    for i,sport in enumerate(('all',)+SPORTS):
        columns[i%3].checkbox(sport.title() if sport=='all' else sport.upper(),key=prefix+sport,on_change=changed,args=(sport,))
    return tuple(st.session_state[saved])


def filter_rows(frame, sports):
    return frame.loc[frame.sport.astype(str).str.lower().isin(sports)].copy()


def sport_summary(frames):
    rows=[]
    for sport in SPORTS:
        row={'Sport':sport.upper()}
        for kind in ('real','paper'):
            frame=filter_rows(frames[kind],(sport,));m=performance(frame)
            staked=pd.to_numeric(frame.loc[frame.status.isin(OFFICIAL),'stake'],errors='coerce').sum()
            prefix=kind.title()+' '
            row.update({prefix+'bets':len(frame),prefix+'W-L-P':f"{m['won']}–{m['lost']}–{m['push']}",
                        prefix+'staked':float(staked),prefix+'profit':m['profit'],
                        prefix+'ROI':f"{m['roi']*100:+.2f}%" if m['roi'] is not None else '—',prefix+'pending':m['pending']})
        rows.append(row)
    return pd.DataFrame(rows)


def show_sport_summary(frames):
    st.subheader('Profit / loss by sport')
    st.caption('All sports, regardless of the filter. Real and paper totals count each bet once. Staked covers settled bets; ROI excludes pushes and voids.')
    st.dataframe(sport_summary(frames),hide_index=True,width='stretch',column_config={
        kind+' '+field:st.column_config.NumberColumn(format='$%.2f')
        for kind in ('Real','Paper') for field in ('staked','profit')})
