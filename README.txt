OPTIONS TRADE SCANNER — Quick Start
========================================

A Schwab + IBD options spread scanner. Detects market regime, screens strong
stocks (IBD50 + S&P500 + Nasdaq 100 + movers), and returns ranked, slightly-ITM
bull call / bear put spreads (or iron condors in neutral markets) with
risk-based position sizing and a daily-review tool for managing open trades.

Runs as a local web app — use it in any browser on your machine, or run the
backend on a Raspberry Pi and reach it from your Mac and phone.

----------------------------------------
FIRST-TIME SETUP
----------------------------------------

1. Install Python packages:
     pip install -r requirements.txt
   (flask, schwab-py, pandas, numpy, requests, lxml, pillow)

2. Edit config.json — paste your Schwab Client ID and Client Secret in place
   of the placeholders. Optionally set "server_port" (default 8080).
   This file stays on your machine only — never share it.

3. In the Schwab Developer Portal (developer.schwab.com -> My Apps), set the
   callback URL to EXACTLY:
       https://127.0.0.1:8182
   NO trailing slash. (This is separate from the web app port.)

4. Run the app:
       python app.py
   Then open the browser to http://127.0.0.1:<port> (8080 by default).

5. Click "Connect Schwab Account" on the screen and log in once. The browser
   handles the whole OAuth flow — no copying URLs into a terminal. You'll hit a
   certificate warning for 127.0.0.1:8182; that's expected, click through it.
   The token saves to schwab_token.json and lasts 7 days.

6. Import your IBD50 list weekly via the IBD50 Import tab (export the XLS from
   ibd.com and drag it in).

----------------------------------------
DAILY USE
----------------------------------------

- SCANNER tab: set your account size + risk %, then Run scan (best 10am-3pm ET).
  Each card shows the spread, recommended contract count, risk at stop, and IV.
- Enter trades in thinkorswim as a Limit order at the card's debit, then set ONE
  GTC profit-target closing order. (See the Daily Playbook tab for full steps.)
- DAILY REVIEW tab: pulls live prices for your open trades and tells you exactly
  when to close (target / stop / 21 DTE). Check it morning and midday.
- TRADE LOG tab: log every trade; it tracks win rate, P&L, and profit factor.
- Update your account size after each closed trade so sizing stays accurate.

In-app guides: SETUP GUIDE (one-time setup) and DAILY PLAYBOOK (trading steps).

----------------------------------------
DATA STORAGE
----------------------------------------

Trades and settings are stored in a single SQLite file: scanner.db
- All clients (Mac browser, phone) that point at the same backend share it.
- To move machines (e.g. Mac -> Raspberry Pi), just copy scanner.db over.
- Theme preference is per-device (stored in the browser).

----------------------------------------
RASPBERRY PI SERVER
----------------------------------------

To run the backend on a Pi so your Mac and phone share one always-on scanner:
- See RASPBERRY_PI_SETUP.md for full setup (packages, static IP, auth, systemd).
- See PI_SERVICE_COMMANDS.md for start/stop/logs/troubleshooting commands.

----------------------------------------
KEY RULES
----------------------------------------

- Position sizing is your main protection: risk ~1.5% of account per trade.
- Exits: close at profit target (50% of max profit), stop (50% of debit), or
  21 DTE — whichever comes first. The Daily Review flags these for you.
- Never hold through earnings (the scanner filters these, but always verify).
- Always confirm the spread + price in thinkorswim before entering.
- Token expires every 7 days — reconnect via the Connect screen (or copy a fresh
  schwab_token.json to the Pi).

----------------------------------------
FILES
----------------------------------------

  app.py                  Python backend (Flask + Schwab API + SQLite)
  static/index.html       Frontend UI (all tabs)
  config.json             Your credentials + port (DO NOT SHARE)
  schwab_token.json       OAuth token (auto-generated, DO NOT SHARE)
  scanner.db              Your trades + settings (auto-created)
  ibd50.json              Your IBD50 list (auto-saved)
  requirements.txt        Python dependencies
  launch.command          Double-click launcher (Mac)
  test_scanner.py         Test suite (run: python3 test_scanner.py)
  RASPBERRY_PI_SETUP.md   Pi server setup guide
  PI_SERVICE_COMMANDS.md  Pi service command reference
  .gitignore              Protects credentials + scanner.db if using git

----------------------------------------
NOTES
----------------------------------------

- The web app port is configurable in config.json ("server_port").
- The OAuth callback port (8182) is fixed and registered with Schwab — do not
  change it.
- Only one scan runs at a time; a running scan can be cancelled, and a
  reconnecting client can cancel an orphaned scan.
