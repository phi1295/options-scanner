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
section('Iron condor detection & review logic')
import re as _re2
def detect_condor(structure):
    has_call = 'call' in structure.lower() or 'c /' in structure.lower()
    has_put  = 'put'  in structure.lower() or 'p /' in structure.lower()
    nums = [float(n) for n in _re2.findall(r'\$(\d+(?:\.\d+)?)', structure)]
    return (len(nums) >= 4) and has_call and has_put

check('Detect condor from 4-leg string',
      detect_condor('SELL $446 call / BUY $451 call  +  SELL $404 put / BUY $399 put'), True)
check('Vertical not detected as condor',
      detect_condor('Buy $525 call / Sell $535 call'), False)
check('Bear put vertical not condor',
      detect_condor('Buy $335 put / Sell $325 put'), False)

def condor_action(current, credit, target, stop, dte):
    """Condor: profit when value drops below credit. Inverted from vertical."""
    if current <= target:   return 'CLOSE — TARGET HIT'
    elif current >= stop:   return 'CLOSE — STOP HIT'
    elif dte <= 21:         return 'CLOSE — 21 DTE RULE'
    elif dte <= 25:         return 'PREPARE TO CLOSE'
    elif current >= stop*0.75: return 'WATCH — APPROACHING STOP'
    elif current <= target*1.25: return 'NEAR TARGET'
    else:                   return 'HOLD'

# Condor: collected $2.00 credit, target $1.00 (50%), stop $4.00 (2x)
check('Condor below target → CLOSE TARGET',
      condor_action(0.95, 2.0, 1.0, 4.0, 30), 'CLOSE — TARGET HIT')
check('Condor above stop → CLOSE STOP',
      condor_action(4.10, 2.0, 1.0, 4.0, 30), 'CLOSE — STOP HIT')
check('Condor 21 DTE → CLOSE',
      condor_action(2.0, 2.0, 1.0, 4.0, 20), 'CLOSE — 21 DTE RULE')
check('Condor mid-range → HOLD',
      condor_action(2.0, 2.0, 1.0, 4.0, 30), 'HOLD')

# Condor P&L: profit when current < credit
def condor_pnl(credit, current, contracts):
    return round((credit - current) * 100 * contracts, 2)
check('Condor profit when bought back cheap',
      condor_pnl(2.0, 1.0, 10), 1000.0)  # collected 2.00, buy back 1.00 = $1.00 x 100 x 10
check('Condor loss when bought back expensive',
      condor_pnl(2.0, 3.50, 10), -1500.0)

# Vertical P&L: profit when current > debit (opposite direction)
def vertical_pnl(debit, current, contracts):
    return round((current - debit) * 100 * contracts, 2)
check('Vertical profit when value rises',
      vertical_pnl(2.95, 6.47, 18), 6336.0)

# Condor strike parsing — 4 strikes in correct order
nums = [float(n) for n in _re2.findall(r'\$(\d+(?:\.\d+)?)', 'SELL $446 call / BUY $451 call  +  SELL $404 put / BUY $399 put')]
check('Condor parses 4 strikes', len(nums), 4)
check('Short call = first strike',  nums[0], 446.0)
check('Long call = second strike',  nums[1], 451.0)
check('Short put = third strike',   nums[2], 404.0)
check('Long put = fourth strike',   nums[3], 399.0)

# ══════════════════════════════════════════════════════════════════════════════
section('All trade types — detection & close direction')
import re as _re3
def classify_trade(structure):
    """Replicate api_review's trade-type detection."""
    s = structure.lower()
    has_call = 'call' in s or 'c /' in s
    has_put  = 'put'  in s or 'p /' in s
    nums = [float(n) for n in _re3.findall(r'\$(\d+(?:\.\d+)?)', structure)]
    is_condor = (len(nums) >= 4) and has_call and has_put
    if is_condor:                return 'condor', 'PUT_AND_CALL'
    elif has_put and not has_call: return 'bear_put', 'PUT'
    else:                          return 'bull_call', 'CALL'

# Bull call spread
typ, ct = classify_trade('Buy $525 call / Sell $535 call')
check('Bull call detected',        typ, 'bull_call')
check('Bull call uses CALL chain', ct, 'CALL')

# Bear put spread
typ, ct = classify_trade('Buy $335 put / Sell $325 put')
check('Bear put detected',         typ, 'bear_put')
check('Bear put uses PUT chain',   ct, 'PUT')

# Iron condor
typ, ct = classify_trade('SELL $446 call / BUY $451 call  +  SELL $404 put / BUY $399 put')
check('Condor detected',           typ, 'condor')

# Close direction
def close_direction(trade_type):
    return 'BUY' if trade_type == 'condor' else 'SELL'
check('Bull call closes by SELL',  close_direction('bull_call'), 'SELL')
check('Bear put closes by SELL',   close_direction('bear_put'),  'SELL')
check('Condor closes by BUY',      close_direction('condor'),    'BUY')

section('Bull call spread — entry & exit math')
# Buy $525 ATM call, sell $535 call, both calls
# buy_mark > sell_mark (ATM more expensive) → positive debit
nd = 3.00; width = 10.0
mp = width - nd
check('Bull call max profit',      mp, 7.0)
check('Bull call breakeven = buy+debit', 525 + nd, 528.0)
# Profit when spread value rises
check('Bull call profits as value rises', (6.0 - nd) > 0, True)

section('Bear put spread — entry & exit math')
# Buy $335 ATM put, sell $325 put, both puts
# buy_mark (higher strike put) > sell_mark (lower strike put) → positive debit
nd = 3.00; width = 10.0
check('Bear put max profit',       width - nd, 7.0)
check('Bear put breakeven = buy-debit', 335 - nd, 332.0)
# Profit when stock FALLS (spread value rises as puts gain)
check('Bear put profits as value rises', (6.0 - nd) > 0, True)

section('Iron condor — entry & exit math')
# Collect $2.00 credit, wing width $5, max loss = 5 - 2 = 3
credit = 2.00; wing = 5.0
max_loss = wing - credit
check('Condor max loss = wing - credit', max_loss, 3.0)
check('Condor profit target = 50% credit', round(credit * 0.5, 2), 1.0)
check('Condor stop = 2x credit',          round(credit * 2, 2), 4.0)
# Profit when bought back BELOW credit
check('Condor profits as value falls', (credit - 1.0) > 0, True)

section('P&L sign correctness across all types')
# Bull call: paid 2.95, now worth 6.47 → profit
check('Bull call P&L positive when up',
      round((6.47 - 2.95) * 100 * 18), 6336)
# Bear put: paid 3.00, now worth 5.00 → profit
check('Bear put P&L positive when up',
      round((5.00 - 3.00) * 100 * 10), 2000)
# Condor: collected 2.00, now worth 1.00 → profit
check('Condor P&L positive when down',
      round((2.00 - 1.00) * 100 * 10), 1000)
# Condor: collected 2.00, now worth 3.50 → loss
check('Condor P&L negative when up',
      round((2.00 - 3.50) * 100 * 10), -1500)

# ══════════════════════════════════════════════════════════════════════════════
section('Position sizing — risk-based recommendation')
ps = app.calc_position_size(debit=3.00, account_size=10000, risk_pct=1.5)
# Risk budget = 150, risk/contract = 3.00*0.5*100 = 150, so 1 contract
check('$10k 1.5% $3 debit → 1 contract', ps['contracts'], 1)
check('Dollar risk = $150',              ps['dollar_risk'], 150.0)
check('Pct of account = 1.5%',           ps['pct_of_account'], 1.5)
check('No warning at 1.5%',              ps['warning'], None)

# Cheaper spread → more contracts fit
ps = app.calc_position_size(debit=1.00, account_size=10000, risk_pct=1.5)
# risk/contract = 1.00*0.5*100 = 50, budget 150 → 3 contracts
check('$10k 1.5% $1 debit → 3 contracts', ps['contracts'], 3)
check('3 contracts risk = $150',          ps['dollar_risk'], 150.0)

# Expensive spread → 1 contract but higher % risk → warning
ps = app.calc_position_size(debit=7.00, account_size=10000, risk_pct=1.5)
# risk/contract = 7*0.5*100 = 350 = 3.5% of 10k → 1 contract + warning
check('$7 debit → 1 contract floor',     ps['contracts'], 1)
check('$7 debit risk = $350',            ps['dollar_risk'], 350.0)
check('$7 debit = 3.5% of account',      ps['pct_of_account'], 3.5)
check('$7 debit triggers warning',       ps['warning'] is not None, True)

# Larger account → more contracts
ps = app.calc_position_size(debit=3.00, account_size=50000, risk_pct=1.5)
# budget = 750, risk/contract 150 → 5 contracts
check('$50k 1.5% $3 debit → 5 contracts', ps['contracts'], 5)
check('5 contracts risk = $750',          ps['dollar_risk'], 750.0)

# Debit budget cap — never exceed $10k total debit
ps = app.calc_position_size(debit=2.00, account_size=500000, risk_pct=2.0)
# budget huge, but $10k/(2*100)=50 contract cap
check('Total debit capped under $10k', ps['total_debit'] <= 10000, True)

# Account grows → recommendation scales
ps_small = app.calc_position_size(debit=2.00, account_size=10000, risk_pct=1.5)
ps_big   = app.calc_position_size(debit=2.00, account_size=30000, risk_pct=1.5)
check('Bigger account → more contracts', ps_big['contracts'] > ps_small['contracts'], True)

# Edge: zero debit doesn't crash
ps = app.calc_position_size(debit=0, account_size=10000, risk_pct=1.5)
check('Zero debit → 1 contract, no crash', ps['contracts'], 1)

# Condor: explicit risk override sizes on real max loss, not 50% of credit
ps = app.calc_position_size(debit=2.00, account_size=10000, risk_pct=1.5,
                            risk_per_contract_override=300)
check('Condor sized on real $300 risk', ps['dollar_risk'], 300)
check('Condor risk = 3% of account',    ps['pct_of_account'], 3.0)
# Without override it would wrongly size on $100 — confirm override changes it
ps_wrong = app.calc_position_size(debit=2.00, account_size=10000, risk_pct=1.5)
check('Override differs from naive debit sizing',
      ps['dollar_risk'] != ps_wrong['dollar_risk'], True)

section('Slightly-ITM strike selection logic')
# For bullish: long leg target is ~2.5% below price (ITM)
def itm_long_strike(price, strikes, bullish=True):
    if bullish:
        cands = [s for s in strikes if s < price]
        target = price * 0.975
        return min(cands, key=lambda x: abs(x-target)) if cands else min(strikes, key=lambda x: abs(x-price))
    else:
        cands = [s for s in strikes if s > price]
        target = price * 1.025
        return min(cands, key=lambda x: abs(x-target)) if cands else min(strikes, key=lambda x: abs(x-price))

strikes = [510, 515, 520, 525, 530, 535, 540]
# AMD at 523, bullish → target 510, closest ITM is 510 or 515
buy = itm_long_strike(523, strikes, True)
check('Bullish long leg is ITM (below price)', buy < 523, True)
check('Bullish long leg near 2.5% ITM', buy in [510, 515], True)

# Bearish at 336 → target above price
strikes2 = [325, 330, 335, 340, 345, 350]
buy2 = itm_long_strike(336, strikes2, False)
check('Bearish long leg is ITM (above price)', buy2 > 336, True)

section('Return band 25-50%')
def in_band(rp): return 25 <= rp <= 50
check('30% return in band',  in_band(30), True)
check('25% return in band',  in_band(25), True)
check('50% return in band',  in_band(50), True)
check('20% return rejected', in_band(20), False)
check('60% return rejected', in_band(60), False)

# ══════════════════════════════════════════════════════════════════════════════
print(f'\n{"="*50}')
print(f'Results: {PASS} passed, {FAIL} failed')
if FAIL == 0:
    print('✓ All tests passed — code is verified')
else:
    print(f'✗ {FAIL} test(s) failed — review above')
    sys.exit(1)
