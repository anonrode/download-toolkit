"""
core.py — Utilities, signal handling, and download backends.

Merged from: core/helpers.py, core/signals.py, core/downloader.py
"""

import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    BASE_DIR, IS_ANDROID, UA_DESKTOP,
    log, safe_get, make_session, make_plain_session,
    log_download, mark_episode_done, mark_episode_current, is_episode_done,
)


# ─── String utilities ─────────────────────────────────────────────

def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = re.sub(r'\s+', ' ', name)
    return name.strip().rstrip('.')


def clean_name(slug: str) -> str:
    name = re.sub(r'[-_]+', ' ', slug)
    name = re.sub(r'\s+', ' ', name).strip()
    return name.title()


def clean_ep_name(raw: str) -> str:
    name = re.sub(r'\([\w\s]+p\)', '', raw)
    name = re.sub(r'\[[\w\s]+\]', '', name)
    name = re.sub(r'download', '', name, flags=re.IGNORECASE)
    name = re.sub(r'[-–|]+', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name or raw


def base_domain(url: str) -> str:
    m = re.search(r'(https?://[^/]+)', url)
    return m.group(1) if m else ''


def is_streaming_link(url: str) -> bool:
    return '.m3u8' in url or 'manifest' in url.lower()


def find_direct_video(text: str) -> str | None:
    for ext in [r'\.m3u8', r'\.mp4', r'\.mkv']:
        found = re.findall(
            r'https?://[^\s"\'<>,\\]+' + ext + r'[^\s"\'<>,\\]*', text
        )
        if found:
            return found[0].rstrip('.,;)')
    return None


# ─── File utilities ───────────────────────────────────────────────

def already_downloaded(folder: str, filename: str) -> tuple[bool, str | None]:
    base = re.sub(r'\.(mp4|mkv|m3u8|webm)$', '', filename)
    for ext in ['mp4', 'mkv', 'webm']:
        filepath = os.path.join(folder, f'{base}.{ext}')
        if os.path.exists(filepath):
            size = os.path.getsize(filepath)
            if size > 10 * 1024 * 1024:
                return True, filepath
            log.debug('Incomplete file %.1f MB — removing: %s', size / 1024 / 1024, filepath)
            try:
                os.remove(filepath)
            except Exception as e:
                log.warning('Could not remove incomplete file: %s', e)
            return False, None
    return False, None


def get_free_space_gb() -> float:
    try:
        if not hasattr(os, 'statvfs'):
            return 999.0
        stat = os.statvfs(BASE_DIR if IS_ANDROID else os.path.expanduser('~'))
        return (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
    except Exception:
        return 999.0


# ─── URL utilities ────────────────────────────────────────────────

def get_referer_for_url(url: str) -> str:
    if 'vikingfile.com' in url or 'vkng' in url:
        return 'https://vikingfile.com/'
    if 'kissorgrab.com' in url:
        return 'https://plutomovies.com/'
    if 'kwik.cx' in url or 'animepahe' in url:
        return 'https://anitaku.com.ro/'
    return base_domain(url) + '/'


def check_url_alive(url: str, session) -> str:
    try:
        r = session.head(url, timeout=10, allow_redirects=True)
        if r.status_code in (403, 404, 410):
            return 'expired'
        if r.status_code == 200:
            return 'ok'
        return 'unknown'
    except Exception:
        return 'unknown'


# ─── Diagnostics ─────────────────────────────────────────────────

def diagnose_page(soup, url: str, expected_pattern: str = None):
    log.warning('STRUCTURE DIAGNOSTIC for: %s', url[:60])
    log.warning('Expected: %s', expected_pattern or 'unknown')

    domain_links: dict[str, list] = {}
    for a in soup.find_all('a', href=True):
        href = a['href']
        if href.startswith('http'):
            dom = base_domain(href)
        elif href.startswith('/'):
            dom = '[relative]'
        else:
            continue
        domain_links.setdefault(dom, []).append(href)

    print(f'\n[!] Site structure changed at: {url[:60]}')
    print(f'[!] Expected: {expected_pattern or "unknown"}')
    print('[!] Links found by domain:')
    for dom, links in sorted(domain_links.items(), key=lambda x: -len(x[1])):
        print(f'  {dom}: {len(links)} links')
        for link in links[:3]:
            print(f'    • {link[:80]}')
    print('[!] Please report this output if the site structure has changed')


# ─── Signal handling ──────────────────────────────────────────────

def _kill_process(state):
    proc = state.get_process()
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            time.sleep(0.5)
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        state.set_process(None)


def setup_signal_handler(state):
    def handler(sig, frame):
        count = state.increment_ctrl_c()
        if count == 1:
            _kill_process(state)
            state.set_paused(True)
            print('\n[⏸] Paused — press Enter to continue, Ctrl+C again to exit')
        else:
            state.set_stop(True)
            state.set_paused(False)
            _kill_process(state)
            print('\n[✗] Exiting...')
            sys.exit(0)

    signal.signal(signal.SIGINT, handler)


def wait_if_paused(state):
    if not state.paused:
        return
    try:
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass
    while state.paused and not state.stop:
        try:
            input()
            state.set_paused(False)
            state.reset_ctrl_c()
            print('[▶] Continuing...')
            return
        except EOFError:
            state.set_paused(False)
            state.reset_ctrl_c()
            return
        except Exception:
            time.sleep(0.1)


# ─── Download summary ─────────────────────────────────────────────

_PRINT_LOCK = threading.Lock()


def safe_print(*args, **kwargs):
    with _PRINT_LOCK:
        print(*args, **kwargs)


class DownloadSummary:
    def __init__(self):
        self.success     = 0
        self.skipped     = 0
        self.failed      = 0
        self.failed_list = []
        self._lock       = threading.Lock()

    def add_success(self):
        with self._lock:
            self.success += 1

    def add_skipped(self):
        with self._lock:
            self.skipped += 1

    def add_failed(self, name: str = ''):
        with self._lock:
            self.failed += 1
            if name:
                self.failed_list.append(name)

    def report(self):
        total = self.success + self.skipped + self.failed
        if total == 0:
            return
        print(f"\n{'='*50}")
        print('  DOWNLOAD COMPLETE')
        print(f'  Total:     {total}')
        print(f'  ✓ Done:    {self.success}')
        if self.skipped:
            print(f'  ✓ Skipped: {self.skipped} (already downloaded)')
        if self.failed:
            print(f'  ✗ Failed:  {self.failed}')
            for name in self.failed_list:
                print(f'    • {name}')
        print(f"{'='*50}")


# ─── Tool installers ─────────────────────────────────────────────

def _install_aria2c(state) -> bool:
    import platform, shutil
    print('[*] Installing aria2...')
    try:
        if state and getattr(state, 'is_android', False):
            env = os.environ.copy()
            env['DEBIAN_FRONTEND'] = 'noninteractive'
            subprocess.run(['pkg', 'install', 'aria2', '-y'], check=True, env=env)
        elif platform.system() == 'Windows':
            print('[!] Install aria2 manually from https://github.com/aria2/aria2/releases')
            return False
        else:
            subprocess.run(['sudo', 'apt', 'install', 'aria2', '-y'], check=True)
        if state:
            state.has_aria2c = True
        print('[✓] aria2 installed')
        return True
    except Exception as e:
        print(f'[!] Failed to install aria2: {e}')
        return False


def _install_ytdlp(state) -> bool:
    print('[*] Installing yt-dlp...')
    try:
        subprocess.run(
            [sys.executable, '-m', 'pip', 'install', 'yt-dlp',
             '--break-system-packages', '-q'],
            check=True
        )
        if state:
            state.has_ytdlp = True
        print('[✓] yt-dlp installed')
        return True
    except Exception as e:
        print(f'[!] Failed to install yt-dlp: {e}')
        return False


# ─── Download backends ───────────────────────────────────────────

def download_with_aria2c(url: str, folder: str, filename: str,
                         summary: DownloadSummary, state,
                         retries: int = 3) -> bool:
    if not state.has_aria2c:
        if not _install_aria2c(state):
            safe_print('[!] aria2c unavailable — falling back to requests')
            return download_with_requests(url, folder, filename, summary, state)

    os.makedirs(folder, exist_ok=True)
    safe_fname   = re.sub(r'[^\w]', '_', filename)[:30]
    session_file = os.path.join(folder, f'.aria2_{safe_fname}.txt')
    filepath     = os.path.join(folder, filename)
    referer      = get_referer_for_url(url)

    safe_print(f'  [↓] Downloading: {filename}')

    for attempt in range(retries):
        try:
            cmd = [
                'aria2c', '-c',
                '--max-tries=0', '--retry-wait=30',
                '--timeout=120', '--connect-timeout=60',
                '--lowest-speed-limit=0',
                '--save-session', session_file,
                '--save-session-interval=30',
                '--file-allocation=none',
                '-x', '16', '-s', '16',
                '--min-split-size', '1M',
                '--piece-length', '1M',
                '--max-concurrent-downloads', '1',
                '--user-agent', UA_DESKTOP,
                '--referer', referer,
                '--header', 'Accept: video/mp4,video/x-matroska,video/*,*/*',
                '--header', 'Accept-Language: en-US,en;q=0.9',
                '--header', f'Origin: {base_domain(referer)}',
                '--allow-overwrite=true',
                '--auto-file-renaming=false',
                '--console-log-level=warn',
                '--summary-interval=0',
                '-d', folder, '-o', filename,
            ]
            if state.bandwidth_limit > 0:
                cmd += ['--max-download-limit', f'{state.bandwidth_limit}K']
            cmd.append(url)

            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
            state.set_process(proc)
            proc.wait()
            code = proc.returncode
            state.set_process(None)

            if code == 0:
                if os.path.exists(filepath):
                    size    = os.path.getsize(filepath)
                    size_mb = size / (1024 * 1024)
                    if size < 100 * 1024:
                        safe_print(f'  [✗] File too small ({size_mb:.2f}MB) — likely error page')
                        try: os.remove(filepath)
                        except Exception: pass
                        if attempt < retries - 1:
                            safe_print(f'  [*] Retrying ({attempt+2}/{retries})...')
                            time.sleep(5)
                            continue
                        summary.add_failed(filename)
                        return False
                    safe_print(f'  [✓] Done: {filename} ({size_mb:.1f}MB)')
                    try:
                        if os.path.exists(session_file):
                            os.remove(session_file)
                    except Exception:
                        pass
                    summary.add_success()
                    log_download(filename, url, filepath)
                    return True
                else:
                    safe_print('  [✗] File not found after download')
                    if attempt < retries - 1:
                        safe_print(f'  [*] Retrying ({attempt+2}/{retries})...')
                        time.sleep(5)
                        continue
                    summary.add_failed(filename)
                    return False
            else:
                safe_print(f'  [✗] aria2c failed (code {code})')
                if attempt < retries - 1:
                    safe_print(f'  [*] Retrying ({attempt+2}/{retries})...')
                    time.sleep(5)
                    continue
                summary.add_failed(filename)
                return False
        except Exception as e:
            safe_print(f'  [!] aria2c error: {e}')
            summary.add_failed(filename)
            return False
    return False


def download_with_ytdlp(url: str, folder: str, filename: str,
                        summary: DownloadSummary, state) -> bool:
    if not state.has_ytdlp:
        if not _install_ytdlp(state):
            safe_print('  [!] yt-dlp unavailable')
            summary.add_failed(filename)
            return False
    if not state.has_ffmpeg:
        safe_print('  [!] ffmpeg not found — install with: pkg install ffmpeg')
        summary.add_failed(filename)
        return False

    os.makedirs(folder, exist_ok=True)
    base         = re.sub(r'\.(mp4|mkv|m3u8)$', '', filename)
    out_template = os.path.join(folder, base + '.%(ext)s')

    safe_print(f'  [↓] yt-dlp ({state.quality_label}): {filename}')
    try:
        cmd = [
            'yt-dlp',
            '-f', state.quality_fmt,
            '--merge-output-format', 'mp4',
            '-o', out_template,
            '--no-playlist',
            '--retries', 'infinite',
            '--fragment-retries', 'infinite',
            '--retry-sleep', '10',
            '--quiet', '--no-warnings',
            '--progress', '--newline',
        ]
        if state.has_aria2c:
            cmd += [
                '--external-downloader', 'aria2c',
                '--external-downloader-args',
                'aria2c:-x 16 -s 16 -c --max-tries=0 --retry-wait=30 '
                '--timeout=120 --connect-timeout=60 '
                '--file-allocation=none --min-split-size=1M',
            ]
        cmd.append(url)

        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        state.set_process(proc)
        proc.wait()
        code = proc.returncode
        state.set_process(None)

        if code == 0:
            final_file = None
            for ext in ['mp4', 'mkv', 'webm']:
                p = os.path.join(folder, f'{base}.{ext}')
                if os.path.exists(p):
                    final_file = p
                    break
            if final_file:
                size_mb = os.path.getsize(final_file) / (1024 * 1024)
                safe_print(f'  [✓] Done: {filename} ({size_mb:.1f}MB)')
            else:
                safe_print(f'  [✓] Done: {filename}')
            summary.add_success()
            log_download(filename, url, final_file or os.path.join(folder, filename))
            return True
        else:
            safe_print('  [✗] yt-dlp failed')
            summary.add_failed(filename)
            return False
    except Exception as e:
        safe_print(f'  [!] yt-dlp error: {e}')
        summary.add_failed(filename)
        return False


def download_with_requests(url: str, folder: str, filename: str,
                           summary: DownloadSummary, state) -> bool:
    filepath = os.path.join(folder, filename)
    os.makedirs(folder, exist_ok=True)
    try:
        s = make_session()
        r = s.get(url, stream=True, timeout=30,
                  headers={**dict(s.headers), 'Referer': get_referer_for_url(url)})
        if r.status_code != 200:
            safe_print(f'  [!] HTTP {r.status_code}')
            summary.add_failed(filename)
            return False
        if 'text/html' in r.headers.get('content-type', ''):
            safe_print('  [!] Got HTML instead of video')
            summary.add_failed(filename)
            return False

        total      = int(r.headers.get('content-length', 0))
        downloaded = 0
        start_time = time.time()

        with open(filepath, 'wb') as f:
            for chunk in r.iter_content(chunk_size=512 * 1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct     = downloaded * 100 // total
                        mb_done = downloaded / (1024 * 1024)
                        mb_tot  = total / (1024 * 1024)
                        elapsed = time.time() - start_time
                        speed   = (downloaded / elapsed / 1024 / 1024) if elapsed > 0 else 0
                        eta     = int((total - downloaded) / (downloaded / elapsed)) if downloaded > 0 else 0
                        safe_print(
                            f'\r  [↓] {pct}% — {mb_done:.1f}/{mb_tot:.1f}MB '
                            f'— {speed:.1f}MB/s — ETA {eta}s',
                            end='', flush=True
                        )
        safe_print()

        if not os.path.exists(filepath) or os.path.getsize(filepath) < 100 * 1024:
            try:
                if os.path.exists(filepath): os.remove(filepath)
            except Exception: pass
            safe_print('  [!] File too small — likely failed')
            summary.add_failed(filename)
            return False

        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        safe_print(f'  [✓] Done: {filename} ({size_mb:.1f}MB)')
        summary.add_success()
        log_download(filename, url, filepath)
        return True
    except Exception as e:
        try:
            if os.path.exists(filepath): os.remove(filepath)
        except Exception: pass
        safe_print(f'  [!] requests error: {e}')
        summary.add_failed(filename)
        return False


# ─── Smart dispatcher ────────────────────────────────────────────

def download_file(url: str, folder: str, filename: str,
                  summary: DownloadSummary, state,
                  check_expiry: bool = True,
                  series_url: str = None,
                  series_name: str = None) -> bool:
    done, _ = already_downloaded(folder, filename)
    if done:
        safe_print('  [✓] Already downloaded — skipping')
        summary.add_skipped()
        if series_url:
            mark_episode_done(series_url, series_name or folder, filename)
        return True

    if series_url and is_episode_done(series_url, filename):
        safe_print('  [✓] Done in previous session — skipping')
        summary.add_skipped()
        return True

    if check_expiry and not is_streaming_link(url):
        s = make_plain_session()
        status = check_url_alive(url, s)
        if status == 'expired':
            safe_print('  [!] Link expired (403/404) — re-paste the series URL for fresh links')
            summary.add_failed(filename)
            return False

    wait_if_paused(state)
    if state.stop:
        return False

    if series_url:
        mark_episode_current(series_url, series_name or folder, filename)

    if is_streaming_link(url):
        result = download_with_ytdlp(url, folder, filename, summary, state)
    else:
        result = download_with_aria2c(url, folder, filename, summary, state)

    if result and series_url:
        mark_episode_done(series_url, series_name or folder, filename)

    return result


# ─── Batch / parallel ────────────────────────────────────────────

def download_batch(items: list, folder: str, summary: DownloadSummary,
                   state, series_url: str = None, series_name: str = None):
    if not items:
        return

    if state.parallel_count == 1:
        for url, filename in items:
            download_file(url, folder, filename, summary, state,
                          series_url=series_url, series_name=series_name)
    else:
        with ThreadPoolExecutor(max_workers=state.parallel_count) as ex:
            futures = {
                ex.submit(download_file, url, folder, filename, summary, state,
                          True, series_url, series_name): filename
                for url, filename in items
            }
            for future in as_completed(futures):
                fname = futures[future]
                try:
                    future.result()
                except Exception as e:
                    safe_print(f'  [!] Thread error for {fname}: {e}')
                    summary.add_failed(fname)


# ─── Prefetcher ──────────────────────────────────────────────────

class Prefetcher:
    """Pre-fetch the next episode's link while the current one downloads."""
    def __init__(self, fetch_fn):
        self.fetch_fn = fetch_fn
        self._result  = None
        self._ready   = threading.Event()

    def prefetch(self, *args, **kwargs):
        self._ready.clear()
        self._result = None
        def _run():
            try:
                self._result = self.fetch_fn(*args, **kwargs)
            except Exception:
                self._result = None
            self._ready.set()
        threading.Thread(target=_run, daemon=True).start()

    def get(self, timeout: int = 30):
        self._ready.wait(timeout=timeout)
        return self._result
