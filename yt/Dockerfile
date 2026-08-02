# ═══════════════════════════════════════════════════════════════════════════
#  YT-MP3 — Production Docker image
#
#  Build:   docker build -t yt-mp3 .
#  Run:     docker compose up -d --build   (see docker-compose.yml)
# ═══════════════════════════════════════════════════════════════════════════

FROM python:3.11-slim

# ffmpeg is REQUIRED: MP3 conversion, trimming, and merged video downloads
# all depend on it. (yt-dlp raises "ffprobe and ffmpeg not found" without it.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

# Persistent state (download history + Ghana Music cache) and downloads dir
RUN mkdir -p /app/data /music/YT-Downloads

# Production defaults — override via environment / compose
ENV HOST=0.0.0.0 \
    PORT=5000 \
    DEBUG=False \
    LOG_LEVEL=INFO \
    DOWNLOAD_DIR=/music/YT-Downloads \
    DEFAULT_QUALITY=320 \
    MAX_CONCURRENT=3 \
    PROXY_FIX=1

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -sf http://127.0.0.1:5000/api/settings || exit 1

# Production WSGI server (same as the VPS systemd setup)
CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "--threads=8", "app:app"]
