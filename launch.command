#!/bin/bash
# ──────────────────────────────────────────────────────────────────────
# Options Trade Scanner — launch script
#
# This activates the PyCharm virtual environment and starts the app.
# Credentials are read from config.json (no need to edit this file).
#
# To run: double-click this file, or from Terminal:  bash launch.sh
# ──────────────────────────────────────────────────────────────────────

# Move to the folder this script lives in
cd "$(dirname "$0")"
APP_DIR="$(pwd)"

echo "════════════════════════════════════════════════════"
echo "  Options Trade Scanner"
echo "════════════════════════════════════════════════════"

# ── Find the virtual environment ──────────────────────────────────────
# PyCharm usually creates .venv inside the project folder.
# We check a few common locations.
VENV=""
for candidate in \
    "$APP_DIR/.venv" \
    "$APP_DIR/venv" \
    "$APP_DIR/../.venv" \
    "/Users/erikbeltran/PycharmProjects/options-scanner/.venv"
do
    if [ -f "$candidate/bin/activate" ]; then
        VENV="$candidate"
        break
    fi
done

if [ -z "$VENV" ]; then
    echo ""
    echo "  ⚠  Could not find the virtual environment."
    echo "     Looked in:"
    echo "       $APP_DIR/.venv"
    echo "       $APP_DIR/venv"
    echo ""
    echo "     Edit launch.sh and add your venv path to the list,"
    echo "     or run the app directly from PyCharm instead."
    echo ""
    read -p "  Press Enter to try with system Python anyway, or Ctrl+C to quit..."
    python3 app.py
    exit 0
fi

echo "  ✓  Using virtual environment:"
echo "     $VENV"
echo ""

# ── Activate venv and run ─────────────────────────────────────────────
source "$VENV/bin/activate"

# Verify required packages are installed
python -c "import flask, schwab, pandas, numpy, lxml" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "  ⚠  Some packages are missing. Installing now…"
    pip install flask schwab-py pandas numpy requests lxml
    echo ""
fi

echo "  Starting scanner — browser will open automatically"
echo "════════════════════════════════════════════════════"
echo ""

python app.py
