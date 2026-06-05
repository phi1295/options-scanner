#!/bin/bash
# Options Trade Scanner — launch script
# Edit the two lines below with your Schwab credentials, then double-click this file.

export SCHWAB_APP_KEY=YOUR_APP_KEY_HERE
export SCHWAB_APP_SECRET=YOUR_APP_SECRET_HERE

cd "$(dirname "$0")"
echo "Starting Options Scanner..."
python app.py
