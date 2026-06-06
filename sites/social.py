"""sites/social.py — Social media / catch-all yt-dlp downloader."""

import os, re, subprocess
from config import BASE_DIR
from core import safe_filename, base_domain
from core import DownloadSummary
from config import log_download


def extract(url: str, session, state):
    domain   = base_domain(url).replace('https://', '').replace('www.', '')
    print(f'[*] Social/Generic mode: {domain}')
    name     = domain.split('.')[0].title()
    folder   = os.path.join(BASE_DIR, 'Social', safe_filename(name))
    os.makedirs(folder, exist_ok=True)
    slug     = url.rstrip('/').split('/')[-1] or 'video'
    slug     = re.sub(r'[^\w-]', '_', slug)[:50]
    filename = safe_filename(f'{slug}.mp4')
    base     = re.sub(r'\.(mp4|mkv|m3u8)$', '', filename)
    out_tmpl = os.path.join(folder, base + '.%(ext)s')
    summary  = DownloadSummary()

    print(f'[*] Downloading: {filename}')
    print(f'[*] Saving to:   {folder}')

    format_chain = [
        'bestvideo[height<=720]+bestaudio/best[height<=720]',
        'bestvideo[height<=480]+bestaudio/best[height<=480]',
        'bestvideo[height<=360]+bestaudio/best[height<=360]',
        'bestvideo+bestaudio/best',
        'best',
    ]

    for fmt in format_chain:
        cmd = [
            'yt-dlp', '-f', fmt,
            '--merge-output-format', 'mp4',
            '-o', out_tmpl,
            '--no-playlist',
            '--retries', '3', '--fragment-retries', '3',
            '--quiet', '--no-warnings', '--progress', '--newline',
            url,
        ]
        if state.has_aria2c:
            cmd += [
                '--external-downloader', 'aria2c',
                '--external-downloader-args',
                'aria2c:-x 16 -s 16 -c --max-tries=0 --retry-wait=30 '
                '--timeout=120 --connect-timeout=60 '
                '--file-allocation=none --min-split-size=1M',
            ]
        result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if result.returncode == 0:
            for ext in ['mp4', 'mkv', 'webm']:
                p = os.path.join(folder, f'{base}.{ext}')
                if os.path.exists(p):
                    size_mb = os.path.getsize(p) / (1024 * 1024)
                    print(f'  [✓] Done: {filename} ({size_mb:.1f}MB)')
                    summary.add_success()
                    log_download(filename, url, p)
                    summary.report()
                    return True
            print(f'  [✓] Done: {filename}')
            summary.add_success()
            summary.report()
            return True
        if 'requested format not available' in result.stderr.lower():
            continue
        print(f'  [✗] yt-dlp failed: {result.stderr[:100]}')
        break

    summary.add_failed(filename)
    summary.report()
    return False
