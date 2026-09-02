# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-user local web app that scans for options spread trade candidates. It combines
Schwab market data with a weekly-imported IBD50 stock list, detects the current market
regime (bullish/bearish/caution/neutral), screens strong stocks, and proposes ranked bull
call spreads / bear put spreads / iron condors with risk-based position sizing. It also
includes a daily-review tool for managing open trades and a trade log for tracking
performance. Designed to run either on a Mac for local use, or on a Raspberry Pi as an
always-on backend reachable from a Mac and phone.

This is a personal trading tool, not a distributed service — there is one backend process,
one SQLite file, and no multi-tenancy.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the app (serves the UI + API on the configured port, default 8080)
python app.py

# Run the full test suite (pure-logic tests; mocks the schwab module so no
# credentials or network access are required)
python3 test_scanner.py

# One-off manual Schwab OAuth flow (used when the web UI's Connect flow isn't
# usable, e.g. server won't start)
python3 authenticate.py
```

There is no test framework/runner — `test_scanner.py` is a self-contained script with a
hand-rolled `check()`/`section()` assertion helper. It prints `Results: N passed, M failed`
at the end and mocks `schwab` before importing `app`, so `app.py` must stay importable
without real Schwab credentials or network access (guard any module-level side effects
accordingly). There is no single-test-runner flag — to check one area, either run the
whole script (it's fast, no network calls) or comment out irrelevant `section()` blocks
temporarily.

There is no linter/formatter configured for this project.

## Architecture

### Two files, one process

- `app.py` — the entire backend: Flask app, Schwab API integration, market regime
  detection, stock screening/scoring, spread/condor construction, position sizing, SQLite
  persistence, and all `/api/*` routes. This is a large single-file module (~2700 lines);
  functions are organized in clear `# ── Section ──` banner comments (config, DB, progress
  streaming, scan concurrency, rate limiter, Schwab auth, IBD50 import, market
  regime/universe, scoring, spread/condor building, position sizing, then the Flask
  routes at the bottom).
- `static/index.html` — the entire frontend: a single HTML file with inline `<script>`
  containing all tab logic (Scanner, IBD50 Import, Daily Review, Trade Log, Instructions,
  Daily Playbook) and inline styles. No build step, no bundler, no frontend framework.

When changing behavior, expect to touch both files together (backend route + the JS that
calls it and renders the response) unless the change is backend-only logic covered by
`test_scanner.py`.

### Request flow for a scan (`GET /api/scan`)

`detect_regime()` (SPY/QQQ/$VIX signal voting) → `get_universe()` (IBD50 + S&P500 +
Nasdaq100 + movers) → `batch_quote_filter()` (cheap price prefilter) → `screen_stock()` per
symbol (up to 60, IBD50-first ordering for determinism) → `score_stock()` (IBD factors when
available, price-based RS proxy otherwise) → `get_best_spread()` / `get_iron_condor()` for
the top-scoring candidates (condors in neutral regime, verticals otherwise) → up to 8 ranked
trade setups returned as JSON. Progress is streamed to the UI separately via Server-Sent
Events through `/api/progress`, fed by calls to `emit()` throughout the scan.

Only one scan may run at a time (`_scan_lock` / `_scan_running`); a second concurrent
request gets HTTP 409. `_scan_cancel` (a `threading.Event`) lets `/api/scan/cancel` stop an
in-flight scan promptly — long-running loops call `check_cancel()` at safe points and raise
`ScanCancelled`.

### Schwab API access

All calls to the schwab-py client go through `schwab_call()`, never the client directly —
it centralizes rate limiting (`RateLimiter`, `_rl`, 80 calls/min), retries with backoff on
transient failures, 429 handling, and distinguishes a dead/expired refresh token
(`_is_dead_refresh_token_error`, which clears the client so the UI prompts reconnect) from a
transient per-call 401.

OAuth uses a **manual copy/paste flow**, not a listening callback server: the Schwab
callback URL is fixed to `https://127.0.0.1:8182`, which only works if a browser runs on the
same machine as the backend. Since the backend may run headless on a Pi, the flow instead
shows the user an auth URL, they complete login in any browser on any device, and paste the
resulting redirect URL (containing `code=`) back into the app (`_start_manual_auth` /
`_complete_manual_auth`, exposed via `/api/auth/start` and `/api/auth/complete`). The token
persists to `schwab_token.json` and is valid ~7 days; moving it between machines (e.g. Mac →
Pi) is a supported way to transfer an active session.

### Data storage

- `scanner.db` (SQLite, WAL mode) — two tables: `trades` (the trade log) and `settings`
  (key/value). This file is the single source of truth for trades/settings and is meant to
  be copied wholesale when moving the backend between machines (e.g. Mac → Pi). Theme
  preference is intentionally kept client-side (browser), not in this DB.
- `ibd50.json` — the user's imported IBD50 dataset (rank, RS Rating, Composite, EPS Rating,
  ACC/DIS, % off high, SMR, etc. per symbol), refreshed weekly via XLS/CSV import
  (`parse_ibd_xls_or_csv`). Stock scoring (`score_stock`) uses this data when present and
  falls back to a Schwab-price-based RS proxy when a symbol isn't in it.
- `config.json` — **contains live Schwab API credentials** (`schwab_client_id`,
  `schwab_client_secret`) plus `server_port`. Never print its contents, commit it, or send it
  anywhere. It's gitignored; treat it as a secret file even when reading it for debugging.
- `schwab_token.json` — OAuth token, also gitignored/sensitive.

### Market regime and scoring model

`detect_regime()` casts one vote per signal (SPY trend vs 50/200-day MA, $VIX level, SPY
10/20-day momentum, QQQ-vs-SPY sector rotation over matched trailing windows, SPY
short-term trend as a breadth proxy) and decides the regime by simple vote thresholds. The
regime determines both the recommended strategy (bull call spread / bear put spread / iron
condor) and scoring weights in `score_stock()`. This voting/threshold design is deliberately
simple and explicit — if changing thresholds or adding signals, keep the vote-counting
pattern consistent since `test_scanner.py` asserts on regime vote counts directly.

Spread construction (`get_best_spread`, `get_iron_condor`) scales strike width to ~2-3% of
the underlying price (snapped to a fixed set of width steps), uses mid/mark prices, filters
on open interest and bid/ask spread, and requires 25-50% return on debit within a 30-45 DTE
window. Among the spreads that clear those filters, verticals are ranked by estimated
probability of profit (`prob_beyond`, which interpolates |delta| between the two legs to
approximate the chance of finishing past a given price), shorter DTE breaking ties — not by
return on debit as before. Position sizing (`calc_position_size`) is risk-based off account
size and a target risk % per trade — this is treated as the primary risk control, so changes
here deserve extra care and test coverage.

The exit rules are a fixed spec, not tunable heuristics: 50%-of-max-profit target,
**50%-of-debit stop**, close at 21 DTE, never hold through earnings. They are documented for
the user in the Setup Guide / Daily Playbook tabs of `static/index.html` (there is no README
despite older references to one), and that copy is the source of truth. A volatility-scaled
stop was tried and reverted — "no exceptions" is a discipline rule, and a stop that varies
per trade is harder to hold to. `calc_atr_pct` survives as display-only context on the trade
card and must not feed any filter, stop, or sizing decision; `test_scanner.py` asserts no
stop-scaling helper exists.

Two probability numbers are reported per vertical and are deliberately kept separate rather
than differenced into an "edge": `pop` is measured at breakeven, `breakeven_win_rate` is the
win rate the profit-target/stop payoff requires. They sit on different bases, and subtracting
them would imply precision neither has.

### Deployment target duality

The same `app.py`/`static/index.html` run unmodified on a Mac (local dev/use) or a
Raspberry Pi (`scanner.service` under systemd, see `RASPBERRY_PI_SETUP.md` and
`PI_SERVICE_COMMANDS.md`). Port is configurable via `config.json`'s `server_port` (or
`SCANNER_PORT` env var); the OAuth callback port 8182 is fixed and must never change (it's
registered with Schwab). stdout/stderr are forced to UTF-8 at startup since systemd
environments can default to latin-1 and choke on the Unicode characters (em-dash, ✓) used
in progress/log output — preserve that reconfigure block if touching startup code.
