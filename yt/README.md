# 🎵 YT-MP3 — YouTube to MP3 Downloader

A sleek, local web application that downloads YouTube audio as high-quality MP3 files. Built with **Flask** + **yt-dlp** + **ffmpeg**.

![Python](https://img.shields.io/badge/Python-3.9+-blue) ![Flask](https://img.shields.io/badge/Flask-3.0-green) ![License](https://img.shields.io/badge/License-MIT-yellow)

---

## ✨ Features

| Feature | Description |
|---|---|
| 🎵 **Audio (MP3)** | Convert YouTube videos to MP3 at 128/192/320 kbps |
| 🎥 **Video (MP4)** | Download high-quality MP4 videos (1080p/720p/480p) |
| 📦 **ZIP Export** | Download entire playlists or batches as a single ZIP file |
| 🧹 **Auto-Cleanup** | Securely streams files to clients and auto-deletes from the host disk |
| 📋 **Batch Download** | Paste multiple URLs at once (one per line) |
| 📂 **Playlist Support** | Download entire YouTube playlists with individual track tracking |
| 🔍 **YouTube Search** | Search YouTube directly within the app |
| ✂️ **Audio Trimming** | Set start/end times to trim the audio |
| 🏷️ **Auto Metadata** | Title, artist, and album art embedded automatically |
| ⏸️ **Pause / Resume** | Pause and resume downloads mid-stream |
| 📊 **Real-time Progress** | Live progress bars, speed, and ETA display |
| 📜 **Download History** | Tracks all past downloads with re-download option |
| ⚠️ **Error Handling** | Clear feedback for invalid URLs, network issues, geo-blocks, etc. |

---

## 🚀 Quick Start

### Prerequisites

1. **Python 3.9+** — [Download Python](https://www.python.org/downloads/)
2. **ffmpeg** — Required for audio conversion

   **Windows (via winget):**
   ```
   winget install ffmpeg
   ```
   **Windows (manual):** Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to PATH.

   **macOS:**
   ```bash
   brew install ffmpeg
   ```

   **Linux:**
   ```bash
   sudo apt install ffmpeg
   ```

### Installation

```bash
# Navigate to the project directory
cd "d:\jokers suff\code\yt"

# Install Python dependencies
pip install -r requirements.txt

# Run the application
python app.py
```

### Open in Browser

Visit **http://127.0.0.1:5000** — that's it!

---

## 🌍 VPS & Cloudflare Deployment (Public Use)

This app is production-ready for public web hosting via Waitress. It features instant auto-deletion of served files, meaning it will never fill up your server's hard drive.

### 1. Cloudflare Zero Trust Setup
If you are exposing this to the public internet using a Cloudflare Tunnel:
* In your Cloudflare Zero Trust dashboard, set the Service Type to **`HTTP`** (not HTTPS).
* Set the URL to **`127.0.0.1:5000`**.
* *Cloudflare provides the external SSL (HTTPS), while communicating with Waitress locally over HTTP. The app includes `ProxyFix` to correctly handle Cloudflare's IP headers.*

### 2. Run as a Systemd Service (Linux 24/7)
To keep the app running permanently:

1. `sudo nano /etc/systemd/system/yt-downloader.service`
2. Paste the following (update `/path/to/...` with your actual directory):
```ini
[Unit]
Description=YouTube Downloader Web App
After=network.target

[Service]
User=root
WorkingDirectory=/path/to/yt-downloader
Environment="PATH=/path/to/yt-downloader/venv/bin"
ExecStart=/path/to/yt-downloader/venv/bin/python app.py
Restart=always

[Install]
WantedBy=multi-user.target
```
3. Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable yt-downloader
sudo systemctl start yt-downloader
```

---

## 📖 Usage Guide

### Downloading Media

1. Open the app in your browser
2. Paste a YouTube URL into the text area
3. Select **Format** (Audio or Video) and **Quality**
4. *(Optional)* Set trim start/end times (e.g., `0:30` to `3:45`)
5. Click **Download**
6. Watch the progress in real-time. Once done, click **💾 Save** to download the file to your device.

### Batch Download

Paste multiple URLs, one per line:
```
https://www.youtube.com/watch?v=VIDEO_ID_1
https://www.youtube.com/watch?v=VIDEO_ID_2
https://www.youtube.com/watch?v=VIDEO_ID_3
```

### Playlist Download

Simply paste a playlist URL — the app detects it automatically:
```
https://www.youtube.com/playlist?list=PLrAXtmEr...
```

### Searching YouTube

1. Click the **🔍 Search** tab
2. Type your query and press Enter
3. Browse results and click **Download MP3** on any result

---

## 📁 Project Structure

```
yt/
├── app.py              # Flask web server & API routes
├── engine.py           # Download engine (yt-dlp integration)
├── config.py           # Application configuration
├── requirements.txt    # Python dependencies
├── README.md           # This file
├── data/               # Runtime data (history, logs)
├── static/
│   ├── css/style.css   # UI styles
│   └── js/app.js       # Frontend logic
└── templates/
    └── index.html      # Main page template
```

---

## ⚙️ Configuration

Edit `config.py` to customize:

| Setting | Default | Description |
|---|---|---|
| `DEFAULT_DOWNLOAD_DIR` | `~/Music/YT-Downloads` | Where MP3s are saved |
| `DEFAULT_QUALITY` | `192` | Default bitrate (kbps) |
| `PORT` | `5000` | Web server port |
| `MAX_CONCURRENT_DOWNLOADS` | `3` | Max parallel downloads |

---

## 🛠️ Tech Stack

- **Backend:** Python, Flask, yt-dlp
- **Audio:** ffmpeg, mutagen (metadata)
- **Frontend:** Vanilla HTML/CSS/JS with glassmorphic dark theme
- **Storage:** JSON file for download history

---

## ⚠️ Legal Disclaimer

This tool is intended for downloading content you have the right to download. Respect copyright laws and YouTube's Terms of Service. The developers are not responsible for any misuse.
