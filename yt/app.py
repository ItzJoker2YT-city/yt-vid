"""
Flask web application — routes and API endpoints for the YT-MP3 downloader.
"""
import os
import json
import logging
import zipfile
import tempfile
import mimetypes
import threading
from logging.handlers import RotatingFileHandler

from flask import Flask, render_template, request, jsonify, send_file, Response

import config
import engine

# ─── App Setup ────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.urandom(24)

# Ensure data directory exists
os.makedirs(os.path.dirname(config.LOG_FILE), exist_ok=True)
os.makedirs(config.DEFAULT_DOWNLOAD_DIR, exist_ok=True)

# Logging
handler = RotatingFileHandler(config.LOG_FILE, maxBytes=5_000_000, backupCount=3)
handler.setLevel(getattr(logging, config.LOG_LEVEL))
formatter = logging.Formatter("[%(asctime)s] %(levelname)s in %(module)s: %(message)s")
handler.setFormatter(formatter)
app.logger.addHandler(handler)
logging.getLogger("engine").addHandler(handler)

# ─── Cache Pre-warming ────────────────────────────────────────────────────────
# Pre-warm search cache for top trending artists in the background so the
# first user click hits the cache instead of waiting for yt-dlp.
_TRENDING_QUERIES = [
    f"{artist} 2026 latest song"
    for artist in [
        "Black Sherif", "Sarkodie", "Stonebwoy", "KiDi", "King Promise",
        "Shatta Wale", "Kuami Eugene", "Gyakie", "Medikal", "Kelvyn Boy",
    ]
]

def _start_prewarm():
    import time
    time.sleep(3)   # let the server finish starting before hitting the network
    engine.prewarm_cache(_TRENDING_QUERIES)

threading.Thread(target=_start_prewarm, daemon=True, name="cache-prewarm").start()




# ─── Page Routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Main page — the downloader UI."""
    return render_template("index.html", default_quality=config.DEFAULT_QUALITY)


# ─── API Routes ───────────────────────────────────────────────────────────────

@app.route("/api/download", methods=["POST"])
def api_download():
    """Start a download for one or more URLs. Handles single videos and playlists."""
    data = request.get_json(silent=True) or {}
    urls = data.get("urls", [])
    quality = data.get("quality", config.DEFAULT_QUALITY)
    output_dir = data.get("output_dir", config.DEFAULT_DOWNLOAD_DIR)
    trim_start = data.get("trim_start", None)
    trim_end = data.get("trim_end", None)
    dl_type = data.get("dl_type", "audio")

    if not urls:
        return jsonify({"error": "No URLs provided"}), 400

    if dl_type not in ["audio", "video"]:
        dl_type = "audio"

    if dl_type == "video":
        if quality not in ["1080p", "720p", "480p", "best"]:
            quality = "best"
    else:
        if quality not in config.QUALITY_OPTIONS:
            quality = config.DEFAULT_QUALITY

    all_tasks = []
    for url in urls:
        url = url.strip()
        if not url:
            continue
        # start_download always returns a list
        # (one item for single video, N items for a playlist)
        task_list = engine.start_download(url, output_dir, quality, trim_start, trim_end, dl_type=dl_type)
        all_tasks.extend([t.to_dict() for t in task_list])

    return jsonify({"tasks": all_tasks})


@app.route("/api/playlist-info", methods=["POST"])
def api_playlist_info():
    """
    Probe a URL and return playlist metadata (title + track count)
    without starting a download. Used by the frontend to show a
    confirmation before queuing potentially hundreds of tracks.
    """
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    info = engine.probe_url(url)
    return jsonify(info)


@app.route("/api/status")
def api_status():
    """Get the status of all active downloads."""
    return jsonify({"tasks": engine.get_all_tasks()})


@app.route("/api/task/<task_id>")
def api_task_status(task_id):
    """Get status of a single task."""
    task = engine.get_task(task_id)
    if task:
        return jsonify(task.to_dict())
    return jsonify({"error": "Task not found"}), 404


@app.route("/api/pause/<task_id>", methods=["POST"])
def api_pause(task_id):
    """Pause a download."""
    if engine.pause_task(task_id):
        return jsonify({"success": True})
    return jsonify({"error": "Cannot pause this task"}), 400


@app.route("/api/resume/<task_id>", methods=["POST"])
def api_resume(task_id):
    """Resume a paused download."""
    if engine.resume_task(task_id):
        return jsonify({"success": True})
    return jsonify({"error": "Cannot resume this task"}), 400


@app.route("/api/cancel/<task_id>", methods=["POST"])
def api_cancel(task_id):
    """Cancel a download."""
    if engine.cancel_task(task_id):
        return jsonify({"success": True})
    return jsonify({"error": "Cannot cancel this task"}), 400


@app.route("/api/remove/<task_id>", methods=["POST"])
def api_remove(task_id):
    """Remove a finished task from the list."""
    if engine.remove_task(task_id):
        return jsonify({"success": True})
    return jsonify({"error": "Task not found"}), 404


@app.route("/api/search", methods=["POST"])
def api_search():
    """Search YouTube for videos."""
    data = request.get_json(silent=True) or {}
    query = data.get("query", "").strip()
    if not query:
        return jsonify({"error": "No search query provided"}), 400

    results = engine.search_youtube(query, max_results=12)
    return jsonify({"results": results})


@app.route("/api/artist-albums", methods=["POST"])
def api_artist_albums():
    """Search YouTube for an artist's albums and playlists."""
    data = request.get_json(silent=True) or {}
    artist = data.get("artist", "").strip()
    if not artist:
        return jsonify({"error": "No artist provided"}), 400
    albums = engine.search_artist_albums(artist)
    return jsonify({"albums": albums})


@app.route("/api/history")
def api_history():
    """Get download history."""
    return jsonify({"history": engine.get_history()})


@app.route("/api/history-ids")
def api_history_ids():
    """Return lightweight set of video IDs + URLs already in history.
    Used by the frontend to show 'Already Downloaded' badges on search results.
    """
    history = engine.get_history()
    ids = set()
    urls = set()
    for item in history:
        if item.get("id"):
            ids.add(item["id"])
        if item.get("url"):
            urls.add(item["url"])
    return jsonify({"ids": list(ids), "urls": list(urls)})


@app.route("/api/history/clear", methods=["POST"])
def api_clear_history():
    """Clear download history."""
    engine.clear_history()
    return jsonify({"success": True})


@app.route("/api/settings")
def api_settings():
    """Return current settings/defaults."""
    return jsonify({
        "default_quality": config.DEFAULT_QUALITY,
        "download_dir": config.DEFAULT_DOWNLOAD_DIR,
        "quality_options": list(config.QUALITY_OPTIONS.keys()),
    })


def stream_and_delete(file_path, original_tasks=None, task_ref=None):
    """
    Generator that streams a file in chunks and then deletes it from disk.
    If original_tasks is provided, it also deletes the source files (used for ZIPs).
    If task_ref is provided, clears task.filepath after streaming so the UI
    updates correctly (only after the file has actually been served).
    """
    chunk_size = 65536
    try:
        with open(file_path, 'rb') as f:
            while True:
                data = f.read(chunk_size)
                if not data:
                    break
                yield data
    finally:
        # Delete the streamed file (e.g. MP3/MP4 or the temp ZIP)
        try:
            os.remove(file_path)
            app.logger.info(f"Deleted file after serving: {file_path}")
        except Exception as e:
            app.logger.error(f"Failed to delete {file_path}: {e}")

        # Clear the task filepath so the UI knows the file is gone
        if task_ref is not None:
            task_ref.filepath = ""

        # If this was a ZIP stream, clean up the original downloaded files too
        if original_tasks:
            for t in original_tasks:
                if getattr(t, 'filepath', None) and os.path.exists(t.filepath):
                    try:
                        os.remove(t.filepath)
                        t.filepath = ""  # Update state so UI knows it's gone
                    except Exception as e:
                        app.logger.error(f"Failed to delete original file {t.filepath}: {e}")

@app.route("/api/download-file/<task_id>")
def api_download_file(task_id):
    """Serve a specific downloaded MP3/MP4 file, then delete it from the host."""
    task = engine.get_task(task_id)
    if not task:
        # Check history if not in active tasks
        history = engine.get_history()
        history_entry = next((item for item in history if item["id"] == task_id), None)
        if history_entry and os.path.exists(history_entry["filepath"]):
            mime_type, _ = mimetypes.guess_type(history_entry["filepath"])
            return Response(stream_and_delete(history_entry["filepath"]), headers={
                "Content-Disposition": f"attachment; filename=\"{history_entry['filename']}\"",
                "Content-Type": mime_type or "application/octet-stream"
            })
        return jsonify({"error": "File not found"}), 404

    if not task.filepath or not os.path.exists(task.filepath):
        return jsonify({"error": "File not found on disk"}), 404

    mime_type, _ = mimetypes.guess_type(task.filepath)
    # Pass task_ref so filepath is cleared AFTER streaming completes,
    # not before — this prevents the Save button from vanishing prematurely.
    response = Response(stream_and_delete(task.filepath, task_ref=task), headers={
        "Content-Disposition": f"attachment; filename=\"{task.filename}\"",
        "Content-Type": mime_type or "application/octet-stream"
    })
    return response


@app.route("/api/download-playlist/<playlist_id>")
def api_download_playlist(playlist_id):
    """Generate and serve a ZIP file of all completed tracks in a playlist."""
    tasks = [t for t in engine.active_downloads.values() if t.playlist_id == playlist_id and t.status == "done"]

    if not tasks:
        return jsonify({"error": "No completed tracks found for this playlist"}), 404

    playlist_title = tasks[0].playlist_title or "playlist"
    return _generate_zip(tasks, f"{playlist_title}_tracks")


@app.route("/api/download-all")
def api_download_all():
    """Generate and serve a ZIP file of ALL completed tracks in the queue."""
    tasks = [t for t in engine.active_downloads.values() if t.status == "done"]

    if not tasks:
        return jsonify({"error": "No completed tracks found in queue"}), 404

    return _generate_zip(tasks, "all_downloads")


def _generate_zip(tasks, base_filename):
    """Helper to generate and return a ZIP response for a list of tasks."""
    safe_title = "".join(c for c in base_filename if c.isalnum() or c in (" ", "-", "_")).strip()
    if not safe_title: safe_title = "downloads"

    temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    temp_zip_path = temp_zip.name
    temp_zip.close()

    try:
        with zipfile.ZipFile(temp_zip_path, "w") as zf:
            for task in tasks:
                if task.filepath and os.path.exists(task.filepath):
                    # Add to ZIP, use filename or title as the name inside zip
                    arcname = task.filename or f"{task.title}.mp3"
                    zf.write(task.filepath, arcname)

        return Response(stream_and_delete(temp_zip_path, original_tasks=tasks), headers={
            "Content-Disposition": f"attachment; filename=\"{safe_title}.zip\"",
            "Content-Type": "application/zip"
        })
    except Exception as e:
        app.logger.error(f"Error creating ZIP: {e}")
        return jsonify({"error": "Failed to create ZIP archive"}), 500


from werkzeug.middleware.proxy_fix import ProxyFix

# Trust headers from Cloudflare / Reverse Proxies (important for HTTPS routing)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        from waitress import serve
        print(f"\n  [YT-MP3] Production WSGI server (Waitress) running at http://{config.HOST}:{config.PORT}")
        print("           (ProxyFix enabled for Cloudflare Zero Trust)\n")
        serve(app, host=config.HOST, port=config.PORT)
    except ImportError:
        print(f"\n  [YT-MP3] Dev Downloader running at http://{config.HOST}:{config.PORT}\n")
        app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)

