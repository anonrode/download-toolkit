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
import json
import time
import datetime
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from downloader import safe_print, UA_DESKTOP, BASE_DIR

# ─── CACHE ────────────────────────────────────────────────────
CACHE_FILE    = os.path.join(os.path.dirname(__file__), '.search_cache.json')
CACHE_TTL     = 86400  # 24 hours in seconds

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

def _verify_403(url, base):
    """GET + title check to confirm a 403 URL is real."""
    s = requests.Session()
    s.headers['User-Agent'] = UA_DESKTOP
    try:
        r = s.get(url, timeout=15, allow_redirects=True)
        if r.status_code == 200:
            title = r.text[r.text.find('<title>')+7:r.text.find('</title>')].lower()
            if base.replace('-', ' ') in title or base in title:
                return url
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

    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(_head_check, (url, domain)): url for url in urls}
        for future in as_completed(futures):
            if cancel_event and cancel_event.is_set():
                for f in futures:
                    f.cancel()
                break
            orig_url = futures[future]
            try:
                _, status, final_url = future.result()
                results_map[orig_url] = (status, final_url)
            except Exception:
                results_map[orig_url] = (0, orig_url)

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
        with ThreadPoolExecutor(max_workers=3) as ex:
            verify_futures = {ex.submit(_verify_403, url, base): url for url in candidates_403}
            verified = {}
            for f in as_completed(verify_futures):
                orig = verify_futures[f]
                verified[orig] = f.result()
        for url in candidates_403:
            if verified.get(url):
                return verified[url]

    return None

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

        url = _probe_patterns(domain, w1, base, season_slug, year)
        if url:
            if cancel_event:
                cancel_event.set()
            with lock:
                results.append((site_name, url))
            safe_print(f"  [✓] Found on {site_name}")
            return

        # Wave 1 missed — run wave 2 only if not cancelled
        if cancel_event and cancel_event.is_set():
            return
        url = _probe_patterns(domain, w2, base, season_slug, year, cancel_event)
    else:
        # Normal search — all patterns at once
        url = _probe_patterns(domain, wave1 + wave2, base, season_slug, year)

    if url:
        with lock:
            results.append((site_name, url))
        safe_print(f"  [✓] Found on {site_name}")

# ─── MAIN SEARCH ──────────────────────────────────────────────

def _run_search(query, site_filter=None, fast=False, hint=None, timeout=45):
    base, season_slug, year = _parse_query(query)
    if not base:
        safe_print("[!] Empty query")
        return []

    # Check cache first
    cached = _cache_get(base)
    if cached:
        safe_print(f"  [cached] {base}")
        return cached

    results = []
    lock    = threading.Lock()
    cancel_event = threading.Event() if fast else None

    threads = []
    if site_filter != 'dramakey':
        t1 = threading.Thread(
            target=_search_site,
            args=('https://thenkiri.com', NKIRI_WAVE1, NKIRI_WAVE2,
                  base, season_slug, year, 'NKiri',
                  results, lock, fast, cancel_event, hint),
            daemon=True
        )
        threads.append(t1)
    if site_filter != 'nkiri':
        t2 = threading.Thread(
            target=_search_site,
            args=('https://dramakey.com', DRAMAKEY_WAVE1, DRAMAKEY_WAVE2,
                  base, season_slug, year, 'DramaKey',
                  results, lock, fast, cancel_event, hint),
            daemon=True
        )
        threads.append(t2)

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)

    if results:
        _cache_set(base, results)

    return results

def _present_results(results, raw_query):
    if not results:
        safe_print(f"\n[!] Nothing found for: {raw_query}")
        safe_print("[*] Try different spelling or paste URL directly")
        return None

    if len(results) == 1:
        site, url = results[0]
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
    print(f"  {'─'*46}")
    for i, (site, url) in enumerate(results, 1):
        print(f"  [{i}] {site}")
        print(f"       {url}")
    print(f"  {'─'*46}")
    try:
        choice = int(input("  Pick (1-%d) or 0 to cancel: " % len(results)).strip())
    except (ValueError, EOFError, KeyboardInterrupt):
        return None
    if 1 <= choice <= len(results):
        return results[choice - 1][1]
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

    safe_print(f"\n[*] Searching: {query}")
    results = _run_search(query, site_filter=site_filter, fast=False, timeout=45)
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

    if hint:
        safe_print(f"\n[*] Fast search ({hint}): {query}")
    else:
        safe_print(f"\n[*] Fast search: {query}")

    results = _run_search(query, site_filter=site_filter, fast=True, hint=hint, timeout=45)
    return _present_results(results, raw_query)

def rebuild_index_command():
    safe_print("[*] No index in this version — search uses direct slug probing")

def clear_search_cache():
    try:
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
            safe_print("[✓] Search cache cleared")
        else:
            safe_print("[*] No cache file found")
    except Exception as e:
        safe_print(f"[!] Could not clear cache: {e}")
