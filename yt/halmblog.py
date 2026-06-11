"""
Halmblog.com Ghana Music scraper — powered by requests + BeautifulSoup.
Extracts song listings and direct MP3 file URLs.

Uses a persistent JSON cache so repeat loads are instant.
New songs are detected by scraping page 1 periodically.
"""
import os
import re
import json
import time
import logging
import threading
from datetime import datetime
from bs4 import BeautifulSoup

import config

logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────
CATEGORY_URL = "https://www.halmblog.com/category/listen/ghana-music/"
BASE_URL = "https://www.halmblog.com"
PAGE_TIMEOUT = 20

_CACHE_FILE = os.path.join(os.path.dirname(config.LOG_FILE), "ghana_music.json")
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SECONDS = 1800          # how often we auto-refresh page 1
_BACKGROUND_THREAD = None

# Simple per-page memory cache (60s)
_song_page_cache = {}
_PAGE_CACHE_TTL = 60

import requests as _requests


def _fetch_html(url: str) -> str:
    """Fetch raw HTML via requests (reliable, no API credits needed)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }
    session = _requests.Session()
    resp = session.get(url, headers=headers, timeout=PAGE_TIMEOUT)
    resp.raise_for_status()
    return resp.text


# ─── Artist / Title Parser ───────────────────────────────────────────────────
def _split_artist_title(raw: str) -> tuple:
    """
    Parse a halmblog title like:
      'Donzy – Blackstars' -> ('Donzy', 'Blackstars')
      'Young Legend – Let Me Go' -> ('Young Legend', 'Let Me Go')
      'Nervous by Shatta Wale' -> ('Shatta Wale', 'Nervous')
    Falls back to ('', raw) if no separator found.
    """
    s = raw.strip()
    if not s:
        return ("", "")

    for sep in ["\u2009", "\u00a0", " – ", " — ", " - ", "–", "—", "-"]:
        if sep in s:
            parts = s.split(sep, 1)
            artist = parts[0].strip()
            title  = parts[1].strip()
            return (artist, title)

    # "by" pattern
    m = re.search(r'\bby\s+(.+)$', s, re.IGNORECASE)
    if m:
        return (m.group(1).strip(), s[:m.start()].strip())

    return ("", s)


# ─── Persistent Cache Helpers ─────────────────────────────────────────────────
def _load_cache() -> dict:
    if not os.path.exists(_CACHE_FILE):
        return {"last_updated": None, "songs": []}
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"last_updated": None, "songs": []}


def _save_cache(data: dict):
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except IOError:
        pass


def _by_url(cache: dict) -> dict:
    return {s["page_url"]: s for s in cache.get("songs", [])}


# ─── Public: get/search cache ─────────────────────────────────────────────────
def get_cached_songs() -> list:
    return _load_cache().get("songs", [])


def search_cached_songs(query: str) -> list:
    q = query.lower().strip()
    if not q:
        return get_cached_songs()
    out = []
    for s in get_cached_songs():
        if q in (s.get("artist") or "").lower() or q in (s.get("title") or "").lower():
            out.append(s)
    return out


# ─── Scrape Listing Page ─────────────────────────────────────────────────────
def scrape_listing(page: int = 1) -> list:
    """
    Scrape the Ghana Music category listing page.
    Returns: [{title, artist, page_url, date, page_slug, thumbnail}].
    """
    url = CATEGORY_URL if page == 1 else f"{CATEGORY_URL}page/{page}/"
    try:
        html = _fetch_html(url)
    except Exception:
        return []

    soup = BeautifulSoup(html, "html.parser")
    results = []
    for art in soup.find_all("article"):
        h2 = art.find("h2")
        if not h2:
            continue
        a = h2.find("a", href=True)
        if not a:
            continue
        raw_t = h2.get_text(strip=True)
        artist, title = _split_artist_title(raw_t)
        link = a["href"]
        if link and not link.startswith("http"):
            link = BASE_URL + link

        date_tag = art.find(class_="date") or art.find("time")
        date_str = date_tag.get_text(strip=True) if date_tag else ""

        img = art.find("img")
        thumb = img.get("data-src") or img.get("data-lazy-src") or img.get("src", "") if img else ""

        results.append({
            "title": title or raw_t,
            "artist": artist,
            "page_url": link,
            "thumbnail": thumb,
            "date": date_str,
            "page_slug": link.rstrip("/").split("/")[-1] if link else "",
        })

    logger.info("Halmblog listing page %d -> %d songs", page, len(results))
    return results


# ─── Scrape Individual Song Page ─────────────────────────────────────────────
def _extract_mp3(href: str) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.lower().startswith("http") and ".mp3" in href.lower():
        return href
    return None


def scrape_song_page(page_url: str) -> dict:
    """Scrape a halmblog song page for direct MP3 URL + metadata."""
    cache_key = page_url
    now = time.time()
    cached = _song_page_cache.get(cache_key)
    if cached and (now - cached["ts"]) < _PAGE_CACHE_TTL:
        return cached["data"]

    try:
        html = _fetch_html(page_url)
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        logger.warning("Failed to fetch song page %s: %s", page_url, e)
        return {"title": "", "artist": "", "mp3_url": None, "thumbnail": "", "page_url": page_url}

    h1 = soup.find("h1")
    raw_t = h1.get_text(strip=True) if h1 else ""
    artist, title = _split_artist_title(raw_t)

    # <audio> tag -> direct src
    mp3_url = None
    audio = soup.find("audio")
    if audio:
        src = audio.get("src") or ""
        src_tag = audio.find("source")
        if src_tag:
            src = src_tag.get("src") or src
        mp3_url = _extract_mp3(src)

    # <a> with .mp3
    if not mp3_url:
        for a in soup.find_all("a", href=True):
            candidate = _extract_mp3(a["href"])
            if candidate:
                mp3_url = candidate
                break

    # regex
    if not mp3_url:
        mp3s = re.findall(r'https?://[^\s"<>]+\.mp3[^\s"<>]*', html, re.IGNORECASE)
        if mp3s:
            mp3_url = mp3s[0]

    # thumbnail
    thumb = ""
    entry = soup.find("article") or soup.find("div", class_="entry-content") or soup
    img = entry.find("img")
    if img:
        thumb = img.get("data-src") or img.get("data-lazy-src") or img.get("src", "")

    result = {
        "title": title or raw_t,
        "artist": artist,
        "mp3_url": mp3_url,
        "thumbnail": thumb,
        "page_url": page_url,
    }
    _song_page_cache[cache_key] = {"data": result, "ts": now}
    return result


# ─── Fast Cache Build (listing only — no song page visits) ────────────────────
def build_cache(max_pages: int = 2) -> dict:
    """Fast cache build: only scrapes listing pages (1 HTTP req each).
    MP3 URLs are filled in later by fill_missing_mp3s().
    Existing songs are MOVED to the top in page order so cache stays fresh."""
    cache = _load_cache()
    existing = _by_url(cache)
    # Start with current cache; we'll move page-1 songs to top
    all_songs = list(cache.get("songs", []))
    new_cnt = 0
    updated_cnt = 0

    for p in range(1, max_pages + 1):
        listings = scrape_listing(page=p)
        if not listings:
            break
        for item in listings:
            key = item["page_url"]
            if key not in existing:
                # Brand new — insert at TOP (most recent)
                all_songs.insert(0, {
                    **item,
                    "mp3_url": None,
                    "has_mp3": False,
                    "scraped_at": datetime.now().isoformat(),
                })
                new_cnt += 1
            else:
                # Already in cache — move to top to keep ordering fresh
                for idx, s in enumerate(all_songs):
                    if s["page_url"] == key:
                        song = all_songs.pop(idx)
                        # Preserve existing mp3_url if we have it
                        all_songs.insert(0, {
                            **item,
                            "mp3_url": song.get("mp3_url"),
                            "has_mp3": bool(song.get("mp3_url")),
                            "scraped_at": song.get("scraped_at", datetime.now().isoformat()),
                        })
                        updated_cnt += 1
                        break

    cache["songs"] = all_songs
    cache["last_updated"] = datetime.now().isoformat()
    with _CACHE_LOCK:
        _save_cache(cache)
    logger.info("Ghana cache built (fast): %d total (%d new, %d reordered)", len(all_songs), new_cnt, updated_cnt)
    return cache


def fill_missing_mp3s(limit: int = 50) -> int:
    """Background task: visit song pages without MP3 and extract links.
    Returns number of MP3s found."""
    cache = _load_cache()
    filled = 0
    pending = [s for s in cache.get("songs", []) if not s.get("mp3_url")][:limit]

    for s in pending:
        try:
            details = scrape_song_page(s["page_url"])
            if details.get("mp3_url"):
                s["mp3_url"] = details["mp3_url"]
                s["has_mp3"] = True
                s["thumbnail"] = details.get("thumbnail") or s.get("thumbnail", "")
                filled += 1
        except Exception as e:
            logger.debug("MP3 fill failed for %s: %s", s["page_url"], e)

    if filled > 0:
        with _CACHE_LOCK:
            _save_cache(cache)
    logger.info("MP3 fill pass complete: %d/%d found", filled, len(pending))
    return filled


def check_for_updates() -> int:
    """Scrape page 1 listing, add any new songs AND reorder existing ones.
    Returns total number of changes (new + reordered)."""
    cache = _load_cache()
    existing = _by_url(cache)
    listings = scrape_listing(page=1)
    added = 0
    reordered = 0

    for i, item in enumerate(listings):
        key = item["page_url"]
        if key not in existing:
            # New song — insert at top (position i to preserve order)
            cache["songs"].insert(i, {
                **item,
                "mp3_url": None,
                "has_mp3": False,
                "scraped_at": datetime.now().isoformat(),
            })
            added += 1
        else:
            # Already in cache — move to same position as on page 1 to keep ordering fresh
            for idx, s in enumerate(cache["songs"]):
                if s["page_url"] == key:
                    if idx != i:
                        song = cache["songs"].pop(idx)
                        cache["songs"].insert(i, {
                            **item,
                            "mp3_url": song.get("mp3_url"),
                            "has_mp3": bool(song.get("mp3_url")),
                            "scraped_at": song.get("scraped_at", datetime.now().isoformat()),
                        })
                        reordered += 1
                    else:
                        # Update metadata in place (title, thumbnail, date may change)
                        cache["songs"][i].update({
                            "title": item["title"],
                            "artist": item["artist"],
                            "thumbnail": item["thumbnail"],
                            "date": item["date"],
                        })
                    break

    total_changes = added + reordered
    if total_changes > 0:
        cache["last_updated"] = datetime.now().isoformat()
        with _CACHE_LOCK:
            _save_cache(cache)
        logger.info("Ghana cache updated: +%d new, %d reordered (total %d)", added, reordered, len(cache["songs"]))
    else:
        logger.info("Ghana cache: no new listings")
    return total_changes


def auto_update_check():
    """Background thread: periodically check page 1."""
    while True:
        try:
            time.sleep(_CACHE_TTL_SECONDS)
            check_for_updates()
        except Exception as e:
            logger.warning("Auto-update error: %s", e)


def start_background_updater():
    """Start background cache updater (run once at app startup)."""
    global _BACKGROUND_THREAD
    if _BACKGROUND_THREAD is not None and _BACKGROUND_THREAD.is_alive():
        return
    _BACKGROUND_THREAD = threading.Thread(target=auto_update_check, daemon=True, name="ghana-updater")
    _BACKGROUND_THREAD.start()
    logger.info("Ghana background updater started")


# ─── Advanced Search (cache + on-the-fly listing scraping) ────────────────────
_MAX_SEARCH_PAGES = 20


def advanced_search(query: str, max_pages: int = 20) -> list:
    """
    Ultra-fast search:
      1. Check local cache.
      2. If < 15 results, scrape listing pages on-the-fly until enough found.
    Returns raw song objects (same shape as cache items).
    """
    q = query.lower().strip()
    if not q:
        return get_cached_songs()

    # 1. Cache results
    results = search_cached_songs(query)
    if len(results) >= 15:
        logger.info("Advanced search '%s': %d from cache", q, len(results))
        return results

    # 2. Not enough — scrape pages until we find enough
    logger.info("Advanced search '%s': cache=%d, scraping...", q, len(results))
    found_urls = {s["page_url"] for s in results}

    for page in range(1, min(max_pages + 1, 101)):
        listings = scrape_listing(page=page)
        if not listings:
            break
        for item in listings:
            artist_l = (item.get("artist") or "").lower()
            title_l  = (item.get("title")  or "").lower()
            if q in artist_l or q in title_l:
                if item["page_url"] not in found_urls:
                    results.append({
                        **item,
                        "mp3_url": None,
                        "has_mp3": False,
                        "scraped_at": datetime.now().isoformat(),
                    })
                    found_urls.add(item["page_url"])
        if len(results) >= 15:
            break
        time.sleep(0.15)   # gentle rate limit

    logger.info("Advanced search '%s': %d total results", q, len(results))
    return results


# ─── Super Search (80-worker army with supervisor) ────────────────────────────
_MAX_SUPER_WORKERS = 80
_MAX_SUPER_PAGES   = 500
_SUPER_MIN_RESULTS = 20
_SUPER_MAX_TIME    = 15.0   # seconds — if we don't have results by now, return what we have


def super_search(query: str,
                 max_workers: int = _MAX_SUPER_WORKERS,
                 max_pages: int = _MAX_SUPER_PAGES,
                 min_results: int = _SUPER_MIN_RESULTS) -> list:
    """
    80-worker parallel search:
      • Supervisor assigns each worker a unique page number (no overlap)
      • Workers send matching songs back to the supervisor
      • Supervisor stops all workers once enough results found or time runs out
      • Fast: can scan 80 pages in ~2 seconds (network-bound)
    """
    q = query.lower().strip()
    if not q:
        return get_cached_songs()

    # 1. Cache first — instant
    cache_results = search_cached_songs(query)
    if len(cache_results) >= min_results:
        logger.info("Super search '%s': %d from cache (instant)", q, len(cache_results))
        return cache_results

    results = list(cache_results)
    found_urls = {s["page_url"] for s in results}
    next_page = [1]         # mutable counter (list for closure)
    stop_event = threading.Event()
    page_lock = threading.Lock()
    results_lock = threading.Lock()

    def _worker(wid: int):
        """One super-worker: get page, scrape, report matches, repeat."""
        while not stop_event.is_set():
            # Supervisor assigns unique page
            with page_lock:
                page = next_page[0]
                if page > max_pages:
                    return
                next_page[0] += 1

            try:
                listings = scrape_listing(page=page)
            except Exception as e:
                logger.debug("Super-worker %d page %d: %s", wid, page, e)
                continue

            if stop_event.is_set():
                return
            if not listings:
                continue   # empty page — keep going (site may have gaps)

            # Find matches
            page_matches = []
            for item in listings:
                artist_l = (item.get("artist") or "").lower()
                title_l  = (item.get("title")  or "").lower()
                if q in artist_l or q in title_l:
                    if item["page_url"] not in found_urls:
                        page_matches.append(item)

            if page_matches:
                with results_lock:
                    for item in page_matches:
                        if item["page_url"] not in found_urls:
                            results.append({
                                **item,
                                "mp3_url": None,
                                "has_mp3": False,
                                "scraped_at": datetime.now().isoformat(),
                            })
                            found_urls.add(item["page_url"])
                    # ENOUGH!  Tell everyone to stop
                    if len(results) >= min_results:
                        logger.info(
                            "Super search '%s': target hit (%d results on page %d), stopping army",
                            q, len(results), page
                        )
                        stop_event.set()
                        return
            # Tiny politeness pause before grabbing next page
            time.sleep(0.02)

    # Launch army with 20 ms stagger to avoid server spike
    threads = []
    t0 = time.time()
    for i in range(max_workers):
        t = threading.Thread(target=_worker, args=(i,), daemon=True, name=f"super-{i}")
        t.start()
        threads.append(t)
        time.sleep(0.02)
        # If target already hit during stagger, bail early
        if stop_event.is_set():
            break

    # Supervisor monitors until done or timeout
    while time.time() - t0 < _SUPER_MAX_TIME:
        alive = [t for t in threads if t.is_alive()]
        if not alive:
            break
        time.sleep(0.1)

    stop_event.set()
    for t in threads:
        if t.is_alive():
            t.join(timeout=0.5)

    elapsed = time.time() - t0
    logger.info("Super search '%s': %d results in %.2f s (last page assigned: %d)",
                q, len(results), elapsed, next_page[0] - 1)
    return results


# ─── Async cache kickoff (never blocks HTTP thread) ───────────────────────────
def _kickoff_cache_build(max_pages: int = 3):
    """Start a background thread to build the cache — returns instantly."""
    def _builder():
        try:
            build_cache(max_pages=max_pages)
        except Exception as e:
            logger.info("Background cache build failed (will retry): %s", e)
    threading.Thread(target=_builder, daemon=True, name="async-cache-build").start()


#  Allow callers to point at super_search when they want speed
search_halmblog = super_search


# ─── Pagination helpers ───────────────────────────────────────────────────────
_PER_PAGE = 20


def get_total_songs() -> int:
    return len(_load_cache().get("songs", []))


def get_total_pages(per_page: int = _PER_PAGE) -> int:
    total = get_total_songs()
    return max(1, (total + per_page - 1) // per_page)


def get_ghana_songs_cached(page: int = 1, limit: int = _PER_PAGE, force_raw: bool = False) -> list:
    """Return songs from cache, sliced by page/limit.
    On an empty cache, returns [] immediately so the frontend shows an
    empty state and polls. The background thread builds the cache in
    parallel so the next request has data (no request-thread blocking).
    """
    cache = _load_cache()
    if force_raw:
        logger.info("Cache force rebuild triggered...")
        # Fast async kickoff — do NOT block the HTTP thread
        _kickoff_cache_build()

    if not cache.get("songs"):
        # Cache not ready yet — return empty so the UI can show a friendly message
        logger.info("Cache empty — returning [] (background build in progress)")
        return []


    songs = cache.get("songs", [])
    start = (page - 1) * limit
    page_songs = songs[start : start + limit]

    return [
        {
            "title": s.get("title", ""),
            "artist": s.get("artist", ""),
            "page_url": s["page_url"],
            "thumbnail": s.get("thumbnail", ""),
            "date": s.get("date", ""),
            "mp3_url": s.get("mp3_url"),
            "has_mp3": bool(s.get("mp3_url")),
        }
        for s in page_songs
    ]


# ─── Deep Cache (background, many pages) ──────────────────────────────────────
def build_deep_cache(max_pages: int = 100) -> dict:
    """Scrape many listing pages for a huge local cache."""
    cache = _load_cache()
    existing = _by_url(cache)
    all_songs = list(existing.values())
    new_cnt = 0

    for p in range(1, max_pages + 1):
        listings = scrape_listing(page=p)
        if not listings:
            break
        for item in listings:
            key = item["page_url"]
            if key not in existing:
                all_songs.append({
                    **item,
                    "mp3_url": None,
                    "has_mp3": False,
                    "scraped_at": datetime.now().isoformat(),
                })
                new_cnt += 1
        time.sleep(0.25)

    cache["songs"] = all_songs
    cache["last_updated"] = datetime.now().isoformat()
    with _CACHE_LOCK:
        _save_cache(cache)
    logger.info("Deep cache: %d total songs (%d new)", len(all_songs), new_cnt)
    return cache


def resume_deep_cache(max_pages: int = 100) -> int:
    """Continue deep cache from last known position."""
    cache = _load_cache()
    existing = _by_url(cache)
    current = len(existing)
    start_page = (current // 15) + 1
    logger.info("Deep cache resume: %d songs, start page %d", current, start_page)

    new_cnt = 0
    for p in range(start_page, max_pages + 1):
        listings = scrape_listing(page=p)
        if not listings:
            break
        for item in listings:
            key = item["page_url"]
            if key not in existing:
                cache["songs"].append({
                    **item,
                    "mp3_url": None,
                    "has_mp3": False,
                    "scraped_at": datetime.now().isoformat(),
                })
                new_cnt += 1
        time.sleep(0.35)

    if new_cnt > 0:
        cache["last_updated"] = datetime.now().isoformat()
        with _CACHE_LOCK:
            _save_cache(cache)
    logger.info("Deep cache resume: +%d (total %d)", new_cnt, len(cache["songs"]))
    return new_cnt
