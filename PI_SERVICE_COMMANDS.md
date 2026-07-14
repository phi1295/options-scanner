# Pi Service — Command Reference

Quick reference for managing and troubleshooting the `scanner.service`
on the Raspberry Pi. Assumes the service is installed per RASPBERRY_PI_SETUP.md.

Adjust the username/path if yours differ from `pi` / `/home/pi/options-scanner`.

---

## Start / Stop / Restart

```bash
# Start the scanner
sudo systemctl start scanner.service

# Stop the scanner
sudo systemctl stop scanner.service

# Restart (use after copying new app.py / index.html to the Pi)
sudo systemctl restart scanner.service

# Reload + restart (use if you edited the .service file itself)
sudo systemctl daemon-reload
sudo systemctl restart scanner.service
```

---

## Enable / Disable auto-start on boot

```bash
# Make it start automatically when the Pi powers on
sudo systemctl enable scanner.service

# Stop it from starting on boot (does NOT stop it right now)
sudo systemctl disable scanner.service

# Enable AND start in one command
sudo systemctl enable --now scanner.service

# Disable AND stop in one command
sudo systemctl disable --now scanner.service
```

---

## Status — is it running?

```bash
# Full status: running/stopped, uptime, recent log lines, PID
sudo systemctl status scanner.service

# Just the active state (prints "active" or "inactive"/"failed")
systemctl is-active scanner.service

# Is it set to start on boot? (prints "enabled" or "disabled")
systemctl is-enabled scanner.service
```

Press `q` to exit the status screen if it opens in a pager.

---

## Logs (journalctl)

```bash
# Live tail — watch logs as they happen (Ctrl+C to stop)
journalctl -u scanner.service -f

# Last 50 lines
journalctl -u scanner.service -n 50

# Last 100 lines, no pager (prints straight to terminal)
journalctl -u scanner.service -n 100 --no-pager

# Everything since the Pi last booted
journalctl -u scanner.service -b

# Logs from a time window
journalctl -u scanner.service --since "1 hour ago"
journalctl -u scanner.service --since "2026-06-09 09:00" --until "2026-06-09 17:00"

# Only errors / warnings (priority warning and above)
journalctl -u scanner.service -p warning

# Jump to the end and follow (most useful while debugging a scan)
journalctl -u scanner.service -ef
```

What to look for in the logs:
- `Database ready` and `Rate limiter` lines = startup succeeded
- `Schwab token loaded successfully` = auth is good
- `No Schwab token` = needs a token (copy from Mac)
- Python tracebacks = a code error; copy the traceback when asking for help

---

## Common troubleshooting

### Service won't start / keeps restarting

```bash
# 1. See why it failed — read the status and recent logs
sudo systemctl status scanner.service
journalctl -u scanner.service -n 50 --no-pager

# 2. Try running it by hand to see the raw error directly
cd /home/pi/options-scanner
source .venv/bin/activate
python3 app.py
#    (Ctrl+C to stop. This shows errors the service might swallow.)
```

### "UnicodeEncodeError: 'latin-1' codec can't encode character"

The service environment defaulted to latin-1 and choked on a Unicode character
(em-dash, checkmark) in a log line. The app now forces UTF-8 internally, but if
you ever see this, add these to the `[Service]` section of the unit file:
```ini
Environment=PYTHONIOENCODING=utf-8
Environment=PYTHONUNBUFFERED=1
```
Then:
```bash
sudo systemctl daemon-reload
sudo systemctl restart scanner.service
```

### Schwab auth / "Connect Schwab Account" from your Mac or phone
The web app's Connect button no longer needs a browser on the Pi at all —
it never did work reliably that way, since Schwab's callback URL is locked
to `127.0.0.1`, which only a browser running on the Pi itself could ever
reach. It now uses a copy/paste flow instead:

1. Open `http://<pi-ip>:8080` from your Mac or phone and click **Connect
   Schwab Account**.
2. Click the link it shows — open it in any browser, on any device.
3. Log in with your Schwab brokerage credentials and click **Allow**.
4. You'll land on a page that fails to load or shows a certificate
   warning — that's expected, ignore it.
5. Copy the full address from that page's address bar (it contains
   `code=`) and paste it into the box back in the scanner app, then click
   **Complete login**.

No SSH access is needed for this anymore. If the web server itself won't
start (so there's no UI to click through), fall back to the terminal
script, which does the same copy/paste flow:
```bash
cd ~/options-scanner
source .venv/bin/activate
python3 authenticate.py
```
It prints a URL — open it in any browser, log in, paste the redirect URL back,
then: `sudo systemctl restart scanner.service`

**Tip: if Chromium fails Schwab's login page, use Firefox instead.**
Chromium on ARM Linux can have JavaScript issues with Schwab's auth flow.
Install Firefox and set it as default:
```bash
sudo apt install -y firefox-esr
```
Then set it as the default browser in the Pi desktop settings, or just
open the URL manually in Firefox.

Or copy a working token from your Mac:
```bash
# Run this FROM YOUR MAC:
scp ~/PycharmProjects/options-scanner/schwab_token.json pi@<pi-ip>:~/options-scanner/
# Then on the Pi:
sudo systemctl restart scanner.service
```

### Port already in use ("Address already in use")

Something is already on the port (maybe an old instance).
```bash
# Find what's using port 8080 (or your configured port)
sudo lsof -i :8080
# Kill it by PID
sudo kill -9 <PID>
# Or stop the service if it's a duplicate
sudo systemctl restart scanner.service
```

### Can't reach the app from Mac/phone

```bash
# Confirm the service is actually running
systemctl is-active scanner.service

# Confirm the Pi's IP (use this in http://<ip>:8080)
hostname -I

# Confirm the app is listening on the port
sudo lsof -i :8080

# Check you can reach it locally on the Pi first
curl -s http://127.0.0.1:8080/api/status
#   JSON response = server is up; the problem is network/firewall.
#   No response = the server itself isn't running.
```

### Packages / virtual environment issues

```bash
cd /home/pi/options-scanner
source .venv/bin/activate
pip install -r requirements.txt        # reinstall/upgrade deps
python3 -c "import flask, schwab, pandas, numpy, lxml, PIL; print('all imports OK')"
```

### Database checks

```bash
cd /home/pi/options-scanner
# Confirm the DB file exists and its size
ls -lh scanner.db

# Peek at it (install sqlite3 CLI if needed: sudo apt install -y sqlite3)
sqlite3 scanner.db "SELECT COUNT(*) AS trades FROM trades;"
sqlite3 scanner.db "SELECT key, value FROM settings;"
```

---

## Updating the app on the Pi

```bash
# 1. FROM YOUR MAC — copy the changed files over
scp ~/PycharmProjects/options-scanner/app.py            pi@<pi-ip>:~/options-scanner/
scp ~/PycharmProjects/options-scanner/static/index.html pi@<pi-ip>:~/options-scanner/static/

# 2. ON THE PI — restart to pick up changes
sudo systemctl restart scanner.service

# 3. Confirm it came back up
systemctl is-active scanner.service
journalctl -u scanner.service -n 20 --no-pager
```

---

## Backups

```bash
# FROM YOUR MAC — pull a copy of the live database
scp pi@<pi-ip>:~/options-scanner/scanner.db ~/scanner-backups/scanner-$(date +%Y%m%d).db
```

---

## Rebooting the Pi

```bash
sudo reboot          # restart the whole Pi (service auto-starts if enabled)
sudo shutdown -h now # power off safely
```

After a reboot, if the service is enabled it starts on its own. Verify with:
```bash
systemctl is-active scanner.service
```
