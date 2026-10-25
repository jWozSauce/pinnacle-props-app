import csv
import io
import json
from types import SimpleNamespace
import pandas as pd
import pytest
from pinnacle_props.bet_import import parse,prepare,preview,estimate,quote,commit
from pinnacle_props.bet_store import BetStore
from pinnacle_props.settle import settle_row,metadata
from tests.test_bet_store import Spreadsheet


def upload(notes='Open bet placed Oct 17, 2026 at 8:05:44 AM (Bet ID: slip-fixture)'):
    r={'Bet #':'1','Event Date':'2026-10-17','Sportsbook':'DraftKings','Sport':'NHL','Event / Matchup':'Example Away vs Example Home','Market / Prop':'A Skater Total Assists','Selection':'Over 0.5','Odds (American)':'300','Stake ($)':'10.00','Result':'Loss','Notes':notes}
    b=io.StringIO();w=csv.DictWriter(b,fieldnames=list(r));w.writeheader();w.writerow(r);return b.getvalue().encode()


class Client:
    run_id='import-test';run_cap=300
    def __init__(self):self.calls=[]
    def request(self,path,params):
        self.calls.append((path,params))
        e=dict(id='event-fixture',sport_key='icehockey_nhl',away_team='Example Away',home_team='Example Home',commence_time='2026-10-17T23:00:00Z')
        payload=dict(timestamp=params['date'],data=[e])
        if path.endswith('/odds'):
            e['bookmakers']=[dict(key=book,markets=[dict(key='player_assists',last_update=params['date'],outcomes=[dict(description='A Skater',name=side,point=.5,price=price) for side,price in [('Over',3.),('Under',1.5)]])]) for book in ['pinnacle','draftkings']]
            payload['data']=e
        return SimpleNamespace(payload=payload,cache_path='fixture-cache',request_id=str(len(self.calls)))
    def _load(self):return {'runs':{self.run_id:21}}


class Feed:
    def games(self,sport,day,official=False):
        return [dict(id='nhl-fixture',provider='nhl',game_date='2026-10-17',start_time='2026-10-17T23:00:00Z',home_team='Example Home',away_team='Example Away',state='final')]
    def box(self,sport,game,official=False):return game,[dict(player_id='p',player_name='A Skater',assists=0,role='skater',time_on_ice_seconds=900,participation='played')]


def test_parse_time_and_missing():
    r=parse(upload(),'bets.csv')[0];assert r['placed_at']=='2026-10-17T12:05:44+00:00';assert r['price_decimal']==4
    assert parse(upload('time unknown'),'bets.csv')[0]['placed_at']==''
    assert r['import_id']==parse(upload(),'renamed.csv')[0]['import_id']
    with pytest.raises(ValueError):parse(upload().replace(b'10.00',b'nan'),'bets.csv')


def test_prepare_guarded_write_dedupe_and_source_survives(tmp_path):
    items=parse(upload(),'bets.csv');store=BetStore(tmp_path,spreadsheet=Spreadsheet());c=Client()
    report=prepare(items,c,data_dir=tmp_path,store=store)
    r=report['records'][0];assert r['price_decimal']==4 and r['rule_version']=='imported'
    assert c.calls[1][1]['date']=='2026-10-17T12:05:44Z'
    assert c.calls[2][1]['date']=='2026-10-17T22:55:00Z'
    assert abs(r['edge']-(1/3-1/4))<1e-10
    result=commit(report,store,Feed());assert result['verified_rows']==1 and result['settled_rows']==1
    saved=store.read('real',reload=True).iloc[0].to_dict();assert saved['status']=='lost'
    assert metadata(saved)['import']['source_file']=='bets.csv'
    assert metadata(saved)['import']['book_snapshot']['price']==3
    assert preview(items,store).duplicate.all()
    assert commit(report,store,Feed())['written']==0
    assert len(store.read('real',reload=True))==1


def test_unknown_entry_and_limits(tmp_path):
    items=parse(upload('unknown'),'bets.csv');c=Client()
    report=prepare(items,c,data_dir=tmp_path)
    assert len(c.calls)==2 and report['records'][0]['fair_p']==''
    assert metadata(report['records'][0])['import']['entry']['status']=='placement time unknown'
    c=Client();c.run_cap=1
    with pytest.raises(ValueError):prepare(items,c,data_dir=tmp_path)
    assert not c.calls
    c.run_cap=1001
    assert prepare(items,c,data_dir=tmp_path)['records'] # live no longer has the old 1,000-credit allowance


def test_shared_read_failure_blocks_before_paid(tmp_path):
    store=BetStore(tmp_path,connector=lambda:(_ for _ in ()).throw(ValueError('unavailable')));c=Client()
    with pytest.raises(ValueError):prepare(parse(upload(),'bets.csv'),c,data_dir=tmp_path,store=store)
    assert not c.calls


def test_missing_and_future_quote(tmp_path):
    r=parse(upload(),'bets.csv')[0];c=Client();res=c.request('/odds',{'date':'2026-10-17T12:05:44Z'})
    assert quote(res,dict(r,player='Other'),'pinnacle')['status']=='no same-line quote'
    res.payload['data']['bookmakers'][0]['markets'][0]['last_update']='2026-10-17T12:10:00Z'
    assert quote(res,r,'pinnacle')['status']=='invalid quote timestamp'


def test_existing_normal_log_is_duplicate_without_import_metadata(tmp_path):
    items=parse(upload(),'bets.csv');store=BetStore(tmp_path,spreadsheet=Spreadsheet())
    r=prepare(items,Client(),data_dir=tmp_path)['records'][0];r['source']='';r['rule_version']='regular'
    store.append_real([r]);assert preview(items,store).duplicate.all()
    c=Client();assert prepare(items,c,data_dir=tmp_path,store=store)['records']==[];assert not c.calls


def test_snapshot_identity_change_stops_without_log_write(tmp_path):
    class Wrong(Client):
        def request(self,path,params):
            res=super().request(path,params)
            if path.endswith('/odds'):res.payload['data']['home_team']='Other'
            return res
    store=BetStore(tmp_path,spreadsheet=Spreadsheet())
    with pytest.raises(ValueError,match='identity'):prepare(parse(upload(),'bets.csv'),Wrong(),data_dir=tmp_path,store=store)
    assert store.read('real').empty


def test_source_format_and_filename_do_not_change_run_identity():
    from pinnacle_props.bet_import import run_id
    a=parse(upload(),'bets.csv');b=parse(upload(),'renamed.csv')
    assert run_id(a)==run_id(b)


def test_missing_pin_has_no_fabricated_edge(tmp_path):
    class NoPin(Client):
        def request(self,path,params):
            r=super().request(path,params)
            if path.endswith('/odds'):r.payload['data']['bookmakers']=[b for b in r.payload['data']['bookmakers'] if b['key']!='pinnacle']
            return r
    r=prepare(parse(upload(),'bets.csv'),NoPin(),data_dir=tmp_path)['records'][0]
    assert r['fair_p']==r['edge']=='' and metadata(r)['import']['clv_pp'] is None


def test_team_punctuation_normalized_without_fuzzy_matching(tmp_path):
    class Punctuation(Client):
        def request(self,path,params):
            result=super().request(path,params)
            rows=result.payload['data'] if isinstance(result.payload['data'],list) else [result.payload['data']]
            for row in rows:row['away_team']='Example. Away'
            return result
    report=prepare(parse(upload(),'bets.csv'),Punctuation(),data_dir=tmp_path)
    assert len(report['records'])==1


def test_profit_difference_retains_owner_value(tmp_path):
    from pinnacle_props.bet_import import grade_preview
    r=prepare(parse(upload(),'bets.csv'),Client(),data_dir=tmp_path)['records'][0]
    m=metadata(r);m['import']['owner_profit']='-9.99';r['source']=json.dumps(m)
    g=grade_preview([r],Feed())[0]
    assert g['comparison']=='agrees' and g['profit_difference']==-.01 and g['owner_profit']=='-9.99'


def test_future_close_skipped_and_future_placement_rejected(tmp_path):
    from types import SimpleNamespace
    from pinnacle_props.bet_import import prepare,parse
    from tests.test_bet_import import Client,upload
    class Future(Client):
        def request(self,path,params):
            assert not path.endswith('/odds')
            self.calls.append((path,params))
            event=dict(id='future-game',away_team='Example Away',home_team='Example Home',commence_time='2099-10-17T23:00:00Z')
            return SimpleNamespace(payload={'data':[event]},request_id='listing')
    items=parse(upload('time unknown').replace(b'2026-10-17',b'2099-10-17'),'bets.csv')
    c=Future();report=prepare(items,c,data_dir=tmp_path)
    assert len(c.calls)==1 and report['records'][0]['status']=='pending'
    assert metadata(report['records'][0])['import']['close']['status']=='close not yet available'
    c=Future();items[0]['placed_at']='2099-10-17T12:00:00Z'
    with pytest.raises(ValueError,match='future'):prepare(items,c,data_dir=tmp_path)
    assert not c.calls
