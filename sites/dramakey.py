"""sites/dramakey.py — DramaKey.com extractor (downloadwella)."""

import os, re
from bs4 import BeautifulSoup
from config import BASE_DIR
from core import safe_filename, clean_name, diagnose_page
from core import DownloadSummary, download_file, Prefetcher
from core import wait_if_paused
from config import mark_series_complete
from config import safe_get
from sites.resolvers import resolve_downloadwella


def extract(url: str, session, state):
    print('[*] DramaKey.com mode')
    slug = url.rstrip('/').split('/')[-1]
    slug = re.sub(r'-s\d+.*$', '', slug, flags=re.IGNORECASE)
    slug = re.sub(r'-(season|episode|complete).*$', '', slug, flags=re.IGNORECASE)
    name = clean_name(slug)
    print(f'[*] Series: {name}')
    folder  = os.path.join(BASE_DIR, safe_filename(name))
    summary = DownloadSummary()

    r = safe_get(session, url)
    if not r:
        return
    soup  = BeautifulSoup(r.text, 'html.parser')
    links = list(dict.fromkeys(
        a['href'] for a in soup.find_all('a', href=True)
        if 'downloadwella.com' in a['href']
    ))
    if not links:
        print('[!] No downloadwella links found')
        diagnose_page(soup, url, 'downloadwella.com links')
        return

    print(f'[*] Found {len(links)} episode(s) — saving to: {folder}')
    pf          = Prefetcher(resolve_downloadwella)
    next_direct = [None]

    for i, ep_url in enumerate(links, 1):
        if state.stop: break
        wait_if_paused(state)
        ep_name = ep_url.split('/')[-1].replace('.html', '')
        print(f'\n[{i}/{len(links)}] {ep_name}')

        direct = next_direct[0] if next_direct[0] is not None else resolve_downloadwella(ep_url, session)
        next_direct[0] = None
        if i < len(links):
            pf.prefetch(links[i], session)

        if direct:
            ext   = 'mkv' if '.mkv' in direct else 'mp4'
            fname = safe_filename(f'{ep_name}.{ext}')
            download_file(direct, folder, fname, summary, state, series_url=url, series_name=name)
            if i < len(links):
                next_direct[0] = pf.get(timeout=60)
        else:
            print('  [✗] Could not extract link')
            summary.add_failed(ep_name)
            if i < len(links):
                next_direct[0] = pf.get(timeout=60)

    if summary.failed == 0 and not state.stop:
        mark_series_complete(url)
    summary.report()
