#!/usr/bin/env python3
"""
Manual Schwab authentication — for the Raspberry Pi / VNC / headless use.

Use this when the normal in-app "Connect Schwab" button can't open a browser
automatically (e.g. over VNC, or on a headless Pi). It prints the Schwab
authorization URL, you open it in ANY browser, log in, and paste the redirect
URL back here. It writes schwab_token.json, which the main app then loads.

Run:   python3 authenticate.py
Then:  restart the scanner (it will load the new token automatically).

This does NOT need a browser on the machine running it — that's the whole point.
"""

import sys, json, pathlib

# Force UTF-8 so prints work under any environment
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

HERE = pathlib.Path(__file__).parent
CONFIG_PATH = HERE / 'config.json'
TOKEN_PATH  = str(HERE / 'schwab_token.json')
CALLBACK_URL = 'https://127.0.0.1:8182'   # must match Schwab portal + main app


def main():
    # Load credentials from config.json (same file the app uses)
    if not CONFIG_PATH.exists():
        print('ERROR: config.json not found. Create it with your Schwab keys first.')
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    key    = cfg.get('schwab_client_id', '')
    secret = cfg.get('schwab_client_secret', '')
    if not key or not secret or 'YOUR_CLIENT_ID' in key:
        print('ERROR: Schwab credentials not set in config.json.')
        sys.exit(1)

    try:
        from schwab.auth import client_from_manual_flow
    except ImportError:
        print('ERROR: schwab-py not installed. Run: pip install -r requirements.txt')
        sys.exit(1)

    print('=' * 64)
    print('  Schwab Manual Authentication')
    print('=' * 64)
    print()
    print('  A Schwab URL will be printed below.')
    print()
    print('  IMPORTANT: Open that URL on your MAC or PHONE, not the Pi.')
    print('  The Pi\'s browser can have trouble with Schwab\'s login page.')
    print()
    print('  Steps:')
    print('  1. Copy the URL printed below')
    print('  2. Open it in any browser on your Mac or phone')
    print('  3. Log in with your SCHWAB BROKERAGE credentials')
    print('     (NOT your Developer Portal credentials)')
    print('  4. Click "Allow" to approve access')
    print('  5. Your browser redirects to a 127.0.0.1:8182 address that')
    print('     shows "can\'t connect" or a certificate warning — THAT IS FINE')
    print('  6. Copy the FULL URL from the address bar (contains code=...)')
    print('  7. Paste that full URL back here when prompted')
    print()
    print('=' * 64)
    print()

    # client_from_manual_flow prints the auth URL and prompts for the redirect URL
    client = client_from_manual_flow(
        api_key=key,
        app_secret=secret,
        callback_url=CALLBACK_URL,
        token_path=TOKEN_PATH,
        enforce_enums=False,
    )

    if client is not None and pathlib.Path(TOKEN_PATH).exists():
        print()
        print('=' * 64)
        print('  ✓  Success — token saved to schwab_token.json')
        print('     Now restart the scanner service:')
        print('       sudo systemctl restart scanner.service')
        print('=' * 64)
    else:
        print('  ✗  Authentication did not complete. Try again.')
        sys.exit(1)


if __name__ == '__main__':
    main()
