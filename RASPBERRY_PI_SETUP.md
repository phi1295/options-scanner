# Raspberry Pi 5 — Options Scanner Server Setup

A complete, ordered guide to running the scanner backend on a Raspberry Pi 5
so your Mac and phone both reach it over your home network.

When you finish, you'll open `http://<pi-ip>:8080` from any device at home.

---

## What you need

- Raspberry Pi 5 with Raspberry Pi OS (64-bit) installed and on your network
- The project files (this folder) copied to the Pi
- Your `config.json` with Schwab credentials
- About 30–40 minutes

---

## Step 1 — Update the Pi and install system packages

SSH into the Pi (or use its desktop + a terminal):

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git
```

The Pi 5 on 64-bit Raspberry Pi OS has Python 3.11+, which is fine.

---

## Step 2 — Copy the project to the Pi

From your **Mac**, copy the project folder over (replace the IP with your Pi's):

```bash
scp -r /Users/erikbeltran/PycharmProjects/options-scanner pi@192.168.1.50:~/
```

Or use a USB stick, or `git clone` if you've pushed the repo somewhere.

**Important — bring your data and credentials too** (these are gitignored, so
they won't come via git):

```bash
scp /Users/erikbeltran/PycharmProjects/options-scanner/config.json pi@192.168.1.50:~/options-scanner/
scp /Users/erikbeltran/PycharmProjects/options-scanner/scanner.db   pi@192.168.1.50:~/options-scanner/
```

`scanner.db` carries all your trades and settings — copying it is the whole
"move from Mac to Pi" step. If you don't have one yet, the Pi will create a
fresh one on first run.

---

## Step 3 — Create a virtual environment and install packages

On the **Pi**:

```bash
cd ~/options-scanner
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

This installs Flask, schwab-py, pandas, numpy, requests, lxml, and pillow.
On a Pi 5 this takes a few minutes (pandas/numpy compile step).

---

## Step 4 — Give the Pi a fixed local IP

So the address your devices point at never changes. Two ways — pick one:

**Option A (recommended): DHCP reservation on your router.**
Log into your router, find the Pi in the connected-devices list, and reserve
its current IP to its MAC address. Nothing to configure on the Pi.

**Option B: static IP on the Pi.** Edit the network config:
```bash
sudo nmtui
```
Set a manual IPv4 address (e.g. `192.168.1.50`), the gateway (your router,
e.g. `192.168.1.1`), and DNS (`192.168.1.1` or `8.8.8.8`). Reboot.

Note the IP — you'll use `http://<that-ip>:8080` everywhere.

---

## Step 5 — First run + one-time Schwab authentication

Schwab's OAuth callback URL is locked to `https://127.0.0.1:8182` — but the
app never actually needs a browser running on the Pi to complete login. The
web UI's Connect flow works by pasting the resulting redirect URL back in,
so it works from any device pointed at the Pi, no VNC/monitor/SSH required:

1. Start the app on the Pi (`python3 app.py`, or as the systemd service).
2. From your Mac or phone, go to `http://<pi-ip>:8080` and click
   **Connect Schwab Account**.
3. Click the link it shows — open it in any browser on any device.
4. Log in to Schwab and click **Allow**.
5. You'll land on a page that fails to load or shows a certificate
   warning for `https://127.0.0.1:8182` — that's expected, ignore it.
6. Copy the full address-bar URL (it contains `code=...`) and paste it
   into the box back in the scanner app, then click **Complete login**.
   The token saves to `schwab_token.json` on the Pi.

**If the web server itself won't start** (so there's no UI to click
through), use the terminal script instead — it does the same copy/paste
flow over SSH, no auto-browser needed:
```bash
cd ~/options-scanner
source .venv/bin/activate
python3 authenticate.py
```
It prints a Schwab URL. Open that URL in ANY browser, log in, approve, then
paste the resulting address-bar URL back into the script when prompted. It
writes `schwab_token.json`, then:
```bash
sudo systemctl restart scanner.service
```

**Alternative — copy a token from your Mac.** Authenticate on the Mac (its
browser opens normally), then copy the token over:
```bash
scp /Users/erikbeltran/PycharmProjects/options-scanner/schwab_token.json pi@192.168.1.50:~/options-scanner/
```
The token works on the Pi as long as the same `config.json` credentials are
present. Re-authenticate weekly when the token expires (any of the methods
above).

Once the token exists, the app starts straight into the scanner — no login
screen — and you can reach it from any device.

---

## Step 6 — Reach it from your Mac and phone

With the app running on the Pi, on any device on your home WiFi:

```
http://192.168.1.50:8080
```

(using your Pi's actual IP). Both Mac and phone now share the same trades and
settings, because they're all talking to the one SQLite database on the Pi.

On your **phone**, use the browser's **Add to Home Screen** to install it as a
PWA — it gets an icon and launches full-screen like an app.

---

## Step 7 — Run automatically on boot (so it's always available)

So you don't have to SSH in and start it manually. Create a systemd service:

```bash
sudo nano /etc/systemd/system/scanner.service
```

Paste (adjust the username/path if different):

```ini
[Unit]
Description=Options Trade Scanner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/options-scanner
Environment=PYTHONIOENCODING=utf-8
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/pi/options-scanner/.venv/bin/python3 /home/pi/options-scanner/app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable scanner.service
sudo systemctl start scanner.service
```

Check it's running:
```bash
sudo systemctl status scanner.service
```

View logs (including scan progress):
```bash
journalctl -u scanner.service -f
```

Now the scanner starts automatically whenever the Pi powers on.

---

## Step 8 — Verify

From your Mac browser, open `http://<pi-ip>:8080`:
- The scanner loads
- Your trades are there (from the copied `scanner.db`)
- The Position Sizing panel shows your saved account size
- A scan runs and returns setups

If all that works, you're done. The Pi is now your always-on scanner server.

---

## Maintenance notes

- **Changing the port:** the web app runs on port 8080 by default. To change
  it, set `"server_port": <number>` in `config.json` (e.g. `8090`) and restart.
  Then use `http://<pi-ip>:<number>` everywhere. Do NOT change the OAuth
  callback port 8182 — it's registered in your Schwab Developer Portal.
- **Weekly Schwab re-auth:** the token lasts 7 days. When it expires, click
  **Connect Schwab Account** from any device (see Step 5) — it no longer
  needs to happen on the Pi itself — or copy a fresh `schwab_token.json`
  from a Mac login.
- **Backups:** your data is one file. Copy `scanner.db` somewhere safe
  periodically: `scp pi@<pi-ip>:~/options-scanner/scanner.db ~/backups/`
- **Updating code:** when you change the app, copy the new `app.py` /
  `static/index.html` to the Pi and restart: `sudo systemctl restart scanner.service`
- **IBD50 weekly import:** do this from any browser pointed at the Pi — the
  uploaded list is stored on the Pi and shared by all clients.

---

## Optional — access from outside your home (later)

By default this only works on your home WiFi. To reach the Pi from anywhere
(e.g. checking Daily Review from work), install **Tailscale** on the Pi and
your phone/Mac — it creates a private encrypted network between your devices
with zero port-forwarding and no exposure to the public internet:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Then you reach the Pi at its Tailscale IP from any of your devices, anywhere.
This keeps your Schwab credentials on your own hardware — nothing is hosted on
a public cloud. Set this up only after the home-network version works.
