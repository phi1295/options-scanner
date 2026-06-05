"""
Options Trade Scanner v4
- XLS/CSV IBD50 import with full multi-factor scoring
- Real IBD RS Rating, EPS Rating, Composite, ACC/DIS, % off High
- Schwab live data, rate limiting, Greeks, dynamic universe
Run: python app.py
"""

import os, json, math, time, webbrowser, threading, csv, io, pathlib, re
from datetime import datetime, timedelta, date
from flask import Flask, jsonify, request, send_from_directory
import pandas as pd
import numpy as np
import urllib.request

app = Flask(__name__, static_folder='static')

# ── Config ────────────────────────────────────────────────────────────────────
_config_path = pathlib.Path(__file__).parent / 'config.json'
_config = {}
if _config_path.exists():
    with open(_config_path) as f:
        _config = json.load(f)

SCHWAB_KEY    = _config.get('schwab_client_id',     os.environ.get('SCHWAB_APP_KEY', ''))
SCHWAB_SECRET = _config.get('schwab_client_secret', os.environ.get('SCHWAB_APP_SECRET', ''))
CALLBACK_URL  = 'https://127.0.0.1:8182'
TOKEN_PATH    = str(pathlib.Path(__file__).parent / 'schwab_token.json')

# ── Progress streaming (Server-Sent Events) ─────────────────────────────────
import queue as _queue
_progress_queue = _queue.Queue(maxsize=200)

def emit(msg, detail=False):
    """Push a progress message. detail=True = shown only in expanded view."""
    try:
        _progress_queue.put_nowait({'msg': msg, 'detail': detail})
    except:
        pass  # queue full — drop message

# ── Rate limiter ──────────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self, calls_per_minute=80):
        self.min_interval = 60.0 / calls_per_minute
        self.last_call    = 0.0
        self._lock        = threading.Lock()
    def wait(self):
        with self._lock:
            gap = self.min_interval - (time.time() - self.last_call)
            if gap > 0: time.sleep(gap)
            self.last_call = time.time()

_rl = RateLimiter(80)

def schwab_call(fn, *args, retries=3, **kwargs):
    for attempt in range(retries):
        _rl.wait()
        try:
            resp = fn(*args, **kwargs)
            if resp.status_code == 200: return resp
            if resp.status_code == 429:
                wait = 61 if '429-005' in resp.headers.get('X-RateLimit-Violated','') else 31
                print(f'Rate limit 429 — waiting {wait}s'); time.sleep(wait); continue
            if resp.status_code == 401:
                print('Token expired — re-authenticating')
                global _schwab_client; _schwab_client = None
                init_schwab()
                if _schwab_client: continue
                return None  # auth failed
            if resp.status_code in (400, 404): return None
            print(f'API {resp.status_code} for {args[:1]}')
            if attempt < retries-1: time.sleep(2**attempt)
            return None
        except Exception as e:
            print(f'Call error (attempt {attempt+1}): {e}')
            if attempt < retries-1: time.sleep(2**attempt)
    return None

# ── Schwab client ─────────────────────────────────────────────────────────────
_schwab_client = None

# ── Auth state ───────────────────────────────────────────────────────────────
_auth_pending = False   # True when login flow is running in background
_auth_error   = None    # Last auth error message if any

def get_client():
    """Return existing client."""
    return _schwab_client

def _try_load_token():
    """Silently load saved token. Returns True if successful."""
    global _schwab_client
    try:
        import schwab
        if not pathlib.Path(TOKEN_PATH).exists():
            return False
        _schwab_client = schwab.auth.client_from_token_file(
            token_path=TOKEN_PATH,
            api_key=SCHWAB_KEY,
            app_secret=SCHWAB_SECRET,
            enforce_enums=False,
        )
        print('  ✓ Schwab token loaded successfully')
        return True
    except Exception as e:
        print(f'  ⚠  Token load failed ({e}) — will require fresh login')
        try: pathlib.Path(TOKEN_PATH).unlink(missing_ok=True)
        except: pass
        return False

def init_schwab():
    """
    Called at startup. Loads token if available.
    If no token, leaves _auth_pending=False so the UI shows the
    Connect Schwab button (needs_auth state). The login flow only
    starts when the user clicks Connect.
    """
    if not SCHWAB_KEY or not SCHWAB_SECRET or 'YOUR_CLIENT_ID' in SCHWAB_KEY:
        print('  ⚠  Schwab credentials not set — edit config.json')
        return
    if _try_load_token():
        return
    print('  ○  No Schwab token — open the scanner and click Connect Schwab')
    # NOTE: do NOT set _auth_pending here — it stays False so the UI
    # shows the Connect button. It becomes True only when the user
    # clicks Connect and the login thread actually starts.

def _login_flow_thread():
    """
    Runs schwab-py's client_from_login_flow in a background thread.
    This function starts the built-in HTTPS callback server on port 8182,
    opens the Schwab login page automatically, and saves the token.
    The UI polls /api/auth/status to detect completion.
    """
    global _schwab_client, _auth_pending, _auth_error
    try:
        import schwab
        print('  Starting Schwab login flow…')
        _schwab_client = schwab.auth.client_from_login_flow(
            api_key=SCHWAB_KEY,
            app_secret=SCHWAB_SECRET,
            callback_url=CALLBACK_URL,
            token_path=TOKEN_PATH,
            enforce_enums=False,
            interactive=False,      # don't wait for console input — open browser directly
            callback_timeout=300.0, # wait up to 5 min for user to complete login
        )
        _auth_pending = False
        _auth_error   = None
        print('  ✓ Schwab authentication complete — token saved!')
    except Exception as e:
        import traceback; traceback.print_exc()
        _auth_error   = str(e)
        _auth_pending = False
        print(f'  ✗ Login flow error: {e}')

# ── IBD50 storage — now stores full row data ──────────────────────────────────
IBD50_PATH = pathlib.Path(__file__).parent / 'ibd50.json'
_ibd50_data = {}   # symbol -> dict of all IBD fields

def load_ibd50():
    global _ibd50_data
    if IBD50_PATH.exists():
        with open(IBD50_PATH) as f:
            stored = json.load(f)
            # Support both old format (list of symbols) and new format (dict)
            if isinstance(stored.get('data'), dict):
                _ibd50_data = stored['data']
            elif isinstance(stored.get('symbols'), list):
                # Migrate old format
                _ibd50_data = {s: {'symbol': s, 'rank': 99} for s in stored['symbols']}
    return _ibd50_data

def save_ibd50(data_dict):
    global _ibd50_data
    _ibd50_data = data_dict
    with open(IBD50_PATH, 'w') as f:
        json.dump({'data': data_dict, 'updated': datetime.now().isoformat(),
                   'count': len(data_dict)}, f, indent=2)

load_ibd50()

def ibd50_symbols():
    return list(_ibd50_data.keys())

def ibd50_get(symbol):
    """Return IBD data for a symbol, or empty dict if not in list."""
    return _ibd50_data.get(symbol, {})

def _parse_ibd_rows(df):
    """Shared row parser for IBD50 DataFrame."""
    result = {}
    def clean_sym(s):
        s = str(s).strip().upper()
        return s if re.match(r'^[A-Z]{1,5}$', s) else None
    def to_num(v, default=None):
        try:
            f = float(str(v).replace(',','').replace('%',''))
            return f if not np.isnan(f) else default
        except: return default
    for _, row in df.iterrows():
        sym = clean_sym(row.get('Symbol',''))
        if not sym: continue
        result[sym] = {
            'symbol':           sym,
            'company':          str(row.get('Company Name', sym)).strip(),
            'rank':             to_num(row.get('Rank'), 99),
            'price':            to_num(row.get('Price'), 0),
            'rs_rating':        to_num(row.get('RS Rating'), None),
            'eps_rating':       to_num(row.get('EPS Rating'), None),
            'composite':        to_num(row.get('Composite Rating'), None),
            'smr':              str(row.get('SMR Rating', '--')).strip(),
            'acc_dis':          str(row.get('ACC/DIS Rating', '--')).strip(),
            'group_rs':         str(row.get('Group Rel Str Rating', '--')).strip(),
            'eps_chg_latest':   to_num(row.get('EPS % Change(Latest Qtr)'), None),
            'eps_chg_prev':     to_num(row.get('EPS % Change(Prev Qtr)'), None),
            'eps_est_curr_qtr': to_num(row.get('EPS EST % Change(Current Qtr)'), None),
            'eps_est_curr_yr':  to_num(row.get('EPS EST % Change(Current Yr)'), None),
            'sales_chg':        to_num(row.get('Sales % Change(Last Qtr)'), None),
            'pct_off_high':     to_num(row.get('% off High'), None),
            'vol_chg':          to_num(row.get('Volume % Change'), None),
            'annual_roe':       to_num(row.get('Annual ROE'), None),
            'profit_margin':    to_num(row.get('Annual Profit Margin (Latest Yr)'), None),
            'qtrs_sponsorship': to_num(row.get('Qtrs of Rising Sponsorship'), None),
            'price_chg_pct':    to_num(row.get('Price % Change'), None),
        }
    print(f'_parse_ibd_rows: parsed {len(result)} stocks')
    return result


def _find_header_and_parse(text):
    """Find Symbol/Company header row and parse into DataFrame."""
    lines = text.split('\n')
    header_idx = None
    for i, line in enumerate(lines):
        if 'Symbol' in line and 'Company' in line:
            header_idx = i
            break
    if header_idx is None:
        print('_find_header_and_parse: no Symbol/Company header found')
        return {}
    df = pd.read_csv(io.StringIO(text), skiprows=header_idx, header=0)
    df = df.dropna(subset=['Symbol'])
    return _parse_ibd_rows(df)


def _find_soffice():
    """Find LibreOffice soffice binary — handles Mac app bundle and Linux PATH."""
    candidates = [
        '/Applications/LibreOffice.app/Contents/MacOS/soffice',  # Mac standard
        '/Applications/LibreOffice.app/Contents/MacOS/soffice.bin',
        '/usr/local/bin/soffice',
        '/usr/bin/soffice',
    ]
    import shutil
    path_version = shutil.which('soffice')
    if path_version:
        candidates.insert(0, path_version)
    for c in candidates:
        if pathlib.Path(c).exists():
            return c
    return None


def parse_ibd_xls_or_csv(content_bytes, filename=''):
    """
    Parse IBD50 export file (XLS, XLSX, or CSV).
    Converts XLS via LibreOffice, with full error handling.
    Returns dict of symbol -> full IBD data dict.
    Raises ValueError with a user-friendly message on failure.
    """
    import tempfile, subprocess as sp, glob
    ext = pathlib.Path(filename).suffix.lower() if filename else ''
    print(f'parse_ibd_xls_or_csv: filename={filename!r} ext={ext!r} bytes={len(content_bytes)}')

    if ext in ('.xls', '.xlsx', '.xlsm'):
        soffice = _find_soffice()
        if not soffice:
            raise ValueError(
                'LibreOffice not found. Install it from libreoffice.org '
                'then restart the app. Or export the IBD50 list as CSV from ibd.com '
                'and upload that instead.')
        print(f'Using soffice: {soffice}')

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(content_bytes)
            tmp_path = tmp.name
        out_dir  = str(pathlib.Path(tmp_path).parent)
        csv_stem = pathlib.Path(tmp_path).stem
        csv_path = str(pathlib.Path(out_dir) / (csv_stem + '.csv'))

        try:
            proc = sp.run(
                [soffice, '--headless', '--convert-to', 'csv',
                 tmp_path, '--outdir', out_dir],
                capture_output=True, timeout=60)
            print(f'LibreOffice exit={proc.returncode}')
            if proc.stderr:
                print(f'LibreOffice stderr: {proc.stderr.decode()[:400]}')
        except sp.TimeoutExpired:
            raise ValueError('LibreOffice timed out converting the file. Try exporting as CSV from ibd.com.')
        except FileNotFoundError:
            raise ValueError(f'soffice not executable at {soffice}. Reinstall LibreOffice.')
        finally:
            try: os.unlink(tmp_path)
            except: pass

        if proc.returncode == 0 and pathlib.Path(csv_path).exists():
            with open(csv_path, encoding='utf-8', errors='ignore') as f:
                text = f.read()
            try: os.unlink(csv_path)
            except: pass
            result = _find_header_and_parse(text)
            if not result:
                raise ValueError(
                    'File converted but no IBD data rows found. '
                    'Make sure this is the XLS export from ibd.com (IBD50 page → Export).')
            return result
        else:
            files = glob.glob(out_dir + '/*')
            print(f'Temp dir contents: {files[:10]}')
            raise ValueError(
                'LibreOffice could not convert the XLS file. '
                'Try exporting as CSV from ibd.com instead and uploading that.')
    else:
        text = content_bytes.decode('utf-8', errors='ignore')
        result = _find_header_and_parse(text)
        if not result:
            raise ValueError(
                'No IBD data found in file. '
                'Make sure this is the CSV or XLS export from ibd.com.')
        return result

# ── ACC/DIS rating helpers ────────────────────────────────────────────────────
ACC_DIS_SCORES = {'A+':10,'A':9,'A-':8,'B+':7,'B':6,'B-':5,'C+':4,'C':3,'C-':2,'D+':1,'D':0,'D-':-1,'E':-5}
SMR_SCORES     = {'A':5,'B':3,'C':1,'D':-2,'E':-5}
GROUP_SCORES   = {'A+':5,'A':4,'A-':3,'B+':2,'B':1,'B-':0,'C+':0,'C':-1,'C-':-2,'D':-3,'E':-5}

def acc_dis_score(rating):
    return ACC_DIS_SCORES.get(str(rating).strip(), 3)

def is_bad_acc_dis(rating):
    return str(rating).strip() in ('D+','D','D-','E')

# ── Market hours ──────────────────────────────────────────────────────────────
_mkt_cache = {'data': None, 'date': None}

def market_status(client):
    today = date.today().isoformat()
    if _mkt_cache['date'] == today and _mkt_cache['data']:
        return _mkt_cache['data']
    try:
        resp = schwab_call(client.get_market_hours,
                           markets=['option'],
                           date=date.today())
        if resp:
            data    = resp.json()
            eqo     = data.get('option', {}).get('EQO', {})
            is_open = eqo.get('isOpen', False)
            if not is_open:
                result = {'is_open': False, 'session': 'closed',
                          'message': 'Options market closed today.'}
            else:
                regular = eqo.get('sessionHours', {}).get('regularMarket', [{}])[0]
                try:
                    start = datetime.fromisoformat(regular.get('start',''))
                    end   = datetime.fromisoformat(regular.get('end',''))
                    now   = datetime.now()
                    if now < start:
                        mins = int((start-now).seconds/60)
                        result = {'is_open':False,'session':'pre',
                                  'message':f'Pre-market. Opens in ~{mins} min.'}
                    elif now > end:
                        result = {'is_open':False,'session':'after',
                                  'message':'After-hours. Using end-of-day data.'}
                    else:
                        result = {'is_open':True,'session':'regular',
                                  'message':f'Live. Closes {end.strftime("%I:%M %p")}.'}
                except:
                    result = {'is_open':is_open,'session':'regular' if is_open else 'closed',
                              'message':'Market status from Schwab.'}
        else:
            result = {'is_open':True,'session':'unknown',
                      'message':'Could not verify market hours.'}
        _mkt_cache.update({'data':result,'date':today})
        return result
    except Exception as e:
        return {'is_open':True,'session':'unknown','message':'Market hours unavailable.'}

# ── Universe ──────────────────────────────────────────────────────────────────
_universe_cache = {'symbols':[],'timestamp':None}

def _wiki_tables(url):
    """Fetch Wikipedia page with browser User-Agent and parse tables."""
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/120.0.0.0 Safari/537.36'
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode('utf-8')
    return pd.read_html(io.StringIO(html))

def fetch_sp500():
    """Fetch S&P 500 tickers from Wikipedia with proper User-Agent."""
    try:
        tables = _wiki_tables(
            'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies')
        # First table on page is the constituents table
        df = tables[0]
        col = 'Symbol' if 'Symbol' in df.columns else df.columns[0]
        syms = df[col].str.replace('.', '-', regex=False).tolist()
        syms = [s for s in syms if isinstance(s, str) and 1 < len(s) <= 6]
        print(f'SP500: {len(syms)} tickers')
        return syms
    except Exception as e:
        print(f'SP500 error: {e}'); return []

def fetch_nasdaq100():
    """Fetch Nasdaq 100 tickers from Wikipedia with proper User-Agent."""
    try:
        tables = _wiki_tables('https://en.wikipedia.org/wiki/Nasdaq-100')
        # Find the table that has Ticker/Symbol column
        df = None
        for t in tables:
            if 'Ticker' in t.columns or 'Symbol' in t.columns:
                df = t; break
        if df is None:
            df = tables[3] if len(tables) > 3 else tables[0]
        col = 'Ticker' if 'Ticker' in df.columns else               'Symbol' if 'Symbol' in df.columns else df.columns[1]
        syms = df[col].str.replace('.', '-', regex=False).tolist()
        syms = [s for s in syms if isinstance(s, str) and 1 < len(s) <= 6]
        print(f'NDX100: {len(syms)} tickers')
        return syms
    except Exception as e:
        print(f'NDX100 error: {e}'); return []

FALLBACK = ['AAPL','MSFT','NVDA','META','GOOGL','AMZN','AVGO','AMD','CRM','ORCL',
            'ADBE','QCOM','TXN','MU','AMAT','KLAC','LRCX','PANW','SNOW','NOW',
            'LLY','UNH','JNJ','ABBV','MRK','TMO','ABT','DHR','ISRG','VRTX',
            'JPM','V','MA','BAC','GS','MS','BLK','SPGI','ICE','COF',
            'COST','HD','NKE','SBUX','MCD','LOW','TGT','BKNG','CMG',
            'CAT','DE','HON','UNP','LMT','XOM','CVX','COP','SLB',
            'TSLA','NFLX','UBER','ABNB','MELI','CRWD','PLTR']

def get_movers(client):
    """Get top % gainers. Correct param is sort_order= not sort=."""
    try:
        movers = []
        for idx in ['$SPX.X', '$COMPX']:
            resp = schwab_call(
                client.get_movers,
                idx,
                sort_order='PERCENT_CHANGE_UP',
                frequency='1',
            )
            if resp:
                data = resp.json()
                # Response can be list or dict with 'screeners' key
                items = data if isinstance(data, list) else data.get('screeners', [])
                for item in items[:15]:
                    s = item.get('symbol', '')
                    if s and 1 < len(s) <= 5 and s.replace('.','').isalpha():
                        movers.append(s)
        return list(dict.fromkeys(movers))
    except Exception as e:
        print(f'Movers error: {e}'); return []

def get_universe(client):
    global _universe_cache
    now = datetime.now()
    age = (now - _universe_cache['timestamp']).total_seconds() if _universe_cache['timestamp'] else 99999
    if age < 14400 and _universe_cache['symbols']:
        base = _universe_cache['symbols']
    else:
        print('Fetching S&P500+NDX100…')
        emit('Fetching S&P500 + Nasdaq 100 universe…')
        base = list(dict.fromkeys(fetch_sp500()+fetch_nasdaq100()))
        if len(base) < 100: base = FALLBACK
        _universe_cache = {'symbols':base,'timestamp':now}
        print(f'Base universe: {len(base)}')

    ibd50  = [s for s in ibd50_symbols() if s not in base]
    movers = [s for s in get_movers(client) if s not in base]
    full   = list(dict.fromkeys(ibd50+movers+base))
    print(f'Universe: {len(full)} (IBD50:{len(ibd50)} movers:{len(movers)} base:{len(base)})')
    return full, {'sp500_ndx':len(base),'ibd50':len(ibd50),'movers':len(movers)}

# ── Batch quote pre-filter ────────────────────────────────────────────────────
def batch_quote_filter(client, symbols, chunk_size=50):
    """
    Filter symbols by price >= $30 and return a dict of live prices.
    Returns: (passing_symbols list, live_prices dict {sym: price})
    """
    passing = []
    live_prices = {}
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i+chunk_size]
        resp  = schwab_call(client.get_quotes, chunk)
        if not resp:
            passing.extend(chunk)
            continue
        data = resp.json()
        for sym in chunk:
            q = data.get(sym, {}).get('quote', {})
            # Use lastPrice (live) first, fall back to closePrice
            p = q.get('lastPrice', 0) or q.get('closePrice', 0)
            if p >= 30:
                passing.append(sym)
                live_prices[sym] = round(p, 2)
    return passing, live_prices

# ── Price history ─────────────────────────────────────────────────────────────
def get_price_history(client, symbol, days=260):
    try:
        resp = schwab_call(client.get_price_history, symbol,
                           period_type='year',
                           period='1',
                           frequency_type='daily',
                           frequency='1',
                           need_extended_hours_data=False)
        if not resp: return None
        candles = resp.json().get('candles',[])
        if not candles: return None
        df = pd.DataFrame(candles)
        df['date'] = pd.to_datetime(df['datetime'], unit='ms')
        return df.sort_values('date').reset_index(drop=True)
    except: return None

def calc_rs_score(df_stock, df_spy):
    try:
        p=[63,126,189,252]; w=[0.4,0.2,0.2,0.2]
        sc=df_spy['close'].values; stk=df_stock['close'].values
        n=min(len(sc),len(stk)); sc=sc[-n:]; stk=stk[-n:]
        return sum(w_*(( stk[-1]/stk[-p_]-1)-(sc[-1]/sc[-p_]-1))
                   for p_,w_ in zip(p,w) if n>p_)
    except: return 0

# ── Multi-factor setup score using IBD data ───────────────────────────────────
def score_stock(stock, regime):
    """
    Comprehensive score using both Schwab price data AND IBD ratings.
    Range: 0-100
    """
    s = 40  # base

    ibd = ibd50_get(stock['symbol'])
    has_ibd = bool(ibd)

    # ── IBD-based factors (up to +45 points) ─────────────────────────────
    if has_ibd:
        # IBD rank bonus: rank 1-10 = +10, 11-25 = +6, 26-50 = +3
        rank = ibd.get('rank', 50) or 50
        s += 10 if rank <= 10 else (6 if rank <= 25 else 3)

        # Real RS Rating (most important IBD signal)
        rs = ibd.get('rs_rating')
        if rs is not None:
            s += 10 if rs >= 97 else (7 if rs >= 93 else (4 if rs >= 88 else 0))

        # Composite Rating
        comp = ibd.get('composite')
        if comp is not None:
            s += 5 if comp >= 97 else (3 if comp >= 90 else 0)

        # EPS Rating
        eps_r = ibd.get('eps_rating')
        if eps_r is not None:
            s += 4 if eps_r >= 90 else (2 if eps_r >= 75 else 0)

        # ACC/DIS: institutional buying/selling
        acc = ibd.get('acc_dis','')
        ad_score = acc_dis_score(acc)
        s += min(ad_score, 5)               # cap at +5
        if is_bad_acc_dis(acc): s -= 10     # heavy penalty for D/E

        # % off High: closer to highs = stronger
        poh = ibd.get('pct_off_high')
        if poh is not None:
            poh = abs(poh)
            if poh <= 5:    s += 5
            elif poh <= 15: s += 3
            elif poh > 35:  s -= 10  # avoid — in downtrend
            elif poh > 25:  s -= 5   # too extended below high

        # SMR rating
        smr = SMR_SCORES.get(str(ibd.get('smr','')).strip(), 0)
        s += smr

        # Volume change: high volume = institutional activity
        vol_chg = ibd.get('vol_chg')
        if vol_chg is not None and vol_chg > 50: s += 3

        # EPS growth momentum
        eps_chg = ibd.get('eps_chg_latest')
        if eps_chg is not None and eps_chg > 25: s += 3

        # Quarters of rising sponsorship
        qtrs = ibd.get('qtrs_sponsorship')
        if qtrs is not None and qtrs >= 3: s += 2

    # ── Schwab price-based factors (up to +15 points) ─────────────────────
    else:
        # No IBD data — use price-based RS proxy
        rs_raw = stock['rs_raw']
        if regime == 'bullish':   s += min(rs_raw*400, 15)
        elif regime == 'bearish': s += min(-rs_raw*400, 15)

    mom = stock['mom10']
    vr  = stock['vol_ratio']

    if regime == 'bullish':
        s += min(mom*1.2, 8)
        if vr > 1.3: s += 3
        if stock['ma200']:
            d = (stock['price']-stock['ma200'])/stock['ma200']*100
            if 3 < d < 30: s += 4
    elif regime == 'bearish':
        s -= min(mom*1.2, 8)
        if vr > 1.3: s += 3
    elif regime == 'neutral':
        s += 5 if abs(mom) < 2 else 0
        s -= abs(mom)*1.5

    return max(0, min(100, round(s)))

# ── Regime ────────────────────────────────────────────────────────────────────
def detect_regime(client):
    signals = {}; votes = []
    spy_df = get_price_history(client, 'SPY', days=300)
    spy_price = spy_ma50 = spy_ma200 = None

    if spy_df is not None and len(spy_df) >= 200:
        c = spy_df['close'].values
        spy_price,spy_ma50,spy_ma200 = c[-1],c[-50:].mean(),c[-200:].mean()
        a50,a200 = spy_price>spy_ma50, spy_price>spy_ma200
        if a50 and a200:
            signals['spy_trend']={'value':f'${spy_price:.0f} above 50d & 200d','signal':'bull'}; votes.append('bullish')
        elif a200:
            signals['spy_trend']={'value':f'${spy_price:.0f} below 50d','signal':'warn'}; votes.append('caution')
        else:
            signals['spy_trend']={'value':f'${spy_price:.0f} below both MAs','signal':'bear'}; votes.append('bearish')
    else:
        signals['spy_trend']={'value':'Unavailable','signal':'neut'}

    vix_resp = schwab_call(client.get_quotes,['$VIX'])
    vix_val  = None
    if vix_resp:
        vq = vix_resp.json().get('$VIX',{}).get('quote',{})
        vix_val = vq.get('lastPrice',0) or vq.get('closePrice',0)
        if vix_val:
            if vix_val<15:   signals['vix']={'value':f'{vix_val:.1f} — low fear','signal':'bull'}; votes.append('bullish')
            elif vix_val<20: signals['vix']={'value':f'{vix_val:.1f} — normal','signal':'bull'}; votes.append('bullish')
            elif vix_val<28: signals['vix']={'value':f'{vix_val:.1f} — elevated','signal':'warn'}; votes.append('caution')
            else:            signals['vix']={'value':f'{vix_val:.1f} — high fear','signal':'bear'}; votes.append('bearish')
        else: signals['vix']={'value':'Unavailable','signal':'neut'}
    else: signals['vix']={'value':'Unavailable','signal':'neut'}

    if spy_df is not None and len(spy_df)>=20:
        c=spy_df['close'].values; m10=(c[-1]/c[-10]-1)*100; m20=(c[-1]/c[-20]-1)*100
        if m10>1 and m20>0:   signals['momentum']={'value':f'+{m10:.1f}% (10d)','signal':'bull'}; votes.append('bullish')
        elif m10<-2 or m20<-3:signals['momentum']={'value':f'{m10:.1f}% (10d)','signal':'bear'}; votes.append('bearish')
        else:                  signals['momentum']={'value':f'{m10:.1f}% (10d)','signal':'neut'}; votes.append('neutral')
    else: signals['momentum']={'value':'Calculating','signal':'neut'}

    etf_resp=schwab_call(client.get_quotes,['QQQ','IWM'])
    if etf_resp and spy_df is not None and len(spy_df)>=20:
        eq=etf_resp.json()
        qqq_p=eq.get('QQQ',{}).get('quote',{}).get('lastPrice',0)
        qqq_pc=eq.get('QQQ',{}).get('quote',{}).get('netPercentChangeInDouble',0) or 0
        spy_pc=(spy_df['close'].values[-1]/spy_df['close'].values[-5]-1)*100 if len(spy_df)>=5 else 0
        if qqq_p:
            diff = qqq_pc - spy_pc
            if diff > 0.5:
                signals['sector_rotation']={'value':f'Tech leading +{diff:.1f}%','signal':'bull'}; votes.append('bullish')
            elif diff < -0.5:
                signals['sector_rotation']={'value':f'Tech lagging {diff:.1f}%','signal':'bear'}; votes.append('bearish')
            else:
                signals['sector_rotation']={'value':'Sector rotation balanced','signal':'neut'}; votes.append('neutral')
        else: signals['sector_rotation']={'value':'Unavailable','signal':'neut'}
    else: signals['sector_rotation']={'value':'Calculating','signal':'neut'}

    if spy_df is not None and len(spy_df)>=20:
        c=spy_df['close'].values
        if c[-1]>c[-10]>c[-20]:   signals['market_breadth']={'value':'Trending higher','signal':'bull'}; votes.append('bullish')
        elif c[-1]<c[-10]<c[-20]: signals['market_breadth']={'value':'Trending lower','signal':'bear'}; votes.append('bearish')
        else:                      signals['market_breadth']={'value':'Choppy','signal':'neut'}; votes.append('neutral')
    else: signals['market_breadth']={'value':'Calculating','signal':'neut'}

    bull=votes.count('bullish'); bear=votes.count('bearish')
    caut=votes.count('caution'); neut=votes.count('neutral')

    if bear>=3:   regime,name,desc,strategy,short,prot=('bearish','Confirmed Downtrend','Market in downtrend. Bear put spreads have the wind at their back.','Bear put spreads','Bear Spreads','Bearish: target weak stocks below MAs. Standard 2–3% risk.')
    elif bull>=3: regime,name,desc,strategy,short,prot=('bullish','Confirmed Uptrend','Market in healthy uptrend. Momentum setups are working.','Bull call spreads','Bull Spreads','Bullish: target strong stocks above both MAs. Standard 2–3% risk.')
    elif caut>=2 or (bear>=2 and bull<=1): regime,name,desc,strategy,short,prot=('caution','Mixed / Caution','Conflicting signals. Protect capital first.','Reduced size or wait','Caution','Caution: cut size in half. Only trade setups scoring 88+.')
    else: regime,name,desc,strategy,short,prot=('neutral','Range-Bound / Neutral','No clear trend. Iron condors collect premium.','Iron condors','Iron Condors','Neutral: sell condors on range-bound stocks with elevated IV.')

    spy_s=f'SPY ${spy_price:.0f}' if spy_price else 'SPY loading'
    vix_s=f'VIX {vix_val:.1f}' if vix_val else 'VIX loading'
    return {'regime':regime,'regime_name':name,'regime_desc':desc,
            'recommended_strategy':strategy,'recommended_strategy_short':short,
            'signals':signals,'protection_note':prot,
            'market_context':f'{spy_s} · {vix_s} · {bull}/5 bullish · {bear}/5 bearish · Strategy: {strategy}',
            'votes':{'bull':bull,'bear':bear,'neutral':neut,'caution':caut}}

# ── Screen stock ──────────────────────────────────────────────────────────────
def screen_stock(client, symbol, spy_df, regime, live_price=None):
    try:
        df = get_price_history(client, symbol, days=260)
        if df is None or len(df)<60: return None
        c=df['close'].values
        # Use live price if available (from get_quotes), else fall back to last candle close
        price = live_price if live_price and live_price > 0 else c[-1]
        if price < 30: return None  # options too illiquid below $30
        ma50 =c[-50:].mean()  if len(c)>=50  else None
        ma200=c[-200:].mean() if len(c)>=200 else None

        if regime=='bullish':
            if not(ma50 and ma200) or price<ma50 or price<ma200: return None
        elif regime=='bearish':
            if not ma50 or price>ma50: return None
        elif regime=='neutral':
            if not ma50 or abs(price-ma50)/ma50>0.08: return None

        # IBD ACC/DIS override — never buy D/E stocks in bullish regime
        ibd = ibd50_get(symbol)
        if regime=='bullish' and ibd and is_bad_acc_dis(ibd.get('acc_dis','')):
            return None  # Institutional selling — skip

        rs_raw=calc_rs_score(df,spy_df)
        # Use live price for momentum if available, else last candle close
        mom10=(price/c[-10]-1)*100 if len(c)>=10 else 0
        vol_ratio=1.0
        if 'volume' in df.columns and len(df)>=20:
            avg=df['volume'].values[-20:].mean()
            vol_ratio=df['volume'].values[-1]/avg if avg>0 else 1

        # IBD % off High override — skip stocks >35% off high in bullish
        if regime=='bullish' and ibd:
            poh=abs(ibd.get('pct_off_high') or 0)
            if poh>35: return None

        return {'symbol':symbol,'price':price,
                'ma50':round(ma50,2) if ma50 else None,
                'ma200':round(ma200,2) if ma200 else None,
                'rs_raw':rs_raw,'mom10':mom10,'vol_ratio':vol_ratio}
    except: return None

# ── Earnings ──────────────────────────────────────────────────────────────────
def has_earnings_soon(client, symbol, days_ahead=38):
    try:
        resp=schwab_call(client.get_instruments,symbols=symbol,projection='fundamental')
        if not resp: return False,None
        instruments=resp.json().get('instruments',[])
        if not instruments: return False,None
        next_eps=instruments[0].get('fundamental',{}).get('nextEpsDate','')
        if not next_eps: return False,None
        eps_date=datetime.strptime(next_eps[:10],'%Y-%m-%d').date()
        days=(eps_date-date.today()).days
        return (0<=days<=days_ahead), eps_date.strftime('%b %d')
    except: return False,None

# ── Options chain ────────────────────────────────────────────────────────────
def get_best_spread(client, symbol, price, regime):
    """
    Build the best bull call spread (or bear put spread) for a stock.

    Key design decisions:
    - Spread width scales with stock price: ~2-3% of stock price
    - Use mark (mid) prices throughout for realistic fills
    - OI filter: both legs need OI >= 10 (retail size)
    - Bid/ask filter: < 20% of mid (percentage, not absolute)
    - Return filter: 20-45% return on debit
    - No delta filter — price proximity is more reliable given Schwab chain issues
    """
    try:
        ct = 'CALL' if regime in ('bullish', 'caution') else 'PUT'
        resp = schwab_call(
            client.get_option_chain,
            symbol=symbol,
            contract_type=ct,
            strike_count=30,
            include_underlying_quote=True,
            strategy='SINGLE'
        )
        if not resp:
            return None

        chain   = resp.json()
        exp_map = chain.get(
            'callExpDateMap' if regime in ('bullish', 'caution') else 'putExpDateMap', {})
        if not exp_map:
            print(f'    {symbol}: empty options chain')
            return None

        today = date.today()

        # ── Target spread width: ~2% of stock price, rounded to nearest $5 ──
        # AMD $523 → ~$10-15 wide, LRCX $336 → ~$7.5-10, IBKR $87 → ~$5
        raw_width   = price * 0.025
        # Round to nearest available width: 2.5, 5, 7.5, 10, 12.5, 15, 20, 25
        width_steps = [2.5, 5, 7.5, 10, 12.5, 15, 20, 25, 30, 40, 50]
        target_width = min(width_steps, key=lambda w: abs(w - raw_width))
        # Also try widths above and below in case target isn't available
        # Always try target width plus the steps immediately below and above it
        idx = width_steps.index(target_width)
        below = width_steps[max(0, idx-1)]
        above = width_steps[min(len(width_steps)-1, idx+1)]
        try_widths = sorted(set([below, target_width, above]))
        print(f'  Building {symbol} (${price:.0f})…')
        emit(f'Building spread for {symbol} (${price:.0f})…')

        def get_opt(sd, strike):
            """Return option dict for closest strike. Tolerance scales with price."""
            tolerance = max(3.0, price * 0.015)  # e.g. $7.5 for MU at $996
            best_key, best_diff = None, 999.0
            for k in sd:
                try:
                    d = abs(float(k) - strike)
                    if d < best_diff:
                        best_diff = d; best_key = k
                except:
                    continue
            if best_key is None or best_diff > tolerance:
                return None
            val = sd[best_key]
            if isinstance(val, list):
                return val[0] if val else None
            if isinstance(val, dict):
                inner = list(val.values())[0] if val else []
                return inner[0] if isinstance(inner, list) and inner else None
            return None

        def opt_mark(o):
            """Return mark price, falling back to mid of bid/ask."""
            if o is None: return 0.0
            m = o.get('mark', 0) or 0
            if m > 0: return m
            b = o.get('bid', 0) or 0
            a = o.get('ask', 0) or 0
            return (b + a) / 2 if (b + a) > 0 else 0.0

        best_result = None

        for exp_str, strikes_dict in sorted(exp_map.items()):
            try:
                parts    = exp_str.split(':')
                exp_date = datetime.strptime(parts[0], '%Y-%m-%d').date()
                dte      = int(parts[1]) if len(parts) > 1 else (exp_date - today).days
            except:
                continue
            if not (18 <= dte <= 42):   # slightly wider window: 18-42
                continue

            strikes = sorted([float(k) for k in strikes_dict.keys()])
            if len(strikes) < 3:
                continue

            # ATM = closest strike to current price
            atm = min(strikes, key=lambda x: abs(x - price))

            # Try each target width — deduplicate by actual (buy_s, sell_s) pair
            tried_pairs = set()
            for tw in try_widths:
                if regime in ('bullish', 'caution'):
                    buy_s  = atm
                    target = atm + tw
                    candidates = [s for s in strikes if s > buy_s]
                    if not candidates: continue
                    sell_s = min(candidates, key=lambda x: abs(x - target))
                else:
                    buy_s  = atm
                    target = atm - tw
                    candidates = [s for s in strikes if s < buy_s]
                    if not candidates: continue
                    sell_s = min(candidates, key=lambda x: abs(x - target))

                actual_width = abs(sell_s - buy_s)
                if actual_width < 1.0: continue  # skip $1 wide — too narrow

                pair = (buy_s, sell_s)
                if pair in tried_pairs: continue
                tried_pairs.add(pair)

                buy_o  = get_opt(strikes_dict, buy_s)
                sell_o = get_opt(strikes_dict, sell_s)

                # None check MUST come before any .get() calls
                if buy_o is None or sell_o is None:
                    continue  # no option data for this strike

                buy_mark_  = opt_mark(buy_o)
                sell_mark_ = opt_mark(sell_o)
                buy_oi_    = buy_o.get('openInterest', 0) or 0
                sell_oi_   = sell_o.get('openInterest', 0) or 0

                if buy_oi_ < 5:
                    continue  # buy OI too low
                if buy_mark_ <= 0 or sell_mark_ <= 0:
                    continue  # zero mark prices

                nd = round(buy_mark_ - sell_mark_, 2)
                if nd <= 0.10:
                    continue  # debit too small
                if nd >= actual_width:
                    continue  # debit >= width, buy leg too ITM
                if nd < actual_width * 0.10:
                    continue  # debit < 10% of width, too OTM

                mp = round(actual_width - nd, 2)
                pt = round(nd + mp * 0.50, 2)
                sl = round(nd * 0.50, 2)
                rp = round((pt - nd) / nd * 100)

                # Accept any return — we exit at 50% of max profit
                # which always yields ~half the max return regardless of range
                # Only filter extremes: <10% (spread too wide) or >300% (too cheap/risky)
                if rp < 10 or rp > 300: continue  # return out of range
                if nd < 0.50: continue                # debit too cheap

                buy_oi    = buy_o.get('openInterest', 0) or 0
                sell_oi   = sell_o.get('openInterest', 0) or 0
                buy_iv    = buy_o.get('volatility', 0) or 0
                buy_delta = abs(buy_o.get('delta', 0.5) or 0.5)
                buy_theta = buy_o.get('theta', 0) or 0
                be        = round(buy_s + nd if regime in ('bullish','caution') else buy_s - nd, 2)
                contracts = max(1, math.floor(10000 / (nd * 100 * 2)))
                leg       = 'call' if regime in ('bullish','caution') else 'put'

                print(f'    {symbol} DTE={dte}: ${buy_s:.0f}/{sell_s:.0f} w={actual_width:.0f} debit=${nd:.2f} return={rp}% OI={buy_oi}/{sell_oi}')

                result = {
                    'expiration': exp_date.strftime('%b %d %Y'),
                    'dte': dte,
                    'buy_leg':  f'Buy ${buy_s:.0f} {leg}',
                    'sell_leg': f'Sell ${sell_s:.0f} {leg}',
                    'net_debit': nd, 'max_profit': mp, 'breakeven': be,
                    'entry': nd, 'profit_target': pt, 'stop_loss': sl,
                    'return_on_debit': rp,
                    'contracts_per_10k': contracts,
                    'capital_at_risk': round(contracts * nd * 100),
                    'buy_oi': buy_oi, 'sell_oi': sell_oi,
                    'iv': round(buy_iv * 100, 1),
                    'delta': round(buy_delta, 2),
                    'theta': round(buy_theta, 3),
                }
                # Selection logic:
                # 1. Prefer return >= 80% (exit at 50% = ~40% gain on debit)
                # 2. Among valid results, prefer highest return
                # 3. Tiebreak: shorter DTE (less time risk)
                if best_result is None:
                    best_result = result
                else:
                    curr_rp  = best_result['return_on_debit']
                    curr_dte = best_result['dte']
                    # Prefer higher return; use shorter DTE as tiebreaker within 10%
                    if rp > curr_rp + 10:
                        best_result = result  # meaningfully better return
                    elif abs(rp - curr_rp) <= 10 and dte < curr_dte:
                        best_result = result  # similar return, shorter DTE

        if best_result:
            print(f'  ✓ {symbol}: spread built {best_result["buy_leg"]}/{best_result["sell_leg"]} debit=${best_result["net_debit"]:.2f} return={best_result["return_on_debit"]}%')
            emit(f'✓ {symbol}: {best_result["buy_leg"]} / {best_result["sell_leg"]} · debit ${best_result["net_debit"]:.2f} · return {best_result["return_on_debit"]}%')
        else:
            print(f'  ✗ {symbol}: no valid spread found across all expirations')
            emit(f'✗ {symbol}: no valid spread found', detail=True)
        return best_result

    except Exception as e:
        import traceback; traceback.print_exc()
        print(f'Spread error {symbol}: {e}')
    return None

def get_iron_condor(client, symbol, price):
    try:
        resp=schwab_call(client.get_option_chain,symbol=symbol,
                         contract_type='ALL',
                         strike_count=30,
                         include_underlying_quote=True,
                         strategy='SINGLE')
        if not resp: return None
        chain=resp.json(); calls=chain.get('callExpDateMap',{}); puts=chain.get('putExpDateMap',{})
        if not calls or not puts: return None
        today=date.today()
        def get_mid(sd, strike):
            """Return (mid_price, oi, iv) for closest strike within tolerance."""
            tolerance = max(3.0, price * 0.015)
            best_key, best_diff = None, 999.0
            for k in sd:
                try:
                    d = abs(float(k) - strike)
                    if d < best_diff:
                        best_diff = d; best_key = k
                except:
                    continue
            if best_key is None or best_diff > tolerance:
                return 0, 0, 0
            val = sd[best_key]
            if isinstance(val, list):
                opts = val
            elif isinstance(val, dict):
                opts = list(val.values())[0] if val else []
            else:
                opts = []
            if opts:
                o = opts[0]
                mark = o.get('mark', 0) or 0
                mid  = mark if mark > 0 else (o.get('bid',0) + o.get('ask',0)) / 2
                return mid, o.get('openInterest', 0) or 0, o.get('volatility', 0) or 0
            return 0, 0, 0
        for exp_str in sorted(calls.keys()):
            try:
                parts=exp_str.split(':')
                exp_date=datetime.strptime(parts[0],'%Y-%m-%d').date()
                dte=int(parts[1]) if len(parts)>1 else (exp_date-today).days
            except: continue
            if not(18<=dte<=38): continue
            cs=sorted([float(k) for k in calls[exp_str].keys()])
            ps=sorted([float(k) for k in puts.get(exp_str,{}).keys()])
            if not cs or not ps: continue
            # Wing width scales with stock price (~1% of price, min $5)
            wing = max(5.0, round(price * 0.01 / 2.5) * 2.5)
            sc=min(cs,key=lambda x:abs(x-price*1.05))
            lc=min(cs,key=lambda x:abs(x-(sc+wing)))
            sp=min(ps,key=lambda x:abs(x-price*0.95))
            lp=min(ps,key=lambda x:abs(x-(sp-wing)))
            sc_m,sc_oi,sc_iv=get_mid(calls[exp_str],sc); lc_m,lc_oi,_=get_mid(calls[exp_str],lc)
            sp_m,sp_oi,sp_iv=get_mid(puts.get(exp_str,{}),sp); lp_m,lp_oi,_=get_mid(puts.get(exp_str,{}),lp)
            if min(sc_oi,lc_oi,sp_oi,lp_oi)<50: continue
            avg_iv=(sc_iv+sp_iv)/2
            if avg_iv>0 and avg_iv<0.15: continue
            nc=round((sc_m-lc_m)+(sp_m-lp_m),2)
            if nc<=0: continue
            # Wing width is $5 for each side
            wing_width = abs(sc - lc)  # actual call spread width
            ml=round(wing_width-nc,2)
            if ml<=0: continue  # credit >= wing width — impossible but guard it
            pt=round(nc*0.50,2); sl=round(nc*2,2)
            rp=round(pt/ml*100) if ml>0 else 0
            contracts=max(1,math.floor(10000/(ml*100*2)))
            return {'expiration':exp_date.strftime('%b %d %Y'),'dte':dte,
                    'buy_leg':f'Sell ${sc:.0f}C/${lc:.0f}C  +  Sell ${sp:.0f}P/${lp:.0f}P',
                    'sell_leg':f'Iron condor — ${nc:.2f} credit',
                    'net_debit':-nc,'max_profit':nc,'breakeven':round((sp+sc)/2,2),
                    'entry':nc,'profit_target':pt,'stop_loss':sl,'return_on_debit':rp,
                    'contracts_per_10k':contracts,'capital_at_risk':round(contracts*ml*100),
                    'buy_oi':sc_oi,'sell_oi':sp_oi,'iv':round(avg_iv*100,1),'delta':0,'theta':0}
    except Exception as e:
        print(f'Condor error {symbol}: {e}')
    return None

# ── Build tags ────────────────────────────────────────────────────────────────
def build_tags(stock, spread, regime, earn_date, is_ibd):
    tags=[]
    ibd=ibd50_get(stock['symbol'])
    if is_ibd:
        rank=ibd.get('rank',50) or 50
        tags.append(f'IBD50 #{int(rank)}')
    p,ma50,ma200=stock['price'],stock['ma50'],stock['ma200']
    if ma50  and p>ma50:  tags.append('Above 50d MA')
    if ma200 and p>ma200: tags.append('Above 200d MA')
    if ma50  and p<ma50:  tags.append('Below 50d MA')
    # Real IBD RS if available, else proxy
    if ibd and ibd.get('rs_rating'): tags.append(f"RS {int(ibd['rs_rating'])}")
    else:
        rs=round(min(99,max(1,50+stock['rs_raw']*500))); tags.append(f'RS ~{rs}')
    # ACC/DIS
    acc=ibd.get('acc_dis','')
    if acc and acc not in('--','nan'): tags.append(f'ACC/DIS {acc}')
    if ibd and ibd.get('composite'): tags.append(f"Comp {int(ibd['composite'])}")
    if spread.get('iv'): tags.append(f"IV {spread['iv']}%")
    if spread.get('delta'): tags.append(f"Δ {spread['delta']}")
    if stock['mom10']>4: tags.append(f"+{stock['mom10']:.1f}% mom")
    if stock['vol_ratio']>1.4: tags.append('High volume')
    if earn_date: tags.append(f'Earnings {earn_date}')
    return tags

# ── Rationale ────────────────────────────────────────────────────────────────
def build_rationale(stock, spread, regime):
    ibd  = ibd50_get(stock['symbol'])
    parts= []

    if ibd:
        rank = ibd.get('rank',50) or 50
        rs   = ibd.get('rs_rating')
        comp = ibd.get('composite')
        acc  = ibd.get('acc_dis','')
        poh  = ibd.get('pct_off_high')
        eps  = ibd.get('eps_chg_latest')
        sales= ibd.get('sales_chg')

        parts.append(f"IBD50 rank #{int(rank)}.")
        if rs:   parts.append(f"RS Rating {int(rs)} (IBD actual).")
        if comp: parts.append(f"Composite {int(comp)}.")
        if acc and acc not in('--','nan'): parts.append(f"ACC/DIS {acc} — {'institutional buying' if acc_dis_score(acc)>=6 else 'mixed activity'}.")
        if poh is not None: parts.append(f"{abs(poh):.1f}% off 52-week high.")
        if eps is not None and eps>0: parts.append(f"EPS growth +{eps:.0f}% last qtr.")
        if sales is not None: parts.append(f"Sales +{sales:.0f}%.")
    else:
        ma_pct=''
        if stock['ma50']:
            pct=(stock['price']-stock['ma50'])/stock['ma50']*100
            ma_pct=f"{'Above' if pct>0 else 'Below'} 50d MA by {abs(pct):.1f}%."
        parts.append(ma_pct)
        rs=round(min(99,max(1,50+stock['rs_raw']*500)))
        parts.append(f"RS proxy ~{rs}.")

    parts.append(f"10d momentum {stock['mom10']:+.1f}%.")
    iv_str=f"IV {spread['iv']}% — {'elevated premium' if spread['iv']>30 else 'normal'}." if spread.get('iv') else ''
    if iv_str: parts.append(iv_str)
    parts.append(f"${spread['entry']:.2f} debit targets ${spread['profit_target']:.2f} ({spread['return_on_debit']}% return).")
    if not ibd: parts.append("Verify IBD RS at ibd.com before entry.")

    return ' '.join(p for p in parts if p)

# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route('/')
def index(): return send_from_directory('static','index.html')

@app.route('/api/auth/status')
def api_auth_status():
    """Poll this to know if auth is needed / in progress / complete."""
    connected = _schwab_client is not None
    return jsonify({
        'connected':      connected,
        'auth_pending':   _auth_pending,
        'auth_error':     _auth_error,
        'needs_auth':     not connected and not _auth_pending,
        'credentials_ok': bool(SCHWAB_KEY and SCHWAB_SECRET
                               and 'YOUR_CLIENT_ID' not in SCHWAB_KEY),
    })

@app.route('/api/auth/start', methods=['POST'])
def api_auth_start():
    """Start the browser-based OAuth flow in a background thread."""
    global _auth_pending, _auth_error
    if _schwab_client:
        return jsonify({'status': 'already_connected'})
    if _auth_pending:
        return jsonify({'status': 'already_pending'})
    _auth_pending = True
    _auth_error   = None
    t = threading.Thread(target=_login_flow_thread, daemon=True)
    t.start()
    return jsonify({'status': 'started',
                    'message': 'A Schwab login page will open in your browser. '
                               'Log in, approve access, then return here.'})

@app.route('/api/auth/disconnect', methods=['POST'])
def api_auth_disconnect():
    """Delete token and reset client — forces re-auth next time."""
    global _schwab_client, _auth_pending, _auth_error
    _schwab_client = None
    _auth_pending  = False
    _auth_error    = None
    try: pathlib.Path(TOKEN_PATH).unlink(missing_ok=True)
    except: pass
    return jsonify({'status': 'disconnected'})

@app.route('/api/status')
def api_status():
    has_creds = bool(SCHWAB_KEY and SCHWAB_SECRET)
    # Source of truth for "connected" is the live client object, not just the file
    connected = _schwab_client is not None
    has_token = pathlib.Path(TOKEN_PATH).exists()
    config_ok = _config_path.exists() and 'YOUR_CLIENT_ID' not in _config.get('schwab_client_id','YOUR_CLIENT_ID')
    client = _schwab_client
    mkt = market_status(client) if client else {'is_open':False,'session':'unknown','message':'Connect Schwab to check market hours.'}
    ibd_summary = {}
    if _ibd50_data:
        rs_vals = [v.get('rs_rating') for v in _ibd50_data.values() if v.get('rs_rating')]
        ibd_summary = {'count':len(_ibd50_data),
                       'avg_rs':round(sum(rs_vals)/len(rs_vals),1) if rs_vals else None,
                       'updated':json.load(open(IBD50_PATH)).get('updated','') if IBD50_PATH.exists() else ''}
    return jsonify({'has_credentials':has_creds,'has_token':has_token,'config_file':config_ok,
                    'ready':connected,'market':mkt,
                    'ibd50':ibd_summary,'universe_count':len(_universe_cache['symbols'])})

@app.route('/api/regime')
def api_regime():
    client=get_client()
    if not client: return jsonify({'error':'Schwab not configured'}),400
    try: return jsonify(detect_regime(client))
    except Exception as e: return jsonify({'error':str(e)}),500

@app.route('/api/progress')
def api_progress():
    """Server-Sent Events endpoint — streams scan progress to the browser."""
    def generate():
        # Clear any stale messages first
        while not _progress_queue.empty():
            try: _progress_queue.get_nowait()
            except: break
        yield 'data: {"msg":"ready","detail":false}\n\n'
        # Stream messages as they arrive
        import time as _time
        idle = 0
        while idle < 120:  # timeout after 2 min of silence
            try:
                item = _progress_queue.get(timeout=0.5)
                import json as _json
                yield f'data: {_json.dumps(item)}\n\n'
                idle = 0
            except:
                idle += 0.5
                yield ': keepalive\n\n'
        yield 'data: {"msg":"done","detail":false}\n\n'
    from flask import Response
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.route('/api/scan')
def api_scan():
    client=get_client()
    if not client: return jsonify({'error':'Schwab not configured. Check config.json.'}),400
    sector_filter=request.args.get('sector','all').lower()
    try:
        emit('Detecting market regime…')
        regime_data=detect_regime(client); regime=regime_data['regime']
        emit(f'Regime: {regime_data["regime_name"]} → {regime_data["recommended_strategy_short"]}')
        mkt=market_status(client); spy_df=get_price_history(client,'SPY',days=260)
        all_symbols,universe_meta=get_universe(client)

        sector_map={
            'technology':  ['AAPL','MSFT','NVDA','META','GOOGL','AVGO','AMD','CRM','ORCL','ADBE','QCOM','TXN','MU','AMAT','KLAC','LRCX','PANW','SNOW','NOW','INTC','CSCO','ANET','FTNT','ZS','CRWD','DDOG','PLTR','ARM'],
            'healthcare':  ['LLY','UNH','JNJ','ABBV','MRK','TMO','ABT','DHR','ISRG','VRTX','AMGN','GILD','REGN','BMY','PFE','CVS','CI','HUM','ELV','MOH'],
            'financials':  ['JPM','V','MA','BAC','GS','MS','BLK','SPGI','ICE','COF','WFC','C','AXP','CB','PGR','MMC','AON','TRV','SCHW','IBKR'],
            'consumer':    ['COST','HD','NKE','SBUX','MCD','LOW','TGT','BKNG','CMG','AMZN','TSLA','TJX','ROST','DG','DLTR','YUM','DPZ','HLT','MAR','ABNB'],
            'industrials': ['CAT','DE','HON','UNP','LMT','RTX','NOC','GD','BA','MMM','EMR','ETN','PH','ROK','CTAS','FAST','GWW','ITW'],
            'energy':      ['XOM','CVX','COP','SLB','EOG','PSX','VLO','MPC','OXY','HAL','DVN'],
        }
        if sector_filter!='all' and sector_filter in sector_map:
            allowed=set(sector_map[sector_filter])
            priority=[s for s in all_symbols if s in ibd50_symbols()]
            filtered=[s for s in all_symbols if s in allowed]
            all_symbols=list(dict.fromkeys(priority+filtered))

        skip={'SPY','QQQ','IWM','GLD','TLT','DIA','$VIX'}
        to_filter=[s for s in all_symbols if s not in skip]
        pre_filtered, live_prices = batch_quote_filter(client, to_filter)
        print(f'After price filter: {len(pre_filtered)} (live prices for {len(live_prices)})')
        emit(f'Universe ready: {len(pre_filtered)} stocks to screen')

        # ── Smart ordering: IBD50 first, then movers, then S&P500 ────────────
        # This means the best candidates are screened first and we can stop early
        ibd_set   = set(ibd50_symbols())
        mover_set = set(get_movers(client))
        def priority(sym):
            if sym in ibd_set:   return 0
            if sym in mover_set: return 1
            return 2
        pre_filtered.sort(key=priority)

        # Screen at most 60 stocks — IBD50+movers first guarantees best picks
        # At 80 req/min: 60 price_history + 60 earnings = ~90s max
        # With IBD50 ordered first, top candidates appear in first 40-50 calls
        candidates=[]; screened=0; max_screen=60
        for symbol in pre_filtered:
            if screened >= max_screen: break
            if screened % 10 == 0:
                print(f'  Screening {screened}/{max_screen}: {symbol}…')
                emit(f'Screening stocks… {screened}/{max_screen}')
            result=screen_stock(client, symbol, spy_df, regime,
                                     live_price=live_prices.get(symbol))
            screened+=1
            if result:
                has_earn,earn_date=has_earnings_soon(client,symbol,days_ahead=38)
                if has_earn:
                    print(f'  Skipping {symbol} — earnings {earn_date}')
                    emit(f'Skip {symbol} — earnings {earn_date}', detail=True)
                    continue
                result['earnings_date']=earn_date
                result['is_ibd50']=symbol in ibd_set
                result['score']=score_stock(result,regime)
                candidates.append(result)
                print(f'  ✓ {symbol} passed (score {result["score"]})')
                emit(f'✓ {symbol} passed screen (score {result["score"]})', detail=True)

        print(f'Screening done: {len(candidates)} candidates from {screened} screened')
        emit(f'Screening done — {len(candidates)} candidates found')

        if not candidates:
            return jsonify({'error':'No stocks passed the screen.','market':mkt,'regime':regime_data})

        candidates.sort(key=lambda x:x['score'],reverse=True)
        strat_map={'bullish':'bull_call_spread','bearish':'bear_put_spread','neutral':'iron_condor','caution':'bull_call_spread'}
        trades=[]

        for stock in candidates[:20]:
            sym=stock['symbol']

            spread=get_iron_condor(client,sym,stock['price']) if regime=='neutral' \
                   else get_best_spread(client,sym,stock['price'],regime)
            if spread is None:
                print(f'  No valid spread found for {sym}')
                continue

            ibd=ibd50_get(sym)
            tags=build_tags(stock,spread,regime,stock.get('earnings_date'),stock['is_ibd50'])

            trades.append({
                'ticker':sym,
                'company':ibd.get('company',sym) if ibd else sym,
                'stock_price':round(stock['price'],2),
                'setup_score':stock['score'],
                'top_pick':False,
                'strategy_type':strat_map.get(regime,'bull_call_spread'),
                'tags':tags,
                'expiration':spread['expiration'],'dte':spread['dte'],
                'buy_leg':spread['buy_leg'],'sell_leg':spread['sell_leg'],
                'net_debit':spread['entry'],'max_profit':spread['max_profit'],
                'breakeven':spread['breakeven'],'entry':spread['entry'],
                'profit_target':spread['profit_target'],'stop_loss':spread['stop_loss'],
                'return_on_debit':spread['return_on_debit'],
                'contracts_per_10k':spread['contracts_per_10k'],
                'capital_at_risk':spread['capital_at_risk'],
                'open_interest':spread['buy_oi'],
                'iv':spread.get('iv','—'),'delta':spread.get('delta','—'),
                'theta':spread.get('theta','—'),
                # IBD data for display
                'ibd_rs':       ibd.get('rs_rating') if ibd else None,
                'ibd_eps':      ibd.get('eps_rating') if ibd else None,
                'ibd_composite':ibd.get('composite') if ibd else None,
                'ibd_acc_dis':  ibd.get('acc_dis') if ibd else None,
                'ibd_rank':     ibd.get('rank') if ibd else None,
                'ibd_pct_off_high': ibd.get('pct_off_high') if ibd else None,
                'rationale':build_rationale(stock,spread,regime),
                'ibd_lookup':f'https://research.investors.com/stock-quotes/nasdaq-{sym.lower()}-{sym}.htm',
            })
            if len(trades)>=5: break

        if not trades:
            return jsonify({'error':'Found candidates but no valid spreads. Markets may be closed.'})

        trades[0]['top_pick']=True
        emit(f'Scan complete — {len(trades)} trade setups ready')
        return jsonify({
            'scan_date':datetime.now().strftime('%B %d, %Y %H:%M'),
            'regime':regime_data,'trades':trades,
            'market_context':regime_data['market_context'],
            'market_status':mkt,'universe_meta':universe_meta,
            'screened':screened,'candidates_found':len(candidates),
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error':str(e)}),500

# ── IBD50 import endpoints ────────────────────────────────────────────────────
@app.route('/api/ibd50',methods=['GET'])
def get_ibd50_route():
    updated=None
    if IBD50_PATH.exists():
        with open(IBD50_PATH) as f: updated=json.load(f).get('updated','')
    stocks=sorted(_ibd50_data.values(), key=lambda x: x.get('rank',99) or 99)
    return jsonify({'stocks':stocks,'symbols':ibd50_symbols(),'count':len(_ibd50_data),'updated':updated})

@app.route('/api/ibd50', methods=['POST'])
def update_ibd50_route():
    print(f'IBD50 upload: content_type={request.content_type!r} '
          f'files={list(request.files.keys())} content_len={request.content_length}')
    try:
        # ── Multipart file upload (drag-and-drop from browser) ────────────
        if request.files:
            f = request.files.get('file')
            if f:
                filename = f.filename or 'upload.xls'
                print(f'Got file: {filename!r}')
                data = parse_ibd_xls_or_csv(f.read(), filename)
                save_ibd50(data)
                return jsonify({'saved': len(data), 'symbols': list(data.keys())})

        # ── Raw bytes with filename query param ───────────────────────────
        raw = request.get_data()
        if raw:
            filename = request.args.get('filename', 'upload.csv')
            print(f'Raw upload: {len(raw)} bytes filename={filename!r}')
            data = parse_ibd_xls_or_csv(raw, filename)
            save_ibd50(data)
            return jsonify({'saved': len(data), 'symbols': list(data.keys())})

        # ── JSON symbol list ──────────────────────────────────────────────
        jdata = request.get_json(silent=True)
        if jdata and 'symbols' in jdata:
            d = {s.strip().upper(): {'symbol': s.strip().upper(), 'rank': i+1}
                 for i, s in enumerate(jdata['symbols']) if s.strip()}
            save_ibd50(d)
            return jsonify({'saved': len(d), 'symbols': list(d.keys())})

        # ── Plain text tickers ────────────────────────────────────────────
        text = (raw or b'').decode('utf-8', errors='ignore').strip()
        if text:
            syms = []
            for row in csv.reader(io.StringIO(text)):
                for cell in row:
                    v = cell.strip().upper()
                    if 1 < len(v) <= 5 and v.replace('.', '').isalpha():
                        syms.append(v)
            syms = list(dict.fromkeys(syms))
            if syms:
                d = {s: {'symbol': s, 'rank': i+1} for i, s in enumerate(syms)}
                save_ibd50(d)
                return jsonify({'saved': len(d), 'symbols': syms})

        return jsonify({'error': 'No file received. Try dragging the XLS file onto the drop zone.'}), 400

    except ValueError as e:
        # User-friendly errors raised by parse_ibd_xls_or_csv
        print(f'IBD50 upload ValueError: {e}')
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500

@app.route('/api/review', methods=['POST'])
def api_review():
    """
    Daily trade review — fetch live spread value for each open position.
    Expects JSON: [{ticker, structure, expiration, debit, target, stop, contracts, entryDate}, ...]
    Returns each trade with: current_value, pnl, pnl_pct, dte, action, action_detail, urgency
    """
    client = get_client()
    if not client:
        return jsonify({'error': 'Schwab not connected'}), 400
    try:
        trades = request.get_json(silent=True) or []
        results = []
        today = date.today()

        for t in trades:
            ticker     = t.get('ticker','')
            structure  = t.get('structure','')   # e.g. "Buy $1000 call / Sell $1010 call"
            expiration = t.get('expiration','')  # e.g. "Jul 17 2026"
            debit      = float(t.get('debit', 0))
            target     = float(t.get('target', 0))
            stop       = float(t.get('stop', 0))
            contracts  = int(t.get('contracts', 1))
            entry_date = t.get('entryDate', '')

            # Parse expiration date and compute DTE
            dte = None
            exp_date = None
            try:
                # Normalize spacing and try multiple formats
                exp_clean = ' '.join(expiration.split())  # collapse multiple spaces
                for fmt in ('%b %d %Y', '%B %d %Y', '%Y-%m-%d'):
                    try:
                        exp_date = datetime.strptime(exp_clean, fmt).date()
                        dte = (exp_date - today).days
                        break
                    except: continue
            except:
                pass

            # Parse strikes from structure string
            # Format: "Buy 000 call / Sell 010 call"
            buy_strike = sell_strike = None
            contract_type = 'CALL'
            try:
                import re as _re
                nums = _re.findall(r'\$(\d+)', structure)
                if len(nums) >= 2:
                    buy_strike  = float(nums[0])
                    sell_strike = float(nums[1])
                if 'put' in structure.lower():
                    contract_type = 'PUT'
            except:
                pass

            # Fetch live options chain
            current_value = None
            if buy_strike and sell_strike and exp_date:
                try:
                    ct = contract_type
                    resp = schwab_call(
                        client.get_option_chain,
                        symbol=ticker,
                        contract_type=ct,
                        strike_count=30,
                        include_underlying_quote=True,
                        strategy='SINGLE'
                    )
                    if resp:
                        chain = resp.json()
                        exp_map = chain.get(
                            'callExpDateMap' if ct == 'CALL' else 'putExpDateMap', {})
                        # Find the right expiration
                        for exp_str, strikes_dict in exp_map.items():
                            try:
                                ed = datetime.strptime(exp_str.split(':')[0], '%Y-%m-%d').date()
                                if ed != exp_date: continue
                            except: continue
                            # Get marks for both strikes
                            def get_mark(sd, strike):
                                # Tolerance scales with strike price — avoids wrong strike matches
                                tolerance = max(2.6, strike * 0.015)
                                best_key, best_diff = None, 999.0
                                for k in sd:
                                    try:
                                        d = abs(float(k) - strike)
                                        if d < best_diff:
                                            best_diff = d; best_key = k
                                    except: continue
                                if best_key is None or best_diff > tolerance: return None
                                val = sd[best_key]
                                opts = val if isinstance(val, list) else (list(val.values())[0] if isinstance(val, dict) and val else [])
                                if not opts: return None
                                o = opts[0]
                                m = o.get('mark', 0) or 0
                                if m > 0: return m
                                b = o.get('bid', 0) or 0
                                a = o.get('ask', 0) or 0
                                return (b + a) / 2 if (b + a) > 0 else None
                            buy_mark  = get_mark(strikes_dict, buy_strike)
                            sell_mark = get_mark(strikes_dict, sell_strike)
                            if buy_mark is not None and sell_mark is not None:
                                current_value = round(buy_mark - sell_mark, 2)
                            break
                except Exception as e:
                    print(f'Review error {ticker}: {e}')

            # Determine action
            pnl = None
            pnl_pct = None
            action = 'HOLD'
            action_detail = ''
            urgency = 'normal'  # normal / warn / critical

            if current_value is not None and debit > 0:
                pnl = round((current_value - debit) * 100 * contracts, 2)
                pnl_pct = round((current_value - debit) / debit * 100, 1)

                if current_value >= target:
                    action = 'CLOSE — TARGET HIT'
                    action_detail = f'Spread at ${current_value:.2f} — sell vertical to close, take your profit'
                    urgency = 'critical'
                elif current_value <= stop:
                    action = 'CLOSE — STOP HIT'
                    action_detail = f'Spread at ${current_value:.2f} — cancel profit target order, close at market'
                    urgency = 'critical'
                elif dte is not None and dte <= 21:
                    action = 'CLOSE — 21 DTE RULE'
                    action_detail = f'{dte} days left — time decay accelerates from here, close regardless of P&L'
                    urgency = 'critical'
                elif dte is not None and dte <= 25:
                    action = 'PREPARE TO CLOSE'
                    action_detail = f'{dte} days left — approaching 21 DTE exit rule, monitor closely'
                    urgency = 'warn'
                elif current_value <= stop * 1.30:
                    action = 'WATCH — APPROACHING STOP'
                    action_detail = f'Spread at ${current_value:.2f}, stop is ${stop:.2f} — monitor closely'
                    urgency = 'warn'
                elif current_value >= target * 0.80:
                    action = 'NEAR TARGET'
                    action_detail = f'Spread at ${current_value:.2f}, target is ${target:.2f} — consider tightening stop'
                    urgency = 'warn'
                else:
                    action = 'HOLD'
                    action_detail = f'Spread at ${current_value:.2f} — between stop (${stop:.2f}) and target (${target:.2f})'
                    urgency = 'normal'
            elif current_value is None:
                action = 'CHECK MANUALLY'
                action_detail = 'Could not fetch live price — verify in TOS'
                urgency = 'warn'

            results.append({
                'ticker':        ticker,
                'structure':     structure,
                'expiration':    expiration,
                'dte':           dte,
                'debit':         debit,
                'target':        target,
                'stop':          stop,
                'contracts':     contracts,
                'entry_date':    entry_date,
                'current_value': current_value,
                'pnl':           pnl,
                'pnl_pct':       pnl_pct,
                'action':        action,
                'action_detail': action_detail,
                'urgency':       urgency,
            })

        return jsonify({'results': results, 'as_of': datetime.now().strftime('%b %d %Y %H:%M')})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/universe/refresh',methods=['POST'])
def refresh_universe_route():
    _universe_cache['timestamp']=None
    return jsonify({'status':'Cache cleared'})

# ── Launch ────────────────────────────────────────────────────────────────────
def open_browser():
    time.sleep(1.5); webbrowser.open('http://127.0.0.1:8080')

if __name__=='__main__':
    print('\n'+'='*58)
    print('  Options Trade Scanner v4 — IBD Multi-Factor Scoring')
    print('='*58)

    if not SCHWAB_KEY or not SCHWAB_SECRET:
        print('\n  ⚠  Edit config.json with your Schwab credentials\n')
    elif 'YOUR_CLIENT_ID' in SCHWAB_KEY:
        print('\n  ⚠  Replace placeholders in config.json\n')
    else:
        print(f'\n  ✓  Credentials loaded from config.json')

    ibd = _ibd50_data
    if ibd:
        rs_vals = [v.get('rs_rating') for v in ibd.values() if v.get('rs_rating')]
        avg_rs  = round(sum(rs_vals)/len(rs_vals), 1) if rs_vals else 'n/a'
        print(f'  ✓  IBD50: {len(ibd)} stocks (avg RS {avg_rs})')
    else:
        print(f'  ○  IBD50: not loaded — upload via IBD50 Import tab')

    print(f'  ✓  Rate limiter: 80 req/min')

    # ── Schwab auth — non-blocking, UI handles OAuth if needed ──────────────
    init_schwab()  # loads token if available, sets _auth_pending if not

    # ── Start Flask ───────────────────────────────────────────────────────
    print(f'\n  Starting scanner at http://127.0.0.1:8080 …\n')
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(debug=False, host='0.0.0.0', port=8080, threaded=True)
