"""
Halmblog.com Ghana Music scraper — powered by requests + BeautifulSoup.
Extracts song listings and direct MP3 file URLs.

Uses a persistent SQLite cache (see cache_db.py) so repeat loads are instant.
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
import cache_db

logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────
CATEGORY_URL = "https://www.halmblog.com/category/listen/ghana-music/"
BASE_URL = "https://www.halmblog.com"
PAGE_TIMEOUT = 20

_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SECONDS = 1800          # how often we auto-refresh page 1
_BACKGROUND_THREAD = None

# Simple per-page memory cache (60s)
_song_page_cache = {}
_PAGE_CACHE_TTL = 60

# Consecutive song-page fetch failures — detects a WAF/IP block so the
# background MP3 filler can back off instead of hammering a blocked endpoint.
_mp3_fail_streak = 0
_MP3_BLOCK_THRESHOLD = 6

# True when the most recent page fetch fell back to a Wayback Machine capture
# (i.e. halmblog's WAF is blocking our IP) — used to slow the MP3 filler and
# to expose wayback serving URLs as download fallbacks.
_fetch_via_wayback = False
_wayback_lock = threading.Lock()
_last_wayback_ts = 0.0
# Save Page Now is expensive for archive.org and rate-limited (429s when
# hammered) — keep at least this many seconds between fresh captures.
_WAYBACK_MIN_INTERVAL = 30.0
# If the newest Wayback snapshot of a page is older than this, request a
# fresh Save Page Now capture instead of serving the stale copy (used for the
# live page-1 feed so new songs are actually picked up).
_WAYBACK_STALE_SECONDS = 6 * 3600

# Guard so only one deep-cache crawl runs at a time — the manual "Load More
# Pages" button and the background auto-crawler share the same worker.
_DEEP_CACHE_LOCK = threading.Lock()
# Halmblog's archive has gaps (e.g. page 151 404s while 152 has songs), and
# Wayback coverage of deep pages is sparse (largest known hole is ~149 pages).
# The crawl skips empty pages cheaply but stops after this many in a row —
# past the last covered page that signals the reachable end of the archive.
_DEEP_EMPTY_STOP = 150


def mp3_filler_blocked() -> bool:
    """True when recent song-page fetches have failed en masse (WAF/IP block)."""
    return _mp3_fail_streak >= _MP3_BLOCK_THRESHOLD

import requests as _requests


_USER_AGENTS = [
    # Browser-like UAs, rotated on retries. Do NOT send an explicit
    # Accept-Encoding (esp. br) or Sec-Fetch-* headers — halmblog.com's WAF
    # answers that combo with 403s / undecodable compressed bodies. Requests
    # handles gzip/deflate on its own.
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]


def _build_headers(ua: str) -> dict:
    return {
        "User-Agent": ua,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.halmblog.com/",
    }


# Reader-proxy fallback for when halmblog.com's WAF blocks our server IP
# entirely (e.g. Cloudflare flagging datacenter IPs). r.jina.ai's reader mode
# is keyless and successfully renders these pages (as markdown), so it's the
# default; extra proxies can be added via HALMBLOG_READER_PROXIES (comma-
# separated prefixes) and are tried before it. Prefixes must serve the target
# URL at <proxy><url>. Wayback SPN is the last resort.
_READER_PROXIES = [
    p.strip() for p in os.environ.get("HALMBLOG_READER_PROXIES", "").split(",") if p.strip()
] or ["https://r.jina.ai/"]
_WAYBACK_SAVE = "https://web.archive.org/save/"
_WAYBACK_LATEST = "https://web.archive.org/web/2/"


def _normalize_href(href: str) -> str:
    """Strip Wayback Machine URL prefixes so cached links point at the real
    site. Snapshots rewrite links to web.archive.org/web/<ts><modifier>/<url>
    where <modifier> is e.g. 'im_' (image), 'if_' (iframe), 'id_' or empty —
    strip that whole prefix (including any modifier)."""
    if not href:
        return href
    m = re.search(r"web\.archive\.org/web/\d+(?:[a-z]{2}_)?/", href)
    if m:
        return href[m.end():]
    return href


def _reader_proxy_fetch(proxy: str, url: str) -> str:
    resp = _requests.get(
        proxy + url,
        # r.jina.ai answers browser User-Agents (and X-Return-Format: html)
        # with 403 on its keyless tier — send a bare UA, no X-Return-Format.
        # Its default reader mode returns markdown, which the scrapers parse.
        headers={"User-Agent": "Mozilla/5.0", "X-Timeout": "20"},
        timeout=PAGE_TIMEOUT + 10,
    )
    head = resp.text[:500]
    # Accept either real HTML ("<") or r.jina.ai's markdown ("Title:" header).
    if resp.status_code != 200 or ("<" not in head and "Title:" not in head):
        raise _requests.HTTPError(f"reader proxy returned {resp.status_code} for {url}")
    return resp.text


def _wayback_timestamp(url: str):
    """Extract the snapshot capture time from a Wayback URL like
    https://web.archive.org/web/20260805041930/<target> — returns a unix
    timestamp, or None when the URL isn't a dated snapshot."""
    m = re.search(r"web\.archive\.org/web/(\d{14})", url)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").timestamp()
    except ValueError:
        return None


def _throttle_wayback():
    """Space out Save Page Now requests (archive.org rate-limits them).
    Called before each SPN save; serialises only the throttle decision, not
    the network call itself."""
    global _last_wayback_ts
    with _wayback_lock:
        wait = _WAYBACK_MIN_INTERVAL - (time.time() - _last_wayback_ts)
        if wait > 0:
            time.sleep(wait)
        _last_wayback_ts = time.time()


def _fetch_wayback(url: str, timeout: int = 90, prefer_fresh: bool = False, allow_spn: bool = True) -> str:
    """Fetch url through the Wayback Machine. Tries the latest EXISTING
    snapshot first (a cheap read, no load on archive.org). When prefer_fresh
    is set (live listing pages) and that snapshot is stale, a fresh Save Page
    Now capture is requested; if the capture is rate-limited we fall back to
    the stale snapshot rather than failing. allow_spn controls whether a Save
    Page Now capture is attempted at all — deep archive pages pass False so
    the crawl doesn't hammer archive.org's rate-limited SPN endpoint.
    archive.org's crawler usually bypasses halmblog's WAF, so this keeps the
    Ghana feed + MP3 lookups working even when the app's IP is blocked."""
    headers = {"User-Agent": _USER_AGENTS[0]}
    latest_text = None

    # 1) Latest existing snapshot (web/2/ = "most recent"). If no snapshot
    # exists it redirects to the live site (which is blocked for us) and we
    # fall through to SPN (when allowed).
    try:
        r = _requests.get(_WAYBACK_LATEST + url, headers=headers, timeout=timeout, allow_redirects=True)
        if "web.archive.org/web/" in r.url and r.status_code == 200 and "<" in r.text[:500]:
            latest_text = r.text
            snap_ts = _wayback_timestamp(r.url)
            stale = snap_ts is None or (time.time() - snap_ts) > _WAYBACK_STALE_SECONDS
            if not prefer_fresh or not stale:
                return latest_text
    except _requests.RequestException:
        pass

    # 2) Fresh capture via Save Page Now (throttled — expensive for archive.org).
    # Deep archive pages opt out so we don't exhaust archive.org's rate limits
    # crawling pages that have no snapshot yet.
    if not allow_spn:
        if latest_text:
            return latest_text
        raise _requests.HTTPError(f"no Wayback snapshot for {url} (SPN disabled)")
    try:
        _throttle_wayback()
        resp = _requests.get(_WAYBACK_SAVE + url, headers=headers, timeout=timeout, allow_redirects=True)
        if resp.status_code not in (200, 201, 202):
            raise _requests.HTTPError(f"Save Page Now returned {resp.status_code} for {url}")
        snap = resp.url if "web.archive.org/web/" in resp.url else _WAYBACK_LATEST + url
        r2 = _requests.get(snap, headers=headers, timeout=timeout)
        if r2.status_code != 200 or "<" not in r2.text[:500]:
            raise _requests.HTTPError(f"snapshot fetch returned {r2.status_code} for {url}")
        return r2.text
    except _requests.RequestException:
        if latest_text:
            logger.warning("Wayback SPN failed for %s — serving stale snapshot instead", url)
            return latest_text
        raise


def _set_via_wayback(value: bool):
    """Record whether the latest successful fetch used a Wayback capture."""
    global _fetch_via_wayback
    _fetch_via_wayback = value


def _fetch_html(url: str, retries: int = 2, prefer_fresh: bool = False, allow_spn: bool = True) -> str:
    """Fetch raw HTML via requests, retrying with rotated User-Agents when the
    WAF blocks us (403/429/5xx or an undecodable compressed body).
    If the site blocks our IP entirely, falls back to a reader proxy.
    prefer_fresh is forwarded to the Wayback fallback so live listing pages
    get a new capture instead of a stale snapshot; allow_spn is forwarded so
    deep archive pages skip the rate-limited Save Page Now endpoint.
    Returns the page text; raises on final failure (callers handle it)."""
    last_err = None
    for attempt in range(retries + 1):
        ua = _USER_AGENTS[attempt % len(_USER_AGENTS)]
        session = _requests.Session()
        try:
            resp = session.get(url, headers=_build_headers(ua), timeout=PAGE_TIMEOUT)
            if resp.status_code in (403, 429, 500, 502, 503, 504):
                raise _requests.HTTPError(f"{resp.status_code} Client/Server Error for {url}")
            resp.raise_for_status()
            text = resp.text
            if "<" not in text[:500]:
                raise _requests.HTTPError(f"non-HTML body (undecoded compression?) for {url}")
            _set_via_wayback(False)
            return text
        except _requests.RequestException as e:
            last_err = e
            logger.debug("Fetch attempt %d failed for %s: %s", attempt + 1, url, e)
            time.sleep(1.0 + attempt)   # gentle backoff between retries

    # Direct fetching is blocked (WAF/IP) — try configured reader proxies,
    # then a fresh Wayback Machine capture. Both listing AND song pages use
    # the fallback so the feed keeps updating and MP3 links stay findable
    # from a blocked IP; the SPN throttle + filler pacing keep archive.org
    # rate limits happy.
    for proxy in _READER_PROXIES:
        try:
            text = _reader_proxy_fetch(proxy, url)
            _set_via_wayback(False)
            logger.info("Fetched %s via reader proxy %s", url, proxy)
            return text
        except _requests.RequestException as pe:
            logger.warning("Reader proxy %s failed for %s: %s", proxy, url, pe)

    if "halmblog.com" in url:
        try:
            text = _fetch_wayback(url, prefer_fresh=prefer_fresh, allow_spn=allow_spn)
            _set_via_wayback(True)
            logger.info("Fetched %s via Wayback SPN (IP blocked)", url)
            return text
        except _requests.RequestException as pe:
            logger.warning("Wayback SPN failed for %s: %s", url, pe)

    raise last_err


# ─── Artist / Title Parser ───────────────────────────────────────────────────
def _split_artist_title(raw: str) -> tuple:
    """
    Parse a halmblog title like:
      'Donzy – Blackstars' -> ('Donzy', 'Blackstars')
      'Young Legend – Let Me Go' -> ('Young Legend', 'Let Me Go')
      'Nervous by Shatta Wale' -> ('Shatta Wale', 'Nervous')
      'Wicked One by Ha-Di' -> ('Ha-Di', 'Wicked One')
    Falls back to ('', raw) if no separator found.
    """
    s = raw.strip()
    if not s:
        return ("", "")

    # Spaced separators ("Artist – Title") first — unambiguous.
    for sep in ["\u2009", "\u00a0", " – ", " — ", " - "]:
        if sep in s:
            parts = s.split(sep, 1)
            artist = parts[0].strip()
            title  = parts[1].strip()
            return (artist, title)

    # "Title by Artist" — use the LAST "by" so hyphens in names (e.g. "Ha-Di")
    # aren't misread as separators, and "Stand By Me by Yaw" parses correctly.
    m = list(re.finditer(r'\bby\s+', s, re.IGNORECASE))
    if m:
        last = m[-1]
        return (s[last.end():].strip(), s[:last.start()].strip())

    # Bare separators last — risky with hyphens in artist names ("Ha-Di"),
    # only used when nothing else matched.
    for sep in ["–", "—", "-"]:
        if sep in s:
            parts = s.split(sep, 1)
            artist = parts[0].strip()
            title  = parts[1].strip()
            return (artist, title)

    return ("", s)


# ─── Persistent Cache Helpers ─────────────────────────────────────────────────
def _load_cache() -> dict:
    return cache_db.load_cache()


def _save_cache(data: dict):
    try:
        cache_db.save_cache(data)
    except Exception as e:
        logger.warning("Failed to save cache to SQLite: %s", e)


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
        # Page 1 is the live feed — ask for a fresh capture when stale. Deep
        # pages (page > 1) only use existing Wayback snapshots; no SPN, so the
        # deep crawl doesn't exhaust archive.org's rate limit.
        html = _fetch_html(url, prefer_fresh=(page == 1), allow_spn=(page == 1))
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
        link = _normalize_href(a["href"])
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

    # Reader proxies (r.jina.ai) return the page as markdown, not HTML — no
    # <article>/<h2> tags. Fall back to parsing its [title](url) song links.
    if not results:
        results = _parse_markdown_listing(html)

    logger.info("Halmblog listing page %d -> %d songs", page, len(results))
    return results


def _parse_markdown_listing(html: str) -> list:
    """Parse a listing page rendered as markdown by r.jina.ai's reader mode.
    Songs appear as '## [Title](https://www.halmblog.com/listen/<slug>/ "Title")'
    with a '[![alt](thumb)](listen_url)' image link to pair a thumbnail with."""
    results = []
    seen = set()
    # Pair thumbnails with their listen URL:
    #   [![alt](img_url)](listen_url "Title")
    thumb_by_listen = {}
    for img_url, listen_url in re.findall(
        r"\[!\[[^\]]*\]\((https?://[^\s)\"]+)\)\]\((https?://[^\s)\"]+)(?:\s+\"[^\"]*\")?\)",
        html,
    ):
        listen_url = _normalize_href(listen_url)
        if listen_url.startswith(BASE_URL + "/listen/"):
            thumb_by_listen.setdefault(listen_url, _normalize_href(img_url))

    for title, url in re.findall(
        r"\[([^\]]+)\]\((https?://[^\s)\"]+)(?:\s+\"[^\"]*\")?\)", html
    ):
        url = _normalize_href(url)
        if not (url.startswith(BASE_URL + "/listen/") or url.startswith("https://www.halmblog.com/listen/")):
            continue
        if url in seen:
            continue
        seen.add(url)
        artist, title = _split_artist_title(title)
        results.append({
            "title": title,
            "artist": artist,
            "page_url": url,
            "thumbnail": thumb_by_listen.get(url, ""),
            "date": "",
            "page_slug": url.rstrip("/").split("/")[-1],
        })
    return results


# ─── Scrape Individual Song Page ─────────────────────────────────────────────
def _markdown_title(html: str) -> str:
    """Extract the page title from r.jina.ai reader-mode output ("Title: ...")."""
    m = re.search(r"^Title:\s*(.+)$", html, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _markdown_thumbnail(html: str) -> str:
    """Extract the first song-image URL from r.jina.ai markdown output."""
    m = re.search(
        r"!\[[^\]]*\]\((https?://[^\s)\"]+\.(?:jpe?g|png|webp)[^\s)\"]*)\)",
        html, re.IGNORECASE,
    )
    return _normalize_href(m.group(1)) if m else ""


def scrape_song_page(page_url: str) -> dict:
    """Scrape a halmblog song page for direct MP3 URL + metadata.

    Returns mp3_url (normalised to the real www.halmblog.com URL) and
    mp3_url_fallback — when the page was fetched via a Wayback snapshot, the
    raw web.archive.org serving URL of the MP3, so downloads can retry
    through archive.org when halmblog's WAF blocks our IP."""
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
        return {"title": "", "artist": "", "mp3_url": None, "mp3_url_fallback": None, "thumbnail": "", "page_url": page_url, "fetch_error": str(e)}

    h1 = soup.find("h1")
    raw_t = h1.get_text(strip=True) if h1 else ""
    artist, title = _split_artist_title(raw_t)

    # Reader proxies (r.jina.ai) return markdown, not HTML — pull the title
    # from its "Title: <song> by <artist>" header and thumbnails from images.
    if not raw_t:
        raw_t = _markdown_title(html)
        artist, title = _split_artist_title(raw_t)

    # Gather every candidate MP3 href: <audio> src, anchors containing ".mp3"
    # (halmblog puts the real file in a plain <a href="...mp3"> in the post
    # body — no audio tag, no download-button class), then a regex over the
    # raw HTML as a final net (also catches links in markdown output).
    mp3_url = None
    mp3_url_fallback = None
    candidates = []
    audio = soup.find("audio")
    if audio:
        src = audio.get("src") or ""
        src_tag = audio.find("source")
        if src_tag:
            src = src_tag.get("src") or src
        candidates.append(src)
    candidates.extend(a["href"] for a in soup.find_all("a", href=True))
    candidates.extend(re.findall(r'https?://[^\s"<>]+\.mp3[^\s"<>]*', html, re.IGNORECASE))

    for cand in candidates:
        raw = (cand or "").strip()
        if ".mp3" not in raw.lower():
            continue
        normalized = _normalize_href(raw)
        if not (normalized.lower().startswith("http://") or normalized.lower().startswith("https://")):
            continue
        mp3_url = normalized
        if normalized != raw:
            # Raw href is a Wayback serving URL (web.archive.org/web/<ts>im_/...)
            # — keep it as a download fallback.
            mp3_url_fallback = raw
        break

    # thumbnail
    thumb = ""
    entry = soup.find("article") or soup.find("div", class_="entry-content") or soup
    img = entry.find("img")
    if img:
        thumb = _normalize_href(img.get("data-src") or img.get("data-lazy-src") or img.get("src", ""))
    if not thumb:
        thumb = _markdown_thumbnail(html)

    result = {
        "title": title or raw_t,
        "artist": artist,
        "mp3_url": mp3_url,
        "mp3_url_fallback": mp3_url_fallback,
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
                        # Preserve existing mp3_url/thumbnail if the fresh
                        # listing didn't provide one (markdown listings often
                        # have no image).
                        all_songs.insert(0, {
                            **item,
                            "mp3_url": song.get("mp3_url"),
                            "has_mp3": bool(song.get("mp3_url")),
                            "thumbnail": item.get("thumbnail") or song.get("thumbnail", ""),
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
    global _mp3_fail_streak
    cache = _load_cache()
    filled = 0
    pending = [s for s in cache.get("songs", []) if not s.get("mp3_url")][:limit]

    for s in pending:
        try:
            details = scrape_song_page(s["page_url"])
            if details.get("fetch_error"):
                _mp3_fail_streak += 1
                logger.debug("Song page blocked: %s (streak %d)", s["page_url"], _mp3_fail_streak)
            else:
                _mp3_fail_streak = 0
                if details.get("mp3_url"):
                    s["mp3_url"] = details["mp3_url"]
                    s["mp3_url_fallback"] = details.get("mp3_url_fallback")
                    s["has_mp3"] = True
                    s["thumbnail"] = details.get("thumbnail") or s.get("thumbnail", "")
                    filled += 1
            # Be gentle — the WAF rate-limits aggressive crawlers, and when the
            # site is IP-blocked (fetches going via Wayback SPN) pace much slower.
            time.sleep(15.0 if _fetch_via_wayback else 0.8)
        except Exception as e:
            _mp3_fail_streak += 1
            logger.debug("MP3 fill failed for %s: %s", s["page_url"], e)

    if filled > 0:
        with _CACHE_LOCK:
            _save_cache(cache)
    logger.info("MP3 fill pass complete: %d/%d found (block streak %d)", filled, len(pending), _mp3_fail_streak)
    return filled


def check_for_updates() -> int:
    """Scrape page 1 listing, add any new songs AND reorder existing ones.
    Returns total number of changes (new + reordered)."""
    cache = _load_cache()
    existing = _by_url(cache)
    listings = scrape_listing(page=1)
    added = 0
    reordered = 0
    meta_changed = 0

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
                            "thumbnail": item.get("thumbnail") or song.get("thumbnail", ""),
                            "scraped_at": song.get("scraped_at", datetime.now().isoformat()),
                        })
                        reordered += 1
                    else:
                        # Update metadata in place (title, thumbnail, date may change)
                        # — keep the old thumbnail if the fresh listing has none.
                        old = cache["songs"][i]
                        new_thumb = item.get("thumbnail") or old.get("thumbnail", "")
                        if (item["title"], item["artist"], item["date"], new_thumb) != (
                            old.get("title"), old.get("artist"), old.get("date"), old.get("thumbnail", "")
                        ):
                            old.update({
                                "title": item["title"],
                                "artist": item["artist"],
                                "thumbnail": new_thumb,
                                "date": item["date"],
                            })
                            meta_changed += 1
                    break

    total_changes = added + reordered + meta_changed
    if total_changes > 0:
        cache["last_updated"] = datetime.now().isoformat()
        with _CACHE_LOCK:
            _save_cache(cache)
        logger.info("Ghana cache updated: +%d new, %d reordered, %d metadata (total %d)", added, reordered, meta_changed, len(cache["songs"]))
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
            "mp3_url_fallback": s.get("mp3_url_fallback"),
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
    deepest = 0

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
                existing[key] = True
                new_cnt += 1
        deepest = p
        time.sleep(0.25)

    cache["songs"] = all_songs
    cache["max_page"] = deepest
    cache["last_updated"] = datetime.now().isoformat()
    with _CACHE_LOCK:
        _save_cache(cache)
    logger.info("Deep cache: %d total songs (%d new, deepest page %d)", len(all_songs), new_cnt, deepest)
    return cache


def resume_deep_cache(max_pages: int = 100) -> int:
    """Continue deep cache from the deepest page we already reached.
    Progress is persisted as 'max_page' in the cache file, so repeated
    clicks genuinely walk further into the archive (page 1 is kept fresh
    by check_for_updates). max_pages is how many MORE pages to crawl past
    the current max_page. Saves incrementally so long crawls aren't lost.
    Only one crawl runs at a time (manual button + background crawler share
    the worker); a concurrent call returns 0 immediately.
    """
    if not _DEEP_CACHE_LOCK.acquire(blocking=False):
        logger.info("Deep cache crawl already running — skipping this request")
        return 0

    try:
        cache = _load_cache()
        existing = _by_url(cache)
        start_page = int(cache.get("max_page") or 1) + 1
        end_page = start_page + max_pages - 1
        new_cnt = 0
        deepest = start_page - 1   # last page that yielded songs
        last_tried = start_page - 1  # last page actually requested
        consecutive_empty = 0
        logger.info("Deep cache resume: %d songs, crawl pages %d–%d", len(existing), start_page, end_page)

        for p in range(start_page, end_page + 1):
            last_tried = p
            listings = scrape_listing(page=p)
            if not listings:
                # Archive gap (404 page, or no Wayback snapshot) — keep going,
                # but bail out once it looks like the real end of the archive.
                consecutive_empty += 1
                if consecutive_empty >= _DEEP_EMPTY_STOP:
                    break
                continue
            consecutive_empty = 0
            added_on_page = 0
            for item in listings:
                key = item["page_url"]
                if key not in existing:
                    cache["songs"].append({
                        **item,
                        "mp3_url": None,
                        "has_mp3": False,
                        "scraped_at": datetime.now().isoformat(),
                    })
                    existing[key] = True
                    new_cnt += 1
                    added_on_page += 1
            deepest = p
            time.sleep(0.35)
            # Save progress incrementally so the live song count updates in the UI
            # and a long crawl isn't lost if the server restarts.
            if added_on_page > 0 or deepest == end_page:
                cache["max_page"] = deepest
                cache["last_updated"] = datetime.now().isoformat()
                with _CACHE_LOCK:
                    _save_cache(cache)

        # Persist how far we actually walked (even past empty gap pages) so the
        # next resume skips the gaps we already know are empty.
        if last_tried > start_page - 1:
            cache["max_page"] = last_tried
            cache["last_updated"] = datetime.now().isoformat()
            with _CACHE_LOCK:
                _save_cache(cache)
        logger.info("Deep cache resume: +%d (total %d, deepest page %d)", new_cnt, len(cache["songs"]), deepest)
        return new_cnt
    finally:
        _DEEP_CACHE_LOCK.release()
