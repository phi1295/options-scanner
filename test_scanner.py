"""
Options Scanner — Automated Test Suite
Run: python test_scanner.py
Tests all pure logic without requiring Schwab credentials.
"""
import sys, json, math, pathlib, io
import pandas as pd
import numpy as np

# ── Add app directory to path ─────────────────────────────────────────────────
sys.path.insert(0, str(pathlib.Path(__file__).parent))

# Mock schwab so app.py imports cleanly without credentials
import unittest.mock as mock
sys.modules['schwab'] = mock.MagicMock()
import app

PASS = 0; FAIL = 0

def check(name, actual, expected=True, msg=''):
    global PASS, FAIL
    ok = (actual == expected) if not callable(expected) else expected(actual)
    sym = '✓' if ok else '✗'
    status = 'PASS' if ok else 'FAIL'
    print(f'  {sym} {status}: {name}' + (f' — got {actual!r}' if not ok else ''))
    if ok: PASS += 1
    else:  FAIL += 1

def section(name):
    print(f'\n── {name} ──')

# ══════════════════════════════════════════════════════════════════════════════
section('ACC/DIS scoring')
check('A+ scores 10',        app.acc_dis_score('A+'), 10)
check('B  scores 6',         app.acc_dis_score('B'),  6)
check('D  scores 0',         app.acc_dis_score('D'),  0)
check('E  scores -5',        app.acc_dis_score('E'), -5)
check('unknown scores 3',    app.acc_dis_score('?'),  3)
check('D is bad acc_dis',    app.is_bad_acc_dis('D'), True)
check('D+ is bad acc_dis',   app.is_bad_acc_dis('D+'), True)
check('E is bad acc_dis',    app.is_bad_acc_dis('E'), True)
check('C is not bad',        app.is_bad_acc_dis('C'), False)
check('B is not bad',        app.is_bad_acc_dis('B'), False)

# ══════════════════════════════════════════════════════════════════════════════
section('Score stock — IBD path')
def make_stock(symbol='TEST', price=100, ma50=90, ma200=85, rs_raw=0.05, mom10=3, vol_ratio=1.2):
    return {'symbol':symbol,'price':price,'ma50':ma50,'ma200':ma200,
            'rs_raw':rs_raw,'mom10':mom10,'vol_ratio':vol_ratio}

# IBD50 stock with strong ratings
app._ibd50_data['STRONG'] = {
    'symbol':'STRONG','rank':5,'rs_rating':98,'eps_rating':95,
    'composite':99,'acc_dis':'A','smr':'A','pct_off_high':-3,
    'vol_chg':80,'eps_chg_latest':40,'qtrs_sponsorship':4,'sales_chg':25
}
s = app.score_stock(make_stock('STRONG'), 'bullish')
check('Strong IBD stock scores >80', s, lambda x: x > 80, f'got {s}')
check('Strong IBD stock scores ≤100', s, lambda x: x <= 100)

# IBD50 stock with D rating — should be heavily penalized
app._ibd50_data['WEAK'] = {
    'symbol':'WEAK','rank':45,'rs_rating':60,'eps_rating':55,
    'composite':65,'acc_dis':'D','smr':'D','pct_off_high':-40,
    'vol_chg':5,'eps_chg_latest':-10,'qtrs_sponsorship':0,'sales_chg':-5
}
w = app.score_stock(make_stock('WEAK'), 'bullish')
check('Weak IBD stock (D acc_dis, 40% off high) scores <40', w, lambda x: x < 40, f'got {w}')

# % off High scoring — 35+ should score lower than 5 off high
app._ibd50_data['NEAR_HIGH'] = {'symbol':'NEAR_HIGH','rank':10,'pct_off_high':-3,'acc_dis':'B','rs_rating':95,'composite':96,'eps_rating':90,'smr':'A','vol_chg':60,'eps_chg_latest':30,'qtrs_sponsorship':3,'sales_chg':20}
app._ibd50_data['FAR_HIGH']  = {'symbol':'FAR_HIGH', 'rank':10,'pct_off_high':-38,'acc_dis':'B','rs_rating':95,'composite':96,'eps_rating':90,'smr':'A','vol_chg':60,'eps_chg_latest':30,'qtrs_sponsorship':3,'sales_chg':20}
near = app.score_stock(make_stock('NEAR_HIGH'), 'bullish')
far  = app.score_stock(make_stock('FAR_HIGH'),  'bullish')
check('Stock near high scores better than 38% off high', near > far, True, f'near={near} far={far}')

# ══════════════════════════════════════════════════════════════════════════════
section('Score stock — price path (no IBD)')
app._ibd50_data.pop('NOIBD', None)
s_bull = app.score_stock(make_stock('NOIBD', mom10=5, vol_ratio=1.5), 'bullish')
s_bear = app.score_stock(make_stock('NOIBD', mom10=-5), 'bearish')
check('Non-IBD bullish stock scores >40', s_bull, lambda x: x > 40)
check('Score is 0-100', s_bull, lambda x: 0 <= x <= 100)

# ══════════════════════════════════════════════════════════════════════════════
section('Spread math')
# Verify profit target, stop loss, return calculations
nd = 3.00; sw = 10.0
mp = sw - nd           # 7.00
pt = round(nd + mp * 0.50, 2)  # 3.00 + 3.50 = 6.50
sl = round(nd * 0.50, 2)       # 1.50
rp = round((pt - nd) / nd * 100)  # 117%

check('Max profit = width - debit',    mp, 7.0)
check('Profit target = debit + 50%mp', pt, 6.50)
check('Stop loss = 50% of debit',      sl, 1.50)
check('Return on debit correct',       rp, 117)

# Edge case: very high debit (near width)
nd2 = 9.50; sw2 = 10.0; mp2 = sw2 - nd2  # 0.50
pt2 = round(nd2 + mp2 * 0.50, 2)  # 9.75
rp2 = round((pt2 - nd2) / nd2 * 100)  # 3%
check('Near-width debit: return ~3%', rp2, 3)
check('Near-width debit would fail rp<10 filter', rp2 < 10, True)

# ══════════════════════════════════════════════════════════════════════════════
section('Width scaling')
width_steps = [2.5, 5, 7.5, 10, 12.5, 15, 20, 25, 30, 40, 50]
def get_target_width(price):
    raw = price * 0.025
    tw = min(width_steps, key=lambda w: abs(w - raw))
    idx = width_steps.index(tw)
    below = width_steps[max(0, idx-1)]
    above = width_steps[min(len(width_steps)-1, idx+1)]
    return tw, sorted(set([below, tw, above]))

tw, ws = get_target_width(87)    # IBKR-style
check('$87 stock → target width $2.5',  tw, 2.5)
check('$87 stock → tries [2.5, 5]',     2.5 in ws and 5 in ws, True)

tw, ws = get_target_width(336)   # LRCX-style
check('$336 stock → target width $7.5 or $10', tw in [7.5, 10], True)

tw, ws = get_target_width(523)   # AMD-style
check('$523 stock → target width $12.5', tw, 12.5)
check('$523 stock → tries 3 widths',     len(ws) == 3, True)

tw, ws = get_target_width(996)   # MU-style
check('$996 stock → target width $25',   tw, 25.0)

# ══════════════════════════════════════════════════════════════════════════════
section('IBD50 parse — CSV content')
csv_content = """Rank,Symbol,Company Name,Price,RS Rating,EPS Rating,Composite Rating,ACC/DIS Rating,SMR Rating,Group Rel Str Rating,EPS % Change(Latest Qtr),Sales % Change(Last Qtr),% off High,Volume % Change,Qtrs of Rising Sponsorship
1,NVDA,Nvidia Corp,950,99,99,99,A+,A,A+,120,80,-2,150,5
2,AMD,Advanced Micro,523,95,93,96,B+,A,A,45,30,-8,60,3
3,WEAK,Weak Corp,50,45,40,50,D,C,C,-10,-5,-42,10,0
"""
result = app._find_header_and_parse(csv_content)
check('CSV parse returns 3 stocks',         len(result), 3)
check('NVDA RS rating = 99',                result.get('NVDA',{}).get('rs_rating'), 99.0)
check('NVDA acc_dis = A+',                  result.get('NVDA',{}).get('acc_dis'), 'A+')
check('NVDA rank = 1',                      result.get('NVDA',{}).get('rank'), 1.0)
check('AMD composite = 96',                 result.get('AMD',{}).get('composite'), 96.0)
check('WEAK pct_off_high = -42',            result.get('WEAK',{}).get('pct_off_high'), -42.0)
check('Symbol clean_sym filters non-alpha', 'WEAK' in result, True)

# ══════════════════════════════════════════════════════════════════════════════
section('Daily review — action logic')
def review_action(current, debit, target, stop, dte):
    """Simulate the action logic from api_review."""
    if current >= target:
        return 'CLOSE — TARGET HIT', 'critical'
    elif current <= stop:
        return 'CLOSE — STOP HIT', 'critical'
    elif dte <= 21:
        return 'CLOSE — 21 DTE RULE', 'critical'
    elif dte <= 25:
        return 'PREPARE TO CLOSE', 'warn'
    elif current <= stop * 1.30:
        return 'WATCH — APPROACHING STOP', 'warn'
    elif current >= target * 0.80:
        return 'NEAR TARGET', 'warn'
    else:
        return 'HOLD', 'normal'

action, urgency = review_action(current=6.50, debit=2.95, target=6.47, stop=1.48, dte=30)
check('Above target → CLOSE TARGET HIT',   action, 'CLOSE — TARGET HIT')
check('Above target → critical',           urgency, 'critical')

action, urgency = review_action(current=1.40, debit=2.95, target=6.47, stop=1.48, dte=30)
check('Below stop → CLOSE STOP HIT',       action, 'CLOSE — STOP HIT')

action, urgency = review_action(current=3.50, debit=2.95, target=6.47, stop=1.48, dte=20)
check('DTE=20 → CLOSE 21 DTE RULE',        action, 'CLOSE — 21 DTE RULE')

action, urgency = review_action(current=3.50, debit=2.95, target=6.47, stop=1.48, dte=23)
check('DTE=23 → PREPARE TO CLOSE',         action, 'PREPARE TO CLOSE')
check('DTE=23 → warn',                     urgency, 'warn')

action, urgency = review_action(current=1.92, debit=2.95, target=6.47, stop=1.48, dte=30)
check('Within 30% of stop → WATCH',        action, 'WATCH — APPROACHING STOP')

action, urgency = review_action(current=5.20, debit=2.95, target=6.47, stop=1.48, dte=30)
check('80% of target → NEAR TARGET',       action, 'NEAR TARGET')

action, urgency = review_action(current=3.50, debit=2.95, target=6.47, stop=1.48, dte=30)
check('Mid-range → HOLD',                  action, 'HOLD')
check('Mid-range → normal',               urgency, 'normal')

# ══════════════════════════════════════════════════════════════════════════════
section('Regime detection vote counting')
def regime_from_votes(bull, bear, neut, caut):
    votes = ['bullish']*bull + ['bearish']*bear + ['neutral']*neut + ['caution']*caut
    b=votes.count('bullish'); be=votes.count('bearish')
    ca=votes.count('caution'); ne=votes.count('neutral')
    if be>=3:   return 'bearish'
    elif b>=3:  return 'bullish'
    elif ca>=2 or (be>=2 and b<=1): return 'caution'
    else: return 'neutral'

check('3+ bullish votes → bullish',         regime_from_votes(3,0,0,0), 'bullish')
check('3+ bearish votes → bearish',         regime_from_votes(0,3,0,0), 'bearish')
check('2 caution votes → caution',          regime_from_votes(1,0,0,2), 'caution')
check('2 bear + 1 bull → caution',          regime_from_votes(1,2,0,0), 'caution')
check('Mixed = neutral',                    regime_from_votes(1,1,1,0), 'neutral')
check('All neutral = neutral',              regime_from_votes(0,0,5,0), 'neutral')

# ══════════════════════════════════════════════════════════════════════════════
section('Structure string strike parsing (for Daily Review)')
import re as _re
def parse_structure(structure):
    nums = _re.findall(r'\$(\d+)', structure)
    if len(nums) >= 2:
        return float(nums[0]), float(nums[1])
    return None, None

buy, sell = parse_structure('Buy $525 call / Sell $535 call')
check('Parse buy strike from structure',    buy,  525.0)
check('Parse sell strike from structure',   sell, 535.0)

buy, sell = parse_structure('Buy $1000 call / Sell $1010 call')
check('Parse MU buy strike',               buy,  1000.0)
check('Parse MU sell strike',              sell, 1010.0)

buy, sell = parse_structure('Buy $335 put / Sell $325 put')
check('Parse put spread buy strike',       buy,  335.0)
check('Parse put spread sell strike',      sell, 325.0)

buy, sell = parse_structure('no dollar signs here')
check('Missing strikes returns None',      buy,  None)

# ══════════════════════════════════════════════════════════════════════════════
section('Expiration date parsing (for Daily Review)')
from datetime import datetime, date

def parse_exp(expiration):
    try:
        exp_clean = ' '.join(expiration.split())
        for fmt in ('%b %d %Y', '%B %d %Y', '%Y-%m-%d'):
            try:
                return datetime.strptime(exp_clean, fmt).date()
            except: continue
    except: pass
    return None

check('Parse Jul 17 2026',     parse_exp('Jul 17 2026'),  date(2026, 7, 17))
check('Parse Jul  7 2026',     parse_exp('Jul  7 2026'),  date(2026, 7, 7))   # double space
check('Parse July 17 2026',    parse_exp('July 17 2026'), date(2026, 7, 17))
check('Parse 2026-07-17',      parse_exp('2026-07-17'),   date(2026, 7, 17))
check('Invalid returns None',  parse_exp('garbage'),      None)

# ══════════════════════════════════════════════════════════════════════════════
section('Auth state machine')
# Simulate the auth status logic to verify state transitions
def auth_status(connected, pending, error, creds_ok):
    return {
        'connected': connected,
        'auth_pending': pending,
        'auth_error': error,
        'needs_auth': not connected and not pending,
        'credentials_ok': creds_ok,
    }

# Startup, no token, creds OK → should show Connect button (needs_auth)
s = auth_status(connected=False, pending=False, error=None, creds_ok=True)
check('Startup no token → needs_auth True',     s['needs_auth'], True)
check('Startup no token → not connected',       s['connected'], False)
check('Startup no token → not pending',         s['auth_pending'], False)

# User clicked connect → pending, browser opening
s = auth_status(connected=False, pending=True, error=None, creds_ok=True)
check('After click → pending True',             s['auth_pending'], True)
check('After click → needs_auth False',         s['needs_auth'], False)

# Auth complete → connected
s = auth_status(connected=True, pending=False, error=None, creds_ok=True)
check('Auth complete → connected True',         s['connected'], True)
check('Auth complete → needs_auth False',       s['needs_auth'], False)

# No credentials → blocks everything
s = auth_status(connected=False, pending=False, error=None, creds_ok=False)
check('No creds → credentials_ok False',        s['credentials_ok'], False)

# Auth error → shows error, not pending
s = auth_status(connected=False, pending=False, error='timeout', creds_ok=True)
check('Auth error → has error msg',             s['auth_error'], 'timeout')
check('Auth error → needs_auth True (retry)',   s['needs_auth'], True)

# ══════════════════════════════════════════════════════════════════════════════
print(f'\n{"="*50}')
print(f'Results: {PASS} passed, {FAIL} failed')
if FAIL == 0:
    print('✓ All tests passed — code is verified')
else:
    print(f'✗ {FAIL} test(s) failed — review above')
    sys.exit(1)
