"""Session-only Community Cloud passphrase gate. Never reads an API credential."""
import hmac
import time
import streamlit as st


def clock():
    return time.monotonic()


def cloud_access():
    expected=st.secrets.get('APP_PASSPHRASE')
    if not isinstance(expected,str) or not expected:
        st.error('App access is not configured. The app remains locked.')
        st.stop()

    def lock():
        st.session_state['_access_unlocked']=False
        st.session_state['_access_input']=''

    def attempt():
        now=clock()
        if now<st.session_state.get('_access_until',0):return
        candidate=st.session_state.get('_access_input','')
        st.session_state['_access_input']=''
        if hmac.compare_digest(candidate.encode('utf-8'),expected.encode('utf-8')):
            st.session_state['_access_unlocked']=True
            st.session_state['_access_failures']=0
            st.session_state['_access_until']=0
        else:
            count=st.session_state.get('_access_failures',0)+1
            st.session_state['_access_failures']=count
            if count>=5:
                st.session_state['_access_until']=now+60
                st.session_state['_access_failures']=0

    if st.session_state.get('_access_unlocked',False):
        st.button('Lock',key='access_lock',on_click=lock)
        return
    waiting=clock()<st.session_state.get('_access_until',0)
    if waiting:st.warning('Too many attempts. Try again after sixty seconds.')
    elif st.session_state.get('_access_failures',0):st.error('Passphrase not accepted.')
    with st.form('access_form'):
        st.text_input('Passphrase',type='password',key='_access_input',disabled=waiting)
        st.form_submit_button('Unlock',on_click=attempt,disabled=waiting,key='access_unlock')
    st.stop()
