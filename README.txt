OPTIONS TRADE SCANNER v3 — Quick Start
========================================

FIRST TIME SETUP:

1. Install Python packages:
   pip install flask schwab-py pandas numpy requests

2. Edit config.json — paste your Schwab Client ID and Client Secret
   (replace the placeholder values)

3. Go to developer.schwab.com → My Apps → your app
   Set callback URL to exactly: https://127.0.0.1:8182/
   (include the trailing slash)

4. Run: python app.py
   First launch opens a Schwab login page — log in once.
   Token saves to schwab_token.json (valid 7 days).

5. Import your IBD50 list via the IBD50 Import tab in the app.

EVERY DAY:
   python app.py   (or double-click launch.sh)

CREDENTIALS:
   Stored in config.json — never touches app.py
   config.json stays on your machine only — never share it

IMPORTANT:
- Scan works best 9:45am–3:45pm ET (options market hours)
- Always verify IBD RS Rating at ibd.com before trading
- Always confirm spread in thinkorswim before entering
- Never risk more than 2–3% of account per trade
- Token expires every 7 days — re-run app.py to re-authorize

FILES:
  app.py              — Python backend (Flask + Schwab API)
  config.json         — Your credentials (DO NOT SHARE)
  schwab_token.json   — OAuth token (auto-generated, DO NOT SHARE)
  ibd50.json          — Your IBD50 list (auto-saved)
  static/index.html   — Frontend UI
  launch.sh           — Double-click launcher
  .gitignore          — Protects credentials if using git

WHAT'S NEW IN v3:
- Credentials in config.json — survive app updates
- Rate limiting (80 req/min) — prevents Schwab 429 errors
- Greeks (IV, Delta, Theta) used for better spread selection
- Market hours from Schwab API — handles holidays correctly
- Batch quote pre-filter — scans run faster
- Trailing slash on callback URL (required by schwab-py)
- Graceful 401/429 error handling with auto-retry
