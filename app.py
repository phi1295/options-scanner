"""
Options Trade Scanner v4
- XLS/CSV IBD50 import with full multi-factor scoring
- Real IBD RS Rating, EPS Rating, Composite, ACC/DIS, % off High
- Schwab live data, rate limiting, Greeks, dynamic universe
Run: python app.py
"""

import os, json, math, time, webbrowser, threading, csv, io, pathlib, re, sqlite3
import sys

# ── Force UTF-8 output ────────────────────────────────────────────────────────
# Under systemd (and some other non-interactive environments) Python may default
# stdout/stderr to latin-1, which crashes on the Unicode characters used in the
# startup banner and progress prints (em-dash, ✓, etc.). Reconfigure to UTF-8 so
# logging works identically whether run by hand or as a service.
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass  # older Python without reconfigure(); env var below is the fallback
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

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

# Port the web server listens on. Set "server_port" in config.json to change it.
# (This is separate from the Schwab OAuth callback port 8182, which is fixed
#  and registered in your Schwab Developer Portal — do not change that.)
try:
    SERVER_PORT = int(_config.get('server_port', os.environ.get('SCANNER_PORT', 8080)))
except (ValueError, TypeError):
    SERVER_PORT = 8080

# ── SQLite storage (single portable file: scanner.db) ─────────────────────────
# This file holds trades + settings, shared across all clients (Mac, phone).
# To move to another machine, just copy scanner.db — it's fully self-contained.
DB_PATH = str(pathlib.Path(__file__).parent / 'scanner.db')
_db_lock = threading.Lock()

def db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')      # safe concurrent reads/writes
    return conn

def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    with _db_lock, db_conn() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY,
            ticker      TEXT, company TEXT, structure TEXT, expiration TEXT,
            entryDate   TEXT, contracts INTEGER, debit REAL,
            target      REAL, stop REAL, returnPct REAL, notes TEXT,
            status      TEXT DEFAULT 'open',
            exitPrice   REAL, pnl REAL,
            ibd_rs      INTEGER, ibd_score INTEGER,
            is_condor   INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        )''')
        conn.commit()

# Columns the trades table accepts — used to filter incoming JSON safely
_TRADE_COLS = ['id','ticker','company','structure','expiration','entryDate',
               'contracts','debit','target','stop','returnPct','notes','status',
               'exitPrice','pnl','ibd_rs','ibd_score','is_condor']



# ── Progress streaming (Server-Sent Events) ─────────────────────────────────
import queue as _queue
_progress_queue = _queue.Queue(maxsize=200)

# ── Scan concurrency control ─────────────────────────────────────────────────
# Only one scan may run at a time. A second request is rejected rather than
# starting a concurrent scan (which would double-hit the Schwab API and race
# on the progress queue). _scan_cancel lets a running scan be stopped.
_scan_lock    = threading.Lock()
_scan_running = False
_scan_cancel  = threading.Event()
_last_scan_tickers = set()   # tickers from the previous scan, for "seen before" flagging

def emit(msg, detail=False):
    """Push a progress message. detail=True = shown only in expanded view."""
    try:
        _progress_queue.put_nowait({'msg': msg, 'detail': detail})
    except:
        pass  # queue full — drop message

class ScanCancelled(Exception):
    """Raised inside a scan when the user requests cancellation."""
    pass

def check_cancel():
    """Call at safe points during a scan; raises if cancellation was requested."""
    if _scan_cancel.is_set():
        raise ScanCancelled()

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
                # The access token is invalid AND the refresh token behind it has
                # likely expired too (refresh tokens last ~7 days). Re-loading the
                # same token file can't fix this — it would just load the same
                # dead token again. Clear the client and surface the failure so
                # the UI shows "needs reconnect" instead of silently looping.
                print('Token expired (401) — clearing client, user must reconnect')
                global _schwab_client, _auth_error
                _schwab_client = None
                _auth_error = 'Schwab session expired. Please reconnect.'
                return None
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
_auth_pending = False   # True while a login flow is awaiting the pasted-back redirect URL
_auth_error   = None    # Last auth error message if any
_manual_auth_url = None # The Schwab authorization URL for the user to open
_pending_auth_context = None # AuthContext (url + oauth state) from get_auth_context(),
                              # held between /api/auth/start and /api/auth/complete

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

def _token_write_func(token, *args, **kwargs):
    """Write a raw Schwab token dict to disk. Passed to client_from_received_url
    in place of schwab-py's internal (underscore-private) file-writer helper."""
    with open(TOKEN_PATH, 'w') as f:
        json.dump(token, f)

def _start_manual_auth():
    """
    Build the Schwab authorization URL for the user to open.

    Schwab's OAuth callback is required to be 127.0.0.1 (schwab-py enforces
    this), which only a browser running on THIS exact machine could ever
    reach. Since this app is normally viewed from a phone or another
    computer, waiting for that callback to arrive here never works. Instead,
    the user opens the URL wherever their browser lives, logs in, and pastes
    the resulting (failed-to-load) redirect URL back into the UI —
    _complete_manual_auth() below exchanges the code it contains for a token
    directly, with no listener involved at all.

    get_auth_context() only builds a URL string locally (no network call),
    so this runs synchronously — no background thread needed.
    """
    global _auth_pending, _auth_error, _manual_auth_url, _pending_auth_context
    import schwab
    _pending_auth_context = schwab.auth.get_auth_context(SCHWAB_KEY, CALLBACK_URL)
    _manual_auth_url = _pending_auth_context.authorization_url
    _auth_pending = True
    _auth_error   = None
    print('  Auth URL ready — waiting for the redirect URL to be pasted back')

def _complete_manual_auth(redirect_url):
    """
    Exchange the pasted-back redirect URL for a token and connect.
    Returns (ok, error_message).
    """
    global _schwab_client, _auth_pending, _auth_error
    global _manual_auth_url, _pending_auth_context
    if not _pending_auth_context:
        return False, 'No login in progress. Click Connect Schwab first.'
    try:
        import schwab
        _schwab_client = schwab.auth.client_from_received_url(
            SCHWAB_KEY, SCHWAB_SECRET, _pending_auth_context,
            redirect_url.strip(), _token_write_func,
            asyncio=False, enforce_enums=False)
        _auth_pending = False
        _auth_error   = None
        _manual_auth_url = None
        _pending_auth_context = None
        print('  ✓ Schwab authentication complete — token saved!')
        return True, None
    except Exception as e:
        print(f'  ✗ Manual auth exchange failed: {e}')
        _auth_error = ('Could not complete login — that link may be expired '
                       'or already used. Click Connect Schwab and try again.')
        _auth_pending = False
        _manual_auth_url = None
        _pending_auth_context = None
        return False, str(e)

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
_mkt_cache = {'hours': None, 'date': None}

def market_status(client):
    """
    Returns the current market session (pre/regular/after/closed).

    IMPORTANT: the market's hours for today don't change, but the CURRENT
    SESSION does — it must be recomputed against the live clock on every
    call. Caching the whole result (including session) was the bug: once
    it computed "regular/open" at, say, 11am, it stayed cached as green
    until midnight even after the market closed at 4pm. The fix caches
    only the day's hours (the part that's genuinely stable) and always
    re-evaluates now() vs those hours fresh.
    """
    today = date.today().isoformat()
    if _mkt_cache['date'] == today and _mkt_cache.get('hours'):
        return _compute_session(_mkt_cache['hours'])
    try:
        resp = schwab_call(client.get_market_hours,
                           markets=['option'],
                           date=date.today())
        if resp:
            data = resp.json()
            eqo  = data.get('option', {}).get('EQO', {})
            is_open_today = eqo.get('isOpen', False)
            regular = eqo.get('sessionHours', {}).get('regularMarket', [{}])[0] if is_open_today else {}
            hours = {'is_open_today': is_open_today,
                     'start': regular.get('start'), 'end': regular.get('end')}
            # Cache only the day's hours — these are genuinely stable for
            # the rest of the day. The session itself is always computed fresh.
            _mkt_cache.update({'hours': hours, 'date': today})
            return _compute_session(hours)
        else:
            # Schwab call failed (rate limit, network, etc). FAIL SAFE: assume
            # closed/unknown rather than open — we genuinely don't know.
            # NOT cached, so the next poll (e.g. 60s later) tries again fresh.
            return {'is_open': False, 'session': 'unknown',
                    'message': 'Could not verify market hours — assuming closed until confirmed.'}
    except Exception:
        # Same fail-safe direction on a hard exception. Not cached, so a
        # transient error self-corrects on the next poll.
        return {'is_open': False, 'session': 'unknown',
                'message': 'Market hours unavailable — assuming closed until confirmed.'}

def _compute_session(hours):
    """Compute the CURRENT session against the live clock from cached day-hours."""
    if not hours.get('is_open_today'):
        return {'is_open': False, 'session': 'closed',
                'message': 'Options market closed today.'}
    try:
        start = datetime.fromisoformat(hours['start'])
        end   = datetime.fromisoformat(hours['end'])
        # Schwab's timestamps are timezone-aware (carry a UTC offset, e.g.
        # "...-04:00"). datetime.now() is naive (no timezone) and Python
        # raises TypeError comparing naive vs aware — this was the actual
        # cause of the "time parse issue" message (fromisoformat itself
        # succeeds; it's the later < / > comparison that fails). Fix: make
        # `now` aware in the SAME timezone as the parsed Schwab timestamps,
        # rather than comparing across mismatched timezone-awareness.
        if start.tzinfo is not None:
            now = datetime.now(start.tzinfo)
        else:
            now = datetime.now()
        if now < start:
            mins = int((start-now).seconds/60)
            return {'is_open': False, 'session': 'pre',
                    'message': f'Pre-market. Opens in ~{mins} min.'}
        elif now > end:
            return {'is_open': False, 'session': 'after',
                    'message': 'After-hours. Using end-of-day data.'}
        else:
            return {'is_open': True, 'session': 'regular',
                    'message': f'Live. Closes {end.strftime("%I:%M %p")}.'}
    except Exception as e:
        print(f'Market session compute error: {type(e).__name__}: {e}')
        return {'is_open': False, 'session': 'unknown',
                'message': 'Market status from Schwab (time parse issue).'}

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
        if regime in ('bullish', 'caution'): s += min(rs_raw*400, 15)
        elif regime == 'bearish':            s += min(-rs_raw*400, 15)

    mom = stock['mom10']
    vr  = stock['vol_ratio']

    if regime in ('bullish', 'caution'):
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

    qqq_df=get_price_history(client,'QQQ',days=30)
    if qqq_df is not None and len(qqq_df)>=5 and spy_df is not None and len(spy_df)>=20:
        # Compare QQQ vs SPY over the SAME trailing window (both ~5-session
        # returns from price history) — a same-day quote % change compared
        # against a multi-day SPY return would mix timeframes and produce
        # a meaningless "leading/lagging" figure.
        qqq_c=qqq_df['close'].values
        qqq_pc=(qqq_c[-1]/qqq_c[-5]-1)*100
        spy_pc=(spy_df['close'].values[-1]/spy_df['close'].values[-5]-1)*100
        diff = qqq_pc - spy_pc
        if diff > 0.5:
            signals['sector_rotation']={'value':f'Tech leading +{diff:.1f}%','signal':'bull'}; votes.append('bullish')
        elif diff < -0.5:
            signals['sector_rotation']={'value':f'Tech lagging {diff:.1f}%','signal':'bear'}; votes.append('bearish')
        else:
            signals['sector_rotation']={'value':'Sector rotation balanced','signal':'neut'}; votes.append('neutral')
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

        if regime in ('bullish', 'caution'):
            # Caution builds bull call spreads, so it needs the same uptrend
            # filter as bullish — stock must be above both moving averages.
            if not(ma50 and ma200) or price<ma50 or price<ma200: return None
        elif regime=='bearish':
            if not ma50 or price>ma50: return None
        elif regime=='neutral':
            if not ma50 or abs(price-ma50)/ma50>0.08: return None

        # IBD ACC/DIS override — never buy D/E stocks in bullish or caution
        ibd = ibd50_get(symbol)
        if regime in ('bullish','caution') and ibd and is_bad_acc_dis(ibd.get('acc_dis','')):
            return None  # Institutional selling — skip

        rs_raw=calc_rs_score(df,spy_df)
        # Use live price for momentum if available, else last candle close
        mom10=(price/c[-10]-1)*100 if len(c)>=10 else 0
        vol_ratio=1.0
        if 'volume' in df.columns and len(df)>=20:
            avg=df['volume'].values[-20:].mean()
            vol_ratio=df['volume'].values[-1]/avg if avg>0 else 1

        # IBD % off High override — skip stocks >35% off high in bullish/caution
        if regime in ('bullish','caution') and ibd:
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
    - Return filter: 25-50% return on debit (matches the enforced check below)
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
            # Entry window: 30-45 DTE. Floor is well above the 21 DTE exit rule
            # so every trade has at least ~9 days of runway before the 21 DTE
            # close kicks in. Entering below 30 DTE would risk recommending a
            # trade the Daily Review flags to close almost immediately.
            if not (30 <= dte <= 45):
                continue

            strikes = sorted([float(k) for k in strikes_dict.keys()])
            if len(strikes) < 3:
                continue

            # Long leg: slightly IN THE MONEY for higher win rate.
            # For calls (bullish): one strike BELOW current price (~60-65 delta)
            # For puts (bearish):  one strike ABOVE current price (~60-65 delta)
            # Being ITM at entry means the stock doesn't have to move much to win,
            # which raises the probability of profit and suits once-a-day monitoring.
            if regime in ('bullish', 'caution'):
                itm_candidates = [s for s in strikes if s < price]
                itm_target = price * 0.975   # ~2.5% ITM, roughly 60-65 delta
                atm = min(itm_candidates, key=lambda x: abs(x - itm_target)) if itm_candidates \
                      else min(strikes, key=lambda x: abs(x - price))
            else:
                itm_candidates = [s for s in strikes if s > price]
                itm_target = price * 1.025
                atm = min(itm_candidates, key=lambda x: abs(x - itm_target)) if itm_candidates \
                      else min(strikes, key=lambda x: abs(x - price))

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

                # Return band tuned for slightly-ITM, higher-win-rate spreads.
                # Target 25-50% return on debit (25% floor = your minimum).
                if rp < 25 or rp > 50: continue  # outside target band
                if nd < 0.50: continue            # debit too cheap

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
                # Selection logic (return band is 25-50% per strategy):
                # 1. Among valid results, prefer the highest return on debit
                # 2. Tiebreak: shorter DTE (less time risk, closes sooner)
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
            if not(30<=dte<=45): continue  # 30-45 DTE: runway above 21 DTE exit
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
            # Iron condor has 4 legs. Clearly label each as BUY or SELL.
            #   sc = short call  (SELL) — collect premium
            #   lc = long call   (BUY)  — protection above
            #   sp = short put   (SELL) — collect premium
            #   lp = long put    (BUY)  — protection below
            condor_legs = [
                {'action':'SELL','type':'call','strike':sc,'role':'Short call (collect premium)'},
                {'action':'BUY', 'type':'call','strike':lc,'role':'Long call (upside protection)'},
                {'action':'SELL','type':'put', 'strike':sp,'role':'Short put (collect premium)'},
                {'action':'BUY', 'type':'put', 'strike':lp,'role':'Long put (downside protection)'},
            ]
            return {'expiration':exp_date.strftime('%b %d %Y'),'dte':dte,
                    'buy_leg':f'SELL ${sc:.0f}C / BUY ${lc:.0f}C  +  SELL ${sp:.0f}P / BUY ${lp:.0f}P',
                    'sell_leg':f'Iron condor — collect ${nc:.2f} credit',
                    'is_condor':True,
                    'condor_legs':condor_legs,
                    'short_call':sc,'long_call':lc,'short_put':sp,'long_put':lp,
                    'net_debit':-nc,'max_profit':nc,'breakeven':round((sp+sc)/2,2),
                    'entry':nc,'profit_target':pt,'stop_loss':sl,'return_on_debit':rp,
                    'contracts_per_10k':contracts,'capital_at_risk':round(contracts*ml*100),
                    'max_loss_per_contract':round(ml*100,2),
                    'buy_oi':sc_oi,'sell_oi':sp_oi,'iv':round(avg_iv*100,1),'delta':0,'theta':0}
    except Exception as e:
        print(f'Condor error {symbol}: {e}')
    return None

def calc_position_size(debit, account_size, risk_pct, max_debit_budget=10000,
                       risk_per_contract_override=None):
    """
    Recommend contract count based on risk per trade.
    Risk per trade    = account_size * risk_pct%
    Risk per contract = stop loss * 100. For a vertical debit spread the stop is
                        50% of debit, so risk/contract = debit * 0.50 * 100.
                        For other structures (e.g. iron condors) pass
                        risk_per_contract_override with the true max risk.
    Contracts = risk budget / risk per contract, minimum 1.
    Capped so total capital deployed stays under max_debit_budget.
    """
    if debit <= 0 and not risk_per_contract_override:
        return {'contracts':1,'dollar_risk':0,'pct_of_account':0,
                'total_debit':0,'warning':None}

    risk_budget       = account_size * (risk_pct / 100.0)
    # Use explicit risk if given (condors), else 50%-of-debit stop (verticals)
    risk_per_contract = risk_per_contract_override if risk_per_contract_override \
                        else debit * 0.50 * 100
    raw_contracts     = risk_budget / risk_per_contract if risk_per_contract > 0 else 1

    contracts = max(1, int(raw_contracts))   # floor, minimum 1

    # Capital deployed per contract — what actually caps the budget and is
    # shown as "Total debit" on the card. For verticals this is the debit
    # paid. For condors (risk_per_contract_override given), the true capital
    # at risk is the override (max loss), NOT the credit collected — using
    # the credit here would understate risk and disagree with dollar_risk,
    # which already correctly uses the override.
    if risk_per_contract_override:
        per_contract_capital = risk_per_contract_override
    else:
        per_contract_capital = debit * 100

    max_by_budget = int(max_debit_budget / per_contract_capital) if per_contract_capital > 0 else contracts
    if max_by_budget >= 1:
        contracts = min(contracts, max_by_budget)
    contracts = max(1, contracts)

    dollar_risk    = round(risk_per_contract * contracts, 2)
    pct_of_account = round(dollar_risk / account_size * 100, 2) if account_size > 0 else 0
    total_debit    = round(per_contract_capital * contracts, 2)

    warning = None
    if pct_of_account > 3.0:
        warning = (f'1 contract risks {pct_of_account:.1f}% of account (above 3% target)')

    return {'contracts':contracts,'dollar_risk':dollar_risk,
            'pct_of_account':pct_of_account,'total_debit':total_debit,
            'warning':warning}

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
    if spread.get('iv'):
        _iv = spread['iv']
        if   _iv < 30: _ivdesc = 'low — buying relatively cheap'
        elif _iv < 50: _ivdesc = 'moderate'
        elif _iv < 70: _ivdesc = 'elevated — paying up, some IV-contraction risk'
        else:          _ivdesc = 'high — rich premium, watch for IV contraction'
        parts.append(f"IV {_iv}% ({_ivdesc}).")
    parts.append(f"${spread['entry']:.2f} debit targets ${spread['profit_target']:.2f} ({spread['return_on_debit']}% return).")
    if not ibd: parts.append("Verify IBD RS at ibd.com before entry.")

    return ' '.join(p for p in parts if p)

# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route('/')
def index(): return send_from_directory('static','index.html')

@app.route('/manifest.json')
def manifest():
    """PWA manifest — makes the app installable to a phone home screen."""
    return jsonify({
        "name": "Options Trade Scanner",
        "short_name": "TradeScan",
        "description": "Schwab + IBD options spread scanner",
        "start_url": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0e0f11",
        "theme_color": "#0e0f11",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}
        ]
    })

@app.route('/icon-<size>.png')
def app_icon(size):
    """Generate a simple app icon on the fly (green 'TS' on dark)."""
    try:
        sz = 512 if '512' in size else 192
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new('RGB', (sz, sz), '#0e0f11')
        d = ImageDraw.Draw(img)
        margin = sz // 8
        d.rounded_rectangle([margin, margin, sz-margin, sz-margin],
                            radius=sz//10, fill='#15171a', outline='#2dd4a0', width=max(2, sz//64))
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", sz//3)
        except:
            font = ImageFont.load_default()
        text = "TS"
        bbox = d.textbbox((0,0), text, font=font)
        tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
        d.text(((sz-tw)/2 - bbox[0], (sz-th)/2 - bbox[1]), text, fill='#2dd4a0', font=font)
        import io
        buf = io.BytesIO(); img.save(buf, 'PNG'); buf.seek(0)
        from flask import Response
        return Response(buf.getvalue(), mimetype='image/png')
    except Exception:
        import base64
        from flask import Response
        px = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==')
        return Response(px, mimetype='image/png')

# ── Trades storage (SQLite, shared across all clients) ───────────────────────
@app.route('/api/trades', methods=['GET'])
def get_trades():
    """Return all trades, newest first (matches the old localStorage order)."""
    with _db_lock, db_conn() as conn:
        rows = conn.execute('SELECT * FROM trades ORDER BY id DESC').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/trades', methods=['POST'])
def add_trade():
    """Insert a new trade. Accepts the trade JSON the frontend builds."""
    t = request.get_json(silent=True) or {}
    data = {k: t.get(k) for k in _TRADE_COLS}
    if not data.get('id'):
        data['id'] = int(time.time() * 1000)   # match Date.now() style ids
    if not data.get('status'):
        data['status'] = 'open'
    cols = [k for k in _TRADE_COLS if data.get(k) is not None]
    placeholders = ','.join('?' for _ in cols)
    with _db_lock, db_conn() as conn:
        conn.execute(f'INSERT OR REPLACE INTO trades ({",".join(cols)}) VALUES ({placeholders})',
                     [data[c] for c in cols])
        conn.commit()
    return jsonify({'status': 'ok', 'id': data['id']})

@app.route('/api/trades/<int:trade_id>', methods=['PUT'])
def update_trade(trade_id):
    """Update an existing trade (status change, exit price, edits)."""
    t = request.get_json(silent=True) or {}
    fields = {k: t.get(k) for k in _TRADE_COLS if k != 'id' and k in t}
    if not fields:
        return jsonify({'status': 'no_change'})
    sets = ','.join(f'{k}=?' for k in fields)
    with _db_lock, db_conn() as conn:
        conn.execute(f'UPDATE trades SET {sets} WHERE id=?',
                     list(fields.values()) + [trade_id])
        conn.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/trades/<int:trade_id>', methods=['DELETE'])
def delete_trade(trade_id):
    with _db_lock, db_conn() as conn:
        conn.execute('DELETE FROM trades WHERE id=?', [trade_id])
        conn.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/trades/clear', methods=['POST'])
def clear_trades():
    with _db_lock, db_conn() as conn:
        conn.execute('DELETE FROM trades')
        conn.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/trades/import', methods=['POST'])
def import_trades():
    """One-time bulk import (e.g. from the Mac's old localStorage)."""
    payload = request.get_json(silent=True) or []
    if not isinstance(payload, list):
        return jsonify({'error': 'expected a list of trades'}), 400
    imported = 0
    with _db_lock, db_conn() as conn:
        for t in payload:
            data = {k: t.get(k) for k in _TRADE_COLS}
            if not data.get('id'):
                data['id'] = int(time.time() * 1000) + imported
            if not data.get('status'):
                data['status'] = 'open'
            cols = [k for k in _TRADE_COLS if data.get(k) is not None]
            placeholders = ','.join('?' for _ in cols)
            conn.execute(f'INSERT OR REPLACE INTO trades ({",".join(cols)}) VALUES ({placeholders})',
                         [data[c] for c in cols])
            imported += 1
        conn.commit()
    return jsonify({'status': 'ok', 'imported': imported})

# ── Settings storage (account size, risk %) ──────────────────────────────────
@app.route('/api/settings', methods=['GET'])
def get_settings():
    with _db_lock, db_conn() as conn:
        rows = conn.execute('SELECT key, value FROM settings').fetchall()
    out = {r['key']: r['value'] for r in rows}
    # Defaults if not yet set
    out.setdefault('account', '10000')
    out.setdefault('risk_pct', '1.5')
    return jsonify(out)

@app.route('/api/settings', methods=['POST'])
def save_settings():
    s = request.get_json(silent=True) or {}
    with _db_lock, db_conn() as conn:
        for k, v in s.items():
            conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                         [str(k), str(v)])
        conn.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/auth/status')
def api_auth_status():
    """Poll this to know if auth is needed / in progress / complete."""
    connected = _schwab_client is not None
    return jsonify({
        'connected':       connected,
        'auth_pending':    _auth_pending,
        'auth_error':      _auth_error,
        'needs_auth':      not connected and not _auth_pending,
        'credentials_ok':  bool(SCHWAB_KEY and SCHWAB_SECRET
                                and 'YOUR_CLIENT_ID' not in SCHWAB_KEY),
        'manual_auth_url': _manual_auth_url,
        'manual_auth_needed': _auth_pending and _manual_auth_url is not None,
    })

@app.route('/api/auth/start', methods=['POST'])
def api_auth_start():
    """
    Build the Schwab authorization URL. No callback listener is started —
    the flow completes when the user pastes the redirect URL back into
    /api/auth/complete, so this works from any device's browser.
    """
    if _schwab_client:
        return jsonify({'status': 'already_connected'})
    _start_manual_auth()
    return jsonify({'status': 'started', 'auth_url': _manual_auth_url,
                    'message': 'Open the link below, log in, then paste the '
                               'resulting redirect URL back here.'})

@app.route('/api/auth/complete', methods=['POST'])
def api_auth_complete():
    """Exchange the pasted-back Schwab redirect URL for a token."""
    data = request.get_json(silent=True) or {}
    redirect_url = (data.get('redirect_url') or '').strip()
    if not redirect_url:
        return jsonify({'status': 'error', 'error': 'Paste the redirect URL first.'}), 400
    ok, err = _complete_manual_auth(redirect_url)
    if ok:
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error', 'error': err}), 400

@app.route('/api/auth/disconnect', methods=['POST'])
def api_auth_disconnect():
    """Delete token and reset client — forces re-auth next time."""
    global _schwab_client, _auth_pending, _auth_error, _manual_auth_url, _pending_auth_context
    _schwab_client = None
    _auth_pending  = False
    _auth_error    = None
    _manual_auth_url = None
    _pending_auth_context = None
    try: pathlib.Path(TOKEN_PATH).unlink(missing_ok=True)
    except: pass
    return jsonify({'status': 'disconnected'})

@app.route('/api/status')
def api_status():
    global _schwab_client
    has_creds = bool(SCHWAB_KEY and SCHWAB_SECRET)
    has_token = pathlib.Path(TOKEN_PATH).exists()
    config_ok = _config_path.exists() and 'YOUR_CLIENT_ID' not in _config.get('schwab_client_id','YOUR_CLIENT_ID')
    client = _schwab_client

    # Proactively verify the token still works, independent of the daily
    # market-hours cache. Without this, a token that dies mid-session would
    # keep showing "connected" until the cache naturally expired at midnight,
    # because market_status() only hits Schwab once per day when cached.
    if client:
        check = schwab_call(client.get_quotes, ['SPY'])
        if check is None and _schwab_client is None:
            # schwab_call's 401 handler already cleared _schwab_client — the
            # token is confirmed dead. Fall through with client=None below.
            client = None

    # Source of truth for "connected" is the live client object, not just the file
    connected = client is not None
    mkt = market_status(client) if client else {'is_open':False,'session':'unknown','message':'Connect Schwab to check market hours.'}
    ibd_summary = {}
    if _ibd50_data:
        rs_vals = [v.get('rs_rating') for v in _ibd50_data.values() if v.get('rs_rating')]
        ibd_summary = {'count':len(_ibd50_data),
                       'avg_rs':round(sum(rs_vals)/len(rs_vals),1) if rs_vals else None,
                       'updated':json.load(open(IBD50_PATH)).get('updated','') if IBD50_PATH.exists() else ''}
    return jsonify({'has_credentials':has_creds,'has_token':has_token,'config_file':config_ok,
                    'ready':connected,'market':mkt,'auth_error':_auth_error,
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
    global _scan_running
    global _last_scan_tickers

    # ── Concurrency guard FIRST: reject a second scan if one is running ──
    # (Checked before the client guard so a duplicate is always rejected
    #  consistently, regardless of connection state.)
    with _scan_lock:
        if _scan_running:
            return jsonify({'error': 'A scan is already running. Wait for it to '
                                     'finish or cancel it before starting another.',
                            'already_running': True}), 409
        _scan_running = True
        _scan_cancel.clear()

    # Now validate the client; release the lock if we can't proceed.
    client=get_client()
    if not client:
        _scan_running = False
        return jsonify({'error':'Schwab not configured. Check config.json.'}),400

    sector_filter=request.args.get('sector','all').lower()
    # Position sizing inputs (from the Position Sizing panel)
    try:    account_size = float(request.args.get('account', 10000))
    except: account_size = 10000.0
    try:    risk_pct = float(request.args.get('risk_pct', 1.5))
    except: risk_pct = 1.5
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
        # Within each tier, sort by IBD score descending so the same
        # highest-scoring stocks are always processed first regardless of
        # set/dict ordering non-determinism. This makes consecutive scans
        # return the same top 8 as long as prices haven't moved significantly.
        ibd_set   = set(ibd50_symbols())
        mover_set = set(get_movers(client))
        def priority(sym):
            if sym in ibd_set:   return 0
            if sym in mover_set: return 1
            return 2
        def sort_key(sym):
            # Primary: tier (0=IBD50, 1=movers, 2=SP500)
            # Secondary: IBD score descending (negate so higher score = earlier)
            ibd = ibd50_get(sym)
            score = ibd.get('rs_rating', 0) if ibd else 0
            return (priority(sym), -score)
        pre_filtered.sort(key=sort_key)

        # Screen at most 60 stocks — IBD50+movers first guarantees best picks
        # At 80 req/min: 60 price_history + 60 earnings = ~90s max
        # With IBD50 ordered first, top candidates appear in first 40-50 calls
        candidates=[]; screened=0; max_screen=60
        for symbol in pre_filtered:
            if screened >= max_screen: break
            check_cancel()   # stop promptly if user cancelled
            if screened % 10 == 0:
                print(f'  Screening {screened}/{max_screen}: {symbol}…')
                emit(f'Screening stocks… {screened}/{max_screen}')
            result=screen_stock(client, symbol, spy_df, regime,
                                     live_price=live_prices.get(symbol))
            screened+=1
            if result:
                has_earn,earn_date=has_earnings_soon(client,symbol,days_ahead=47)
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

        # Caution regime: only trade the very strongest setups (score >= 88).
        # This enforces the protection rule shown in the regime banner.
        score_floor = 88 if regime == 'caution' else 0

        for stock in candidates[:20]:
            check_cancel()   # stop promptly if user cancelled
            sym=stock['symbol']
            if stock['score'] < score_floor:
                print(f'  Skipping {sym} — score {stock["score"]} below caution floor {score_floor}')
                continue

            spread=get_iron_condor(client,sym,stock['price']) if regime=='neutral' \
                   else get_best_spread(client,sym,stock['price'],regime)
            if spread is None:
                print(f'  No valid spread found for {sym}')
                continue

            ibd=ibd50_get(sym)
            tags=build_tags(stock,spread,regime,stock.get('earnings_date'),stock['is_ibd50'])
            # Risk-based position size recommendation.
            # Verticals: risk = 50% of debit (the stop). Condors: real max loss per contract.
            if spread.get('is_condor'):
                # Condor max loss per contract = (wing width - credit) * 100, computed
                # directly from the spread's own fields (no dependency on legacy sizing).
                condor_risk_per_contract = spread.get('max_loss_per_contract')
                if not condor_risk_per_contract:
                    # Fallback: derive from capital_at_risk if present
                    cpc = spread.get('contracts_per_10k', 1) or 1
                    condor_risk_per_contract = (spread.get('capital_at_risk', 0) / cpc) if cpc else 0
                psize = calc_position_size(spread['entry'], account_size, risk_pct,
                                           risk_per_contract_override=condor_risk_per_contract)
            else:
                psize = calc_position_size(spread['entry'], account_size, risk_pct)

            trades.append({
                'ticker':sym,
                'company':ibd.get('company',sym) if ibd else sym,
                'stock_price':round(stock['price'],2),
                'setup_score':stock['score'],
                'top_pick':False,
                'seen_before': sym in _last_scan_tickers,
                'strategy_type':strat_map.get(regime,'bull_call_spread'),
                'tags':tags,
                'expiration':spread['expiration'],'dte':spread['dte'],
                'buy_leg':spread['buy_leg'],'sell_leg':spread['sell_leg'],
                'is_condor':spread.get('is_condor',False),
                'condor_legs':spread.get('condor_legs',None),
                'net_debit':spread['entry'],'max_profit':spread['max_profit'],
                'breakeven':spread['breakeven'],'entry':spread['entry'],
                'profit_target':spread['profit_target'],'stop_loss':spread['stop_loss'],
                'return_on_debit':spread['return_on_debit'],
                'contracts_per_10k':spread['contracts_per_10k'],
                'capital_at_risk':spread['capital_at_risk'],
                'rec_contracts':psize['contracts'],
                'rec_dollar_risk':psize['dollar_risk'],
                'rec_pct_of_account':psize['pct_of_account'],
                'rec_total_debit':psize['total_debit'],
                'rec_warning':psize['warning'],
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
            if len(trades)>=8: break

        if not trades:
            return jsonify({'error':'Found candidates but no valid spreads. Markets may be closed.'})

        # Update the "seen before" memory for the next scan
        _last_scan_tickers = {t['ticker'] for t in trades}

        trades[0]['top_pick']=True
        emit(f'Scan complete — {len(trades)} trade setups ready')
        return jsonify({
            'scan_date':datetime.now().strftime('%B %d, %Y %H:%M'),
            'regime':regime_data,'trades':trades,
            'market_context':regime_data['market_context'],
            'market_status':mkt,'universe_meta':universe_meta,
            'screened':screened,'candidates_found':len(candidates),
        })
    except ScanCancelled:
        emit('Scan cancelled')
        return jsonify({'error': 'Scan cancelled', 'cancelled': True}), 499
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error':str(e)}),500
    finally:
        # Always release the scan lock so the next scan can run
        _scan_running = False
        _scan_cancel.clear()

@app.route('/api/scan/cancel', methods=['POST'])
def api_scan_cancel():
    """Request cancellation of the running scan (if any)."""
    if _scan_running:
        _scan_cancel.set()
        emit('Cancelling scan…')
        return jsonify({'status': 'cancelling'})
    return jsonify({'status': 'no_scan_running'})

@app.route('/api/scan/status')
def api_scan_status():
    """Report whether a scan is currently running (for reconnecting clients)."""
    return jsonify({'running': _scan_running})

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

            # Detect trade type from structure string
            import re as _re
            has_call = 'call' in structure.lower() or 'c /' in structure.lower() or 'c ' in structure.lower()
            has_put  = 'put'  in structure.lower() or 'p /' in structure.lower() or 'p ' in structure.lower()
            all_nums = [float(n) for n in _re.findall(r'\$(\d+(?:\.\d+)?)', structure)]
            # Iron condor = 4 strikes AND both calls and puts present
            is_condor = (len(all_nums) >= 4) and has_call and has_put

            # Helper to read mark for a strike from a strikes_dict
            def _mark_from(sd, strike):
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

            current_value    = None
            underlying_price = None   # will be populated from chain response

            if is_condor and exp_date:
                # Condor: structure is "SELL $Xc / BUY $Yc + SELL $Zp / BUY $Wp"
                # Strike order in string: short_call, long_call, short_put, long_put
                try:
                    sc, lc, sp_, lp_ = all_nums[0], all_nums[1], all_nums[2], all_nums[3]
                    # Need both call and put chains
                    resp = schwab_call(client.get_option_chain, symbol=ticker,
                                       contract_type='ALL', strike_count=40,
                                       include_underlying_quote=True, strategy='SINGLE')
                    if resp:
                        chain = resp.json()
                        underlying_price = chain.get('underlyingPrice')
                        calls = chain.get('callExpDateMap', {})
                        puts  = chain.get('putExpDateMap', {})
                        def find_exp(m):
                            for es, sd in m.items():
                                try:
                                    ed = datetime.strptime(es.split(':')[0], '%Y-%m-%d').date()
                                    if ed == exp_date: return sd
                                except: continue
                            return None
                        cdict = find_exp(calls); pdict = find_exp(puts)
                        if cdict and pdict:
                            sc_m = _mark_from(cdict, sc); lc_m = _mark_from(cdict, lc)
                            sp_m = _mark_from(pdict, sp_); lp_m = _mark_from(pdict, lp_)
                            if None not in (sc_m, lc_m, sp_m, lp_m):
                                # Current cost to CLOSE the condor (buy back shorts, sell longs)
                                # = (short_call - long_call) + (short_put - long_put)
                                current_value = round((sc_m - lc_m) + (sp_m - lp_m), 2)
                except Exception as e:
                    print(f'Condor review error {ticker}: {e}')
            elif exp_date and len(all_nums) >= 2:
                # Vertical spread: 2 strikes
                buy_strike  = all_nums[0]
                sell_strike = all_nums[1]
                ct = 'PUT' if (has_put and not has_call) else 'CALL'
                try:
                    resp = schwab_call(client.get_option_chain, symbol=ticker,
                                       contract_type=ct, strike_count=30,
                                       include_underlying_quote=True, strategy='SINGLE')
                    if resp:
                        chain = resp.json()
                        underlying_price = chain.get('underlyingPrice')
                        exp_map = chain.get('callExpDateMap' if ct == 'CALL' else 'putExpDateMap', {})
                        for exp_str, strikes_dict in exp_map.items():
                            try:
                                ed = datetime.strptime(exp_str.split(':')[0], '%Y-%m-%d').date()
                                if ed != exp_date: continue
                            except: continue
                            buy_mark  = _mark_from(strikes_dict, buy_strike)
                            sell_mark = _mark_from(strikes_dict, sell_strike)
                            if buy_mark is not None and sell_mark is not None:
                                current_value = round(buy_mark - sell_mark, 2)
                            break
                except Exception as e:
                    print(f'Review error {ticker}: {e}')

            # Determine action — condors and verticals behave OPPOSITELY
            pnl = None
            pnl_pct = None
            action = 'HOLD'
            action_detail = ''
            urgency = 'normal'  # normal / warn / critical

            if current_value is not None and is_condor:
                # CONDOR: you collected `debit` as a credit at entry.
                # current_value = cost to buy it back now.
                # Profit when current_value DROPS below the credit received.
                # target = 50% of credit (buy back cheap), stop = 2x credit.
                credit = debit  # for condors the "debit" field stores the credit received
                pnl = round((credit - current_value) * 100 * contracts, 2)
                pnl_pct = round((credit - current_value) / credit * 100, 1) if credit > 0 else 0

                if current_value <= target:
                    action = 'CLOSE — TARGET HIT'
                    action_detail = f'Condor at ${current_value:.2f} (collected ${credit:.2f}) — BUY the iron condor to close, lock in profit'
                    urgency = 'critical'
                elif current_value >= stop:
                    action = 'CLOSE — STOP HIT'
                    action_detail = f'Condor at ${current_value:.2f} vs ${credit:.2f} credit — cancel target order, BUY to close at market'
                    urgency = 'critical'
                elif dte is not None and dte <= 21:
                    action = 'CLOSE — 21 DTE RULE'
                    action_detail = f'{dte} days left — gamma risk rises near expiry, BUY the condor to close regardless of P&L'
                    urgency = 'critical'
                elif dte is not None and dte <= 25:
                    action = 'PREPARE TO CLOSE'
                    action_detail = f'{dte} days left — approaching 21 DTE exit rule, monitor closely'
                    urgency = 'warn'
                elif current_value >= stop * 0.75:
                    action = 'WATCH — APPROACHING STOP'
                    action_detail = f'Condor at ${current_value:.2f}, stop is ${stop:.2f} — price moving against you'
                    urgency = 'warn'
                elif current_value <= target * 1.25:
                    action = 'NEAR TARGET'
                    action_detail = f'Condor at ${current_value:.2f}, target is ${target:.2f} — close approaching'
                    urgency = 'warn'
                else:
                    action = 'HOLD'
                    action_detail = f'Condor at ${current_value:.2f} — between target (${target:.2f}) and stop (${stop:.2f})'
                    urgency = 'normal'

            elif current_value is not None and debit > 0:
                # VERTICAL spread (bull call / bear put): you paid `debit`.
                # Profit when current_value RISES above debit.
                pnl = round((current_value - debit) * 100 * contracts, 2)
                pnl_pct = round((current_value - debit) / debit * 100, 1)

                if current_value >= target:
                    action = 'CLOSE — TARGET HIT'
                    action_detail = f'Spread at ${current_value:.2f} — SELL the vertical to close, take your profit'
                    urgency = 'critical'
                elif current_value <= stop:
                    action = 'CLOSE — STOP HIT'
                    action_detail = f'Spread at ${current_value:.2f} — cancel profit target order, SELL to close at market'
                    urgency = 'critical'
                elif dte is not None and dte <= 21:
                    action = 'CLOSE — 21 DTE RULE'
                    action_detail = f'{dte} days left — time decay accelerates from here, SELL the vertical to close regardless of P&L'
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
                'underlying_price': round(underlying_price, 2) if underlying_price else None,
                'pnl':           pnl,
                'pnl_pct':       pnl_pct,
                'action':        action,
                'action_detail': action_detail,
                'urgency':       urgency,
                'is_condor':     is_condor,
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
    time.sleep(1.5); webbrowser.open(f'http://127.0.0.1:{SERVER_PORT}')

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

    # ── Initialize SQLite storage (creates scanner.db if missing) ───────────
    init_db()
    print(f'  ✓  Database ready: {DB_PATH}')

    # ── Schwab auth — non-blocking, UI handles OAuth if needed ──────────────
    init_schwab()  # loads token if available, sets _auth_pending if not

    # ── Start Flask ───────────────────────────────────────────────────────
    print(f'\n  Starting scanner at http://127.0.0.1:{SERVER_PORT} …\n')
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(debug=False, host='0.0.0.0', port=SERVER_PORT, threaded=True)
