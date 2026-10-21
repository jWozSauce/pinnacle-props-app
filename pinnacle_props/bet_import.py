"""Preview-first Bet Tracker imports. No bets are placed; all paid reads use Client.

Historical contract: https://the-odds-api.com/liveapi/guides/v4/#get-historical-event-odds
Snapshots are at or before placement, never rounded into the future.
"""
from .live_api import LiveClient, operational_client, charged, accounting, failure_receipt
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import uuid
from zipfile import ZipFile
from xml.etree import ElementTree as ET
import pandas as pd
from .bet_store import HEADER, BetStore, StoreUnavailable
from .bet_duplicates import select_new
from .identity import name_key
from .devig import wpo
from .odds import american_to_decimal, decimal
from .odds_api import Client, normalize, write_json
from .records import DATA, record_lock
from .settings import load_config
from .settle import metadata, settle_row, OFFICIAL
from .grade import Resolver
from .settlement_sources import FreeFeed

REQUIRED=['Bet #','Event Date','Sportsbook','Sport','Event / Matchup','Market / Prop','Selection','Odds (American)','Stake ($)']
TEAMS={'SJ Sharks':'San Jose Sharks','NSH Predators':'Nashville Predators','DET Red Wings':'Detroit Red Wings','STL Blues':'St. Louis Blues','NY Islanders':'New York Islanders','TOR Maple Leafs':'Toronto Maple Leafs'}
BOOKS={'thescore bet':'espnbet','espn bet':'espnbet','draftkings':'draftkings','fanduel':'fanduel','betmgm':'betmgm','caesars':'williamhill_us'}
MARKETS={'assists':'player_assists','points':'player_points','rebounds':'player_rebounds','threes':'player_threes','shots on goal':'player_shots_on_goal','saves':'player_total_saves','goals':'player_goals','receptions':'player_receptions','passing yards':'player_pass_yds','receiving yards':'player_reception_yds','rushing yards':'player_rush_yds','hits':'batter_hits','total bases':'batter_total_bases','strikeouts':'pitcher_strikeouts'}
NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def _xlsx(body):
    """Read cached Bets cells only, never execute formulas/macros or extract ZIP paths."""
    with ZipFile(io.BytesIO(body)) as z:
        if sum(i.file_size for i in z.infolist())>20_000_000:raise ValueError('Workbook expands beyond 20 MB')
        if len(z.infolist())>500:raise ValueError('Workbook contains too many parts')
        wb=ET.fromstring(z.read('xl/workbook.xml'))
        sheets=[s for s in wb.findall('s:sheets/s:sheet',NS) if s.get('name')=='Bets']
        if len(sheets)!=1:raise ValueError('Workbook needs one Bets sheet')
        rid=sheets[0].get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
        rels=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        target=next(r.get('Target') for r in rels if r.get('Id')==rid and r.get('TargetMode')!='External')
        path=target.lstrip('/') if target.startswith('/') else 'xl/'+target
        if '..' in PurePosixPath(path).parts:raise ValueError('Invalid worksheet path')
        strings=[]
        if 'xl/sharedStrings.xml' in z.namelist():
            strings=[''.join(v.itertext()) for v in ET.fromstring(z.read('xl/sharedStrings.xml'))]
        rows=[]
        for row in ET.fromstring(z.read(path)).findall('s:sheetData/s:row',NS):
            cells={}
            for c in row.findall('s:c',NS):
                letters=re.match('[A-Z]+',c.get('r',''))
                if not letters:raise ValueError('Invalid cell reference')
                col=0
                for ch in letters[0]:col=26*col+ord(ch)-64
                if col>100:raise ValueError('Too many columns')
                t=c.get('t');value=c.find('s:v',NS)
                v=value.text if value is not None else ''
                if t=='inlineStr':v=''.join(n.text or '' for n in c.findall('.//s:t',NS))
                elif t=='s':v=strings[int(v)]
                cells[col-1]=v or ''
            if cells:rows.append([cells.get(i,'') for i in range(max(cells)+1)])
        return rows


def parse(body,name):
    if len(body)>5_000_000:raise ValueError('Import exceeds 5 MB')
    suffix=Path(name).suffix.lower()
    if suffix=='.xlsx':table=_xlsx(body)
    elif suffix=='.csv':table=list(csv.reader(io.StringIO(body.decode('utf-8-sig'))))
    else:raise ValueError('Choose a CSV or XLSX workbook')
    headers=[i for i,r in enumerate(table) if set(REQUIRED)<=set(r)]
    if len(headers)!=1:raise ValueError('Expected one Bets header with the required columns')
    header=table[headers[0]]
    if len(set(header))!=len(header):raise ValueError('Duplicate column headings')
    config=load_config();out=[];seen=set()
    for values in table[headers[0]+1:]:
        r=dict(zip(header,values))
        if not str(r.get('Bet #','')).strip():continue
        n=str(r['Bet #']).strip()
        if n in seen:raise ValueError('Duplicate Bet # in upload')
        seen.add(n)
        day=str(r['Event Date']).strip()
        if re.fullmatch(r'\d+(\.0+)?',day):day=(pd.Timestamp('1899-12-30')+pd.Timedelta(days=float(day))).date().isoformat()
        else:day=pd.Timestamp(day).date().isoformat()
        sport=str(r['Sport']).lower().strip()
        if sport not in config['sports']:raise ValueError('Unsupported sport in bet '+n)
        match=re.fullmatch(r'(.+?)\s+(?:vs\.?|@|at)\s+(.+)',r['Event / Matchup'],re.I)
        if not match:raise ValueError('Matchup needs away vs home, bet '+n)
        away,home=[TEAMS.get(v.strip(),v.strip()) for v in match.groups()]
        prop=re.fullmatch(r'(.+?)\s+(?:Total\s+)?('+ '|'.join(sorted(MARKETS,key=len,reverse=True))+')',r['Market / Prop'],re.I)
        if not prop:raise ValueError('Unsupported player prop, bet '+n)
        player=prop[1].strip();market=MARKETS[prop[2].lower()]
        selection=re.fullmatch(r'(Over|Under)\s+(\d+(?:\.\d+)?)',r['Selection'].strip(),re.I)
        if not selection:raise ValueError('Selection needs Over/Under and a line')
        book=BOOKS.get(r['Sportsbook'].strip().lower(),r['Sportsbook'].strip().lower())
        if not re.fullmatch('[a-z][a-z0-9_]*',book):raise ValueError('Unknown sportsbook; use its API key')
        price=american_to_decimal(r['Odds (American)']);stake=float(r['Stake ($)'])
        if not math.isfinite(stake) or stake<=0:raise ValueError('Stake must be finite and positive')
        notes=str(r.get('Notes',''))
        placed=str(r.get('Placed At','')).strip()
        if placed:
            t=pd.Timestamp(placed);t=t.tz_localize('America/New_York',ambiguous='raise',nonexistent='raise') if t.tzinfo is None else t
        else:
            clock=re.search(r'placed .*? at (\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M)',notes,re.I)
            t=pd.Timestamp(day+' '+clock[1]).tz_localize('America/New_York') if clock else None
        placed=t.tz_convert('UTC').isoformat() if t is not None else ''
        if t is not None and t.tz_convert('America/New_York').date().isoformat()!=day:raise ValueError('Placement date differs; provide explicit correct Event Date and Placed At')
        slip=re.search(r'Bet ID:\s*([^\s)]+)',notes)
        item=dict(number=n,game_date=day,sport=sport,away_team=away,home_team=home,player=player,market=market,
            line=float(selection[2]),side=selection[1].lower(),book=book,price_decimal=price,
            odds_american=float(str(r['Odds (American)']).replace('−','-')),stake=stake,placed_at=placed,
            owner_result=str(r.get('Result','')),owner_profit=str(r.get('Profit / Loss ($)','')),
            slip_id=slip[1] if slip else '',source_file=Path(name).name,source_sha256=hashlib.sha256(body).hexdigest(),notes=notes)
        identity=[item[k] for k in ('game_date','sport','away_team','home_team','player','market','line','side','book','price_decimal','stake','slip_id')]
        item['import_id']=hashlib.sha256(json.dumps(identity,ensure_ascii=False).encode()).hexdigest()
        out.append(item)
    if not out or len(out)>100:raise ValueError('Import must contain 1 to 100 bets')
    return out


def run_id(items):
    identity=sorted((r['import_id'],r['placed_at']) for r in items)
    return 'import-'+hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:20]


def estimate(items):
    days={(r['sport'],r['game_date']) for r in items}
    # Safe upper bound before event discovery; do not assume overlapping snapshots.
    return len(days)+10*sum(1+bool(r['placed_at']) for r in items)


def fresh(store):
    # A mirror is not evidence that this import has not already been written.
    _,values=store._worksheet('real')
    return pd.DataFrame(store._records(values),columns=HEADER)


def preview(items,store):
    current=fresh(store);ids={metadata(r).get('import',{}).get('import_id') for r in current.to_dict('records')}
    from .identity import name_key
    def semantic(r):
        try:return (str(r['sport']),str(r['game_date']),name_key(r['away_team']),name_key(r['home_team']),name_key(r['player']),r['market'],float(r['line']),r['side'])
        except (ValueError,KeyError):return None
    contracts={semantic(r) for r in current.to_dict('records')}
    return pd.DataFrame([dict(r,duplicate=r['import_id'] in ids or semantic(r) in contracts) for r in items])


def iso(t):return pd.Timestamp(t).tz_convert('UTC').strftime('%Y-%m-%dT%H:%M:%SZ')


def quote(result,r,book,*,require_pair=True):
    if result is None:return {'status':'placement time unknown'}
    f=normalize(result.payload['data']);stamp=pd.Timestamp(result.payload['timestamp'])
    f=f[(f.book==book)&(f.player==r['player'])&(f.market==r['market'])].copy()
    f['point']=pd.to_numeric(f.point,errors='coerce')
    lines=sorted(f.point.dropna().unique().tolist())
    f=f[f.point==r['line']]
    if f.empty:return {'status':'no same-line quote','available_lines':lines}
    updates=pd.to_datetime(f.last_update,utc=True,errors='coerce')
    if updates.isna().any() or (updates>stamp).any():return {'status':'invalid quote timestamp'}
    if not require_pair:
        side=f[f.side.str.lower()==r['side']]
        if len(side)!=1:return {'status':'missing or ambiguous book side'}
        return dict(status='matched',price=decimal(side.iloc[0].price),timestamp=result.payload['timestamp'])
    if len(f)!=2 or set(f.side.str.lower())!={'over','under'}:return {'status':'incomplete or ambiguous pair','available_lines':lines}
    over=decimal(f.loc[f.side.str.lower()=='over','price'].iloc[0]);under=decimal(f.loc[f.side.str.lower()=='under','price'].iloc[0])
    fair=wpo([over,under])[('over','under').index(r['side'])]
    return dict(status='matched',line=r['line'],over=over,under=under,fair_p=fair,
        price=over if r['side']=='over' else under,timestamp=result.payload['timestamp'])


def prepare(items,client,*,data_dir=DATA,store=None):
    with failure_receipt(client,Path(data_dir)/'imports'/('prepared-'+client.run_id)/'failed.json',{'run_id':client.run_id}):
        return _prepare(items,client,data_dir=data_dir,store=store)


def _prepare(items,client,*,data_dir=DATA,store=None):
    if client.run_cap<=0:raise ValueError('Import requires a positive estimate')
    if store is not None:
        seen=preview(items,store);items=seen[~seen.duplicate].drop(columns='duplicate').to_dict('records')
    if not items:return {'records':[],'credits':0,'message':'All rows already imported'}
    if estimate(items)>client.run_cap:raise ValueError('Estimated import exceeds the selected cap; split the file')
    root=Path(data_dir)/'imports'/('prepared-'+client.run_id);root.mkdir(parents=True,exist_ok=True)
    if isinstance(client,LiveClient):
        attempt=root/'attempt.json';client.resume_only=attempt.exists()
        if not attempt.exists():
            client.preflight()
            write_json(attempt,{'at':datetime.now(timezone.utc).isoformat(),'estimate':client.run_cap})
    config=load_config();listings={};results={};records=[]
    for r in items:
        sport=config['sports'][r['sport']]['key'];base='/v4/historical/sports/'+sport+'/events'
        daykey=(sport,r['game_date'])
        if daykey not in listings:
            at=pd.Timestamp(r['game_date'],tz='America/New_York')
            listings[daykey]=client.request(base,{'date':iso(at)})
        listing=listings[daykey]
        matches=[e for e in listing.payload['data'] if name_key(e['away_team'])==name_key(r['away_team']) and name_key(e['home_team'])==name_key(r['home_team']) and pd.Timestamp(e['commence_time']).tz_convert('America/New_York').date().isoformat()==r['game_date']]
        if len(matches)!=1:raise ValueError('Historical game missing or ambiguous for bet '+r['number']+'; no write')
        event=matches[0];start=pd.Timestamp(event['commence_time'])
        if r['placed_at'] and pd.Timestamp(r['placed_at'])>=start:raise ValueError('In-play import needs separate review; no write')
        close_at=iso(start-pd.Timedelta(minutes=5))
        # Each game requests the union of imported books (always one band here).
        books=sorted({'pinnacle'}|{x['book'] for x in items if x['sport']==r['sport']})
        if len(books)>10:raise ValueError('Import is limited to ten bookmakers per sport/date')
        got={}
        for label,at in [('entry',iso(r['placed_at']) if r['placed_at'] else ''),('close',close_at)]:
            if not at:got[label]=None;continue
            key=(event['id'],r['market'],at,tuple(books))
            if key not in results:
                res=client.request(base+'/'+event['id']+'/odds',dict(date=at,markets=r['market'],bookmakers=','.join(books),oddsFormat='decimal'))
                data=res.payload['data']
                if any(data.get(k)!=event[k] for k in ('id','home_team','away_team','commence_time')):raise ValueError('Historical event identity/start changed; review before import')
                stamp=pd.Timestamp(res.payload['timestamp'])
                if stamp>pd.Timestamp(at) or stamp>=start or pd.Timestamp(at)-stamp>pd.Timedelta(minutes=6):raise ValueError('Historical snapshot timing invalid')
                results[key]=res
            got[label]=results[key]
        pin=quote(got['entry'],r,'pinnacle');book=quote(got['entry'],r,r['book'],require_pair=False);close=quote(got['close'],r,'pinnacle')
        provenance=dict(r,entry=pin,book_snapshot=book,close=close,
            clv_pp=(close['fair_p']-1/r['price_decimal']) if close['status']=='matched' else None,
            historical_sources={label:dict(timestamp=res.payload['timestamp'],raw_cache=res.cache_path,request_id=res.request_id) for label,res in got.items() if res is not None},listing_request_id=listing.request_id)
        record=dict.fromkeys(HEADER,'')
        record.update({k:r[k] for k in ['sport','game_date','away_team','home_team','player','market','line','side','book','price_decimal','odds_american','stake']})
        record.update(row_id=str(uuid.uuid4()),logged_at=r['placed_at'] or datetime.now(timezone.utc).isoformat(),
            start_time=event['commence_time'],event_id=event['id'],scan_id=client.run_id,row=r['number'],rule_version='imported',status='pending',
            fair_p=pin.get('fair_p',''),edge=pin['fair_p']-1/r['price_decimal'] if pin['status']=='matched' else '',
            source=json.dumps({'import':provenance},sort_keys=True),note=f"Imported from {r['source_file']}, bet #{r['number']}, slip id {r['slip_id'] or 'unknown'}. Entry: {pin['status']}. Placement time: {r['placed_at'] or 'unknown; logged_at is import time'}. Owner result: {r['owner_result']}.")
        records.append(record)
    report=dict(records=records,credits=charged(client),accounting=accounting(client),cap=client.run_cap,run_id=client.run_id)
    write_json(root/'prepared.json',report)
    return report


def grade_preview(records,feed):
    resolver=Resolver(feed,official=True);out=[]
    for r in records:
        game,players,reason=resolver.resolve(r)
        new,reason=settle_row(r,game,players) if game else (dict(r),reason)
        owner=metadata(r)['import']['owner_result'].lower()
        expected={'win':'won','loss':'lost','push':'push','void':'void'}.get(owner)
        owner_profit=metadata(r)['import'].get('owner_profit','')
        delta=round(float(new['profit'])-float(owner_profit),2) if new.get('profit','')!='' and owner_profit!='' else None
        out.append(dict(row_id=r['row_id'],number=r['row'],official_status=new['status'],stat=new.get('stat',''),profit=new.get('profit',''),owner_result=owner,
            owner_profit=owner_profit,profit_difference=delta,comparison='agrees' if expected==new['status'] else 'unverified' if new['status']=='pending' else 'DIFFERS',reason=reason))
    return out


def commit(report,store,feed):
    """Fresh dedupe, pending append, then grade only this import's remote rows."""
    records=report['records']
    current=fresh(store);selected,skipped=select_new(records,current)
    receipt=store.append_real(selected)
    if receipt.get('queued'):return dict(receipt,grades=[],skipped=skipped)
    current=fresh(store);ids={r['row_id'] for r in records};resolver=Resolver(feed,official=True)
    changes=[];grades=[]
    for r in current.to_dict('records'):
        if r['row_id'] not in ids:continue
        game,players,reason=resolver.resolve(r)
        new,reason=settle_row(r,game,players) if game else (r,reason)
        if new!=r:changes.append((r,new))
        grades.extend(grade_preview([new],feed))
    if changes:store.grade('real',changes)
    verified=fresh(store);verified=verified[verified.row_id.isin(ids)]
    return dict(receipt,skipped=skipped,grades=grades,verified_rows=len(verified),settled_rows=int(verified.status.isin(OFFICIAL).sum()))


def main():
    p=argparse.ArgumentParser(description='Preview/import already placed bets; never places bets')
    p.add_argument('file',type=Path);p.add_argument('--data-dir',type=Path,default=DATA)
    p.add_argument('--backfill',action='store_true',help='Authorize the displayed historical estimate')
    p.add_argument('--write',action='store_true',help='Append prepared rows and grade from official sources')
    a=p.parse_args()
    try:
        items=parse(a.file.read_bytes(),a.file.name);store=BetStore(a.data_dir)
        view=preview(items,store);todo=view[~view.duplicate].drop(columns='duplicate').to_dict('records')
        print(view[['number','game_date','player','book','stake','placed_at','duplicate']].to_string(index=False))
        print(f'Historical bound: {estimate(todo)} credits')
        if not a.backfill:
            if a.write:raise ValueError('--write requires --backfill')
            return
        rid=run_id(todo)
        path=a.data_dir/'imports'/('prepared-'+rid)/'prepared.json'
        report=json.loads(path.read_text()) if path.exists() else prepare(todo,operational_client(data_dir=a.data_dir,run_id=rid,run_cap=estimate(todo)),data_dir=a.data_dir,store=store)
        feed=FreeFeed(a.data_dir/'settlement_cache')
        print(json.dumps(grade_preview(report['records'],feed),indent=2))
        if a.write:print(json.dumps(commit(report,store,feed),indent=2))
    except Exception:
        p.exit(1,'Import stopped. Saved evidence retained; no automatic network retry. Check input, shared access and accounting before resuming.\n')

if __name__=='__main__':main()
