"""Local receipt extraction; uncertain fields always require an editable preview."""
import hashlib
import io
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import pandas as pd
from .bet_import import MARKETS
from .odds import american_to_decimal

BOOK_NAMES={'betonline':'betonlineag','betonline.ag':'betonlineag','draftkings':'draftkings','fanduel':'fanduel',
            'betmgm':'betmgm','caesars':'williamhill_us','betrivers':'betrivers','bovada':'bovada','hard rock':'hardrockbet','thescore':'espnbet'}
MARKET_NAMES=dict(MARKETS,**{'three point field goals made':'player_threes','three pointers made':'player_threes',
    '3-pointers made':'player_threes','3 pointers':'player_threes','three pointers':'player_threes'})


def ocr(path):
    executable=shutil.which('tesseract')
    if executable:
        result=subprocess.run([executable,str(path),'stdout','--psm','6'],capture_output=True,timeout=45,check=True)
    elif sys.platform=='darwin' and shutil.which('swift'):
        result=subprocess.run(['swift',str(Path(__file__).with_name('receipt_ocr.swift')),str(path)],capture_output=True,timeout=90,check=True)
    else:raise ValueError('Receipt OCR is unavailable on this deployment')
    return result.stdout.decode('utf-8',errors='replace')


def extract(body,name):
    if len(body)>5_000_000:raise ValueError('Receipt exceeds 5 MB')
    suffix=Path(name).suffix.lower()
    with tempfile.TemporaryDirectory(prefix='pp-receipt-') as directory:
        root=Path(directory);file=root/('receipt'+suffix);file.write_bytes(body)
        if suffix=='.pdf':
            from pypdf import PdfReader
            reader=PdfReader(io.BytesIO(body))
            if reader.is_encrypted or not 1<=len(reader.pages)<=3:raise ValueError('Use an unencrypted receipt of one to three pages')
            text='\n'.join(page.extract_text() or '' for page in reader.pages)
            # Some books put their name only in a logo. Extract it when possible.
            if not any(k in text.lower() for k in BOOK_NAMES) or len(text.strip())<30:
                if not shutil.which('pdftoppm'):
                    if not text.strip():raise ValueError('PDF image extraction is unavailable')
                else:
                    for i in range(len(reader.pages)):
                        target=root/f'page{i}'
                        subprocess.run(['pdftoppm','-f',str(i+1),'-l',str(i+1),'-scale-to','1800','-png','-singlefile',str(file),str(target)],capture_output=True,timeout=45,check=True)
                        try:extra=ocr(target.with_suffix('.png'))
                        except (ValueError,subprocess.SubprocessError):
                            if not text.strip():raise ValueError('OCR failed; use a clearer receipt') from None
                            break
                        # Prefer embedded text for values; OCR supplements a missing logo only.
                        if text.strip():
                            for book in BOOK_NAMES:
                                if book in extra.lower() and book not in text.lower():text+='\n'+book
                        else:text=extra
            return text
        if suffix not in ('.png','.jpg','.jpeg','.webp'):raise ValueError('Choose a PDF or receipt image')
        from PIL import Image,ImageOps
        with Image.open(file) as image:
            if image.width*image.height>25_000_000:raise ValueError('Receipt image is too large')
            image=ImageOps.exif_transpose(image).convert('RGB');image.thumbnail((2500,3500));image.save(root/'image.png')
        return ocr(root/'image.png')


def parse_text(text):
    text=text.replace('−','-');flat=' '.join(text.split());lower=flat.lower();out={}
    books={v for k,v in BOOK_NAMES.items() if k in lower}
    if len(books)==1:out['book']=books.pop()
    date=re.search(r'(?:Accepted Date|Placed Date|Date)\s*:?\s*(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})',flat,re.I)
    if date:out['game_date']=pd.Timestamp(date[1]).date().isoformat()
    slip=re.search(r'Bet\s*ID\s*:?\s*([\w-]+)',flat,re.I)
    if slip:out['slip_id']=slip[1]
    for label,field in [('Risk|Stake|Wager Amount','stake'),('To Win|Potential Profit','to_win')]:
        m=re.search(r'(?:'+label+r')\s*:?\s*\$?([\d,]+\.\d{2})',flat,re.I)
        if m:out[field]=float(m[1].replace(',',''))
    selections=list(re.finditer(r'\b(Over|Under)\s+(\d+(?:\.\d+)?)\b',flat,re.I))
    if len(selections)!=1:raise ValueError('Upload one single-selection receipt; multiple or missing selections need review')
    m=selections[0];out.update(side=m[1].lower(),line=float(m[2]))
    before=flat[:m.start()]
    player=re.search(r'(?:Description|Selection|Player)\s*:?\s*(.+?)(?:\s*\([A-Z]{2,4}\))?\s*$',before,re.I)
    if not player:
        last=text[:text.lower().find(m[0].lower())].strip().splitlines()[-1] if text.strip() else ''
        if last and len(last)<80:out['player']=re.sub(r'\s*\([A-Z]{2,4}\)\s*$','',last).strip()
    else:out['player']=player[1].strip()
    tail=flat[m.end():].lower()
    for label in sorted(MARKET_NAMES,key=len,reverse=True):
        if re.match(r'\s*'+re.escape(label)+r'\b',tail):out['market']=MARKET_NAMES[label];break
    price=re.search(r'(?:Odds|Price)\s*:?\s*([+-]\d{3,5})',flat,re.I)
    if price:out['odds_american']=float(price[1]);out['price_decimal']=american_to_decimal(price[1]);out['price_source']='printed odds'
    elif out.get('stake',0)>0 and out.get('to_win',0)>0:
        out['price_decimal']=round(1+out['to_win']/out['stake'],2)
        from .odds import decimal_to_american
        out['odds_american']=round(decimal_to_american(out['price_decimal']),2);out['price_source']='estimated from printed risk/to-win; confirm rounding'
    return out


def item(fields,body,name):
    """Use the same historical importer after explicit correction of missing game fields."""
    from .bet_import import parse
    import csv
    r={'Bet #':'1','Event Date':fields['game_date'],'Sportsbook':fields['book'],'Sport':fields['sport'],
       'Event / Matchup':fields['away_team']+' @ '+fields['home_team'],'Market / Prop':fields['player']+' '+next(k for k,v in MARKETS.items() if v==fields['market']),
       'Selection':fields['side'].title()+' '+str(fields['line']),'Odds (American)':fields['odds_american'],'Stake ($)':fields['stake'],
       'Placed At':fields.get('placed_at',''),'Result':'pending','Notes':'Bet ID: '+fields.get('slip_id','unknown')}
    stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=list(r));writer.writeheader();writer.writerow(r)
    result=parse(stream.getvalue().encode(),'receipt.csv')[0]
    result.update(source_file=Path(name).name,source_sha256=hashlib.sha256(body).hexdigest(),receipt_fields=dict(fields))
    return result


def matches(frame,fields):
    from .identity import name_key
    def same(r):
        try:
            return (str(r['game_date'])==str(fields['game_date']) and name_key(r['player'])==name_key(fields['player'])
                and r['market']==fields['market'] and float(r['line'])==float(fields['line'])
                and r['side']==fields['side'] and r['book']==fields['book']
                and (not fields.get('sport') or r['sport']==fields['sport'])
                and (not fields.get('away_team') or name_key(r['away_team'])==name_key(fields['away_team']))
                and (not fields.get('home_team') or name_key(r['home_team'])==name_key(fields['home_team'])))
        except (KeyError,ValueError,TypeError):return False
    return [r for r in frame.to_dict('records') if same(r)]
