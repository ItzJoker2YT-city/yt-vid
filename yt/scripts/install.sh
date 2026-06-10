#!/bin/bash
# ═════════════════════════════════════════════════════════════════════════════
#  YT-MP3 Downloader — VPS Installation Script
#
#  Usage:
#    chmod +x install.sh
#    sudo ./install.sh
#
#  Supports: Ubuntu 20.04+, Debian 11+, CentOS 8+, AlmaLinux 8+
# ═════════════════════════════════════════════════════════════════════════════

set -euo pipefail

APP_DIR="/opt/yt-mp3"
DOWNLOAD_DIR="/music/YT-Downloads"
USER="yt-mp3"
SERVICE_FILE="/etc/systemd/system/yt-mp3.service"
PYTHON="python3"

# ─── 0. Root check ───────────────────────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    echo "❌ Run as root: sudo ./install.sh"
    exit 1
fi

# ─── 1. Detect OS ────────────────────────────────────────────────────────────
echo "🔧 Detecting OS..."
if command -v apt-get &> /dev/null; then
    PKG=apt
    apt-get update
    apt-get install -y python3-pip python3-venv ffmpeg curl git nginx
elif command -v dnf &> /dev/null; then
    PKG=dnf
    dnf install -y python3-pip ffmpeg curl git nginx
elif command -v yum &> /dev/null; then
    PKG=yum
    yum install -y python3-pip ffmpeg curl git nginx
else
    echo "❌ Unsupported distro. Install python3, pip, ffmpeg, curl, git, nginx manually."
    exit 1
fi

# ─── 2. Create user ──────────────────────────────────────────────────────────
if ! id "$USER" &>/dev/null; then
    useradd -r -m -s /bin/false -d "$APP_DIR" "$USER"
fi

# ─── 3. Prepare directories ──────────────────────────────────────────────────
mkdir -p "$APP_DIR" "$DOWNLOAD_DIR"
chown "$USER:$USER" "$APP_DIR" "$DOWNLOAD_DIR"

# ─── 4. Copy app code ────────────────────────────────────────────────────────
echo "📦 Copying application files..."
rm -rf "$APP_DIR"/*
cp -r ./* "$APP_DIR/" || true
chown -R "$USER:$USER" "$APP_DIR"

# ─── 5. Python venv ──────────────────────────────────────────────────────────
echo "🐍 Creating virtual environment..."
if [ ! -d "$APP_DIR/venv" ]; then
    sudo -u "$USER" $PYTHON -m venv "$APP_DIR/venv"
fi
sudo -u "$USER" "$APP_DIR/venv/bin/pip" install --upgrade pip
sudo -u "$USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# ─── 6. Create .env ──────────────────────────────────────────────────────────
if [ ! -f "$APP_DIR/.env" ]; then
    cat > "$APP_DIR/.env" <<EOF
HOST=0.0.0.0
PORT=5000
DEBUG=False
LOG_LEVEL=INFO
DOWNLOAD_DIR=$DOWNLOAD_DIR
DEFAULT_QUALITY=320
MAX_CONCURRENT=50
PROXY_FIX=1
EOF
    chown "$USER:$USER" "$APP_DIR/.env"
fi

# ─── 7. systemd service ──────────────────────────────────────────────────────
echo "⚙️  Creating systemd service..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=YT-MP3 Downloader
After=network.target

[Service]
Type=simple
User=$USER
Group=$USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/waitress-serve --host=0.0.0.0 --port=5000 --threads=8 app:app
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal
SyslogIdentifier=yt-mp3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable yt-mp3

# ─── 8. Nginx reverse proxy ──────────────────────────────────────────────────
echo "🌐 Setting up Nginx..."
cat > /etc/nginx/sites-available/yt-mp3 <<'EOF'
server {
    listen 80;
    server_name _;  # accept any host

    client_max_body_size 50m;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }

    location /static/ {
        alias /opt/yt-mp3/static/;
        expires 1d;
        access_log off;
    }
}
EOF

if [ -d /etc/nginx/sites-enabled ] && [ ! -L /etc/nginx/sites-enabled/yt-mp3 ]; then
    ln -sf /etc/nginx/sites-available/yt-mp3 /etc/nginx/sites-enabled/yt-mp3
    rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
fi

nginx -t && systemctl restart nginx

# ─── 9. UFW firewall ─────────────────────────────────────────────────────────
if command -v ufw &>/dev/null; then
    ufw allow 'Nginx Full' 2>/dev/null || true
    ufw allow OpenSSH
    ufw --force enable
fi

# ─── 10. Start the app ───────────────────────────────────────────────────────
echo "🚀 Starting YT-MP3..."
systemctl restart yt-mp3

# ─── Done ────────────────────────────────────────────────────────────────────
echo ""
echo "✅ YT-MP3 installed successfully!"
echo ""
echo "   Service:  systemctl status yt-mp3"
echo "   Logs:     journalctl -u yt-mp3 -f"
echo "   Web UI:   http://$(curl -s ifconfig.me)"
echo "   Downloads: $DOWNLOAD_DIR"
echo ""
echo "   To add a domain + SSL with Certbot:"
echo "      sudo apt install certbot python3-certbot-nginx"
echo "      sudo certbot --nginx -d yourdomain.com"
echo ""
