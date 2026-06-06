"""sites/naijavault.py — NaijaVault extractor."""

import os
import re
import time

from bs4 import BeautifulSoup

from config import BASE_DIR, safe_get, mark_series_complete
from core import safe_filename, clean_name, clean_ep_name, diagnose_page, DownloadSummary, download_file, wait_if_paused
from sites.resolvers import resolve_vikingfile


def extract(url: str, session, state):
    print('[*] NaijaVault mode')
    slug = url.rstrip('/').split('/')[-1]
    name = re.sub(r'-\d{4}.*$', '', slug)
    name = re.sub(r'-season-\d+.*$', '', name, flags=re.IGNORECASE)
    name = clean_name(name)
    print(f'[*] Title: {name}')
    folder  = os.path.join(BASE_DIR, safe_filename(name))
    summary = DownloadSummary()

    session.headers['Referer'] = 'https://www.naijavault.com/'
    r = safe_get(session, url, timeout=30)
    if not r:
        return
    soup = BeautifulSoup(r.text, 'html.parser')

    # Collect /dl- episode links — dict.fromkeys preserves order, dedupes by href
    dl_links = list(dict.fromkeys(
        (a.get_text(strip=True), a['href'])
        for a in soup.find_all('a', href=True)
        if '/dl-' in a['href'] and 'naijavault.com' in a['href']
    ))

    # Single movie page: downloadURL JS var is directly on the page
    if not dl_links:
        if re.search(r'var downloadURL = "([^"]+)"', r.text):
            page_title = soup.find('title')
            label      = page_title.get_text(strip=True) if page_title else slug
            dl_links   = [(label, url)]

    if not dl_links:
        print('[!] No episode links found')
        diagnose_page(soup, url, '/dl- links')
        return

    print(f'[*] Found {len(dl_links)} episode(s) — saving to: {folder}')

    for i, (label, dl_url) in enumerate(dl_links, 1):
        if state.stop:
            break
        wait_if_paused(state)

        print(f'\n[{i}/{len(dl_links)}] {label[:50]}')
        session.headers['Referer'] = url
        r2 = safe_get(session, dl_url, timeout=20)
        if not r2:
            print('  [✗] Could not fetch download page')
            summary.add_failed(label)
            continue

        # Prefer fileTitle from JS for the filename, fall back to label
        title_match = re.search(r'var fileTitle = "([^"]+)"', r2.text)
        raw_name    = title_match.group(1) if title_match else clean_ep_name(label) or f'episode-{i}'
        ep_name     = safe_filename(re.sub(r'\.(mkv|mp4)$', '', raw_name, flags=re.IGNORECASE))

        # Pattern 1 — var downloadURL (vikingfile or other direct)
        vf_match = re.search(r'var downloadURL = "([^"]+)"', r2.text)
        if vf_match:
            vf_url = vf_match.group(1)
            direct = resolve_vikingfile(vf_url, session) if 'vikingfile.com' in vf_url else vf_url
            if direct:
                ext = 'mkv' if '.mkv' in direct else 'mp4'
                download_file(direct, folder, f'{ep_name}.{ext}', summary, state,
                              series_url=url, series_name=name)
            else:
                print('  [✗] VikingFile resolution failed')
                summary.add_failed(ep_name)
            time.sleep(1)
            continue

        # Pattern 2 — cdn.filevault.com.ng direct link
        fv = re.findall(r'https?://cdn\.filevault\.com\.ng/[^\s"\'<>]+', r2.text)
        if fv:
            ext = 'mkv' if '.mkv' in fv[0] else 'mp4'
            download_file(fv[0], folder, f'{ep_name}.{ext}', summary, state,
                          series_url=url, series_name=name)
            time.sleep(1)
            continue

        print('  [✗] No download URL found on page')
        summary.add_failed(ep_name)
        time.sleep(1)

    if summary.failed == 0 and not state.stop:
        mark_series_complete(url)
    summary.report()
