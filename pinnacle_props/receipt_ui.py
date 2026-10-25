"""Preview a single receipt locally before an explicit guarded real-log write."""
import hashlib
import uuid
import streamlit as st
from . import receipt
from .bet_store import HEADER
from .bet_import import preview
from .recommendations import log_paper
from .import_ui import render_import
import pandas as pd


def render_receipt(data_dir,store,*,uploaded=None):
    with st.expander('Import a receipt PDF or screenshot'):
        st.caption('One straight player-prop bet, up to 5 MB. Text recognition runs locally. Review every field; unclear values stay editable. No bet is placed by this app.')
        upload=uploaded if uploaded is not None else st.file_uploader('Receipt',type=['pdf','png','jpg','jpeg','webp'],key='receipt_file')
        if upload is None:return
        body=upload.getvalue();digest=hashlib.sha256(body).hexdigest();key='receipt_'+digest
        try:
            if key not in st.session_state:
                text=receipt.extract(body,upload.name)
                st.session_state[key]=receipt.parse_text(text)
            extracted=st.session_state[key]
            st.caption(extracted.get('price_source','Price could not be read; enter the receipt odds.'))
            # Unknown sport and event are never inferred from a player's name.
            fields={}
            fields['game_date']=st.text_input('Event date (YYYY-MM-DD; confirm accepted date is game date)',value=extracted.get('game_date',''),key=key+'_date')
            fields['player']=st.text_input('Player',value=extracted.get('player',''),key=key+'_player')
            fields['book']=st.text_input('Sportsbook',value=extracted.get('book',''),key=key+'_book')
            markets=sorted(set(receipt.MARKETS.values()));options=['']+markets
            fields['market']=st.selectbox('Prop market',options,index=options.index(extracted.get('market','')),key=key+'_market')
            fields['side']=st.selectbox('Side',['over','under'],index=['over','under'].index(extracted.get('side','over')),key=key+'_side')
            fields['line']=st.number_input('Receipt line',min_value=0.,value=float(extracted.get('line',0)),step=.5,key=key+'_line')
            fields['odds_american']=st.number_input('Receipt odds (American)',value=float(extracted.get('odds_american',0)),step=1.,key=key+'_price')
            fields['stake']=st.number_input('Receipt stake ($)',min_value=0.,value=float(extracted.get('stake',0)),step=.01,key=key+'_stake')
            fields['slip_id']=st.text_input('Receipt bet ID',value=extracted.get('slip_id',''),key=key+'_slip')
            fields['placed_at']=st.text_input('Placement timestamp, if printed (optional; Eastern unless offset supplied)',value='',key=key+'_placed')
            # Fresh read, not a stale offline mirror, is required for provenance matching.
            _,values=store._worksheet('paper')
            paper=pd.DataFrame(store._records(values),columns=HEADER)
            found=receipt.matches(paper,fields)
            if len(found)>1:st.error('More than one paper source matches. Review the game identity before importing.');return
            matched=found[0] if found else {}
            sports=['','nfl','nba','nhl','mlb','wnba']
            fields['sport']=st.selectbox('Receipt sport',sports,index=sports.index(matched.get('sport','')),key=key+'_sport')
            fields['away_team']=st.text_input('Away team',value=matched.get('away_team',''),key=key+'_away')
            fields['home_team']=st.text_input('Home team',value=matched.get('home_team',''),key=key+'_home')
            fields['to_win_printed']=extracted.get('to_win','');fields['extraction']=dict(extracted)
            if not all(fields.get(k) for k in ('game_date','player','book','market','sport','away_team','home_team')):
                st.info('Complete the event and receipt fields to preview. Missing values are not guessed.');return
            item=receipt.item(fields,body,upload.name)
            view=preview([item],store)
            st.dataframe(view[['game_date','sport','away_team','home_team','player','market','line','side','book','odds_american','stake','placed_at','duplicate']],hide_index=True,width='stretch')
            if bool(view.iloc[0].duplicate):st.warning('This contract is already in the real log. No additional row will be written.');return
            found=receipt.matches(paper,item)
            if len(found)==1:
                st.caption('Matched a saved paper recommendation. Its frozen provenance will be copied; no historical API request is needed.')
                ready=st.checkbox('I reviewed these receipt fields and already placed this bet',key=key+'_confirm')
                if st.button('Import receipt into real log',disabled=not ready,key=key+'_write'):
                    identity=hashlib.sha256(str(sorted(item.items())).encode()).hexdigest()
                    sub=st.session_state.setdefault(key+'_submission_'+identity,str(uuid.uuid4()))
                    result=log_paper(store,found[0]['row_id'],item['stake'],item['price_decimal'],
                        note='Imported from receipt; matched saved recommendation',submission_id=sub,receipt_item=item)
                    st.session_state['bet_receipt']=result['message'];store.cache.clear();st.rerun()
            else:
                st.info('No matching saved recommendation. Preview and explicitly prepare historical event/price evidence below. Unknown placement time leaves entry probability blank.')
                render_import(data_dir,store,items_override=[item],prefix='receipt',title='Prepare and confirm this receipt')
        except Exception:
            st.error('Receipt import stopped. Check the receipt, corrected fields and shared-log connection. No automatic retry or paid request.')
