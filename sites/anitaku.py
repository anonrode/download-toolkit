"""sites/anitaku.py — Anitaku extractor."""

import os, re, time
from bs4 import BeautifulSoup
from config import BASE_DIR, ANITAKU_BASE
from core import safe_filename, clean_name, already_downloaded, diagnose_page
from core import DownloadSummary, download_with_ytdlp
from core import wait_if_paused
from config import safe_get


def extract(url: str, session, state):
    print('[*] Anitaku mode')
    slug       = url.rstrip('/').split('/')[-1]
    is_episode = 'episode-' in slug
    name       = re.sub(r'-episode-\d+.*$', '', slug) if is_episode else slug
    name       = clean_name(name)
    print(f'[*] Title: {name}')
    folder  = os.path.join(BASE_DIR, safe_filename(name))
    summary = DownloadSummary()

    def download_episode(ep_url, ep_name):
        r = safe_get(session, ep_url, referer=ANITAKU_BASE + '/', timeout=30)
        if not r:
            print(f'  [✗] Could not fetch: {ep_name}')
            summary.add_failed(ep_name)
            return

        tamil = re.search(r"""(https://tamilembed\.lol/embed/[^\s"'<>]+)""", r.text)
        if tamil:
            print('  [*] Found tamilembed stream')
            download_with_ytdlp(tamil.group(1), folder, safe_filename(f'{ep_name}.mp4'), summary, state)
            return

        soup   = BeautifulSoup(r.text, 'html.parser')
        iframe = soup.find('iframe', src=re.compile(r'tamilembed|embed'))
        if iframe:
            src = iframe.get('src', '')
            if not src.startswith('http'):
                src = 'https:' + src
            print('  [*] Found embed via iframe')
            download_with_ytdlp(src, folder, safe_filename(f'{ep_name}.mp4'), summary, state)
            return

        print('  [*] Trying yt-dlp on episode page directly')
        result = download_with_ytdlp(ep_url, folder, safe_filename(f'{ep_name}.mp4'), summary, state)
        if not result:
            print(f'  [✗] All methods failed for: {ep_name}')
            diagnose_page(soup, ep_url, 'tamilembed.lol embed URL')

    if is_episode:
        print(f'[*] Single episode — saving to: {folder}')
        download_episode(url, safe_filename(slug))
        summary.report()
        return

    print('[*] Fetching episode list...')
    r = safe_get(session, url, referer=ANITAKU_BASE + '/', timeout=30)
    if not r:
        print('[!] Could not fetch series page')
        return
    soup     = BeautifulSoup(r.text, 'html.parser')
    ep_links = []
    seen     = set()

    for a in soup.find_all('a', href=True):
        href = a['href']
        text = a.get_text(strip=True)
        if 'episode-' in href and ANITAKU_BASE in href and href not in seen:
            ep_slug    = href.rstrip('/').split('/')[-1]
            anime_base = slug.rstrip('/')
            if ep_slug.startswith(anime_base) or anime_base in ep_slug:
                seen.add(href)
                ep_links.append((href, text or ep_slug))

    if not ep_links:
        for a in soup.find_all('a', href=True):
            href = a['href']
            if 'episode-' in href and href not in seen:
                seen.add(href)
                ep_links.append((href, a.get_text(strip=True) or href.split('/')[-1]))

    if not ep_links:
        print('[!] No episode links found')
        return

    ep_links.sort(key=lambda x: int(m.group(1)) if (m := re.search(r'episode-(\d+)', x[0])) else 0)
    print(f'[*] Found {len(ep_links)} episode(s) — saving to: {folder}')

    for i, (ep_url, _) in enumerate(ep_links, 1):
        if state.stop: break
        wait_if_paused(state)
        ep_name = safe_filename(ep_url.rstrip('/').split('/')[-1])
        print(f'\n[{i}/{len(ep_links)}] {ep_name}')
        done, _ = already_downloaded(folder, f'{ep_name}.mp4')
        if done:
            print('  [✓] Already downloaded — skipping')
            summary.add_skipped()
            continue
        download_episode(ep_url, ep_name)
        time.sleep(1)

    summary.report()
