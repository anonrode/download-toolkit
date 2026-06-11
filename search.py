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
    '{base}-complete-korean-drama',
    '{base}-complete-tv-series',
    '{base}-complete-nollywood',
    '{base}-{year}-download-hollywood-movie',
    '{base}-{year}-download-korean-movie',
    '{base}-{year}-download-chinese-movie',
    '{base}-{year}-download-foreign-movie',
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
    # If base is empty or just numbers, use original slug
    if not base or base.isdigit():
        base = re.sub(r'[^a-z0-9-]', '', re.sub(r'\s+', '-', q.strip())).strip('-')
    return base, season_slug, year

# ─── SINGLE SITE SEARCH ───────────────────────────────────────

def _probe(base_url, patterns, base, season_slug, year):
    """
    Fire all pattern HEAD requests in parallel via ThreadPoolExecutor.
    - 200 = confirmed found
    - 403 = collect and verify with GET + title check
    - 404 = skip
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    base_url = base_url.rstrip('/')
    urls = [
        base_url + '/' + p.replace('{base}', base)
                          .replace('{season}', season_slug)
                          .replace('{year}', year) + '/'
        for p in patterns
    ]

    def head_check(url):
        s = requests.Session()
        s.headers.update({
            'User-Agent':      UA_DESKTOP,
            'Referer':         base_url,
            'Accept':          'text/html,application/xhtml+xml,*/*;q=0.9',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection':      'keep-alive',
        })
        try:
            r = s.head(url, timeout=10, allow_redirects=True)
            return url, r.status_code, r.url
        except Exception:
            return url, 0, url

    # Map original_url -> (status, final_url)
    results_map = {}
    candidates_403 = []

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(head_check, url): url for url in urls}
        for future in as_completed(futures):
            orig_url = futures[future]
            _, status, final_url = future.result()
            results_map[orig_url] = (status, final_url)

    # Return highest-priority 200 hit — walk patterns in order
    for u in urls:
        status, final_url = results_map.get(u, (0, u))
        if status == 200:
            return final_url

    # Collect 403 candidates in pattern priority order
    for u in urls:
        status, final_url = results_map.get(u, (0, u))
        if status == 403:
            candidates_403.append(final_url)

    # Verify 403 candidates with GET + title check
    def verify(url):
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

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(verify, url): url for url in candidates_403}
        verified = {orig: None for orig in candidates_403}
        for future in as_completed(futures):
            orig = futures[future]
            verified[orig] = future.result()
    # Return first verified hit in original pattern priority order
    for url in candidates_403:
        if verified.get(url):
            return verified[url]

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

def _search_anitaku(base, season_slug, year, results, lock):
    """Search Anitaku via its working keyword search endpoint."""
    try:
        keyword = base.replace('-', '+')
        s = requests.Session()
        s.headers['User-Agent'] = UA_DESKTOP
        r = s.get(f'https://anitaku.com.ro/search.html?keyword={keyword}', timeout=15)
        if not r or r.status_code != 200:
            return
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, 'html.parser')
        for li in soup.select('ul.items li'):
            a = li.select_one('p.name a')
            if a and a.get('href'):
                href = a['href']
                if not href.startswith('http'):
                    href = 'https://anitaku.com.ro' + href
                with lock:
                    results.append(('Anitaku', href))
                return  # first result only
    except Exception:
        pass

# ─── MAIN SEARCH ──────────────────────────────────────────────

def search(raw_query, session=None):
    # Check for site-specific suffix — "search vincenzo nkiri"
    site_filter = None
    query = raw_query.strip()
    if query.lower().endswith(' nkiri'):
        site_filter = 'nkiri'
        query = query[:-6].strip()
    elif query.lower().endswith(' dramakey'):
        site_filter = 'dramakey'
        query = query[:-9].strip()
    elif query.lower().endswith(' anitaku'):
        site_filter = 'anitaku'
        query = query[:-8].strip()

    base, season_slug, year = _parse_query(query)

    if not base:
        safe_print("[!] Empty query")
        return None

    safe_print(f"\n  Searching: {query}")
    if site_filter:
        safe_print(f"  Site: {site_filter}")
    safe_print(f"  {'─'*44}")

    results = []
    lock    = threading.Lock()

    threads = []
    if site_filter not in ('dramakey', 'anitaku'):
        t1 = threading.Thread(target=_search_nkiri, args=(base, season_slug, year, results, lock))
        threads.append(t1)
    if site_filter not in ('nkiri', 'anitaku'):
        t2 = threading.Thread(target=_search_dramakey, args=(base, season_slug, year, results, lock))
        threads.append(t2)
    if site_filter not in ('nkiri', 'dramakey'):
        t3 = threading.Thread(target=_search_anitaku, args=(base, season_slug, year, results, lock))
        threads.append(t3)

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

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
