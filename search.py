"""
search.py — Slug-based search for NKiri and DramaKey.
No index, no Google. Direct HEAD requests against known URL patterns.
"""

import re
import threading
import requests

from downloader import safe_print, UA_DESKTOP

# ─── SLUG PATTERNS ────────────────────────────────────────────

NKIRI_PATTERNS = [
    '{base}-{season}-complete-korean-drama',
    '{base}-{season}-complete-tv-series',
    '{base}-{season}-complete-japanese-drama',
    '{base}-{season}-complete-chinese-drama',
    '{base}-{season}-complete-nollywood',
    '{base}-{year}-download-hollywood-movie',
    '{base}-{year}-download-korean-movie',
    '{base}-{year}-download-chinese-movie',
    '{base}-{year}-download-foreign-movie',
    '{base}-complete-korean-drama',
    '{base}-complete-tv-series',
    '{base}-complete-nollywood',
    '{base}-korean-drama',
    '{base}',
]

DRAMAKEY_PATTERNS = [
    '{base}-{season}-complete-chinese-drama',
    '{base}{season}-complete-chinese-drama',
    '{base}-{season}-complete-thai-drama',
    '{base}{season}-complete-thai-drama',
    '{base}-{season}-complete-korean-drama',
    '{base}{season}-complete-korean-drama',
    '{base}-complete-chinese-drama',
    '{base}-complete-thai-drama',
    '{base}-complete-korean-drama',
    '{base}',
]

# ─── QUERY PARSING ────────────────────────────────────────────

def _parse_query(raw):
    q = raw.strip().lower()
    # Extract season number
    season_m = re.search(r'season[\s-]?(\d+)|\bs(\d+)\b', q)
    season_n = int(season_m.group(1) or season_m.group(2)) if season_m else None
    season_slug = ('s%02d' % season_n) if season_n else 's01'
    # Extract year
    year_m = re.search(r'(20\d{2})', q)
    year = year_m.group(1) if year_m else '2026'
    # Build base slug — strip season, year, noise words
    slug = re.sub(r'\s+', '-', q)
    slug = re.sub(r'[^a-z0-9-]', '', slug)
    base = re.sub(r'-s\d+-?', '-', slug).strip('-')
    base = re.sub(r'-season-\d+-?', '-', base).strip('-')
    base = re.sub(r'-20\d{2}-?', '-', base).strip('-')
    base = base.strip('-')
    return base, season_slug, year

# ─── SINGLE SITE SEARCH ───────────────────────────────────────

def _probe(base_url, patterns, base, season_slug, year):
    """Try each pattern, return first URL that returns 200."""
    s = requests.Session()
    s.headers['User-Agent'] = UA_DESKTOP
    s.headers['Referer'] = base_url
    base_url = base_url.rstrip('/')

    for pattern in patterns:
        url = base_url + '/' + pattern.replace('{base}', base).replace('{season}', season_slug).replace('{year}', year) + '/'
        try:
            r = s.head(url, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                return r.url
        except Exception:
            continue
    return None

def _search_nkiri(base, season_slug, year, results, lock):
    url = _probe('https://thenkiri.com/', NKIRI_PATTERNS, base, season_slug, year)
    if url:
        with lock:
            results.append(('NKiri', url))

def _search_dramakey(base, season_slug, year, results, lock):
    url = _probe('https://dramakey.com/', DRAMAKEY_PATTERNS, base, season_slug, year)
    if url:
        with lock:
            results.append(('DramaKey', url))

# ─── MAIN SEARCH ──────────────────────────────────────────────

def search(raw_query, session=None):
    base, season_slug, year = _parse_query(raw_query)

    if not base:
        safe_print("[!] Empty query")
        return None

    safe_print(f"\n  Searching: {raw_query}")
    safe_print(f"  {'─'*44}")

    results = []
    lock    = threading.Lock()

    t1 = threading.Thread(target=_search_nkiri,    args=(base, season_slug, year, results, lock), daemon=True)
    t2 = threading.Thread(target=_search_dramakey, args=(base, season_slug, year, results, lock), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)

    if not results:
        safe_print(f"\n  [!] Nothing found for: {raw_query}")
        safe_print(f"  [*] Try different spelling or paste URL directly")
        return None

    # If only one result — ask yes/no
    if len(results) == 1:
        site, url = results[0]
        safe_print(f"\n  Found on {site}:")
        safe_print(f"  {url}")
        ans = input("\n  Download this? [Y/n]: ").strip().lower()
        if ans in ('', 'y', 'yes'):
            return url
        return None

    # Multiple results — numbered list
    print()
    for i, (site, url) in enumerate(results, 1):
        print(f"  [{i}] {site}")
        print(f"       {url}")

    print(f"\n  {'─'*44}")
    try:
        choice = int(input("  Pick (1-%d) or 0 to cancel: " % len(results)).strip())
    except (ValueError, EOFError):
        return None

    if 1 <= choice <= len(results):
        return results[choice - 1][1]
    return None

def rebuild_index_command():
    safe_print("[*] No index in this version — search uses direct slug probing")
