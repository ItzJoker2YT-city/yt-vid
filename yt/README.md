# 🎵 YT-MP3 — YouTube to MP3 Downloader

A fast, multi-feature downloader for DJs and music lovers. Download YouTube audio and video, browse Ghanaian music from Halmblog.com, search by artist, and manage downloads with a clean web UI.

---

## Quick Start

```bash
pip install -r requirements.txt
python app.py          # Open http://127.0.0.1:5000
```

---

## VPS Deployment

### Option 1 — Docker Compose (RECOMMENDED)

```bash
git clone <repo>
cd yt-mp3
cp .env.example .env
nano .env              # Edit if needed

sudo docker compose up -d --build
```
Server runs on port `5000`. Add a reverse proxy (Nginx / Caddy / Cloudflare Tunnel) for HTTPS.

### Option 2 — One-Command Installer (Bare Metal)

```bash
scp -r . root@your-vps-ip:/tmp/yt-mp3
ssh root@your-vps-ip
cd /tmp/yt-mp3
chmod +x scripts/install.sh
sudo ./scripts/install.sh
```
Done! Nginx reverse proxy, systemd service, firewall, and auto-start all set up.

### Option 3 — Manual Install

On any Ubuntu/Debian VPS:

```bash
# 1. Dependencies
sudo apt update && sudo apt install -y python3-venv ffmpeg nginx git curl

# 2. Create user + dirs
sudo useradd -r -s /bin/false -d /opt/yt-mp3 yt-mp3
sudo mkdir -p /opt/yt-mp3 /music/YT-Downloads
sudo chown yt-mp3:yt-mp3 /opt/yt-mp3 /music/YT-Downloads

# 3. Copy code
sudo cp -r . /opt/yt-mp3/

# 4. Python venv
sudo -u yt-mp3 python3 -m venv /opt/yt-mp3/venv
sudo -u yt-mp3 /opt/yt-mp3/venv/bin/pip install -r /opt/yt-mp3/requirements.txt

# 5. Environment
sudo tee /opt/yt-mp3/.env > /dev/null <<'EOF'
HOST=0.0.0.0
PORT=5000
DEBUG=False
LOG_LEVEL=INFO
DOWNLOAD_DIR=/music/YT-Downloads
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
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable yt-mp3
sudo systemctl start yt-mp3

# 7. Nginx reverse proxy
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

# 8. Firewall
sudo ufw allow 'Nginx Full' 2>/dev/null || true
sudo ufw allow OpenSSH
sudo ufw --force enable
```

### Add SSL with Certbot (Strongly Recommended)

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` on VPS |
| `PORT` | `5000` | Port to listen on |
| `DEBUG` | `False` | `True` = dev auto-reload |
| `DOWNLOAD_DIR` | `~/Music/YT-Downloads` | Where MP3s get saved |
| `DEFAULT_QUALITY` | `320` | Default audio kbps |
| `MAX_CONCURRENT` | `3` | How many downloads at once |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `PROXY_FIX` | `` | Set to `1` behind Nginx/Cloudflare |

---

## Features

- ⬇️ **Download** — Paste YouTube URLs, pick MP3 (128–320kbps) or MP4 (480p–1080p)
- 🔍 **Search** — Search YouTube, browse 120+ Ghana artists, find albums
- 🇬🇭 **Ghana Music** — Live feed from Halmblog.com with auto-scraper
- 📋 **History** — Track what you downloaded, re-download instantly
- 🎙️ (REMOVED) ~~Identify~~ — Song recognition via AcoustID

---

## Project Structure

```
yt-mp3/
├── app.py               # Flask routes & API
├── config.py            # Settings (env vars)
├── engine.py            # Download engine (yt-dlp)
├── halmblog.py          # Ghana music scraper
├── requirements.txt     # Python deps
├── Dockerfile           # Docker image
├── docker-compose.yml   # Production compose
├── .env.example         # Env template
├── scripts/install.sh   # One-click VPS installer
├── static/              # CSS + JS
├── templates/           # HTML
└── data/                # History + cache
```
