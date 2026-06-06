"""sites/plutomovies.py — PlutoMovies extractor."""

import os, re, time
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from config import BASE_DIR, PLUTO_BASE, EP_KEYWORDS
from core import safe_filename
from core import DownloadSummary, download_file, download_batch
from core import wait_if_paused
from config import safe_get
from sites.resolvers import resolve_plutomovies_dl


def _ep_name_from_tag(a) -> str:
    text = a.get_text(strip=True)
    if text and len(text) > 3:
        return safe_filename(text)
    img = a.find('img')
    if img:
        alt = img.get('alt', '').strip()
        if alt and len(alt) > 3:
            return safe_filename(alt)
    return safe_filename(a['href'].rstrip('/').split('/')[-1])


def extract(url: str, session, state):
    print('[*] PlutoMovies mode')
    is_movie = '/movie/' in url
    slug     = url.rstrip('/').split('/')[-1]
    name     = re.sub(r'-\d{4}.*$', '', slug).replace('-', ' ').title()
    print(f'[*] Title: {name}')
    folder  = os.path.join(BASE_DIR, safe_filename(name))
    summary = DownloadSummary()

    session.headers['Referer'] = PLUTO_BASE + '/'
    r = safe_get(session, url, timeout=30)
    if not r:
        return
    soup    = BeautifulSoup(r.text, 'html.parser')
    dl_link = next((a['href'] for a in soup.find_all('a', href=True)
                    if 'dl.plutomovies.com' in a['href']), None)

    # Movie or single page with direct link
    if is_movie or dl_link:
        if dl_link:
            print(f'[*] Direct link found — saving to: {folder}')
            direct = resolve_plutomovies_dl(dl_link, session)
            if direct:
                ext = 'mkv' if 'mkv' in direct.lower() else 'mp4'
                download_file(direct, folder, safe_filename(f'{name}.{ext}'), summary, state)
            else:
                print('[✗] Could not resolve download link')
                summary.add_failed(name)
        else:
            print('[✗] No download link found on page')
            summary.add_failed(name)
        summary.report()
        return

    # Series — collect season links
    season_links = []
    for a in soup.find_all('a', href=True):
        href     = a['href']
        full_url = urljoin(PLUTO_BASE, href)
        if '/series/' in href and 'season' in href.lower() and '#' not in href:
            if full_url != url and full_url not in season_links:
                season_links.append(full_url)
    if not season_links:
        season_links = [url]

    print(f'[*] Found {len(season_links)} season(s)')

    for season_url in season_links:
        season_name = season_url.rstrip('/').split('/')[-1]
        for a in soup.find_all('a', href=True):
            href = a['href'].split('#')[0]
            if urljoin(PLUTO_BASE, href) == season_url:
                txt = a.get_text(strip=True)
                if txt:
                    season_name = txt
                break
        print(f'\n[*] Season: {season_name}')

        # Step 1: collect all episodes across all pages
        page          = 1
        seen_eps      = set()
        all_season_eps = []
        print('  [*] Scanning all pages...')

        while True:
            page_url = season_url if page == 1 else f'{season_url}/page/{page}'
            r2 = safe_get(session, page_url, timeout=30)
            if not r2 or r2.status_code == 404:
                break
            soup2    = BeautifulSoup(r2.text, 'html.parser')
            ep_items = []
            for a in soup2.find_all('a', href=True):
                href     = a['href'].split('#')[0]
                full_url = urljoin(PLUTO_BASE, href)
                if '/series/' not in href: continue
                if full_url == season_url or full_url in seen_eps: continue
                if not any(x in href.lower() for x in EP_KEYWORDS): continue
                ep_items.append((full_url, _ep_name_from_tag(a)))

            seen_urls  = set()
            unique_eps = []
            for ep_url, ep_name in ep_items:
                if ep_url not in seen_urls:
                    seen_urls.add(ep_url)
                    unique_eps.append((ep_url, ep_name))
            if not unique_eps:
                break
            for ep_url, _ in unique_eps:
                seen_eps.add(ep_url)
            print(f'  [*] Page {page}: {len(unique_eps)} episode(s)')
            all_season_eps.extend(unique_eps)
            page += 1
            time.sleep(0.5)

        if not all_season_eps:
            print('  [!] No episodes found for this season')
            continue

        # Step 2: sort EP1 → EP last
        def ep_sort_key(item):
            ep_url, ep_name = item
            m = re.search(r'[Ee](?:pisode\s*)?(\d+)', ep_name)
            if m: return int(m.group(1))
            m = re.search(r'-e(\d+)', ep_url.lower())
            return int(m.group(1)) if m else 0

        all_season_eps.sort(key=ep_sort_key)
        print(f'  [*] Total: {len(all_season_eps)} episode(s) sorted')

        # Step 3: extract download links
        items = []
        for i, (ep_url, ep_name) in enumerate(all_season_eps, 1):
            if state.stop: break
            wait_if_paused(state)
            print(f'\n  [{i}/{len(all_season_eps)}] Extracting: {ep_name}')
            r3 = safe_get(session, ep_url, timeout=30)
            if not r3:
                print('  [✗] Could not fetch episode page')
                summary.add_failed(ep_name)
                continue
            soup3   = BeautifulSoup(r3.text, 'html.parser')
            dl_link = next((a['href'] for a in soup3.find_all('a', href=True)
                            if 'dl.plutomovies.com' in a['href']), None)
            if not dl_link:
                print('  [✗] No download link on episode page')
                summary.add_failed(ep_name)
                continue
            direct = resolve_plutomovies_dl(dl_link, session)
            if direct:
                ext = 'mkv' if 'mkv' in direct.lower() else 'mp4'
                items.append((direct, safe_filename(f'{ep_name}.{ext}')))
            else:
                print('  [✗] Could not resolve download link')
                summary.add_failed(ep_name)
            time.sleep(0.5)

        # Step 4: download
        if items:
            print(f'\n  [*] Downloading {len(items)} episode(s)...')
            download_batch(items, folder, summary, state, series_url=url, series_name=name)

    summary.report()
