# 🎵 YT-MP3 — YouTube to MP3 Downloader

A fast, self-hosted downloader for DJs and music lovers. Download YouTube audio (MP3, 128–320 kbps) and video (MP4, up to 1080p) from a clean web UI, plus a **live Ghana Music feed** scraped from Halmblog.com with auto-updates and an archive deep-cache.

```
✅ Works out of the box on a fresh VPS — no paid services, no API keys.
```

---

## Requirements (VPS)

| Requirement | Why | Install |
|---|---|---|
| **ffmpeg** | MP3 conversion, trimming, merged video. **Without it, MP3 downloads fail** (`ffprobe and ffmpeg not found`) | `sudo apt install -y ffmpeg` |
| Python **3.10+** (3.11+ recommended) | App runtime; yt-dlp warns on 3.10 | `sudo apt install -y python3-venv python3-pip` |
| Nginx (optional) | Reverse proxy for HTTPS / domain | `sudo apt install -y nginx` |
| ~1 GB RAM, ~2 GB disk | Downloads + cache | — |

> 💡 Everything below already installs ffmpeg for you — it's only listed here so you know why it matters.

---

## Deploy on your VPS

**First, get the code onto the VPS** — the app lives in the `yt/` folder of the repo:

```bash
git clone https://github.com/ItzJoker2YT-city/yt-vid.git
cd yt-vid/yt        # ← the Flask app root
```

You now have 3 options, all proven. **Option 1 is the quickest.**

---

### Option 1 — One-command installer (RECOMMENDED, bare metal)

Works on Ubuntu 20.04+, Debian 11+, CentOS/AlmaLinux 8+. Installs ffmpeg + nginx + systemd service + firewall and health-checks the app:

```bash
cd yt-vid/yt
sudo ./scripts/install.sh
```

That's it. After it finishes:

```bash
systemctl status yt-mp3           # running?
journalctl -u yt-mp3 -f           # live logs
curl http://your-vps-ip           # web UI (via nginx on port 80)
```

What it sets up:
- App at `/opt/yt-mp3` (running as the `yt-mp3` user, waitress WSGI server)
- `.env` with production defaults (`HOST=0.0.0.0`, `PORT=5000`, `PROXY_FIX=1`, …)
- systemd service `yt-mp3` with `Restart=always`
- Nginx reverse proxy on port 80 → `127.0.0.1:5000`
- UFW firewall (HTTP + SSH)
- Downloads saved to `/music/YT-Downloads`

---

### Option 2 — Manual systemd setup

If you prefer to do it by hand (or the installer's distro isn't yours):

```bash
# 1. Dependencies (ffmpeg is the important one)
sudo apt update && sudo apt install -y python3-venv python3-pip ffmpeg nginx curl git

# 2. User + directories
sudo useradd -r -s /bin/false -d /opt/yt-mp3 yt-mp3
sudo mkdir -p /opt/yt-mp3 /music/YT-Downloads
sudo chown yt-mp3:yt-mp3 /opt/yt-mp3 /music/YT-Downloads

# 3. Copy the app (from the yt/ folder of your clone)
cd yt-vid/yt
sudo tar --exclude='.git' --exclude='data/app.log' -cf - . | sudo tar -C /opt/yt-mp3 -xf -
sudo chown -R yt-mp3:yt-mp3 /opt/yt-mp3

# 4. Python venv + deps
sudo -u yt-mp3 python3 -m venv /opt/yt-mp3/venv
sudo -u yt-mp3 /opt/yt-mp3/venv/bin/pip install -r /opt/yt-mp3/requirements.txt

# 5. Config
sudo tee /opt/yt-mp3/.env > /dev/null <<'EOF'
HOST=0.0.0.0
PORT=5000
DEBUG=False
LOG_LEVEL=INFO
DOWNLOAD_DIR=/music/YT-Downloads
DEFAULT_QUALITY=320
MAX_CONCURRENT=3
PROXY_FIX=1
EOF

# 6. systemd service
sudo tee /etc/systemd/system/yt-mp3.service > /dev/null <<'EOF'
[Unit]
Description=YT-MP3 Downloader
After=network.target

[Service]
Type=simple
User=yt-mp3
Group=yt-mp3
WorkingDirectory=/opt/yt-mp3
EnvironmentFile=/opt/yt-mp3/.env
ExecStart=/opt/yt-mp3/venv/bin/waitress-serve --host=0.0.0.0 --port=5000 --threads=8 app:app
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now yt-mp3

# 7. (Optional) Nginx on port 80
sudo tee /etc/nginx/sites-available/yt-mp3 > /dev/null <<'EOF'
server {
    listen 80;
    server_name _;
    client_max_body_size 50m;
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }
}
EOF
sudo ln -sf /etc/nginx/sites-available/yt-mp3 /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl restart nginx

# 8. (Optional) Firewall
sudo ufw allow 'Nginx Full'
sudo ufw allow OpenSSH
sudo ufw --force enable
```

---

### Option 3 — Docker Compose

The repo ships a production `Dockerfile` (Python 3.11 + **ffmpeg** + waitress) and `docker-compose.yml` with persistent volumes:

```bash
cd yt-vid/yt
sudo docker compose up -d --build
```

- Web UI: `http://your-vps-ip:5000` (change `PORT` in the shell env to use a different host port)
- Downloads + cache persist across restarts in the `yt-downloads` / `yt-data` volumes
- Optional Nginx/Cloudflare Tunnel in front of port 5000

---

### Add SSL (strongly recommended)

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
```

Or skip nginx entirely and use a **Cloudflare Tunnel** pointing to `http://127.0.0.1:5000` — keep `PROXY_FIX=1` either way (the app already handles proxy headers).

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address — use `0.0.0.0` on a VPS |
| `PORT` | `5000` | Listen port |
| `DEBUG` | `False` | `True` = Flask dev auto-reload (never in prod) |
| `DOWNLOAD_DIR` | `~/Music/YT-Downloads` | Where finished MP3/MP4 files are saved |
| `DEFAULT_QUALITY` | `320` | Default audio kbps (`128` / `192` / `320`) |
| `MAX_CONCURRENT` | `3` | Max simultaneous downloads |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `PROXY_FIX` | *(empty)* | Set to `1` when running behind Nginx/Cloudflare |

---

## 🇬🇭 How the Ghana Music feed works

No setup needed — it's fully automatic:

1. **On first start** the app scrapes the Halmblog.com Ghana Music listing in the background and builds a local SQLite cache (`data/ghana_music.db`). The feed may be empty until this first build finishes.
2. **Auto-update (live)** — every ~30–60 s the app re-checks page 1 and pins any newly posted songs to the top of the feed (the "auto-updating live" dot).
3. **Deep cache** — the **"➕ Load More Pages (Deep Cache)"** button crawls deeper into the archive in the background and grows the cache page-by-page (progress shown live, resume position persisted as `max_page`).
4. **Search and MP3 links** — search uses the cached archive for fast results. A background filler rotates through song pages to attach direct `.mp3` URLs over time. For songs without a link, **Find MP3** checks the source page; it reports when no direct file is available rather than silently falling back to YouTube.

> 🛡️ The scraper is hardened for Halmblog.com's bot protection (browser-like headers, no `Accept-Encoding: br`, automatic retry fallback) — scraping works from cloud/VPS IPs just like it does locally.

---

## Updating

```bash
# Bare metal
cd yt-vid/yt && git pull
sudo rsync -a --exclude='data/' --exclude='venv/' --exclude='.env' ./ /opt/yt-mp3/
sudo systemctl restart yt-mp3

# Docker
cd yt-vid/yt && git pull && sudo docker compose up -d --build
```

## Backup

The only state worth backing up (everything else is reproducible):

```bash
/opt/yt-mp3/data/          # download history + Ghana cache
/music/YT-Downloads/       # finished downloads
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ERROR: Postprocessing: ffprobe and ffmpeg not found` | `sudo apt install -y ffmpeg`, then `sudo systemctl restart yt-mp3` |
| `Sign in to confirm you're not a bot` on every YouTube download | Your server's IP is bot-flagged by YouTube (common on datacenter/VPS IPs). Drop a logged-in `cookies.txt` in the app folder — see [YouTube bot-check fix](#youtube-bot-check-fix) |
| App won't start / port busy | `sudo systemctl status yt-mp3` and `journalctl -u yt-mp3 -f`; change `PORT` in `.env` |
| Ghana feed empty on a fresh install | It self-builds in ~1–2 min — hit **🔄 Refresh**. If it stays empty, the VPS can't reach `halmblog.com` (firewall/egress) |
| Server not responding after install | `curl http://localhost:5000/api/settings` on the VPS; check `systemctl status yt-mp3` |
| Downloads run out of disk | Point `DOWNLOAD_DIR` at a bigger volume |

---

## YouTube bot-check fix

YouTube bot-checks datacenter/VPS IPs, so downloads may fail with
`Sign in to confirm you're not a bot`. PO-token providers alone do **not** clear
this (verified) — the reliable fix is a logged-in session via cookies.

> **Why cookies keep dying:** Google invalidates a logged-in session the moment
> it sees it used from a flagged datacenter IP (session-hijacking protection).
> So cookies work for a while, then "expire". The **permanent** fix is the proxy
> below — a clean/residential IP means Google never flags the session in the
> first place.

### Option 1 — Proxy rotation (permanent fix, recommended)

Route all YouTube traffic through clean/residential IPs. Cookies then stay
valid indefinitely because the requests come from a trusted IP.

Set the `YTDLP_PROXY` env var (comma-separated list) and restart the app:

```bash
# HTTP/S proxies (most providers) — comma-separated, tried in order:
export YTDLP_PROXY="http://user:password@proxy1.example.com:8080,http://proxy2.example.com:3128"
# SOCKS5 proxies also work:
export YTDLP_PROXY="socks5://user:password@proxy.example.com:1080"
sudo systemctl restart yt-mp3
```

The engine **round-robins** across the list and **fails over** to the next proxy
whenever a route is bot-flagged, unreachable, or drops the connection — so a
couple of flaky free proxies won't take the app down. The proxy applies to every
yt-dlp call (search, probe, album scan, download). Leave it empty to connect
directly.

### Option 2 — Cookies (works until the session gets invalidated)

1. Export your YouTube cookies in **Netscape format** (browser extension such as
   "Get cookies.txt LOCALLY"). **Use a throwaway account** — downloads may risk
   the account.
2. Save the file as `cookies.txt` in the app folder (`yt/cookies.txt`), or set
   the `YTDLP_COOKIES_FILE` env var to its path.
3. Restart the app: `sudo systemctl restart yt-mp3`.

The app picks the file up automatically on the next download — no code changes.
When the file is absent, downloads still run anonymously.

> Note: the datacenter-IP bot block cannot be beaten from the server's own IP —
> cookies and PO tokens only work until Google re-flags the account. Only the
> proxy (or moving the app to a residential connection) makes this permanent.

---

## Project Structure

```
yt/
├── app.py               # Flask routes & API
├── config.py            # Settings (env vars)
├── engine.py            # Download engine (yt-dlp)
├── halmblog.py          # Ghana Music scraper + cache
├── requirements.txt     # Python deps
├── Dockerfile           # Production image (ffmpeg included)
├── docker-compose.yml   # Production compose
├── scripts/install.sh   # One-command VPS installer
├── static/              # CSS + JS
├── templates/           # HTML
└── data/                # History + Ghana cache (persistent)
```

## Features

- ⬇️ **Download** — single URLs, batch, playlists; MP3 128–320 kbps or MP4 480p–1080p; trim start/end
- 🔍 **Search** — YouTube search, artist/album lookup, Ghana artist browser
- 🇬🇭 **Ghana Music** — periodically refreshed feed from Halmblog.com with fast archive search and deep cache
- 📥 **Queue** — pause/resume/cancel, download all as ZIP
- 📋 **History** — persistent download history with re-download
