"""sites/dramarain.py — DramaRain / DramaKey.cc extractor."""

import os, re
from bs4 import BeautifulSoup
from config import BASE_DIR
from core import safe_filename, clean_name, diagnose_page
from core import DownloadSummary, download_file
from core import wait_if_paused
from config import safe_get
from sites.resolvers import resolve_drip_waffi


def extract(url: str, session, state):
    site   = 'DramaKey.cc' if 'dramakey.cc' in url else 'DramaRain'
    print(f'[*] {site} mode')
    slug   = url.rstrip('/').split('/')[-1]
    slug   = re.sub(r'-(chinese|korean|thai|japanese|drama|tvshows|movies?).*$', '', slug, flags=re.IGNORECASE)
    name   = clean_name(slug)
    print(f'[*] Title: {name}')
    folder  = os.path.join(BASE_DIR, safe_filename(name))
    summary = DownloadSummary()

    site_referer = 'https://dramakey.cc/' if 'dramakey.cc' in url else 'https://dramarain.com/'
    session.headers['Referer'] = site_referer
    r = safe_get(session, url, referer=site_referer)
    if not r:
        return
    soup = BeautifulSoup(r.text, 'html.parser')

    # Method 1: direct drip.waffi.cloud links already in page
    drip_links = [(a.text.strip(), a['href']) for a in soup.find_all('a', href=True)
                  if 'drip.waffi.cloud' in a['href']]
    if drip_links:
        print(f'[*] Found {len(drip_links)} direct link(s) — saving to: {folder}')
        for i, (label, link) in enumerate(drip_links, 1):
            if state.stop: break
            wait_if_paused(state)
            fname = safe_filename(f'{label or f"episode-{i}"}.mp4')
            print(f'\n[{i}/{len(drip_links)}] {fname}')
            download_file(link, folder, fname, summary, state)
        summary.report()
        return

    # Method 2: download page links that redirect to drip
    dl_links = [(a.text.strip(), a['href']) for a in soup.find_all('a', href=True)
                if any(x in a['href'] for x in ['dramarain.com/download', 'dramakey.cc/download', 'drip.waffi.cloud'])]
    if dl_links:
        print(f'[*] Found {len(dl_links)} episode(s) — saving to: {folder}')
        for i, (label, dl_url) in enumerate(dl_links, 1):
            if state.stop: break
            wait_if_paused(state)
            fname = safe_filename(f'{label or f"episode-{i}"}.mp4')
            print(f'\n[{i}/{len(dl_links)}] {fname}')
            if 'drip.waffi.cloud' in dl_url:
                direct = dl_url
            else:
                session.headers['Referer'] = site_referer
                direct = resolve_drip_waffi(dl_url, session)
            if direct:
                download_file(direct, folder, fname, summary, state)
            else:
                print('  [✗] Could not resolve link')
                summary.add_failed(fname)
            import time; time.sleep(0.5)
        summary.report()
        return

    print(f'[!] No download links found')
    diagnose_page(soup, url, 'drip.waffi.cloud links')
