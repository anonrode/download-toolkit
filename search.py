"""
search.py — Search engine for Download Toolkit.

Primary:  Local index built from each site's listing page — instant, offline.
Fallback: Google site: search per site when index has no results.
"""

import os
import re
import json
import time
import threading
import difflib
from bs4 import BeautifulSoup

from downloader import safe_print, BASE_DIR, UA_DESKTOP

# ─── CONSTANTS ────────────────────────────────────────────────
INDEX_FILE    = os.path.join(BASE_DIR, '.search_index.json')
INDEX_MAX_AGE = 6 * 3600   # rebuild if older than 6 hours

# Sites and their listing/category pages + content type
SITE_LISTINGS = {
    'naijavault.com': {
        'urls': [
            'https://www.naijavault.com/nollywood/',
            'https://www.naijavault.com/yoruba-movies/',
            'https://www.naijavault.com/igbo-movies/',
            'https://www.naijavault.com/hausa-movies/',
        ],
        'link_filter': lambda href: (
            'naijavault.com' in href and
            '/dl-' not in href and
            '/?s=' not in href and
            href.count('/') >= 4
        ),
        'tags': {'nollywood', 'nigerian', 'yoruba', 'igbo', 'hausa', 'african'},
    },
    'nkiri.com': {
        'urls': [
            'https://nkiri.com/category/korean-series/',
            'https://nkiri.com/category/hollywood/',
            'https://nkiri.com/category/nollywood/',
        ],
        'link_filter': lambda href: (
            'nkiri.com' in href and
            '/category/' not in href and
            '/page/' not in href and
            href.count('/') >= 4
        ),
        'tags': {'korean', 'hollywood', 'nollywood'},
    },
    'plutomovies.com': {
        'urls': [
            'https://plutomovies.com/genre/nollywood/',
            'https://plutomovies.com/genre/korean-drama/',
            'https://plutomovies.com/genre/tv-series/',
        ],
        'link_filter': lambda href: (
            'plutomovies.com' in href and
            ('/series/' in href or '/movie/' in href) and
            '/genre/' not in href and
            '/page/' not in href
        ),
        'tags': {'nollywood', 'korean', 'series', 'movie'},
    },
    'dramarain.com': {
        'urls': [
            'https://dramarain.com/category/korean-drama/',
            'https://dramarain.com/category/chinese-drama/',
            'https://dramarain.com/category/thai-drama/',
        ],
        'link_filter': lambda href: (
            'dramarain.com' in href and
            '/category/' not in href and
            '/page/' not in href and
            href.count('/') >= 4
        ),
        'tags': {'korean', 'chinese', 'thai', 'asian', 'drama'},
    },
    'myasiantv9.com.ro': {
        'urls': [
            'https://myasiantv9.com.ro/category/korean-drama/',
            'https://myasiantv9.com.ro/category/chinese-drama/',
        ],
        'link_filter': lambda href: (
            'myasiantv9.com' in href and
            '/category/' not in href and
            '/page/' not in href and
            'episode-' not in href and
            href.count('/') >= 4
        ),
        'tags': {'korean', 'chinese', 'asian', 'drama'},
    },
    'anitaku.com.ro': {
        'urls': [
            'https://anitaku.com.ro/anime-list.html',
        ],
        'link_filter': lambda href: (
            'anitaku.com.ro' in href and
            '/anime/' in href and
            'episode-' not in href
        ),
        'tags': {'anime', 'japanese', 'animation'},
    },
    '9jarocks.net': {
        'urls': [
            'https://9jarocks.net/category/nollywood/',
            'https://9jarocks.net/category/yoruba/',
        ],
        'link_filter': lambda href: (
            '9jarocks.net' in href and
            '/category/' not in href and
            '/page/' not in href and
            href.count('/') >= 4
        ),
        'tags': {'nollywood', 'nigerian', 'yoruba', 'african'},
    },
}

# Content type detection from query keywords
CONTENT_TAGS = {
    'anime':     {'anime', 'naruto', 'bleach', 'one piece', 'attack on titan', 'demon slayer',
                  'jujutsu', 'dragon ball', 'one punch', 'sword art', 'my hero'},
    'korean':    {'korean', 'kdrama', 'k-drama', 'k drama', 'oppa', 'goblin', 'descendants',
                  'crash landing', 'itaewon', 'vincenzo', 'squid game'},
    'chinese':   {'chinese', 'cdrama', 'c-drama', 'mandarin', 'wuxia', 'xianxia'},
    'thai':      {'thai', 'thailand', 'lakorn'},
    'nollywood': {'wura', 'nollywood', 'nigerian', 'yoruba', 'igbo', 'hausa', 'anikulapo',
                  'king of boys', 'jenifa', 'obi cubana', 'african', 'kannywood',
                  'house of zaki', 'breaded life', 'naija', 'village'},
}

# ─── INDEX MANAGEMENT ─────────────────────────────────────────

def _load_index():
    try:
        if os.path.exists(INDEX_FILE):
            with open(INDEX_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {'entries': [], 'built_at': 0}

def _save_index(index):
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(INDEX_FILE, 'w', encoding='utf-8') as f:
            json.dump(index, f, indent=2)
    except Exception:
        pass

def _index_age():
    idx = _load_index()
    return time.time() - idx.get('built_at', 0)

def _fetch_listing(url, link_filter):
    """Fetch one listing page and return (title, url) pairs."""
    try:
        import requests
        s = requests.Session()
        s.headers['User-Agent'] = UA_DESKTOP
        r = s.get(url, timeout=20)
        if not r or r.status_code != 200:
            return []
        soup    = BeautifulSoup(r.text, 'html.parser')
        results = []
        seen    = set()
        for a in soup.find_all('a', href=True):
            href  = a['href']
            title = a.get_text(strip=True)
            if link_filter(href) and href not in seen and len(title) > 3:
                seen.add(href)
                results.append({'title': title, 'url': href})
        return results
    except Exception as e:
        safe_print(f"  [index] Error fetching {url}: {e}")
        return []

def _build_index_for_site(site, config):
    """Build index entries for one site across all its listing pages."""
    entries = []
    seen    = set()
    for listing_url in config['urls']:
        fetched = _fetch_listing(listing_url, config['link_filter'])
        for item in fetched:
            if item['url'] not in seen:
                seen.add(item['url'])
                entries.append({
                    'title':  item['title'],
                    'url':    item['url'],
                    'site':   site,
                    'tags':   list(config['tags']),
                })
    return entries

def build_index(silent=True):
    """Build the full search index from all sites. Run in background thread."""
    if not silent:
        safe_print("[*] Building search index...")

    all_entries = []
    threads     = []
    results     = {}
    lock        = threading.Lock()

    def fetch_site(site, config):
        entries = _build_index_for_site(site, config)
        with lock:
            results[site] = entries

    for site, config in SITE_LISTINGS.items():
        t = threading.Thread(target=fetch_site, args=(site, config), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=30)

    for site in SITE_LISTINGS:
        all_entries.extend(results.get(site, []))

    index = {'entries': all_entries, 'built_at': time.time()}
    _save_index(index)

    if not silent:
        safe_print(f"[✓] Index built — {len(all_entries)} titles across {len(SITE_LISTINGS)} sites")
    return index

def refresh_index_if_stale():
    """Called on startup — rebuilds index in background if older than INDEX_MAX_AGE."""
    def _run():
        if _index_age() > INDEX_MAX_AGE:
            build_index(silent=True)
    threading.Thread(target=_run, daemon=True).start()

# ─── QUERY PROCESSING ─────────────────────────────────────────

def _clean_query(raw):
    """Normalise user query for best search results."""
    q = raw.strip().lower()
    # Remove noise words
    noise = ['download', 'watch', 'free', 'online', 'full', 'hd', '720p', '480p', '1080p',
             'episode', 'episodes', 'complete', 'season', 'series', 'subtitles', 'sub', 'eng']
    # But keep season/episode numbers — extract them first
    season_m  = re.search(r'\b(?:s(?:eason\s*)?|s)(\d+)\b', q)
    episode_m = re.search(r'\b(?:e(?:pisode\s*)?|ep\s*)(\d+)\b', q)
    season_n  = int(season_m.group(1)) if season_m else None
    episode_n = int(episode_m.group(1)) if episode_m else None

    # Strip noise but keep show name and numbers
    for word in noise:
        q = re.sub(rf'\b{re.escape(word)}\b', ' ', q)
    q = re.sub(r'\s+', ' ', q).strip()
    return q, season_n, episode_n

def _detect_content_type(query):
    """Detect what kind of content the query is about."""
    q_lower = query.lower()
    matched = set()
    for content_type, keywords in CONTENT_TAGS.items():
        if any(kw in q_lower for kw in keywords):
            matched.add(content_type)
    return matched

def _sites_for_query(content_types):
    """Return only sites relevant to the detected content type."""
    if not content_types:
        return list(SITE_LISTINGS.keys())  # search all if unclear
    relevant = set()
    for site, config in SITE_LISTINGS.items():
        site_tags = config['tags']
        if any(ct in site_tags for ct in content_types):
            relevant.add(site)
    return list(relevant) if relevant else list(SITE_LISTINGS.keys())

# ─── FUZZY MATCHING ───────────────────────────────────────────

def _score(title, query, season_n=None):
    """Score a result title against the query. Returns 0.0 – 1.0+"""
    t = title.lower()
    q = query.lower()

    # Base fuzzy match
    base = difflib.SequenceMatcher(None, q, t).ratio()

    # Boost: all query words appear in title
    words = [w for w in q.split() if len(w) > 2]
    if words and all(w in t for w in words):
        base += 0.3

    # Boost: season number matches
    if season_n:
        season_patterns = [
            f'season {season_n}', f's{season_n:02d}', f's{season_n}',
            f'season{season_n}', f' {season_n}'
        ]
        if any(p in t for p in season_patterns):
            base += 0.2
        else:
            base -= 0.1  # penalise wrong season

    # Boost: title starts with query words
    first_word = q.split()[0] if q.split() else ''
    if first_word and t.startswith(first_word):
        base += 0.1

    return base

def _search_index(query, season_n=None, sites=None):
    """Search local index. Returns list of scored results."""
    index   = _load_index()
    entries = index.get('entries', [])
    if not entries:
        return []

    results = []
    for entry in entries:
        if sites and entry['site'] not in sites:
            continue
        score = _score(entry['title'], query, season_n)
        if score > 0.3:  # minimum relevance threshold
            results.append({**entry, 'score': score})

    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:20]

# ─── GOOGLE FALLBACK ──────────────────────────────────────────

def _google_search_site(query, site, max_results=5):
    """Search Google for query restricted to one site."""
    try:
        import requests
        search_url = f'https://www.google.com/search?q={requests.utils.quote(query)}+site:{site}&num=10'
        s = requests.Session()
        s.headers['User-Agent'] = UA_DESKTOP
        s.headers['Accept-Language'] = 'en-US,en;q=0.9'
        r = s.get(search_url, timeout=15)
        if not r or r.status_code != 200:
            return []
        soup    = BeautifulSoup(r.text, 'html.parser')
        results = []
        seen    = set()
        for a in soup.find_all('a', href=True):
            href = a['href']
            # Google wraps links in /url?q=...
            m = re.search(r'/url\?q=(https?://[^&]+)', href)
            if m:
                url = requests.utils.unquote(m.group(1))
            elif href.startswith('https://') and site in href:
                url = href
            else:
                continue
            if site not in url or url in seen:
                continue
            # Filter out search/category/pagination pages
            if any(x in url for x in ['?s=', '/category/', '/page/', '/tag/', '#']):
                continue
            title = a.get_text(strip=True)
            if not title or len(title) < 3:
                # Try to build title from URL slug
                slug  = url.rstrip('/').split('/')[-1]
                title = slug.replace('-', ' ').title()
            seen.add(url)
            results.append({'title': title, 'url': url, 'site': site})
            if len(results) >= max_results:
                break
        return results
    except Exception as e:
        safe_print(f"  [search] Google failed for {site}: {e}")
        return []

def _google_search_all_sites(query, sites, max_per_site=4):
    """Run Google site: searches in parallel for all relevant sites."""
    results = {}
    threads = []
    lock    = threading.Lock()

    def _search(site):
        found = _google_search_site(query, site, max_per_site)
        with lock:
            results[site] = found

    for site in sites:
        t = threading.Thread(target=_search, args=(site,), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=20)

    return results

# ─── RESULT GROUPING ──────────────────────────────────────────

def _group_results(results):
    """Group results by show — same show from multiple sites becomes one entry."""
    groups = {}
    for r in results:
        title = r['title'].lower()
        title = re.sub(r'\s*[-–|]\s*.*$', '', title)
        title = re.sub(r'\s*(season|s)\s*\d+.*$', '', title, flags=re.IGNORECASE).strip()
        # Find existing group with similar title
        matched_key = None
        for key in groups:
            ratio = difflib.SequenceMatcher(None, key, title).ratio()
            if ratio > 0.75:
                matched_key = key
                break
        key = matched_key or title
        if key not in groups:
            groups[key] = {
                'display_title': r['title'],
                'sources':       [],
                'score':         r.get('score', 0.5),
            }
        # Add source if not already there
        existing_sites = [s['site'] for s in groups[key]['sources']]
        if r['site'] not in existing_sites:
            groups[key]['sources'].append({'site': r['site'], 'url': r['url'], 'title': r['title']})
        # Keep highest score
        if r.get('score', 0) > groups[key]['score']:
            groups[key]['score']         = r['score']
            groups[key]['display_title'] = r['title']

    # Sort groups by score
    sorted_groups = sorted(groups.values(), key=lambda x: x['score'], reverse=True)
    return sorted_groups

def _stars(score):
    if score >= 0.8:
        return '★★★'
    elif score >= 0.6:
        return '★★ '
    else:
        return '★  '

# ─── MAIN SEARCH FUNCTION ─────────────────────────────────────

def search(raw_query, session=None):
    """
    Full search flow:
    1. Clean query, detect content type, pick relevant sites
    2. Search local index (instant)
    3. If weak results, fall back to Google site: search
    4. Group, deduplicate, display
    5. Return chosen (title, url) or None
    """
    query, season_n, episode_n = _clean_query(raw_query)
    if not query:
        safe_print("[!] Empty query")
        return None

    content_types = _detect_content_type(raw_query)
    relevant_sites = _sites_for_query(content_types)

    safe_print(f"\n  Searching: {raw_query}")
    if content_types:
        safe_print(f"  Type detected: {', '.join(content_types)}")
    safe_print(f"  Sites: {', '.join(relevant_sites)}")
    safe_print(f"  {'─'*44}")

    # ── Step 1: Local index ────────────────────────────────────
    index_results = _search_index(query, season_n, sites=relevant_sites)

    # ── Step 2: Google fallback if index is empty or weak ─────
    google_results = []
    best_index_score = index_results[0]['score'] if index_results else 0

    if best_index_score < 0.6 or not index_results:
        safe_print(f"  [*] Index {'empty' if not index_results else 'weak'} — searching Google...")
        g_raw = _google_search_all_sites(query, relevant_sites, max_per_site=5)
        for site, items in g_raw.items():
            for item in items:
                item['score'] = _score(item['title'], query, season_n) + 0.1  # slight boost for recency
                google_results.append(item)

    # Merge: index results first, then google, dedup by URL
    seen_urls = set()
    merged    = []
    for r in index_results + google_results:
        if r['url'] not in seen_urls:
            seen_urls.add(r['url'])
            merged.append(r)

    if not merged:
        safe_print(f"\n  [!] No results found for: {raw_query}")
        safe_print(f"  [*] Try: different spelling, fewer words, or paste URL directly")
        return None

    # ── Step 3: Group by show ──────────────────────────────────
    groups = _group_results(merged)

    # ── Step 4: "Feeling lucky" — obvious single match ─────────
    if len(groups) == 1 and groups[0]['score'] >= 0.85:
        g = groups[0]
        safe_print(f"\n  Found: {g['display_title']}")
        if len(g['sources']) == 1:
            safe_print(f"  Source: {g['sources'][0]['site']}")
            ans = input("\n  Download this? [Y/n]: ").strip().lower()
            if ans in ('', 'y', 'yes'):
                return g['sources'][0]['url']
            return None

    # ── Step 5: Display results ────────────────────────────────
    print()
    index_list = []  # flat list for selection

    for i, g in enumerate(groups[:10], 1):
        stars = _stars(g['score'])
        print(f"  [{i}] {g['display_title']}  {stars}")
        if len(g['sources']) > 1:
            for s in g['sources']:
                print(f"       └─ {s['site']}")
        else:
            print(f"       {g['sources'][0]['site']}")
        index_list.append(g)

    print(f"\n  {'─'*44}")
    print(f"  Pick a number, or 0 to cancel:")

    try:
        choice = int(input("  > ").strip())
    except (ValueError, EOFError):
        return None

    if choice == 0 or choice > len(index_list):
        return None

    chosen = index_list[choice - 1]

    # If multiple sources for chosen show, let user pick
    if len(chosen['sources']) > 1:
        print(f"\n  Sources for: {chosen['display_title']}")
        for j, s in enumerate(chosen['sources'], 1):
            print(f"  [{j}] {s['site']}")
            print(f"       {s['url'][:60]}")
        try:
            src_choice = int(input("\n  Pick source: ").strip())
            if 1 <= src_choice <= len(chosen['sources']):
                return chosen['sources'][src_choice - 1]['url']
        except (ValueError, EOFError):
            pass
        return None

    return chosen['sources'][0]['url']

def rebuild_index_command():
    """Manual index rebuild — called from 'index rebuild' REPL command."""
    safe_print("[*] Rebuilding search index...")
    idx = build_index(silent=False)
    safe_print(f"[✓] Done — {len(idx['entries'])} titles indexed")
