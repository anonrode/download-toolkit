"""sites/myasiantv.py — MyAsianTV extractor."""

import os, re, time
from bs4 import BeautifulSoup
from config import BASE_DIR
from core import safe_filename, clean_name, base_domain
from core import DownloadSummary, download_file
from core import wait_if_paused
from config import safe_get
from sites.resolvers import resolve_embed


def extract(url: str, session, state):
    print('[*] MyAsianTV mode')
    slug   = url.rstrip('/').split('/')[-1]
    name   = re.sub(r'-episode-\d+.*$', '', slug)
    name   = re.sub(r'-\d{4}.*$', '', name)
    name   = clean_name(name)
    print(f'[*] Series: {name}')
    folder  = os.path.join(BASE_DIR, safe_filename(name))
    bd      = base_domain(url)
    summary = DownloadSummary()

    if 'episode-' in url:
        ep_links = [url]
    else:
        print('[*] Fetching episode list...')
        r = safe_get(session, url, referer=bd + '/', timeout=30)
        if not r:
            return
        soup      = BeautifulSoup(r.text, 'html.parser')
        show_slug = re.sub(r'-\d{4}.*$', '', slug)
        ep_links  = list(dict.fromkeys(
            a['href'] for a in soup.find_all('a', href=True)
            if ('episode-' in a['href'] and bd in a['href'] and show_slug in a['href'])
        ))
        if not ep_links:
            print('[!] No episode links found')
            return
        ep_links.sort(key=lambda u: int(m.group(1)) if (m := re.search(r'episode-(\d+)', u)) else 0)
        print(f'[*] Found {len(ep_links)} episode(s) — saving to: {folder}')

    for i, ep_url in enumerate(ep_links, 1):
        if state.stop: break
        wait_if_paused(state)
        ep_name = ep_url.rstrip('/').split('/')[-1]
        print(f'\n[{i}/{len(ep_links)}] {ep_name}')
        r = safe_get(session, ep_url, referer=bd + '/', timeout=30)
        if not r:
            print('  [✗] Could not fetch episode page')
            summary.add_failed(ep_name)
            continue
        soup   = BeautifulSoup(r.text, 'html.parser')
        iframe = soup.find('iframe', src=re.compile(r'vidbasic|vidmoly')) or soup.find('iframe', src=True)
        if not iframe:
            print('  [!] No iframe found')
            summary.add_failed(ep_name)
            continue
        src = iframe.get('src', '')
        if not src.startswith('http'):
            src = 'https:' + src
        direct = resolve_embed(src, session)
        if direct:
            download_file(direct, folder, safe_filename(f'{ep_name}.mp4'), summary, state)
        else:
            print('  [✗] Could not extract video')
            summary.add_failed(ep_name)
        time.sleep(1)
    summary.report()
