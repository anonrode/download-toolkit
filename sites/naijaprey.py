"""sites/naijaprey.py — NaijaPrey extractor."""

import os, re, time
from bs4 import BeautifulSoup
from config import BASE_DIR
from core import safe_filename, clean_name
from core import DownloadSummary, download_file
from core import wait_if_paused
from config import safe_get
from sites.resolvers import resolve_wildshare


def extract(url: str, session, state):
    print('[*] NaijaPrey mode')
    slug    = url.rstrip('/').split('/')[-1]
    name    = clean_name(slug)
    print(f'[*] Title: {name}')
    folder  = os.path.join(BASE_DIR, safe_filename(name))
    summary = DownloadSummary()

    r = safe_get(session, url, referer='https://www.naijaprey.tv/')
    if not r:
        return
    soup     = BeautifulSoup(r.text, 'html.parser')
    ep_links = list(dict.fromkeys(
        a['href'] for a in soup.find_all('a', href=True)
        if 'vdl.np-downloader.com' in a['href']
    ))
    print(f'[*] Found {len(ep_links)} episode(s) — saving to: {folder}')

    for i, ep_url in enumerate(ep_links, 1):
        if state.stop: break
        wait_if_paused(state)
        ep_name = ep_url.rstrip('/').split('/')[-1]
        print(f'\n[{i}/{len(ep_links)}] {ep_name}')
        try:
            r2 = safe_get(session, ep_url, referer='https://www.naijaprey.tv/')
            if not r2:
                summary.add_failed(ep_name)
                continue
            soup2  = BeautifulSoup(r2.text, 'html.parser')
            ws_url = next((a['href'] for a in soup2.find_all('a', href=True)
                           if 'wildshare.net' in a['href']), None)
            if ws_url:
                direct = resolve_wildshare(ws_url)
                if direct:
                    ext = 'mkv' if '.mkv' in direct else 'mp4'
                    download_file(direct, folder, safe_filename(f'{ep_name}.{ext}'), summary, state)
                else:
                    print('  [✗] Wildshare failed')
                    summary.add_failed(ep_name)
            else:
                print('  [!] No wildshare link found')
                summary.add_failed(ep_name)
        except Exception as e:
            print(f'  [!] Error: {e}')
            summary.add_failed(ep_name)
        time.sleep(1)
    summary.report()
