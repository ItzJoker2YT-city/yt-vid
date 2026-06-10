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
import time
from logging.handlers import RotatingFileHandler

from flask import Flask, render_template, request, jsonify, send_file, Response

import config
import engine
from halmblog import (
    get_ghana_songs_cached, search_cached_songs, super_search, get_cached_songs,
    get_total_pages, get_total_songs,
    start_background_updater, build_cache, fill_missing_mp3s, resume_deep_cache,
)

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

# ─── Ghana Music Auto-Update ──────────────────────────────────────────────────
def _start_ghana_cache():
    import time
    time.sleep(2)
    try:
        # Fast build: listing only (1-2 HTTP requests)
        build_cache(max_pages=3)
    except Exception as e:
        app.logger.warning("Initial Ghana music cache build failed: %s", e)

    # 1. Page-1 rapid updater: checks every 60s for NEWLY posted songs on page 1
    def _rapid_updater():
        from halmblog import check_for_updates
        backoff = 60.0   # start at 60s
        max_backoff = 600.0
        last_run_new = True
        while True:
            try:
                time.sleep(backoff)
                added = check_for_updates()  # checks page 1 only — finds new posts
                if added > 0:
                    last_run_new = True
                    backoff = max(30.0, backoff * 0.5)   # got new songs → check faster!
                    app.logger.info("Rapid updater: +%d NEW listings (next check in %.0fs)", added, backoff)
                else:
                    if last_run_new:
                        backoff = 60.0
                    else:
                        backoff = min(max_backoff, backoff * 1.15)  # nothing new → back off
                    last_run_new = False
            except Exception as e:
                app.logger.debug("Rapid updater error: %s", e)
                time.sleep(120)
    threading.Thread(target=_rapid_updater, daemon=True, name="ghana-rapid-updater").start()

    # 2. Old periodic updater (30-min) keeps checking in case we miss anything
    start_background_updater()

    # 3. Background MP3 filler (fills missing MP3s over time)
    def _mp3_filler():
        while True:
            try:
                time.sleep(120)   # every 2 minutes, fill 15 missing MP3s
                fill_missing_mp3s(limit=15)
                time.sleep(1800)  # then every 30 minutes
            except Exception as e:
                app.logger.debug("MP3 filler error: %s", e)
    threading.Thread(target=_mp3_filler, daemon=True, name="ghana-mp3-filler").start()

    # 4. Deep cache builder: constantly crawling new pages in background
    def _deep_auto_crawler():
        while True:
            try:
                time.sleep(300)   # every 5 minutes, try to extend cache
                resume_deep_cache(max_pages=100)
                time.sleep(1800)  # then rest 30 min
            except Exception as e:
                app.logger.debug("Deep crawler error: %s", e)
    threading.Thread(target=_deep_auto_crawler, daemon=True, name="ghana-deep-crawler").start()

threading.Thread(target=_start_ghana_cache, daemon=True, name="ghana-cache-build").start()




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


@app.route("/api/ghana-music")
def api_ghana_music():
    """Return Ghana Music songs from local cache (fast, no external APIs).
    On first call (empty cache) triggers a build; afterwards serves from JSON file.
    """
    try:
        page = request.args.get("page", 1, type=int)
        limit = request.args.get("limit", 20, type=int)
        force = request.args.get("force", "").lower() == "true"
        songs = get_ghana_songs_cached(page=page, limit=limit, force_raw=force)
        return jsonify({"songs": songs})
    except Exception as e:
        app.logger.error("Ghana music cache failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/ghana-music/info")
def api_ghana_music_info():
    """Return total pages and total songs count for pagination."""
    try:
        return jsonify({
            "total_songs": get_total_songs(),
            "total_pages": get_total_pages(),
            "per_page": 20,
        })
    except Exception as e:
        app.logger.error("Ghana music info failed: %s", e)
        return jsonify({"error": str(e)}), 500


# Temporary in-memory cache for paginated search results
_search_result_cache = {}
_SEARCH_CACHE_TTL = 600  # 10 minutes

@app.route("/api/ghana-music/search", methods=["POST"])
def api_ghana_music_search():
    """Advanced search: cache first, then scrape pages on-the-fly if needed.
    Supports pagination via page/limit query params."""
    data = request.get_json(silent=True) or {}
    query = data.get("query", "").strip()
    page = request.args.get("page", 1, type=int)
    limit = request.args.get("limit", 20, type=int)

    if not query:
        return jsonify({"songs": get_cached_songs(), "total_results": len(get_cached_songs()), "query": ""})

    cache_key = query.lower().strip()

    try:
        # Use super_search for blazing speed (80 workers)
        results = super_search(query)
        _search_result_cache[cache_key] = {"results": results, "ts": __import__("time").time()}

        start = (page - 1) * limit
        page_results = results[start : start + limit]

        return jsonify({
            "songs": [
                {
                    "title": s.get("title", ""),
                    "artist": s.get("artist", ""),
                    "page_url": s.get("page_url", ""),
                    "thumbnail": s.get("thumbnail", ""),
                    "date": s.get("date", ""),
                    "mp3_url": s.get("mp3_url"),
                    "has_mp3": bool(s.get("mp3_url")),
                }
                for s in page_results
            ],
            "total_results": len(results),
            "query": query,
            "page": page,
            "limit": limit,
        })
    except Exception as e:
        app.logger.error("Ghana music search failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/ghana-music/search-next", methods=["POST"])
def api_ghana_music_search_next():
    """Return the next page of a previously-run search (fast, from memory)."""
    data = request.get_json(silent=True) or {}
    query = data.get("query", "").strip()
    page = data.get("page", 1)
    limit = data.get("limit", 20)
    if not query:
        return jsonify({"songs": [], "total_results": 0})

    cache_key = query.lower().strip()
    cached = _search_result_cache.get(cache_key)
    if not cached or (__import__("time").time() - cached["ts"]) > _SEARCH_CACHE_TTL:
        # Stale or missing — fall back to re-searching
        try:
            results = super_search(query)
            _search_result_cache[cache_key] = {"results": results, "ts": __import__("time").time()}
        except Exception as e:
            app.logger.error("Ghana music search-next failed: %s", e)
            return jsonify({"error": str(e)}), 500
    else:
        results = cached["results"]

    start = (page - 1) * limit
    page_results = results[start : start + limit]
    return jsonify({
        "songs": [
            {
                "title": s.get("title", ""),
                "artist": s.get("artist", ""),
                "page_url": s.get("page_url", ""),
                "thumbnail": s.get("thumbnail", ""),
                "date": s.get("date", ""),
                "mp3_url": s.get("mp3_url"),
                "has_mp3": bool(s.get("mp3_url")),
            }
            for s in page_results
        ],
        "total_results": len(results),
        "query": query,
        "page": page,
        "limit": limit,
    })


@app.route("/api/ghana-music/deep-cache", methods=["POST"])
def api_ghana_music_deep_cache():
    """Trigger deep cache build/resume in the background."""
    data = request.get_json(silent=True) or {}
    max_pages = min(data.get("max_pages", 50), 200)
    threading.Thread(
        target=resume_deep_cache,
        args=(max_pages,),
        daemon=True,
        name="ghana-deep-cache",
    ).start()
    return jsonify({
        "success": True,
        "message": f"Deep cache building started (up to {max_pages} pages)"
    })


@app.route("/api/ghana-music/detail", methods=["POST"])
def api_ghana_music_detail():
    """Scrape a specific Halmblog song page for MP3 link."""
    data = request.get_json(silent=True) or {}
    page_url = data.get("url", "").strip()
    if not page_url:
        return jsonify({"error": "No URL provided"}), 400
    try:
        from halmblog import scrape_song_page
        detail = scrape_song_page(page_url)
        return jsonify(detail)
    except Exception as e:
        app.logger.error("Ghana music detail scrape failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/ghana-music/download", methods=["POST"])
def api_ghana_music_download():
    """
    Start a direct MP3 download from a Halmblog.com song page.
    Body: { mp3_url, title, artist, thumbnail } — all extracted by the frontend.
    """
    data = request.get_json(silent=True) or {}
    mp3_url = data.get("mp3_url", "").strip()
    title = data.get("title", "").strip()
    artist = data.get("artist", "").strip()
    thumbnail = data.get("thumbnail", "").strip()
    quality = data.get("quality", config.DEFAULT_QUALITY)
    output_dir = data.get("output_dir", config.DEFAULT_DOWNLOAD_DIR)

    if not mp3_url:
        return jsonify({"error": "No MP3 URL provided"}), 400

    try:
        tasks = engine.start_direct_download(
            mp3_url, title=title, artist=artist, thumbnail=thumbnail,
            output_dir=output_dir, quality=quality,
        )
        if tasks and len(tasks) > 0:
            return jsonify({
                "success": True,
                "task_id": tasks[0].id,
                "tasks": [t.to_dict() for t in tasks]
            })
        return jsonify({"error": "Failed to queue download"}), 500
    except Exception as e:
        app.logger.error("Ghana music direct download failed: %s", e)
        return jsonify({"error": str(e)}), 500


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


# ─── Streaming helpers start ---------------------------------------------------

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
    return _generate_zip(tasks, playlist_title)


@app.route("/api/download-all")
def api_download_all():
    """Generate and serve a ZIP file of ALL completed tracks in the queue."""
    tasks = [t for t in engine.active_downloads.values() if t.status == "done"]

    if not tasks:
        return jsonify({"error": "No completed tracks found in queue"}), 404

    return _generate_zip(tasks, "all_downloads")


def _generate_zip(tasks, base_filename):
    """Helper to generate and return a ZIP response for a list of tasks."""
    safe_title = "".join(c for c in base_filename if c.isalnum() or c in (" ", "-", "_" )).strip()
    if not safe_title:
        safe_title = "downloads"

    def _playlist_scan_dir(task):
        if not getattr(task, "playlist_title", None):
            return None
        safe_pl = "".join(c for c in task.playlist_title if c not in r'\\/:*?"<>|').strip()
        if not safe_pl:
            return None
        return os.path.join(task.output_dir, safe_pl)

    def _playlist_scan(task, limit=200):
        scan_dir = _playlist_scan_dir(task)
        if not scan_dir or not os.path.isdir(scan_dir):
            return []

        ext = ".mp4" if getattr(task, "dl_type", "audio") == "video" else ".mp3"
        out = []
        try:
            for name in os.listdir(scan_dir):
                if not name.lower().endswith(ext):
                    continue
                full = os.path.join(scan_dir, name)
                if os.path.isfile(full):
                    out.append((os.path.getmtime(full), full, name))
        except Exception:
            return []

        out.sort(key=lambda x: x[0], reverse=True)  # newest first
        return out[:limit]

    # If some playlist tasks missed filepath during conversion resolution,
    # scan the playlist output folder so the ZIP is still complete.
    task_file_map = {}
    for task in tasks:
        if task.filepath and os.path.exists(task.filepath):
            task_file_map[task.id] = task.filepath
            continue

        scanned = _playlist_scan(task)
        if scanned:
            task_file_map[task.id] = scanned[0][1]

    temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    temp_zip_path = temp_zip.name
    temp_zip.close()

    try:
        with zipfile.ZipFile(temp_zip_path, "w") as zf:
            for task in tasks:
                filepath = task_file_map.get(task.id)
                if filepath and os.path.exists(filepath):
                    arcname = task.filename or os.path.basename(filepath)
                    zf.write(filepath, arcname)

        return Response(
            stream_and_delete(temp_zip_path, original_tasks=tasks),
            headers={
                "Content-Disposition": f"attachment; filename=\"{safe_title}.zip\"",
                "Content-Type": "application/zip",
            },
        )
    except Exception as e:
        app.logger.error(f"Error creating ZIP: {e}")
        return jsonify({"error": "Failed to create ZIP archive"}), 500



# Trust headers from Cloudflare / Reverse Proxies (important for HTTPS routing)
if config.ENABLE_PROXY_FIX:
    from werkzeug.middleware.proxy_fix import ProxyFix
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

