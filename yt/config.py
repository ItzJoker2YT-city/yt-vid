"""
Application configuration module.
Centralizes all configurable settings for the YT-MP3 downloader.
Supports environment variable overrides for VPS deployment.
"""
import os

# ─── Base directory ──────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ─── Download directory ──────────────────────────────────────────────────────
DEFAULT_DOWNLOAD_DIR = os.environ.get(
    "DOWNLOAD_DIR", os.path.join(os.path.expanduser("~"), "Music", "YT-Downloads")
)

# ─── Database file for download history ──────────────────────────────────────
HISTORY_DB = os.path.join(BASE_DIR, "data", "history.json")

# ─── Supported audio quality options (kbps) ──────────────────────────────────
QUALITY_OPTIONS = {
    "128": "128",
    "192": "192",
    "320": "320",
}

# ─── Default audio quality ───────────────────────────────────────────────────
DEFAULT_QUALITY = os.environ.get("DEFAULT_QUALITY", "320")

# ─── Flask server settings ───────────────────────────────────────────────────
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "5000"))
DEBUG = os.environ.get("DEBUG", "False").lower() in ("true", "1", "yes")

# ─── yt-dlp base options ─────────────────────────────────────────────────────
YTDLP_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
    "socket_timeout": 30,
    "nocheckcertificate": True,
    "ignoreerrors": True,
    "no_color": True,
    "geo_bypass": True,
}

# ─── Maximum concurrent downloads ────────────────────────────────────────────
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT", "3"))

# ─── Logging configuration ───────────────────────────────────────────────────
LOG_FILE = os.path.join(BASE_DIR, "data", "app.log")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# ─── Reverse proxy support (Nginx / Cloudflare / Caddy) ──────────────────────
ENABLE_PROXY_FIX = os.environ.get("PROXY_FIX", "").lower() in ("true", "1", "yes")
