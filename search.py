"""
search.py — Query normalisation, result scoring/grouping, and multi-engine search.

Merged from: search/query.py, search/results.py, search/google.py
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from urllib.parse import quote_plus, urlparse, unquote

import requests
from bs4 import BeautifulSoup

from config import UA_DESKTOP, log


# ─── Query normalisation ─────────────────────────────────────────

_NOISE = re.compile(
    r'\b(download|watch|stream|free|full|hd|480p|720p|1080p|360p|mp4|mkv|episode|ep)\b',
    re.IGNORECASE
)
_SEASON_SHORT = re.compile(r'\bs(\d+)\b', re.IGNORECASE)
_EPISODE_CODE = re.compile(r'\bs(\d+)e\d+\b', re.IGNORECASE)


def normalise(raw: str) -> str:
    q = raw.strip()
    q = _EPISODE_CODE.sub(r's\1', q)
    q = _SEASON_SHORT.sub(lambda m: f'season {m.group(1)}', q)
    q = _NOISE.sub('', q)
    q = re.sub(r'\s+', ' ', q).strip()
    return q or raw.strip()


def detect_content_type(query: str) -> str:
    q = query.lower()

    anime_hints = [
        'anime', 'naruto', 'bleach', 'one piece', 'attack on titan',
        'demon slayer', 'dragon ball', 'jujutsu', 'my hero',
    ]
    asian_hints = [
        'kdrama', 'korean', 'chinese', 'thai drama', 'cdrama',
        'japanese drama', 'taiwanese',
    ]
    nollywood_hints = [
        'nollywood', 'nigerian', 'yoruba', 'igbo', 'hausa',
        'wura', 'anikulapo', 'kings of boys', 'brotherhood',
        'the real housewives', 'tinsel', 'jenifa',
    ]

    for hint in anime_hints:
        if hint in q: return 'anime'
    for hint in asian_hints:
        if hint in q: return 'asian'
    for hint in nollywood_hints:
        if hint in q: return 'nollywood'
    return 'general'


def sites_for_query(query: str, all_sites: list[str]) -> list[str]:
    ctype      = detect_content_type(query)
    anime_only = {'anitaku.com.ro'}
    asian_only = {'myasiantv9.com.ro'}

    if ctype == 'anime':
        skip = set()
    elif ctype == 'asian':
        skip = anime_only
    elif ctype == 'nollywood':
        skip = anime_only | asian_only
    else:
        skip = set()

    return [s for s in all_sites if s not in skip]


# ─── Result scoring & grouping ───────────────────────────────────

def _title_score(result_title: str, query: str) -> float:
    t = result_title.lower()
    q = query.lower()

    base_score = SequenceMatcher(None, q, t).ratio()
    words      = q.split()
    word_hits  = sum(1 for w in words if w in t) / max(len(words), 1)
    score      = (base_score + word_hits) / 2

    q_seasons = re.findall(r'season\s*(\d+)', q)
    t_seasons = re.findall(r'season\s*(\d+)', t)
    if q_seasons and t_seasons:
        if set(q_seasons) & set(t_seasons):
            score = min(score + 0.15, 1.0)
        else:
            score = max(score - 0.10, 0.0)

    return score


def _site_trust(site: str) -> float:
    trust = {
        'naijavault.com':    0.9,
        'nkiri.com':         0.85,
        'plutomovies.com':   0.8,
        'dramarain.com':     0.75,
        'myasiantv9.com.ro': 0.75,
        'anitaku.com.ro':    0.75,
    }
    return trust.get(site, 0.5)


def _stars(score: float) -> str:
    if score >= 0.65: return '★★★'
    if score >= 0.40: return '★★ '
    return '★  '


def _group_key(title: str) -> str:
    t = title.lower()
    t = re.sub(r'\(.*?\)', '', t)
    t = re.sub(r'(episode|ep|added|complete|part)\s*\d*', '', t, flags=re.IGNORECASE)
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def score_and_group(results: list[dict], query: str) -> list[dict]:
    for r in results:
        r['score'] = _title_score(r['title'], query)
        r['stars'] = _stars(r['score'])

    results = [r for r in results if r['score'] >= 0.20]

    groups: dict[str, list] = {}
    for r in results:
        key = _group_key(r['title'])
        groups.setdefault(key, []).append(r)

    flat = []
    for key, members in groups.items():
        members.sort(key=lambda r: _site_trust(r['site']), reverse=True)
        best_score = max(m['score'] for m in members)
        for m in members:
            m['group_key']   = key
            m['group_score'] = best_score
        flat.extend(members)

    flat.sort(key=lambda r: (r['group_score'], r['score']), reverse=True)
    return flat


def display_results(scored: list[dict]) -> list[dict]:
    if not scored:
        print('\n[!] No results found')
        return []

    print(f"\n{'─'*52}")

    numbered   = []
    last_group = None

    for r in scored:
        gk = r['group_key']
        if gk != last_group:
            if last_group is not None:
                print()
            last_group = gk

        numbered.append(r)
        n       = len(numbered)
        title   = r['title'][:46]
        site    = r['site']
        stars   = r['stars']
        snippet = r.get('snippet', '')

        print(f'  [{n}] {title}')
        print(f'       {site}  {stars}')
        if snippet:
            print(f'       {snippet[:70]}')

    print(f"{'─'*52}")
    return numbered


# ─── Search engines ───────────────────────────────────────────────

_HEADERS = {
    'User-Agent': UA_DESKTOP,
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}


def _is_junk_url(href: str) -> bool:
    parsed = urlparse(href)
    path   = parsed.path.rstrip('/')
    if not path or path == '/':
        return True
    if '?s=' in href or '/dl-' in href or '/page/' in href:
        return True
    return False


def _ddg_search(query: str, site: str) -> list[dict]:
    full_query = f'{query} site:{site}'
    try:
        s = requests.Session()
        s.headers.update(_HEADERS)
        r = s.post('https://html.duckduckgo.com/html/', data={'q': full_query}, timeout=12)
        log.debug('DDG [%s] → %d', site, r.status_code)
        if r.status_code != 200:
            return []
        return _parse_ddg(r.text, site)
    except Exception as e:
        log.warning('DDG failed for %s: %s', site, e)
        return []


def _parse_ddg(html: str, site: str) -> list[dict]:
    soup    = BeautifulSoup(html, 'html.parser')
    results = []
    for a in soup.select('a.result__a'):
        href = a.get('href', '')
        if 'uddg=' in href:
            m = re.search(r'uddg=([^&]+)', href)
            if m:
                href = unquote(m.group(1))
        if not href.startswith('http') or site not in href:
            continue
        if _is_junk_url(href):
            continue
        title   = a.get_text(strip=True)
        snippet = ''
        parent  = a.find_parent(class_='result')
        if parent:
            snip = parent.find(class_='result__snippet')
            if snip:
                snippet = snip.get_text(strip=True)
        results.append({'title': title, 'url': href, 'snippet': snippet, 'site': site})
    log.debug('DDG parsed %d results for %s', len(results), site)
    return results


def _google_search(query: str, site: str) -> list[dict]:
    full_query = f'{query} site:{site}'
    url        = f'https://www.google.com/search?q={quote_plus(full_query)}&num=10&hl=en'
    try:
        s = requests.Session()
        s.headers.update(_HEADERS)
        r = s.get(url, timeout=12)
        log.debug('Google [%s] → %d', site, r.status_code)
        if r.status_code in (429, 403):
            log.warning('Google blocked for %s', site)
            return []
        if r.status_code != 200:
            return []
        return _parse_google(r.text, site)
    except Exception as e:
        log.warning('Google failed for %s: %s', site, e)
        return []


def _parse_google(html: str, site: str) -> list[dict]:
    soup    = BeautifulSoup(html, 'html.parser')
    results = []
    for g in soup.select('div.g, div[data-sokoban-container]'):
        a = g.find('a', href=True)
        if not a:
            continue
        href = a['href']
        if href.startswith('/url?'):
            m = re.search(r'[?&]q=([^&]+)', href)
            if m:
                href = unquote(m.group(1))
        if not href.startswith('http') or site not in href:
            continue
        if _is_junk_url(href):
            continue
        title_tag = g.find('h3')
        title     = title_tag.get_text(strip=True) if title_tag else href.split('/')[-1]
        snippet   = ''
        span      = g.find('span', class_=re.compile(r'st|aCOpRe|VwiC3b'))
        if span:
            snippet = span.get_text(strip=True)
        results.append({'title': title, 'url': href, 'snippet': snippet, 'site': site})
    log.debug('Google parsed %d results for %s', len(results), site)
    return results


def _bing_search(query: str, site: str) -> list[dict]:
    url = f'https://www.bing.com/search?q={quote_plus(f"{query} site:{site}")}'
    try:
        s = requests.Session()
        s.headers.update(_HEADERS)
        r = s.get(url, timeout=12)
        log.debug('Bing [%s] → %d', site, r.status_code)
        if r.status_code != 200:
            return []
        return _parse_bing(r.text, site)
    except Exception as e:
        log.warning('Bing failed for %s: %s', site, e)
        return []


def _parse_bing(html: str, site: str) -> list[dict]:
    soup    = BeautifulSoup(html, 'html.parser')
    results = []
    for li in soup.select('li.b_algo'):
        a = li.find('a', href=True)
        if not a:
            continue
        href = a['href']
        if not href.startswith('http') or site not in href:
            continue
        if _is_junk_url(href):
            continue
        title   = a.get_text(strip=True)
        snippet = ''
        snip    = li.find(class_='b_caption')
        if snip:
            snippet = snip.get_text(strip=True)
        results.append({'title': title, 'url': href, 'snippet': snippet, 'site': site})
    log.debug('Bing parsed %d results for %s', len(results), site)
    return results


def _search_site(query: str, site: str) -> list[dict]:
    """Try DDG → Google → Bing. Returns first non-empty result set."""
    results = _ddg_search(query, site)
    if results:
        return results
    log.debug('DDG empty for %s — trying Google', site)
    time.sleep(0.5)
    results = _google_search(query, site)
    if results:
        return results
    log.debug('Google empty for %s — trying Bing', site)
    time.sleep(0.5)
    return _bing_search(query, site)


def search_all_sites(query: str, sites: list[str]) -> list[dict]:
    """Search all sites in parallel. Returns merged, deduped list."""
    all_results = []
    seen_urls   = set()

    with ThreadPoolExecutor(max_workers=min(len(sites), 6)) as ex:
        futures = {ex.submit(_search_site, query, site): site for site in sites}
        for future in as_completed(futures):
            site = futures[future]
            try:
                results = future.result()
                for r in results:
                    if r['url'] not in seen_urls:
                        seen_urls.add(r['url'])
                        all_results.append(r)
            except Exception as e:
                log.warning('Search thread failed for %s: %s', site, e)

    return all_results
