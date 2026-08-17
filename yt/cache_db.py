"""
SQLite-backed store for the halmblog Ghana music cache.

Schema:
  songs(page_url TEXT PRIMARY KEY, position INTEGER, title TEXT, artist TEXT,
        thumbnail TEXT, date TEXT, page_slug TEXT, mp3_url TEXT,
        mp3_url_fallback TEXT, has_mp3 INTEGER, scraped_at TEXT)
  meta(key TEXT PRIMARY KEY, value TEXT)   -- last_updated, max_page

The cache is order-sensitive (page-1 songs float to the top), so a save
rewrites every row in one transaction with position = list index. That is
fast enough at this scale (~6k rows) and keeps the in-memory list order as
the single source of truth, so halmblog's load → mutate → save flow works
unchanged. WAL mode lets the app's background threads read/write safely.
"""

import json
import logging
import os
import sqlite3

logger = logging.getLogger("cache_db")

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "ghana_music.db")
LEGACY_JSON = os.path.join(os.path.dirname(__file__), "data", "ghana_music.json")

_SONG_COLUMNS = (
    "page_url", "position", "title", "artist", "thumbnail", "date",
    "page_slug", "mp3_url", "mp3_url_fallback", "has_mp3", "scraped_at",
)

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS songs (
    page_url         TEXT PRIMARY KEY,
    position         INTEGER NOT NULL,
    title            TEXT NOT NULL DEFAULT '',
    artist           TEXT NOT NULL DEFAULT '',
    thumbnail        TEXT NOT NULL DEFAULT '',
    date             TEXT NOT NULL DEFAULT '',
    page_slug        TEXT NOT NULL DEFAULT '',
    mp3_url          TEXT,
    mp3_url_fallback TEXT,
    has_mp3          INTEGER NOT NULL DEFAULT 0,
    scraped_at       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_songs_position ON songs(position);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _row_to_song(row: sqlite3.Row) -> dict:
    song = dict(row)
    song["has_mp3"] = bool(song["has_mp3"])
    return song


def _migrate_from_json(conn: sqlite3.Connection) -> None:
    """One-time import of the legacy ghana_music.json cache into SQLite."""
    if not os.path.exists(LEGACY_JSON):
        return
    try:
        with open(LEGACY_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return
    songs = data.get("songs", []) if isinstance(data, dict) else []
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO songs (page_url, position, title, artist, thumbnail, "
            "date, page_slug, mp3_url, mp3_url_fallback, has_mp3, scraped_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    s.get("page_url", ""), i,
                    s.get("title", ""), s.get("artist", ""),
                    s.get("thumbnail", ""), s.get("date", ""),
                    s.get("page_slug", ""), s.get("mp3_url") or None,
                    s.get("mp3_url_fallback") or None,
                    1 if (s.get("has_mp3") or s.get("mp3_url")) else 0,
                    s.get("scraped_at", ""),
                )
                for i, s in enumerate(songs)
                if s.get("page_url")
            ],
        )
        meta = data if isinstance(data, dict) else {}
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_updated', ?)",
                     (meta.get("last_updated") or "",))
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('max_page', ?)",
                     (str(meta.get("max_page") or 1),))
    if songs:
        logger.info("Migrated %d songs from %s into SQLite", len(songs), LEGACY_JSON)


def load_cache() -> dict:
    """Return {last_updated, max_page, songs} mirroring the legacy JSON shape."""
    conn = _connect()
    try:
        songs = [_row_to_song(r) for r in conn.execute(
            "SELECT * FROM songs ORDER BY position")]
        if not songs:
            _migrate_from_json(conn)
            songs = [_row_to_song(r) for r in conn.execute(
                "SELECT * FROM songs ORDER BY position")]
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM meta")}
        return {
            "last_updated": meta.get("last_updated") or None,
            "max_page": int(meta["max_page"]) if meta.get("max_page") else None,
            "songs": songs,
        }
    finally:
        conn.close()


def save_cache(data: dict) -> None:
    """Persist the full cache. Song order is preserved via the position column."""
    conn = _connect()
    try:
        rows = []
        seen = set()
        for i, s in enumerate(data.get("songs", [])):
            url = s.get("page_url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            rows.append((
                url, i,
                s.get("title", ""), s.get("artist", ""),
                s.get("thumbnail", ""), s.get("date", ""),
                s.get("page_slug", ""), s.get("mp3_url") or None,
                s.get("mp3_url_fallback") or None,
                1 if (s.get("has_mp3") or s.get("mp3_url")) else 0,
                s.get("scraped_at", ""),
            ))
        with conn:
            conn.execute("DELETE FROM songs")
            conn.executemany(
                "INSERT INTO songs (page_url, position, title, artist, thumbnail, "
                "date, page_slug, mp3_url, mp3_url_fallback, has_mp3, scraped_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_updated', ?)",
                         (data.get("last_updated") or "",))
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('max_page', ?)",
                         (str(data.get("max_page") or 1),))
    finally:
        conn.close()
