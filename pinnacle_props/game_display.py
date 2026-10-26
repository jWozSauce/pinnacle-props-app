"""Game context from saved rows only. Never guess book event identifiers or teams."""
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlsplit
import hashlib,json,re
from .identity import name_key

BOOK_HOSTS={'draftkings':('sportsbook.draftkings.com',),'fanduel':('sportsbook.fanduel.com',),
    'betmgm':('sports.betmgm.com',),'betonlineag':('www.betonline.ag','betonline.ag'),
    'espnbet':('www.thescore.bet','thescore.bet'),'bovada':('www.bovada.lv','bovada.lv'),
    'williamhill_us':('sportsbook.caesars.com',)}

def text(value):
    if value is None:return ''
    value=str(value).strip()
    return '' if value.lower() in ('nan','nat','none','<na>') else value

def game_label(row):
    away=text(row.get('away_team'));home=text(row.get('home_team'))
    matchup=f'{away} @ {home}' if away and home else text(row.get('game')) or 'Matchup unavailable'
    try:
        when=datetime.fromisoformat(text(row.get('start_time') or row.get('date')).replace('Z','+00:00'))
        if when.tzinfo is None:raise ValueError()
        when=when.astimezone(ZoneInfo('America/New_York'))
        clock=when.strftime('%I:%M').lstrip('0')+' '+when.strftime('%p').lower()
        start=when.strftime('%a')+' '+clock+' ET'
    except (TypeError,ValueError):start='Start time unavailable'
    return matchup+' · '+start

def book_link(row):
    # Odds API event ids are not sportsbook ids. Only retained provider event URLs
    # on known book hosts are usable; absence produces no invented link.
    value=text(row.get('book_event_url') or row.get('event_url'))
    try:
        url=urlsplit(value)
        if url.scheme=='https' and not url.username and not url.password and url.hostname in BOOK_HOSTS.get(row.get('book'),()) and url.path not in ('','/'):
            return value
    except ValueError:pass
    return None

def player_label(row,root=None):
    player=text(row.get('player_name') or row.get('player'))
    team=text(row.get('player_team'))
    if not team:
        try:
            meta=json.loads(row.get('source') or '{}');digest=meta.get('raw_key','')
            if meta.get('provider')!='espn' or not re.fullmatch('[0-9a-f]{64}',digest):return player
            if root is None:
                from .settings import ROOT
                root=ROOT/'data'/'settlement_cache'
            body=(Path(root)/(digest+'.raw')).read_bytes()
            if hashlib.sha256(body).hexdigest()!=digest:return player
            box=json.loads(body)
            if str(box.get('header',{}).get('id'))!=str(meta.get('game_id')):return player
            matches=set()
            for group in box.get('boxscore',{}).get('players',[]):
                for category in group.get('statistics',[]):
                    for item in category.get('athletes',[]):
                        athlete=item.get('athlete',{})
                        if name_key(athlete.get('displayName',''))==name_key(player):
                            matches.add((str(athlete.get('id')),group.get('team',{}).get('abbreviation','')))
            if len(matches)==1:team=next(iter(matches))[1]
        except (OSError,ValueError,TypeError,KeyError):pass
    return player+(' ('+team+')' if team else '')

def caption(row):
    import streamlit as st
    st.caption(game_label(row))
    url=book_link(row)
    if url:st.link_button('Open at book',url)
