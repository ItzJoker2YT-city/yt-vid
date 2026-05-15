"""
Download engine module.
Handles all YouTube downloading, conversion, and metadata operations using yt-dlp.
"""
import os
import json
import uuid
import shutil
import logging
import threading
from datetime import datetime

import yt_dlp
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC

import config

logger = logging.getLogger(__name__)


def _find_ffmpeg():
    """
    Locate the ffmpeg executable.
    Priority: system PATH → winget install dir → imageio-ffmpeg bundled binary.
    Returns the directory containing ffmpeg, or None if not found.
    """
    # 1. Already on PATH?
    if shutil.which("ffmpeg"):
        return None  # yt-dlp will find it automatically

    # 2. Common winget install location
    winget_path = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Microsoft", "WinGet", "Packages"
    )
    if os.path.isdir(winget_path):
        for root, dirs, files in os.walk(winget_path):
            if "ffmpeg.exe" in files:
                return root

    # 3. imageio-ffmpeg bundled binary (pip-installed fallback)
    try:
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        if ffmpeg_exe and os.path.exists(ffmpeg_exe):
            return os.path.dirname(ffmpeg_exe)
    except Exception:
        pass

    return None


# Resolve ffmpeg location once at import time
FFMPEG_LOCATION = _find_ffmpeg()
if FFMPEG_LOCATION:
    logger.info("ffmpeg located at: %s", FFMPEG_LOCATION)
else:
    logger.info("ffmpeg found on system PATH")

# Global registry of active downloads — keyed by download_id
active_downloads = {}
download_lock = threading.Lock()

# ─── Search cache ─────────────────────────────────────────────────────────────
# Simple in-memory cache: { query_key -> {"results": [...], "ts": timestamp} }
_search_cache = {}
_SEARCH_CACHE_TTL = 300  # seconds (5 minutes)

import time as _time



class DownloadTask:
    """Represents a single download task with progress tracking."""

    def __init__(self, url, output_dir, quality, trim_start=None, trim_end=None,
                 playlist_id=None, playlist_title=None, track_index=None, track_total=None, dl_type="audio"):
        self.id = str(uuid.uuid4())[:8]
        self.url = url
        self.output_dir = output_dir
        self.quality = quality
        self.trim_start = trim_start
        self.trim_end = trim_end
        self.dl_type = dl_type

        # Playlist grouping metadata
        self.playlist_id = playlist_id          # shared ID for all tracks in a playlist
        self.playlist_title = playlist_title    # human-readable playlist name
        self.track_index = track_index          # 1-based position in playlist
        self.track_total = track_total          # total tracks in playlist

        # State
        self.status = "queued"          # queued | downloading | converting | done | error | paused
        self.progress = 0.0
        self.speed = ""
        self.eta = ""
        self.title = ""
        self.thumbnail = ""
        self.duration = ""
        self.filesize = ""
        self.filename = ""   # basename only
        self.filepath = ""   # full absolute path (for serving)
        self.error_message = ""
        self.created_at = datetime.now().isoformat()
        self.completed_at = None

        # Internal
        self._cancel_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # Not paused by default

    def to_dict(self):
        """Serialize task state for the frontend."""
        return {
            "id": self.id,
            "url": self.url,
            "status": self.status,
            "progress": round(self.progress, 1),
            "speed": self.speed,
            "eta": self.eta,
            "title": self.title,
            "thumbnail": self.thumbnail,
            "duration": self.duration,
            "filesize": self.filesize,
            "filename": self.filename,
            "has_file": bool(self.filepath and os.path.exists(self.filepath)),
            "error_message": self.error_message,
            "quality": self.quality,
            "dl_type": self.dl_type,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            # Playlist fields
            "playlist_id": self.playlist_id,
            "playlist_title": self.playlist_title,
            "track_index": self.track_index,
            "track_total": self.track_total,
        }


def _progress_hook(task):
    """Returns a yt-dlp progress hook bound to a specific DownloadTask."""

    def hook(d):
        if task._cancel_event.is_set():
            raise Exception("Download cancelled by user")

        # Block while paused
        task._pause_event.wait()

        if d["status"] == "downloading":
            task.status = "downloading"
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                task.progress = (downloaded / total) * 100
            task.speed = d.get("_speed_str", "")
            task.eta = d.get("_eta_str", "")
            task.filesize = d.get("_total_bytes_str", "")

        elif d["status"] == "finished":
            task.status = "converting"
            task.progress = 100
            task.filename = d.get("filename", "")

    return hook


def _build_ytdlp_opts(task):
    """Build yt-dlp options dict for a given download task."""
    os.makedirs(task.output_dir, exist_ok=True)

    # Sanitise title for safe filenames; playlist tracks go into a subfolder
    if task.playlist_title:
        safe_pl = "".join(c for c in task.playlist_title if c not in r'\/:*?"<>|').strip()
        out_dir = os.path.join(task.output_dir, safe_pl)
    else:
        out_dir = task.output_dir
    os.makedirs(out_dir, exist_ok=True)

    outtmpl = os.path.join(out_dir, "%(title)s.%(ext)s")

    opts = {
        **config.YTDLP_BASE_OPTS,
        "outtmpl": outtmpl,
        "progress_hooks": [_progress_hook(task)],
        "writethumbnail": True,
        "postprocessor_args": [],
        "noplaylist": True,   # single-video opts never pull a playlist
    }

    if task.dl_type == "video":
        if task.quality == "1080p":
            video_format = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best"
        elif task.quality == "720p":
            video_format = "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best"
        elif task.quality == "480p":
            video_format = "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best"
        else:
            video_format = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"

        opts["format"] = video_format
        opts["postprocessors"] = [
            {'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'},
            {'key': 'FFmpegMetadata'},
            {'key': 'EmbedThumbnail'}
        ]
    else:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": task.quality,
            }
        ]

    # Inject ffmpeg location if not on PATH
    if FFMPEG_LOCATION:
        opts["ffmpeg_location"] = FFMPEG_LOCATION

    # Audio trimming via ffmpeg postprocessor args
    if task.trim_start or task.trim_end:
        pp_args = []
        if task.trim_start:
            pp_args += ["-ss", task.trim_start]
        if task.trim_end:
            pp_args += ["-to", task.trim_end]
        opts["postprocessor_args"] = {"ffmpeg": pp_args}

    return opts


def _embed_metadata(filepath, info):
    """Embed ID3 metadata (title, artist, thumbnail) into the MP3 file."""
    try:
        audio = MP3(filepath, ID3=ID3)
        try:
            audio.add_tags()
        except Exception:
            pass

        tags = audio.tags
        tags.add(TIT2(encoding=3, text=info.get("title", "")))
        tags.add(TPE1(encoding=3, text=info.get("uploader", "Unknown Artist")))
        tags.add(TALB(encoding=3, text=info.get("album", "YouTube Download")))

        # Embed thumbnail if available
        thumb_path = filepath.rsplit(".", 1)[0] + ".webp"
        if not os.path.exists(thumb_path):
            thumb_path = filepath.rsplit(".", 1)[0] + ".jpg"
        if not os.path.exists(thumb_path):
            thumb_path = filepath.rsplit(".", 1)[0] + ".png"

        if os.path.exists(thumb_path):
            with open(thumb_path, "rb") as img:
                mime = "image/jpeg" if thumb_path.endswith(".jpg") else "image/png"
                tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=img.read()))
            # Clean up thumbnail file
            try:
                os.remove(thumb_path)
            except OSError:
                pass

        audio.save()
        logger.info("Metadata embedded for: %s", filepath)
    except Exception as e:
        logger.warning("Failed to embed metadata: %s", e)


def _run_download(task):
    """Execute a single-video download in a background thread."""
    try:
        opts = _build_ytdlp_opts(task)

        with yt_dlp.YoutubeDL(opts) as ydl:
            # Extract info first to get title/thumbnail
            info = ydl.extract_info(task.url, download=False)

            if info is None:
                raise Exception("Could not retrieve video information")

            # Populate task metadata
            task.title = info.get("title", "Unknown")
            task.thumbnail = info.get("thumbnail", "")
            duration_secs = info.get("duration", 0)
            if duration_secs:
                mins, secs = divmod(int(duration_secs), 60)
                task.duration = f"{mins}:{secs:02d}"

            # Perform download + conversion
            info = ydl.extract_info(task.url, download=True)

            # Locate the output file
            raw_filename = ydl.prepare_filename(info)
            base_name = raw_filename.rsplit(".", 1)[0]
            if task.dl_type == "video":
                expected_path = base_name + ".mp4"
            else:
                expected_path = base_name + ".mp3"

            # Check inside a playlist subfolder if the file isn't at root
            if not os.path.exists(expected_path) and task.playlist_title:
                safe_pl = "".join(c for c in task.playlist_title if c not in r'\/:*?"<>|').strip()
                alt_path = os.path.join(task.output_dir, safe_pl, os.path.basename(expected_path))
                if os.path.exists(alt_path):
                    expected_path = alt_path

            # Fallback for weird extensions
            if not os.path.exists(expected_path) and os.path.exists(raw_filename):
                expected_path = raw_filename

            if os.path.exists(expected_path):
                if task.dl_type == "audio":
                    _embed_metadata(expected_path, info)
                task.filepath = expected_path
                task.filename = os.path.basename(expected_path)
            else:
                task.filepath = ""
                task.filename = os.path.basename(raw_filename)

            task.status = "done"
            task.progress = 100
            task.completed_at = datetime.now().isoformat()
            _save_history_entry(task, info, expected_path if os.path.exists(expected_path) else raw_filename)

            logger.info("Download complete: %s → %s", task.title, task.filename)

    except Exception as e:
        task.status = "error"
        task.error_message = _friendly_error(e)
        logger.error("Download failed for %s: %s", task.url, e)



def _run_playlist(url, output_dir, quality, parent_cancel, parent_pause, dl_type="audio"):
    """
    Detect a playlist URL, spawn one DownloadTask per track, and run them
    sequentially so the queue shows individual per-song progress.
    Returns the list of created tasks.
    """
    probe_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",  # fast — no full download
        "ignoreerrors": True,
    }
    if FFMPEG_LOCATION:
        probe_opts["ffmpeg_location"] = FFMPEG_LOCATION

    tasks = []
    try:
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None:
                return tasks

            if info.get("_type") != "playlist":
                # Not actually a playlist — fall through to normal download
                return tasks

            playlist_title = info.get("title", "Playlist")
            playlist_id = str(uuid.uuid4())[:8]
            entries = [e for e in info.get("entries", []) if e]
            total = len(entries)

            logger.info("Playlist '%s' — %d tracks detected", playlist_title, total)

            for i, entry in enumerate(entries, start=1):
                # Resolve the video URL from the flat entry
                entry_url = (
                    entry.get("webpage_url")
                    or entry.get("url")
                    or (f"https://www.youtube.com/watch?v={entry['id']}" if entry.get("id") else None)
                )
                if not entry_url:
                    continue

                # Create a dedicated task for this track
                task = DownloadTask(
                    url=entry_url,
                    output_dir=output_dir,
                    quality=quality,
                    playlist_id=playlist_id,
                    playlist_title=playlist_title,
                    track_index=i,
                    track_total=total,
                    dl_type=dl_type,
                )
                # Pre-fill title from flat info so the UI shows something immediately
                task.title = entry.get("title", f"Track {i}")

                # Share the parent cancel/pause signals
                task._cancel_event = parent_cancel
                task._pause_event = parent_pause

                with download_lock:
                    active_downloads[task.id] = task

                tasks.append(task)

    except Exception as e:
        logger.error("Playlist probe failed for %s: %s", url, e)
        return tasks

    # Run downloads sequentially (avoids hammering YouTube)
    def _run_all():
        for task in tasks:
            if parent_cancel.is_set():
                task.status = "error"
                task.error_message = "Cancelled"
                continue
            parent_pause.wait()  # respect global pause
            _run_download(task)

    thread = threading.Thread(target=_run_all, daemon=True)
    thread.start()
    return tasks


def _save_history_entry(task, info, filepath):
    """Append a completed download to the history file."""
    entry = {
        "id": task.id,
        "title": info.get("title", task.title),
        "artist": info.get("uploader", "Unknown"),
        "url": task.url,
        "filename": os.path.basename(filepath),
        "filepath": filepath,
        "quality": task.quality,
        "duration": task.duration,
        "thumbnail": info.get("thumbnail", ""),
        "downloaded_at": datetime.now().isoformat(),
    }

    history_file = config.HISTORY_DB
    os.makedirs(os.path.dirname(history_file), exist_ok=True)

    history = []
    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                history = json.load(f)
        except (json.JSONDecodeError, IOError):
            history = []

    history.insert(0, entry)
    # Keep last 500 entries
    history = history[:500]

    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def probe_url(url):
    """
    Quickly fetch metadata for a URL without downloading anything.
    Returns a dict with: is_playlist, title, track_count, thumbnail.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
    }
    if FFMPEG_LOCATION:
        opts["ffmpeg_location"] = FFMPEG_LOCATION

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return {"error": "Could not retrieve info"}

            if info.get("_type") == "playlist":
                entries = [e for e in info.get("entries", []) if e]
                return {
                    "is_playlist": True,
                    "title": info.get("title", "Playlist"),
                    "track_count": len(entries),
                    "thumbnail": (entries[0].get("thumbnails") or [{}])[-1].get("url", "")
                    if entries else "",
                }
            else:
                duration_secs = info.get("duration", 0)
                duration_str = ""
                if duration_secs:
                    mins, secs = divmod(int(duration_secs), 60)
                    duration_str = f"{mins}:{secs:02d}"
                return {
                    "is_playlist": False,
                    "title": info.get("title", "Unknown"),
                    "track_count": 1,
                    "thumbnail": info.get("thumbnail", ""),
                    "duration": duration_str,
                }
    except Exception as e:
        logger.error("probe_url failed: %s", e)
        return {"error": str(e)}


# ─── Public API ───────────────────────────────────────────────────────────────

def start_download(url, output_dir=None, quality=None, trim_start=None, trim_end=None, dl_type="audio"):
    """
    Queue a download for a URL.
    Automatically detects playlists and spawns per-track tasks.
    Returns a list of tasks (single-item list for videos, multi-item for playlists).
    """
    output_dir = output_dir or config.DEFAULT_DOWNLOAD_DIR
    quality = quality or config.DEFAULT_QUALITY

    # Heuristic: playlist URLs contain 'list=' and no 'v=' param,
    # or explicitly contain 'playlist?list='
    is_playlist = (
        "playlist?list=" in url
        or ("list=" in url and "watch?v=" not in url)
    )

    if is_playlist:
        # Shared cancel/pause events for the whole playlist
        cancel_event = threading.Event()
        pause_event = threading.Event()
        pause_event.set()
        tasks = _run_playlist(url, output_dir, quality, cancel_event, pause_event, dl_type)
        if tasks:
            return tasks  # list of per-track DownloadTask objects
        # If probe returned empty, fall through to single-video

    task = DownloadTask(url, output_dir, quality, trim_start, trim_end, dl_type=dl_type)

    with download_lock:
        active_downloads[task.id] = task

    thread = threading.Thread(target=_run_download, args=(task,), daemon=True)
    thread.start()

    return [task]


def start_batch_download(urls, output_dir=None, quality=None):
    """Queue multiple downloads. Returns list of task objects."""
    tasks = []
    for url in urls:
        url = url.strip()
        if url:
            task = start_download(url, output_dir, quality)
            tasks.append(task)
    return tasks


def get_task(task_id):
    """Get a download task by ID."""
    return active_downloads.get(task_id)


def get_all_tasks():
    """Get all active download tasks."""
    with download_lock:
        return [t.to_dict() for t in active_downloads.values()]


def pause_task(task_id):
    """Pause a download."""
    task = active_downloads.get(task_id)
    if task and task.status == "downloading":
        task._pause_event.clear()
        task.status = "paused"
        return True
    return False


def resume_task(task_id):
    """Resume a paused download."""
    task = active_downloads.get(task_id)
    if task and task.status == "paused":
        task._pause_event.set()
        task.status = "downloading"
        return True
    return False


def cancel_task(task_id):
    """Cancel a download."""
    task = active_downloads.get(task_id)
    if task:
        task._cancel_event.set()
        task._pause_event.set()  # Unblock if paused
        task.status = "error"
        task.error_message = "Cancelled by user"
        return True
    return False


def remove_task(task_id):
    """Remove a completed/failed task from the active list."""
    with download_lock:
        if task_id in active_downloads:
            del active_downloads[task_id]
            return True
    return False


def _friendly_error(exc):
    """Map raw yt-dlp exceptions to user-friendly messages."""
    msg = str(exc).lower()
    if "video unavailable" in msg or "this video is not available" in msg:
        return "Video unavailable — it may be private or deleted."
    if "copyright" in msg:
        return "Download blocked due to a copyright claim."
    if "age" in msg and "restrict" in msg:
        return "Age-restricted video — cannot download without login."
    if "private" in msg:
        return "This video is private."
    if "region" in msg or "geo" in msg:
        return "Video not available in your region."
    if "cancelled" in msg or "canceled" in msg:
        return "Cancelled by user."
    if "ffmpeg" in msg:
        return "FFmpeg error during conversion — is FFmpeg installed?"
    if "network" in msg or "timed out" in msg or "timeout" in msg:
        return "Network error — check your internet connection."
    return str(exc)


def search_youtube(query, max_results=10):
    """Search YouTube and return results.

    Speed strategy:
    - extract_flat=True  -> yt-dlp reads the search-page only, skipping
                            a separate HTTP fetch for every video (~80% faster).
    - CDN thumbnail URL  -> built from video ID, no extra network hit.
    - In-memory cache    -> repeat queries return instantly (5-min TTL).
    """
    cache_key = f"{query.lower().strip()}|{max_results}"
    now = _time.time()

    cached = _search_cache.get(cache_key)
    if cached and (now - cached["ts"]) < _SEARCH_CACHE_TTL:
        logger.info("Search cache hit: %s", query)
        return cached["results"]

    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "extract_flat": True,   # <-- main speedup: no per-video page fetches
        "socket_timeout": 10,
    }

    search_query = f"ytsearch{max_results}:{query}"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            result = ydl.extract_info(search_query, download=False)
            if not result:
                return []
            entries = result.get("entries", [])

            results = []
            for entry in entries:
                if not entry:
                    continue

                vid_id = entry.get("id", "")

                duration_secs = entry.get("duration") or 0
                if duration_secs:
                    mins, secs = divmod(int(duration_secs), 60)
                    duration_str = f"{mins}:{secs:02d}"
                else:
                    duration_str = ""

                # YouTube CDN thumbnail — always available, zero extra requests
                thumb = (f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg"
                         if vid_id else entry.get("thumbnail", ""))

                results.append({
                    "id": vid_id,
                    "title": entry.get("title", "Unknown"),
                    "url": (entry.get("webpage_url")
                            or entry.get("url")
                            or f"https://www.youtube.com/watch?v={vid_id}"),
                    "thumbnail": thumb,
                    "duration": duration_str,
                    "channel": (entry.get("uploader")
                                or entry.get("channel")
                                or entry.get("uploader_id", "Unknown")),
                    "views": entry.get("view_count", 0),
                })

            _search_cache[cache_key] = {"results": results, "ts": now}
            logger.info("Search '%s' -> %d results", query, len(results))
            return results

    except Exception as e:
        logger.error("YouTube search failed: %s", e)
        return []


def prewarm_cache(queries, max_results=10):
    """Pre-populate the search cache for a list of queries.
    Run from a daemon thread after startup so first searches hit the cache.
    """
    for query in queries:
        cache_key = f"{query.lower().strip()}|{max_results}"
        if cache_key not in _search_cache:
            try:
                search_youtube(query, max_results)
                logger.info("Cache pre-warmed: %s", query)
                _time.sleep(1.5)   # polite delay between requests
            except Exception as e:
                logger.warning("Prewarm failed for '%s': %s", query, e)


def _find_artist_channel(artist, ydl):
    """
    Locate the artist's YouTube channel URL.
    Priority:
      1. Auto-generated "Artist - Topic" channel (most accurate for discography)
      2. Top search result's channel URL (fallback)
    Returns a channel URL string or None.
    """
    for query in [f"{artist} - Topic", f"{artist} official"]:
        try:
            info = ydl.extract_info(f"ytsearch3:{query}", download=False)
            if not info:
                continue
            for entry in (info.get("entries") or []):
                if not entry:
                    continue
                ch_url = entry.get("channel_url") or entry.get("uploader_url")
                if not ch_url:
                    continue
                # Prefer the Topic channel if we can detect it
                ch_name = (entry.get("channel") or entry.get("uploader") or "").lower()
                if "topic" in ch_name or artist.lower() in ch_name:
                    return ch_url
        except Exception as e:
            logger.warning("Channel lookup query '%s' failed: %s", query, e)
    return None


def search_artist_albums(artist, max_results=6):
    """Search YouTube for an artist's albums and playlists.

    Strategy (channel-first):
      1. Locate the artist's YouTube channel (Topic channel preferred).
      2. Pull their channel /playlists page — these are their real albums/EPs.
      3. Hard-filter out mixtapes, freestyles, compilations.
      4. If channel yields < 3 albums, fall back to keyword search.
      5. Always try to return at least 3 results.
    """
    from datetime import datetime
    year = datetime.now().year
    cache_key = f"albums|{artist.lower().strip()}|{year}"
    now = _time.time()

    cached = _search_cache.get(cache_key)
    if cached and (now - cached["ts"]) < _SEARCH_CACHE_TTL:
        logger.info("Album cache hit: %s", artist)
        return cached["results"]

    # Keywords that identify real albums/EPs
    ALBUM_KEYWORDS = {"album", "ep", "collection", "deluxe", "full album", "official album"}
    # Content that is NOT an album — hard reject these
    REJECT_KEYWORDS = {
        "mixtape", "mix tape", " mix ",      # DJ mixes / mashups
        "freestyle", "cypher",               # rap freestyles
        "remix",                             # remixed tracks
        "best of", "greatest hits",          # compilations
        "compilation", "instrumental",
        "karaoke", "cover", "reaction",
        "music video", "official video",     # video singles
        "lyric video", "lyrics video",
        "animated", "visualizer",            # visual-only content
    }

    opts_flat = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "extract_flat": True,
        "socket_timeout": 15,
    }

    seen_ids = set()
    results = []

    def _is_album_entry(title, duration_secs, track_count):
        """Return True if this entry looks like a real album/EP."""
        t = title.lower()

        # Hard reject — never an album
        if any(kw in t for kw in REJECT_KEYWORDS):
            return False

        keyword_match = any(kw in t for kw in ALBUM_KEYWORDS)
        has_tracks    = bool(track_count and 5 <= track_count <= 30)
        is_long_video = bool(duration_secs and duration_secs >= 1200)  # >= 20 min

        # A short video (< 15 min) that only matched a keyword like "ep" is almost
        # certainly a music video ABOUT an EP, not the EP itself — skip it.
        if keyword_match and not has_tracks:
            if duration_secs and duration_secs < 900:
                return False

        return keyword_match or has_tracks or is_long_video


    def _build_result(entry, vid_id, artist_name):
        title = entry.get("title", "Unknown")
        duration_secs = entry.get("duration") or 0
        if duration_secs:
            m, s = divmod(int(duration_secs), 60)
            h, m2 = divmod(m, 60)
            duration_str = f"{h}h {m2}m" if h else f"{m}:{s:02d}"
        else:
            duration_str = ""
        url = (entry.get("webpage_url") or entry.get("url")
               or f"https://www.youtube.com/watch?v={vid_id}")
        thumb = (entry.get("thumbnail")
                 or f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg")
        is_playlist = "list=" in url or "playlist" in url
        return {
            "id": vid_id,
            "title": title,
            "url": url,
            "thumbnail": thumb,
            "duration": duration_str,
            "duration_secs": duration_secs,
            "channel": (entry.get("uploader") or entry.get("channel") or artist_name),
            "is_playlist": is_playlist,
            "track_count": entry.get("playlist_count") or None,
        }

    # ── Step 1: Find the artist's YouTube channel ──────────────────────────
    channel_url = None
    try:
        with yt_dlp.YoutubeDL(opts_flat) as ydl:
            channel_url = _find_artist_channel(artist, ydl)
    except Exception as e:
        logger.warning("Channel find failed for '%s': %s", artist, e)

    # ── Step 2: Fetch playlists directly from channel /playlists page ──────
    if channel_url:
        playlists_url = channel_url.rstrip("/") + "/playlists"
        logger.info("Fetching channel playlists: %s", playlists_url)
        try:
            with yt_dlp.YoutubeDL({**opts_flat, "extract_flat": "in_playlist"}) as ydl:
                info = ydl.extract_info(playlists_url, download=False)
                if info:
                    for entry in (info.get("entries") or []):
                        if not entry or len(results) >= max_results:
                            break
                        pl_id = entry.get("id", "")
                        if not pl_id or pl_id in seen_ids:
                            continue
                        title = entry.get("title", "")
                        track_count = entry.get("playlist_count") or 0
                        if not _is_album_entry(title, 0, track_count):
                            continue
                        seen_ids.add(pl_id)
                        results.append(_build_result(entry, pl_id, artist))
        except Exception as e:
            logger.warning("Channel playlist fetch failed for '%s': %s", artist, e)

    # ── Step 3: Keyword fallback if channel gave < 3 albums ───────────────
    if len(results) < 3:
        fallback_queries = [
            f"{artist} full album official",
            f"{artist} album {year}",
            f"{artist} EP official",
        ]
        try:
            with yt_dlp.YoutubeDL(opts_flat) as ydl:
                for query in fallback_queries:
                    if len(results) >= max_results:
                        break
                    info = ydl.extract_info(
                        f"ytsearch{max_results}:{query}", download=False
                    )
                    if not info:
                        continue
                    for entry in (info.get("entries") or []):
                        if not entry or len(results) >= max_results:
                            break
                        vid_id = entry.get("id", "")
                        if not vid_id or vid_id in seen_ids:
                            continue
                        title = entry.get("title", "")
                        duration_secs = entry.get("duration") or 0
                        track_count = entry.get("playlist_count") or 0
                        if not _is_album_entry(title, duration_secs, track_count):
                            continue
                        seen_ids.add(vid_id)
                        results.append(_build_result(entry, vid_id, artist))
        except Exception as e:
            logger.error("Album fallback search failed for '%s': %s", artist, e)

    _search_cache[cache_key] = {"results": results, "ts": now}
    logger.info("Album search '%s' -> %d results (channel=%s)", artist, len(results), bool(channel_url))
    return results



def get_history():
    """Load download history from the JSON file."""
    if os.path.exists(config.HISTORY_DB):
        try:
            with open(config.HISTORY_DB, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


def clear_history():
    """Clear the download history."""
    if os.path.exists(config.HISTORY_DB):
        os.remove(config.HISTORY_DB)
    return True
