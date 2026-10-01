"""
Database layer for the YT-MP3 app — backed by Neon (serverless Postgres).

Set DATABASE_URL to your Neon connection string, e.g.
  postgresql://user:pass@ep-xxx-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require

If DATABASE_URL is not set, the app falls back to the local SQLite file
(data/ghana_music.db) so local development still works with zero setup.

Tables:
  songs(page_url TEXT PRIMARY KEY, position INTEGER, title TEXT, artist TEXT,
        thumbnail TEXT, date TEXT, page_slug TEXT, mp3_url TEXT,
        mp3_url_fallback TEXT, has_mp3 BOOLEAN, scraped_at TEXT)
  meta(key TEXT PRIMARY KEY, value TEXT)   -- last_updated, max_page
  history(id TEXT PRIMARY KEY, downloaded_at TEXT, entry JSONB)

The song cache is order-sensitive (page-1 songs float to the top), so a save
rewrites every row in one transaction with position = list index, keeping
halmblog's load -> mutate -> save flow unchanged.

On first run against an empty Neon database, existing data is imported
automatically from data/ghana_music.db, data/ghana_music.json and
data/history.json if those files exist.
"""

import json
import logging
import os
import sqlite3
import threading

logger = logging.getLogger("cache_db")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "ghana_music.db")
LEGACY_JSON = os.path.join(DATA_DIR, "ghana_music.json")
LEGACY_HISTORY_JSON = os.path.join(DATA_DIR, "history.json")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

HISTORY_LIMIT = 500

_SONG_COLUMNS = (
    "page_url", "position", "title", "artist", "thumbnail", "date",
    "page_slug", "mp3_url", "mp3_url_fallback", "has_mp3", "scraped_at",
)
_INSERT_SONG_COLS = ", ".join(_SONG_COLUMNS)

# ─── Postgres (Neon) ─────────────────────────────────────────────────────────

_PG_SCHEMA = """
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
    has_mp3          BOOLEAN NOT NULL DEFAULT FALSE,
    scraped_at       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_songs_position ON songs(position);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS history (
    id            TEXT PRIMARY KEY,
    downloaded_at TEXT NOT NULL DEFAULT '',
    entry         JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_downloaded_at ON history(downloaded_at DESC);
"""

_pool = None
_pool_lock = threading.Lock()
_schema_ready = False


def _pg_pool():
    """Lazily create a small connection pool (Neon closes idle connections,
    so the pool validates connections before handing them out)."""
    global _pool, _schema_ready
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool
                _pool = ConnectionPool(
                    DATABASE_URL,
                    min_size=1,
                    max_size=int(os.environ.get("DB_POOL_MAX", "5")),
                    kwargs={"row_factory": dict_row, "autocommit": False},
                    check=ConnectionPool.check_connection,
                    max_idle=240,
                    open=True,
                )
                with _pool.connection() as conn:
                    conn.execute(_PG_SCHEMA)
                    _pg_import_legacy(conn)
                _schema_ready = True
                logger.info("Connected to Neon Postgres")
    return _pool


def _song_row(s: dict, i: int) -> tuple:
    return (
        s.get("page_url", ""), i,
        s.get("title", "") or "", s.get("artist", "") or "",
        s.get("thumbnail", "") or "", s.get("date", "") or "",
        s.get("page_slug", "") or "", s.get("mp3_url") or None,
        s.get("mp3_url_fallback") or None,
        bool(s.get("has_mp3") or s.get("mp3_url")),
        s.get("scraped_at", "") or "",
    )


def _read_legacy_songs() -> dict | None:
    """Return legacy cache data from the old SQLite file or JSON file."""
    if os.path.exists(DB_PATH):
        try:
            data = _sqlite_load_cache()
            if data["songs"]:
                return data
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not read legacy SQLite cache: %s", e)
    if os.path.exists(LEGACY_JSON):
        try:
            with open(LEGACY_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("songs"):
                return data
        except (json.JSONDecodeError, IOError):
            pass
    return None


def _pg_import_legacy(conn) -> None:
    """One-time import of local data into an empty Neon database."""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM songs")
        if cur.fetchone()["n"] == 0:
            data = _read_legacy_songs()
            if data:
                _pg_write_cache(cur, data)
                logger.info("Imported %d songs into Neon", len(data["songs"]))
        cur.execute("SELECT COUNT(*) AS n FROM history")
        if cur.fetchone()["n"] == 0 and os.path.exists(LEGACY_HISTORY_JSON):
            try:
                with open(LEGACY_HISTORY_JSON, "r", encoding="utf-8") as f:
                    entries = json.load(f)
                if isinstance(entries, list) and entries:
                    for e in entries[:HISTORY_LIMIT]:
                        _pg_insert_history(cur, e)
                    logger.info("Imported %d history entries into Neon", len(entries))
            except (json.JSONDecodeError, IOError):
                pass
    conn.commit()


def _pg_write_cache(cur, data: dict) -> None:
    rows, seen = [], set()
    for i, s in enumerate(data.get("songs", [])):
        url = s.get("page_url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        rows.append(_song_row(s, i))
    cur.execute("DELETE FROM songs")
    if rows:
        cur.executemany(
            f"INSERT INTO songs ({_INSERT_SONG_COLS}) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            rows,
        )
    upsert = ("INSERT INTO meta (key, value) VALUES (%s, %s) "
              "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    cur.execute(upsert, ("last_updated", data.get("last_updated") or ""))
    cur.execute(upsert, ("max_page", str(data.get("max_page") or 1)))


def _pg_insert_history(cur, entry: dict) -> None:
    from psycopg.types.json import Jsonb
    cur.execute(
        "INSERT INTO history (id, downloaded_at, entry) VALUES (%s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET downloaded_at = EXCLUDED.downloaded_at, "
        "entry = EXCLUDED.entry",
        (str(entry.get("id") or entry.get("downloaded_at") or os.urandom(8).hex()),
         entry.get("downloaded_at", "") or "", Jsonb(entry)),
    )


# ─── SQLite fallback (local dev, no DATABASE_URL) ────────────────────────────

_SQLITE_SCHEMA = """
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


def _sqlite_connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SQLITE_SCHEMA)
    return conn


def _sqlite_load_cache() -> dict:
    conn = _sqlite_connect()
    try:
        songs = [dict(r) for r in conn.execute("SELECT * FROM songs ORDER BY position")]
        if not songs and os.path.exists(LEGACY_JSON):
            try:
                with open(LEGACY_JSON, "r", encoding="utf-8") as f:
                    legacy = json.load(f)
                if isinstance(legacy, dict) and legacy.get("songs"):
                    _sqlite_save_cache(legacy, conn)
                    songs = [dict(r) for r in conn.execute(
                        "SELECT * FROM songs ORDER BY position")]
            except (json.JSONDecodeError, IOError):
                pass
        for s in songs:
            s["has_mp3"] = bool(s["has_mp3"])
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM meta")}
        return {
            "last_updated": meta.get("last_updated") or None,
            "max_page": int(meta["max_page"]) if meta.get("max_page") else None,
            "songs": songs,
        }
    finally:
        conn.close()


def _sqlite_save_cache(data: dict, conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or _sqlite_connect()
    try:
        rows, seen = [], set()
        for i, s in enumerate(data.get("songs", [])):
            url = s.get("page_url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            r = list(_song_row(s, i))
            r[9] = 1 if r[9] else 0
            rows.append(tuple(r))
        with conn:
            conn.execute("DELETE FROM songs")
            conn.executemany(
                f"INSERT INTO songs ({_INSERT_SONG_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_updated', ?)",
                         (data.get("last_updated") or "",))
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('max_page', ?)",
                         (str(data.get("max_page") or 1),))
    finally:
        if own:
            conn.close()


_history_lock = threading.Lock()


def _json_history_load() -> list:
    if os.path.exists(LEGACY_HISTORY_JSON):
        try:
            with open(LEGACY_HISTORY_JSON, "r", encoding="utf-8") as f:
                h = json.load(f)
                return h if isinstance(h, list) else []
        except (json.JSONDecodeError, IOError):
            return []
    return []


# ─── Public API ──────────────────────────────────────────────────────────────

def load_cache() -> dict:
    """Return {last_updated, max_page, songs} mirroring the legacy JSON shape."""
    if not USE_POSTGRES:
        return _sqlite_load_cache()
    with _pg_pool().connection() as conn:
        songs = conn.execute(
            f"SELECT {_INSERT_SONG_COLS} FROM songs ORDER BY position").fetchall()
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    return {
        "last_updated": meta.get("last_updated") or None,
        "max_page": int(meta["max_page"]) if meta.get("max_page") else None,
        "songs": [dict(s) for s in songs],
    }


def save_cache(data: dict) -> None:
    """Persist the full cache. Song order is preserved via the position column."""
    if not USE_POSTGRES:
        return _sqlite_save_cache(data)
    with _pg_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                _pg_write_cache(cur, data)


def add_history_entry(entry: dict) -> None:
    """Add a completed download to history, keeping the newest HISTORY_LIMIT."""
    if not USE_POSTGRES:
        with _history_lock:
            history = _json_history_load()
            history.insert(0, entry)
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(LEGACY_HISTORY_JSON, "w", encoding="utf-8") as f:
                json.dump(history[:HISTORY_LIMIT], f, indent=2, ensure_ascii=False)
        return
    with _pg_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                _pg_insert_history(cur, entry)
                cur.execute(
                    "DELETE FROM history WHERE id NOT IN ("
                    "SELECT id FROM history ORDER BY downloaded_at DESC LIMIT %s)",
                    (HISTORY_LIMIT,),
                )


def get_history() -> list:
    """Return download history, newest first."""
    if not USE_POSTGRES:
        return _json_history_load()
    with _pg_pool().connection() as conn:
        rows = conn.execute(
            "SELECT entry FROM history ORDER BY downloaded_at DESC LIMIT %s",
            (HISTORY_LIMIT,)).fetchall()
    return [r["entry"] for r in rows]


def clear_history() -> None:
    if not USE_POSTGRES:
        if os.path.exists(LEGACY_HISTORY_JSON):
            os.remove(LEGACY_HISTORY_JSON)
        return
    with _pg_pool().connection() as conn:
        conn.execute("DELETE FROM history")
        conn.commit()
