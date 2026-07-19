"""
search.py — Slug-based search for NKiri and DramaKey.
No index, no Google. Direct HEAD requests against known URL patterns.

Commands:
  search <title>              — full search, both sites, all patterns
  fsearch <title>             — fast search: proven patterns first, cancels
                                remaining if hit found; falls back to full
                                search if no hit
  fsearch <title> korean      — content hint: prioritise korean patterns
  fsearch <title> chinese     — content hint: prioritise chinese patterns
  fsearch <title> thai        — content hint: prioritise thai patterns
  fsearch <title> nollywood   — content hint: prioritise nollywood patterns
"""

import os
import re
import sys
import json
import time
import datetime
import threading
from urllib.parse import quote, urlparse

# Lazy `requests`: the legacy search path and cache use it, but importing it
# (+ urllib3 + charset_normalizer, ~700ms) would block every launch since main
# imports this module at startup for ensure_async(). Loads on first use.
class _LazyRequests:
    _mod = None
    def _load(self):
        if _LazyRequests._mod is None:
            import requests as _r
            _LazyRequests._mod = _r
        return _LazyRequests._mod
    def __getattr__(self, name):
        return getattr(self._load(), name)

requests = _LazyRequests()
from concurrent.futures import ThreadPoolExecutor, as_completed

# Async engine is optional. If aiohttp is missing (or asyncio can't start),
# search transparently falls back to the legacy requests/ThreadPool path
# below — nothing here is load-bearing for the synchronous fallback.
import subprocess

# Detect aiohttp WITHOUT importing it — the actual `import aiohttp` costs
# ~400ms (C extensions) and would block every launch even though search
# isn't touched until the user runs it. find_spec is cheap; the real import
# happens lazily in the async functions below, so the cost is paid only on
# the first search, not at startup.
import importlib.util as _ilu
USE_ASYNC = _ilu.find_spec('aiohttp') is not None
asyncio = None   # bound lazily by _ensure_async_imported()
aiohttp = None


def ensure_async():
    """One-time, non-blocking bootstrap so users who installed before the async
    engine existed (and never re-run setup.sh) still pick it up. Code updates
    reach them via the launch-time `git reset --hard`, but a new pip dep does
    not — so we self-install aiohttp.

    Critical: this must NOT slow down or hang startup. On Termux, installing
    aiohttp compiles C extensions and can take minutes, so the install runs in
    a background daemon thread with a timeout, and a marker file ensures it is
    only ever attempted ONCE — a slow or failed install is never retried on
    later launches. Legacy search works the whole time; async simply takes over
    on the next launch once aiohttp is present. Returns immediately.
    """
    if USE_ASYNC:
        return
    marker = os.path.join(CONFIG_DIR, '.aiohttp_install_tried')
    if os.path.exists(marker):
        return  # already tried once — don't block or retry, legacy path is fine

    def _install():
        try:
            # Record the attempt up front so a hang/crash can't cause a re-run.
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(marker, 'w') as f:
                f.write(str(int(time.time())))
        except Exception:
            pass
        try:
            subprocess.run(
                ['pip', 'install', 'aiohttp', '--break-system-packages', '-q'],
                check=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=300,
            )
            global USE_ASYNC, aiohttp, asyncio
            import asyncio as _aio
            import aiohttp as _http
            asyncio = _aio
            aiohttp = _http
            USE_ASYNC = True
            safe_print("[*] Faster search engine ready — active on next launch.")
        except Exception:
            # Non-fatal and silent: legacy search keeps working, and the marker
            # above means we won't try again.
            pass

    threading.Thread(target=_install, daemon=True, name='aiohttp-bootstrap').start()

from .downloader import safe_print, UA_DESKTOP, BASE_DIR, CONFIG_DIR
from .messages import render as render_message

# ─── CACHE ────────────────────────────────────────────────────
CACHE_FILE    = os.path.join(os.path.dirname(__file__), '.search_cache.json')
CACHE_TTL     = 86400  # 24 hours in seconds

def _is_termux():
    return os.path.exists('/storage/emulated/0')

def _search_workers(default=12):
    """Keep mobile search responsive by avoiding large request bursts."""
    workers = 6 if _is_termux() else default
    try:
        config_path = os.path.join(CONFIG_DIR, '.config.json')
        if os.path.exists(config_path):
            with open(config_path, encoding='utf-8') as f:
                workers = int(json.load(f).get('search_workers', workers))
    except Exception:
        pass
    # Clamp to a sane range. Ceiling is an absolute burst cap (not `default`),
    # so an explicit config value can raise workers above the default while
    # still guarding against a runaway request burst.
    return max(2, min(workers, 32))

def _search_timeout(default=45):
    timeout = default
    try:
        config_path = os.path.join(CONFIG_DIR, '.config.json')
        if os.path.exists(config_path):
            with open(config_path, encoding='utf-8') as f:
                timeout = int(json.load(f).get('search_timeout', timeout))
    except Exception:
        pass
    return timeout

def _load_cache():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_cache(cache):
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass

def _cache_get(base):
    cache = _load_cache()
    now   = time.time()
    entry = cache.get(base)
    if entry and (now - entry.get('ts', 0)) < CACHE_TTL:
        # JSON deserialises tuples as lists — convert back
        return [tuple(r) for r in entry.get('results', [])]
    return None

def _cache_set(base, results):
    cache = _load_cache()
    cache[base] = {'results': results, 'ts': time.time()}
    # Keep cache from growing too large — max 200 entries
    if len(cache) > 200:
        oldest = sorted(cache.items(), key=lambda x: x[1].get('ts', 0))
        for k, _ in oldest[:50]:
            del cache[k]
    _save_cache(cache)

# ─── SLUG PATTERNS ────────────────────────────────────────────
# Wave 1 = proven to hit from real data (fast probe)
# Wave 2 = fallback patterns (only runs if wave 1 misses)

NKIRI_WAVE1 = [
    '{base}-korean-drama',
    '{base}',
]
NKIRI_WAVE2 = [
    '{base}-complete-korean-drama',
    '{base}-complete-tv-series',
    '{base}-complete-nollywood',
    '{base}-complete-chinese-drama',
    '{base}-complete-japanese-drama',
    '{base}-{season}-complete-korean-drama',
    '{base}-{season}-complete-tv-series',
    '{base}-{season}-complete-nollywood',
    '{base}-{season}-complete-chinese-drama',
    '{base}-{season}-complete-japanese-drama',
    '{base}-{year}-download-korean-movie',
    '{base}-{year}-download-hollywood-movie',
    '{base}-{year}-download-chinese-movie',
    '{base}-{year}-download-foreign-movie',
]

DRAMAKEY_WAVE1 = [
    '{base}-complete-chinese-drama',
    '{base}-complete-korean-drama',
    '{base}-complete-thai-drama',
]
DRAMAKEY_WAVE2 = [
    '{base}-{season}-complete-chinese-drama',
    '{base}{season}-complete-chinese-drama',
    '{base}-{season}-complete-thai-drama',
    '{base}{season}-complete-thai-drama',
    '{base}-{season}-complete-korean-drama',
    '{base}{season}-complete-korean-drama',
    '{base}',
]

# Full pattern lists (wave1 + wave2) for normal search
NKIRI_PATTERNS    = NKIRI_WAVE1    + NKIRI_WAVE2
DRAMAKEY_PATTERNS = DRAMAKEY_WAVE1 + DRAMAKEY_WAVE2

# Content type hint — reorders wave 1 to put the relevant pattern first
CONTENT_HINTS = {
    'korean':   ('{base}-korean-drama',          '{base}-complete-korean-drama'),
    'chinese':  ('{base}-complete-chinese-drama', '{base}-complete-chinese-drama'),
    'thai':     ('{base}-complete-thai-drama',    '{base}-complete-thai-drama'),
    'nollywood':('{base}-complete-nollywood',     None),
    'japanese': ('{base}-complete-japanese-drama',None),
}

# ─── PLUTOMOVIES (real search engine, no slug-guessing) ───────
# Unlike NKiri/DramaKey, PlutoMovies URLs carry an internal numeric ID
# (e.g. /movie/901465/mortal-kombat-ii-2026) that can't be derived from
# the title — slug-probing doesn't apply here. PlutoMovies instead has
# a real server-side search at /search/{query}/page/{n}, so this is a
# single HTTP GET + HTML parse rather than the wave1/wave2 pattern walk.
PLUTOMOVIES_RESULT_RE = re.compile(
    r'<a href="(/(movie|series)/\d+/[a-z0-9-]+)" data-href="[^"]*" title="([^"]+)">\s*<strong>',
    re.IGNORECASE
)
# Series search results include every individual episode alongside the
# season entry itself — this filters those out, applied to 'series' kind
# only (movies never carry an SxxEyy marker in their title).
PLUTOMOVIES_EPISODE_RE = re.compile(r'\bS\d{1,2}\s?E\d{1,3}\b', re.IGNORECASE)
PLUTOMOVIES_MAX_RESULTS = 12

# ─── DRAMAKEY.CC / DRAMARAIN SLUG PATTERNS ────────────────────
# dramakey.cc has NO server-side search — ?s= returns the same static
# 24-card catalog regardless of query (confirmed: identical byte-length
# for every query) and filters client-side in JS. So it is slug-guessed,
# not searched. Its card URLs follow /{country}/{slug} (confirmed live:
# /chinese/sweet-trap, /korean/..., etc.). DramaRain shares the extractor
# and is slug-guessed with the same DramaKey-style suffix patterns.
DRAMAKEY_CC_COUNTRIES = ['chinese', 'korean', 'thai', 'japanese', 'philippines']
DRAMAKEY_CC_PATTERNS  = [c + '/{base}' for c in DRAMAKEY_CC_COUNTRIES]

# ─── QUERY PARSING ────────────────────────────────────────────

def _parse_query(raw):
    q = raw.strip().lower()
    # Extract season number
    season_m   = re.search(r'season[\s-]?(\d+)|\bs(\d+)\b', q)
    season_n   = int(season_m.group(1) or season_m.group(2)) if season_m else None
    season_slug = ('s%02d' % season_n) if season_n else 's01'
    # Extract year
    year_m = re.search(r'(20\d{2})', q)
    year   = year_m.group(1) if year_m else str(datetime.date.today().year)
    # Build base slug
    slug = re.sub(r'\s+', '-', q)
    slug = re.sub(r'[^a-z0-9-]', '', slug)
    base = re.sub(r'-s\d+-?', '-', slug).strip('-')
    base = re.sub(r'-season-\d+-?', '-', base).strip('-')
    base = re.sub(r'-20\d{2}-?', '-', base).strip('-')
    base = base.strip('-')
    if not base or base.isdigit():
        base = re.sub(r'[^a-z0-9-]', '', re.sub(r'\s+', '-', q.strip())).strip('-')
    return base, season_slug, year

def _parse_content_hint(raw):
    """Extract trailing content type hint from query. Returns (clean_query, hint_or_None)."""
    words = raw.strip().split()
    if words and words[-1].lower() in CONTENT_HINTS:
        return ' '.join(words[:-1]), words[-1].lower()
    return raw, None

# ─── LOW-LEVEL PROBE ──────────────────────────────────────────

def _head_check(args):
    url, domain = args
    s = requests.Session()
    s.headers.update({
        'User-Agent':      UA_DESKTOP,
        'Referer':         domain,
        'Accept':          'text/html,application/xhtml+xml,*/*;q=0.9',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection':      'keep-alive',
    })
    try:
        r = s.head(url, timeout=10, allow_redirects=True)
        return url, r.status_code, r.url
    except Exception:
        return url, 0, url
    finally:
        try:
            s.close()
        except Exception:
            pass

def _verify_403(url, base):
    """GET + title check to confirm a 403 URL is real."""
    s = requests.Session()
    s.headers['User-Agent'] = UA_DESKTOP
    try:
        r = s.get(url, timeout=15, allow_redirects=True)
        if r.status_code == 200:
            # Guard both indices: if <title> is absent, find() returns -1 and
            # the slice becomes r.text[6:-1] — nearly the whole page — which
            # makes almost any page "match" and verifies bogus 403 hits.
            start = r.text.find('<title>')
            end = r.text.find('</title>')
            if start != -1 and end != -1 and end > start:
                title = r.text[start + 7:end].lower()
                if base.replace('-', ' ') in title or base in title:
                    return url
    except Exception:
        pass
    finally:
        try:
            s.close()
        except Exception:
            pass
    return None

def _probe_patterns(base_url, patterns, base, season_slug, year, cancel_event=None):
    domain = base_url.rstrip('/')
    urls   = [
        domain + '/' + p.replace('{base}', base)
                        .replace('{season}', season_slug)
                        .replace('{year}', year) + '/'
        for p in patterns
    ]

    results_map    = {}
    candidates_403 = []

    ex = ThreadPoolExecutor(max_workers=_search_workers(12))
    try:
        futures = {ex.submit(_head_check, (url, domain)): url for url in urls}
        for future in as_completed(futures):
            if cancel_event and cancel_event.is_set():
                break
            orig_url = futures[future]
            try:
                _, status, final_url = future.result()
                results_map[orig_url] = (status, final_url)
                if status == 200:
                    ex.shutdown(wait=False, cancel_futures=True)
                    return final_url
            except Exception:
                results_map[orig_url] = (0, orig_url)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    # Walk patterns in priority order — 200 hits first
    for u in urls:
        status, final_url = results_map.get(u, (0, u))
        if status == 200:
            return final_url

    # Collect 403 in priority order and verify
    for u in urls:
        status, final_url = results_map.get(u, (0, u))
        if status == 403:
            candidates_403.append(final_url)

    if candidates_403:
        verified = {}
        ex = ThreadPoolExecutor(max_workers=3)
        try:
            verify_futures = {ex.submit(_verify_403, url, base): url for url in candidates_403}
            for f in as_completed(verify_futures):
                orig = verify_futures[f]
                verified[orig] = f.result()
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        for url in candidates_403:
            if verified.get(url):
                return verified[url]

    return None

def _plutomovies_fetch_page(query_clean, page, timeout=15):
    url = f"https://plutomovies.com/search/{quote(query_clean)}/page/{page}"
    s = requests.Session()
    s.headers.update({
        'User-Agent': UA_DESKTOP,
        'Referer': 'https://plutomovies.com/',
        'Accept': 'text/html,application/xhtml+xml,*/*;q=0.9',
    })
    try:
        r = s.get(url, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.text
    except Exception:
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass

def _search_plutomovies(query, results, lock, timeout=15):
    """
    Real search engine — no slug-guessing. Single GET request (with a
    page-2 fallback only if page 1 is fully empty). Filters out
    individual-episode results, keeping season/series/movie-level
    entries only.
    """
    # Apostrophes break PlutoMovies' backend search entirely (confirmed —
    # site has no issue with the rest of the query, only the apostrophe
    # character itself) — must strip before encoding.
    query_clean = query.replace("'", "").replace("\u2019", "")
    if not query_clean.strip():
        return

    html = _plutomovies_fetch_page(query_clean, 1, timeout)
    matches = PLUTOMOVIES_RESULT_RE.findall(html) if html else []

    if not matches:
        html2 = _plutomovies_fetch_page(query_clean, 2, timeout)
        if html2:
            matches = PLUTOMOVIES_RESULT_RE.findall(html2)

    if not matches:
        return

    seen = set()
    found_any = False
    for link, kind, title in matches:
        title = title.strip()
        if kind == 'series' and PLUTOMOVIES_EPISODE_RE.search(title):
            continue  # skip individual episode entries, keep season-level only
        full_url = "https://plutomovies.com" + link
        if full_url in seen:
            continue
        seen.add(full_url)
        with lock:
            results.append((f"PlutoMovies ({kind}): {title}", full_url))
        found_any = True
        if len(seen) >= PLUTOMOVIES_MAX_RESULTS:
            break

    if found_any:
        safe_print("  " + render_message('search_found_on', site='PlutoMovies'))

# ─── SITE SEARCHERS ───────────────────────────────────────────

def _search_site(domain, wave1, wave2, base, season_slug, year,
                 site_name, results, lock,
                 fast=False, cancel_event=None, hint=None):
    """
    Search one site. In fast mode:
    - Run wave1 first. If hit found, fire cancel_event to stop other wave2s.
    - Only run wave2 if wave1 misses.
    In normal mode: run all patterns (wave1+wave2) together.
    """
    if fast:
        w1 = list(wave1)
        w2 = list(wave2)

        # Apply content hint — move matching pattern to front of wave1
        if hint and hint in CONTENT_HINTS:
            nkiri_pat, dk_pat = CONTENT_HINTS[hint]
            hint_pat = nkiri_pat if 'thenkiri' in domain else dk_pat
            if hint_pat:
                if hint_pat in w1:
                    w1.remove(hint_pat)
                    w1.insert(0, hint_pat)
                elif hint_pat in w2:
                    w2.remove(hint_pat)
                    w1.insert(0, hint_pat)

        url = _probe_patterns(domain, w1, base, season_slug, year, cancel_event)
        if url:
            if cancel_event:
                cancel_event.set()
            with lock:
                results.append((site_name, url))
            safe_print("  " + render_message('search_found_on', site=site_name))
            return

        # Wave 1 missed — run wave 2 only if not cancelled
        if cancel_event and cancel_event.is_set():
            return
        url = _probe_patterns(domain, w2, base, season_slug, year, cancel_event)
    else:
        # Normal search — all patterns at once
        url = _probe_patterns(domain, wave1 + wave2, base, season_slug, year, cancel_event)

    if url:
        with lock:
            results.append((site_name, url))
        safe_print("  " + render_message('search_found_on', site=site_name))

# ─── RELEVANCE FILTER ─────────────────────────────────────────
# WordPress search endpoints (NKiri RSS, NaijaVault WP-JSON, NaijaPrey RSS,
# 9jaRocks RSS) rank fuzzily — a query for "reborn rich" returned the real
# match DEAD LAST of 7 items, behind 6 unrelated titles. Slug-guessing never
# had this problem (exact URL or nothing), so search endpoints MUST be
# relevance-filtered or the right answer buries itself. We score by query-
# token overlap in the result title, boost exact/substring matches, and drop
# zero-overlap junk. Slug-probe hits (NKiri/DramaKey/Pluto) are already exact
# and skip this — they carry score=None and are never dropped or reordered.

_REL_STOP = {'the', 'a', 'an', 'of', 'and', 's01', 's02', 'complete', 'season'}
# Keep-threshold for fuzzy search results. 0.6 means a 2-word query needs BOTH
# words (1/2 = 0.5 is dropped), a 3-word query needs 2+ (2/3 = 0.67 kept). The
# +0.5 exact-substring boost still lets a genuine phrase match clear the bar.
# Chosen to kill 1-shared-token noise ("blood sisters" -> "in cold blood",
# "the two sisters") without dropping real hits.
_RELEVANCE_MIN = 0.6

def _rel_tokens(text):
    toks = re.findall(r'[a-z0-9]+', (text or '').lower())
    return {t for t in toks if t not in _REL_STOP and len(t) > 1}

def _relevance_score(query, title):
    """0.0–1.0 fraction of query tokens present in the title, +0.5 exact-substring
    boost. Returns 0.0 for zero overlap (caller drops these)."""
    q = _rel_tokens(query)
    if not q:
        return 1.0
    t = _rel_tokens(title)
    overlap = len(q & t) / len(q)
    if query.strip().lower() in (title or '').lower():
        overlap = min(1.0, overlap + 0.5)
    return overlap

def _filter_by_relevance(query, scored_results):
    """scored_results: list of (site, url, title). Drops zero-overlap items,
    sorts best-first, returns [(site, url), ...]."""
    ranked = []
    for site, url, title in scored_results:
        score = _relevance_score(query, title)
        if score >= _RELEVANCE_MIN:
            ranked.append((score, site, url))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [(site, url) for _, site, url in ranked]

# ─── ASYNC ENGINE ─────────────────────────────────────────────
# One aiohttp session, all sources probed concurrently, first slug-hit can
# cancel the rest (fast mode). Search endpoints (RSS/JSON/HTML) run alongside
# slug probes; their results are relevance-filtered before merging. This is a
# strict superset of the legacy path — same (site, url) result schema.

_RSS_ITEM  = re.compile(r'<item>(.*?)</item>', re.I | re.S)
_RSS_TITLE = re.compile(r'<title>(.*?)</title>', re.I | re.S)
_RSS_LINK  = re.compile(r'<link>(.*?)</link>', re.I | re.S)

def _rss_clean(s):
    return (s or '').replace('<![CDATA[', '').replace(']]>', '').strip()

def _async_conn_limit(default=20):
    return 10 if _is_termux() else default

# 9jaRocks RSS returns live my9jarocks.bz URLs, sometimes with a www. prefix.
# detect_site() strips www. and now maps both my9jarocks.bz and the legacy
# 9jarocks.com/.net aliases to extract_9jarocks, so these URLs are accepted
# as-is. No host rewrite needed — the RSS links are already the live, working
# pages (verified: 200 / real content). Kept as a pass-through hook in case a
# future mirror needs remapping.
def _normalize_9jarocks(url):
    return url

async def _afetch(session, url, timeout=12):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout),
                               allow_redirects=True) as r:
            return r.status, str(r.url), await r.text()
    except Exception:
        return 0, url, ''

async def _ahead(session, url):
    try:
        async with session.head(url, timeout=aiohttp.ClientTimeout(total=10),
                                allow_redirects=True) as r:
            return url, r.status, str(r.url)
    except Exception:
        return url, 0, url

async def _averify_title(session, url, base):
    """GET the page and confirm the query appears in its <title>. Guards against
    soft-404s — e.g. dramakey.cc/philippines/{anything} returns 200 serving the
    homepage catalog rather than a real series page. A genuine series page's
    title carries the drama name; the catalog homepage does not."""
    status, _, text = await _afetch(session, url)
    if status != 200 or not text:
        return False
    m = re.search(r'<title>(.*?)</title>', text, re.I | re.S)
    if not m:
        return False
    title = m.group(1).lower()
    return _relevance_score(base.replace('-', ' '), title) >= 0.5

async def _aprobe_slug(session, base_url, patterns, base, season_slug, year,
                       site_name, cancel_event, verify_title=False):
    """Probe all slug patterns CONCURRENTLY; return the highest-priority 200.
    Priority = pattern order (wave1 before wave2), so we keep the pattern index
    and pick the lowest-index 200, matching legacy behavior. When verify_title
    is set, a 200 must also pass a <title> relevance check (soft-404 guard)."""
    if cancel_event.is_set():
        return None
    domain = base_url.rstrip('/')
    urls = []
    for p in patterns:
        path = p.replace('{base}', base).replace('{season}', season_slug).replace('{year}', year)
        urls.append(domain + '/' + path.strip('/') + '/')
    results = await asyncio.gather(*[_ahead(session, u) for u in urls],
                                   return_exceptions=True)
    status_by_url = {}
    for res in results:
        if isinstance(res, Exception):
            continue
        url, status, final = res
        status_by_url[url] = (status, final)
    # Walk in priority order — first 200 wins (mirrors legacy _probe_patterns).
    for u in urls:
        status, final = status_by_url.get(u, (0, u))
        if status == 200:
            if verify_title and not await _averify_title(session, final, base):
                continue  # soft-404: 200 but page isn't the series we asked for
            return (site_name, final)
    return None

async def _asearch_rss(session, feed_url, site_name, query, url_fixup=None):
    """Fetch a WordPress RSS search feed, return scored (site, url, title) list."""
    status, _, text = await _afetch(session, feed_url)
    if status != 200 or not text:
        return []
    out = []
    for item in _RSS_ITEM.findall(text):
        t = _RSS_TITLE.search(item)
        l = _RSS_LINK.search(item)
        if not (t and l):
            continue
        title = _rss_clean(t.group(1))
        link = _rss_clean(l.group(1))
        if url_fixup:
            link = url_fixup(link)
        out.append((site_name, link, title))
    return out

async def _asearch_naijavault(session, query):
    url = f"https://naijavault.com/wp-json/wp/v2/posts?search={quote(query)}"
    status, _, text = await _afetch(session, url)
    if status != 200 or not text:
        return []
    out = []
    try:
        data = json.loads(text)
        for post in (data if isinstance(data, list) else []):
            title = (post.get('title', {}) or {}).get('rendered', '')
            link = post.get('link', '')
            if link:
                out.append(('NaijaVault', link, title))
    except Exception:
        pass
    return out

async def _asearch_pluto(session, query):
    """PlutoMovies real HTML search — reuse the proven regex + episode filter."""
    query_clean = query.replace("'", "").replace('’', '')
    if not query_clean.strip():
        return []
    for page in (1, 2):
        url = f"https://plutomovies.com/search/{quote(query_clean)}/page/{page}"
        status, _, html = await _afetch(session, url)
        matches = PLUTOMOVIES_RESULT_RE.findall(html) if (status == 200 and html) else []
        if matches:
            out, seen = [], set()
            for link, kind, title in matches:
                title = title.strip()
                if kind == 'series' and PLUTOMOVIES_EPISODE_RE.search(title):
                    continue
                full = 'https://plutomovies.com' + link
                if full in seen:
                    continue
                seen.add(full)
                # Pluto search is already server-side relevant; carry title but
                # it won't be dropped (kept out of the relevance-drop path).
                out.append((f"PlutoMovies ({kind}): {title}", full))
                if len(out) >= PLUTOMOVIES_MAX_RESULTS:
                    break
            return out
    return []

async def _arun(query, site_filter, fast, hint, timeout):
    base, season_slug, year = _parse_query(query)
    conn = aiohttp.TCPConnector(limit=_async_conn_limit(20), limit_per_host=6, ssl=False)
    headers = {
        'User-Agent':      UA_DESKTOP,
        'Accept':          'text/html,application/xhtml+xml,application/json,*/*;q=0.9',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    cancel_event = asyncio.Event()
    slug_results = []       # exact hits, never relevance-dropped
    search_scored = []      # (site, url, title) for relevance filtering
    pluto_results = []      # already relevance-ranked by server

    async with aiohttp.ClientSession(connector=conn, headers=headers) as session:
        tasks = []

        # NKiri: slug probe + RSS search
        if site_filter not in ('dramakey', 'plutomovies'):
            nkiri_pat = list(NKIRI_WAVE1) + ([] if fast else list(NKIRI_WAVE2))
            tasks.append(('slug', _aprobe_slug(session, 'https://thenkiri.com', nkiri_pat,
                          base, season_slug, year, 'NKiri', cancel_event)))
            tasks.append(('search', _asearch_rss(
                session, f"https://thenkiri.com/search/{quote(query)}/feed/rss2/",
                'NKiri', query)))

        # DramaKey.com + .cc + DramaRain: slug probe only (no server search)
        if site_filter not in ('nkiri', 'plutomovies'):
            dk_pat = list(DRAMAKEY_WAVE1) + ([] if fast else list(DRAMAKEY_WAVE2))
            tasks.append(('slug', _aprobe_slug(session, 'https://dramakey.com', dk_pat,
                          base, season_slug, year, 'DramaKey', cancel_event)))
            tasks.append(('slug', _aprobe_slug(session, 'https://dramakey.cc',
                          DRAMAKEY_CC_PATTERNS, base, season_slug, year,
                          'DramaKey.cc', cancel_event, verify_title=True)))
            tasks.append(('slug', _aprobe_slug(session, 'https://dramarain.com', dk_pat,
                          base, season_slug, year, 'DramaRain', cancel_event)))

        # Search-only sources
        if site_filter not in ('nkiri', 'dramakey'):
            tasks.append(('pluto', _asearch_pluto(session, query)))
            tasks.append(('search', _asearch_rss(
                session, f"https://9jarocks.com/search/{quote(query)}/feed/rss2/",
                '9jaRocks', query, url_fixup=_normalize_9jarocks)))
            tasks.append(('search', _asearch_rss(
                session, f"https://www.naijaprey.tv/search/{quote(query)}/feed/rss2/",
                'NaijaPrey', query)))
            tasks.append(('search', _asearch_naijavault(session, query)))

        kinds = [k for k, _ in tasks]
        coros = [c for _, c in tasks]

        async def _guard(coro):
            # Per-task deadline so ONE slow source can't discard the others'
            # results. gather() with a single outer wait_for would cancel every
            # task (even completed ones) on timeout — that regressed real hits.
            try:
                return await asyncio.wait_for(coro, timeout=timeout)
            except (asyncio.TimeoutError, Exception):
                return None

        done = await asyncio.gather(*[_guard(c) for c in coros],
                                    return_exceptions=True)

        for kind, res in zip(kinds, done):
            if isinstance(res, Exception):
                continue
            if kind == 'slug' and res:
                slug_results.append(res)
                if fast:
                    cancel_event.set()
            elif kind == 'search' and res:
                search_scored.extend(res)
            elif kind == 'pluto' and res:
                pluto_results.extend(res)

    # Stream "found on X" for each exact slug hit as we finish (UI feedback).
    for site, _ in slug_results:
        safe_print("  " + render_message('search_found_on', site=site))

    # Merge: exact slug hits first, then relevance-filtered search, then Pluto.
    ranked_search = _filter_by_relevance(query, search_scored)
    merged = slug_results + ranked_search + pluto_results
    # Dedupe on a normalized key (drop query string + trailing slash) so an RSS
    # result carrying ?utm_source= doesn't duplicate the clean slug-probe hit.
    seen, final = set(), []
    for site, url in merged:
        try:
            p = urlparse(url)
            key = (p.netloc.lower(), p.path.rstrip('/').lower())
        except Exception:
            key = (url,)
        if key in seen:
            continue
        seen.add(key)
        final.append((site, url))
    return final

def _ensure_async_imported():
    """Bind aiohttp/asyncio into module globals on first async search.

    Kept out of module top-level so the ~400ms aiohttp C-extension import
    doesn't block every launch. Returns True if the async engine is usable."""
    global aiohttp, asyncio, USE_ASYNC
    if aiohttp is not None and asyncio is not None:
        return True
    try:
        import asyncio as _aio
        import aiohttp as _http
        asyncio = _aio
        aiohttp = _http
        return True
    except Exception:
        USE_ASYNC = False
        return False


def _run_search_async(query, site_filter=None, fast=False, hint=None, timeout=45):
    """Async entry. Sets the Windows selector policy (aiohttp needs it, and it
    keeps Ctrl+C responsive), runs the engine, returns [(site, url), ...]."""
    if not _ensure_async_imported():
        return None
    if sys.platform == 'win32':
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass
    return asyncio.run(_arun(query, site_filter, fast, hint, timeout))

# ─── MAIN SEARCH ──────────────────────────────────────────────

def _run_search(query, site_filter=None, fast=False, hint=None, timeout=45):
    """Cache-aware dispatcher. Prefers the aiohttp async engine; falls back to
    the legacy requests/ThreadPool path if aiohttp is unavailable or the async
    run raises. Cache read/write is shared across both paths."""
    base, season_slug, year = _parse_query(query)
    if not base:
        safe_print(render_message('search_empty_query'))
        return []
    cache_key = f"{site_filter or 'all'}:{base}:{season_slug}:{year}:{'fast' if fast else 'full'}:{hint or ''}"

    use_cache = _search_cache_enabled()
    if use_cache:
        cached = _cache_get(cache_key)
        if cached:
            safe_print("  " + render_message('search_cached', query=base))
            return cached

    results = None
    if USE_ASYNC:
        try:
            results = _run_search_async(query, site_filter, fast, hint, timeout)
        except Exception:
            results = None  # fall through to legacy
    if results is None:
        results = _run_search_legacy(query, site_filter, fast, hint, timeout)

    if results and use_cache:
        _cache_set(cache_key, results)
    return results

def _search_cache_enabled():
    try:
        config_path = os.path.join(CONFIG_DIR, '.config.json')
        if os.path.exists(config_path):
            with open(config_path) as f:
                return json.load(f).get('search_cache', True)
    except Exception:
        pass
    return True

def _run_search_legacy(query, site_filter=None, fast=False, hint=None, timeout=45):
    base, season_slug, year = _parse_query(query)
    if not base:
        return []

    results = []
    lock    = threading.Lock()
    cancel_event = threading.Event()

    threads = []
    if site_filter not in ('dramakey', 'plutomovies'):
        t1 = threading.Thread(
            target=_search_site,
            args=('https://thenkiri.com', NKIRI_WAVE1, NKIRI_WAVE2,
                  base, season_slug, year, 'NKiri',
                  results, lock, fast, cancel_event, hint),
            daemon=True
        )
        threads.append(t1)
    if site_filter not in ('nkiri', 'plutomovies'):
        t2 = threading.Thread(
            target=_search_site,
            args=('https://dramakey.com', DRAMAKEY_WAVE1, DRAMAKEY_WAVE2,
                  base, season_slug, year, 'DramaKey',
                  results, lock, fast, cancel_event, hint),
            daemon=True
        )
        threads.append(t2)
    if site_filter not in ('nkiri', 'dramakey'):
        t3 = threading.Thread(
            target=_search_plutomovies,
            args=(query, results, lock),
            kwargs={'timeout': timeout},
            daemon=True
        )
        threads.append(t3)

    for t in threads:
        t.start()
    # Join against a single shared deadline. A per-thread join(timeout=timeout)
    # would wait the FULL timeout for EACH thread in turn, so a hung search
    # could block for timeout * len(threads) (~135s at the 45s default) instead
    # of timeout. A timed join (not a bare join) also keeps the main thread
    # responsive to Ctrl+C on Windows.
    deadline = time.time() + timeout
    for t in threads:
        t.join(timeout=max(0, deadline - time.time()))
    if any(t.is_alive() for t in threads):
        cancel_event.set()

    return results

def _present_results(results, raw_query):
    if not results:
        safe_print("\n" + render_message('search_nothing_found', query=raw_query))
        safe_print(render_message('search_try_again'))
        return None

    # Limit each site/platform to at most 3 results to ensure search results diversity
    counts = {}
    balanced = []
    for site, url in results:
        base_site = site.split(' (')[0] if ' (' in site else site
        base_site = base_site.split(':')[0] if ':' in base_site else base_site
        counts[base_site] = counts.get(base_site, 0) + 1
        if counts[base_site] <= 3:
            balanced.append((site, url))
    
    display_results = balanced[:8]

    if len(display_results) == 1:
        site, url = display_results[0]
        print(f"\n  Found on {site}:")
        print(f"  {url}")
        try:
            ans = input("\n  Download this? [Y/n]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return None
        if ans in ('', 'y', 'yes'):
            return url
        return None

    print()
    print(f"  {'-'*55}")
    for i, (site, url) in enumerate(display_results, 1):
        if site.startswith("PlutoMovies "):
            source = site.replace("PlutoMovies ", "Pluto")
            print(f"  [{i}] [{source}]")
        else:
            # NKiri / DramaKey / DramaRain
            slug = url.rstrip('/').split('/')[-1]
            title = slug.replace('-', ' ').title()
            print(f"  [{i}] [{site}] {title}")
    print(f"  {'-'*55}")
    try:
        choice = int(input("  Pick (1-%d) or 0 to cancel: " % len(display_results)).strip())
    except (ValueError, EOFError, KeyboardInterrupt):
        return None
    if 1 <= choice <= len(display_results):
        return display_results[choice - 1][1]
    return None

def search(raw_query, session=None):
    """Normal search — all patterns, both sites, full timeout."""
    site_filter = None
    query = raw_query.strip()
    if query.lower().endswith(' nkiri'):
        site_filter = 'nkiri'
        query = query[:-6].strip()
    elif query.lower().endswith(' dramakey'):
        site_filter = 'dramakey'
        query = query[:-9].strip()
    elif query.lower().endswith(' plutomovies'):
        site_filter = 'plutomovies'
        query = query[:-12].strip()

    timeout = _search_timeout(45)
    safe_print("\n" + render_message('search_running', query=query))
    results = _run_search(query, site_filter=site_filter, fast=False, timeout=timeout)
    return _present_results(results, raw_query)

def fsearch(raw_query, session=None):
    """
    Fast search — wave 1 proven patterns first, cancels wave 2 if hit found.
    Supports content hint: fsearch vincenzo korean
    Falls back to wave 2 automatically if wave 1 misses.
    """
    query, hint = _parse_content_hint(raw_query.strip())

    site_filter = None
    if query.lower().endswith(' nkiri'):
        site_filter = 'nkiri'
        query = query[:-6].strip()
    elif query.lower().endswith(' dramakey'):
        site_filter = 'dramakey'
        query = query[:-9].strip()
    elif query.lower().endswith(' plutomovies'):
        site_filter = 'plutomovies'
        query = query[:-12].strip()

    if hint:
        safe_print("\n" + render_message('fast_search_running_hint', hint=hint, query=query))
    else:
        safe_print("\n" + render_message('fast_search_running', query=query))

    timeout = _search_timeout(45)
    results = _run_search(query, site_filter=site_filter, fast=True, hint=hint, timeout=timeout)
    return _present_results(results, raw_query)

def rebuild_index_command():
    safe_print(render_message('search_no_index'))

def clear_search_cache():
    try:
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
            safe_print(render_message('cache_cleared'))
        else:
            safe_print(render_message('cache_none'))
    except Exception as e:
        safe_print(render_message('cache_clear_failed', error=e))
