"""sites/jarocks.py — 9jaRocks extractor."""

import os, re, time
from bs4 import BeautifulSoup
from config import BASE_DIR
from core import safe_filename, clean_name
from core import DownloadSummary, download_file
from core import wait_if_paused
from config import safe_get
from sites.resolvers import resolve_loadedfiles


def extract(url: str, session, state):
    print('[*] 9jaRocks mode')
    slug    = url.rstrip('/').split('/')[-1]
    name    = clean_name(re.sub(r'-id\d+.*$', '', slug))
    print(f'[*] Title: {name}')
    folder  = os.path.join(BASE_DIR, safe_filename(name))
    summary = DownloadSummary()

    r = safe_get(session, url, referer='https://9jarocks.net/')
    if not r:
        return
    soup     = BeautifulSoup(r.text, 'html.parser')
    lf_links = list(dict.fromkeys(
        a['href'] for a in soup.find_all('a', href=True)
        if 'loadedfiles.org' in a['href']
    ))
    print(f'[*] Found {len(lf_links)} file(s) — saving to: {folder}')

    for i, lf_url in enumerate(lf_links, 1):
        if state.stop: break
        wait_if_paused(state)
        fname = lf_url.split('/')[-1][:60]
        print(f'\n[{i}/{len(lf_links)}] {fname}')
        direct = resolve_loadedfiles(lf_url, session)
        if direct:
            ext = 'mkv' if '.mkv' in direct else 'mp4'
            download_file(direct, folder, safe_filename(f'{fname}.{ext}'), summary, state)
        else:
            print(f'  [✗] Could not extract: {fname}')
            summary.add_failed(fname)
        time.sleep(0.5)
    summary.report()
