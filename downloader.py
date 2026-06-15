"""
downloader.py — Download backends, resume state, history, disk space.
"""

import os
import re
import sys
import json
import time
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── SHARED STATE (imported from main) ────────────────────────
# These are set by main.py and read here. Imported at call time
# to avoid circular imports.
def _globals():
    import main as m
    return m

# ─── CONSTANTS ────────────────────────────────────────────────
IS_ANDROID   = os.path.exists('/storage/emulated/0')
BASE_DIR     = '/storage/emulated/0/Anon' if IS_ANDROID else os.path.join(os.path.expanduser('~'), 'Downloads', 'Anon')
LOG_FILE     = os.path.join(BASE_DIR, '.download_history.json')
RESUME_FILE  = os.path.join(BASE_DIR, '.resume_state.json')
DIAG_LOG     = os.path.join(BASE_DIR, '.diag.log')

UA_DESKTOP   = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

PRINT_LOCK   = threading.Lock()

def safe_print(*args, **kwargs):
    with PRINT_LOCK:
        print(*args, **kwargs)

# ─── DISK SPACE ───────────────────────────────────────────────
def get_free_space_gb():
    try:
        if not hasattr(os, 'statvfs'):
            return 999
        stat = os.statvfs(BASE_DIR if IS_ANDROID else os.path.expanduser('~'))
        return (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
    except Exception:
        return 999

def check_disk_space(min_gb=1.0):
    try:
        free = get_free_space_gb()
        if free < min_gb:
            safe_print(f"[!] Low disk space: {free:.1f}GB free. Downloads may fail.")
        else:
            safe_print(f"[✓] Disk space: {free:.1f}GB free")
    except Exception:
        pass

def assert_disk_space(min_mb=200):
    """Check before each episode. Stops download if critically low."""
    free_gb = get_free_space_gb()
    if free_gb < (min_mb / 1024):
        safe_print(f"[!] Critically low disk space ({free_gb*1024:.0f}MB free) — stopping")
        return False
    return True

# ─── DOWNLOAD HISTORY ─────────────────────────────────────────
def load_history():
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_history(history):
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(LOG_FILE, 'w') as f:
            json.dump(history, f, indent=2)
    except Exception:
        pass

def _media_scan(filepath):
    """Trigger Android media scanner so file appears in Gallery/WhatsApp immediately."""
    if not IS_ANDROID:
        return
    try:
        subprocess.Popen(
            ['termux-media-scan', filepath],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL
        )
    except Exception:
        pass

def log_download(name, url, filepath):
    history = load_history()
    if name not in history:
        history[name] = []
    entry = {'url': url, 'file': filepath, 'time': time.strftime('%Y-%m-%d %H:%M')}
    if entry not in history[name]:
        history[name].append(entry)
    save_history(history)
    _media_scan(filepath)

def show_history():
    history = load_history()
    if not history:
        print("[*] No download history yet")
        return
    print(f"\n{'='*50}")
    print(f"  DOWNLOAD HISTORY")
    print(f"{'='*50}")
    for name, entries in list(history.items())[-20:]:
        print(f"\n  {name} ({len(entries)} file(s))")
        for e in entries[-3:]:
            print(f"    • {e['time']} — {os.path.basename(e['file'])}")
    print(f"{'='*50}")

# ─── RESUME STATE ─────────────────────────────────────────────
def load_resume_state():
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        if os.path.exists(RESUME_FILE):
            with open(RESUME_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_resume_state(state):
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(RESUME_FILE, 'w') as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass

def mark_episode_done(series_url, series_name, ep_filename):
    state = load_resume_state()
    key = series_url
    if key not in state:
        state[key] = {'name': series_name, 'done': [], 'failed': [], 'current': None}
    if ep_filename not in state[key]['done']:
        state[key]['done'].append(ep_filename)
    state[key]['current'] = None
    save_resume_state(state)

def mark_episode_current(series_url, series_name, ep_filename):
    state = load_resume_state()
    key = series_url
    if key not in state:
        state[key] = {'name': series_name, 'done': [], 'failed': [], 'current': None}
    state[key]['current'] = ep_filename
    state[key]['name'] = series_name
    save_resume_state(state)

def mark_series_complete(series_url):
    state = load_resume_state()
    if series_url in state:
        del state[series_url]
        save_resume_state(state)

def is_episode_done_in_state(series_url, ep_filename):
    state = load_resume_state()
    if series_url in state:
        return ep_filename in state[series_url].get('done', [])
    return False

def show_resume_list():
    state = load_resume_state()
    if not state:
        print("[*] No paused downloads found")
        return False
    print("\n" + "="*50)
    print("  PAUSED DOWNLOADS")
    print("="*50)
    for i, (url, info) in enumerate(state.items(), 1):
        name    = info.get('name', 'Unknown')
        done    = len(info.get('done', []))
        current = info.get('current', None)
        status  = f"paused at: {current}" if current else f"{done} episode(s) done"
        print(f"  [{i}] {name}")
        print(f"       {status}")
        print(f"       {url[:60]}")
    print(f"{'='*50}")
    return True

# ─── DOWNLOAD SUMMARY ─────────────────────────────────────────
class DownloadSummary:
    def __init__(self):
        self.success     = 0
        self.skipped     = 0
        self.failed      = 0
        self._lock       = threading.Lock()
        self.failed_list = []

    def add_success(self):
        with self._lock:
            self.success += 1

    def add_skipped(self):
        with self._lock:
            self.skipped += 1

    def add_failed(self, name=''):
        with self._lock:
            self.failed += 1
            if name:
                self.failed_list.append(name)

    def report(self):
        total = self.success + self.skipped + self.failed
        if total == 0:
            return
        print(f"\n{'='*50}")
        print(f"  DOWNLOAD COMPLETE")
        print(f"  Total:     {total}")
        print(f"  ✓ Done:    {self.success}")
        if self.skipped:
            print(f"  ✓ Skipped: {self.skipped} (already downloaded)")
        if self.failed:
            print(f"  ✗ Failed:  {self.failed}")
            for name in self.failed_list:
                print(f"    • {name}")
        print(f"{'='*50}")
        # Termux notification when batch finishes
        if IS_ANDROID and total > 1:
            _notify(f"Download done — {self.success}/{total} episodes")

    def offer_retry(self):
        """Return failed list so caller can retry them."""
        return list(self.failed_list)

# ─── NOTIFICATION ─────────────────────────────────────────────
def _notify(message):
    try:
        subprocess.run(
            ['termux-notification', '--title', 'Download Toolkit', '--content', message],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5
        )
    except Exception:
        pass

# ─── HELPERS ──────────────────────────────────────────────────
def already_downloaded(folder, filename):
    base = re.sub(r'\.(mp4|mkv|m3u8|webm)$', '', filename)
    for ext in ['mp4', 'mkv', 'webm']:
        filepath = os.path.join(folder, f"{base}.{ext}")
        if os.path.exists(filepath):
            size = os.path.getsize(filepath)
            if size > 10 * 1024 * 1024:
                return True, filepath
            else:
                safe_print(f"  [!] Incomplete file ({size/1024/1024:.1f}MB) — re-downloading")
                try:
                    os.remove(filepath)
                except Exception as e:
                    safe_print(f"  [!] Could not remove: {e}")
                return False, None
    return False, None

def base_domain(url):
    m = re.search(r'(https?://[^/]+)', url)
    return m.group(1) if m else ''

def get_referer_for_url(url):
    if 'vikingfile.com' in url:
        return 'https://vikingfile.com/'
    if 'kissorgrab.com' in url:
        return 'https://plutomovies.com/'
    if 'kwik.cx' in url or 'animepahe' in url:
        return 'https://anitaku.com.ro/'
    return base_domain(url) + '/'

def is_streaming_link(url):
    return '.m3u8' in url or 'manifest' in url.lower()

def check_url_alive(url, session):
    try:
        r = session.head(url, timeout=10, allow_redirects=True)
        if r.status_code in (403, 404, 410):
            return 'expired'
        if r.status_code == 200:
            return 'ok'
        return 'unknown'
    except Exception:
        return 'unknown'

def safe_filename(name):
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = re.sub(r'\s+', ' ', name)
    name = name.strip().lstrip('.').rstrip('.')
    return name

def find_direct_video(text):
    for ext in [r'\.m3u8', r'\.mp4', r'\.mkv']:
        found = re.findall(r'https?://[^\s"\'<>,\\]+' + ext + r'[^\s"\'<>,\\]*', text)
        if found:
            return found[0].rstrip('.,;)')
    return None

def make_session():
    import requests
    s = requests.Session()
    s.headers.update({'User-Agent': UA_DESKTOP})
    return s

# ─── TOOL INSTALLERS ──────────────────────────────────────────
def _install_aria2c():
    import platform
    safe_print("[*] Installing aria2...")
    try:
        if IS_ANDROID:
            env = os.environ.copy()
            env['DEBIAN_FRONTEND'] = 'noninteractive'
            subprocess.run(['pkg', 'install', 'aria2', '-y'], check=True, env=env)
        elif platform.system() == 'Windows':
            safe_print("[!] Install aria2 manually from https://github.com/aria2/aria2/releases")
            return False
        else:
            subprocess.run(['sudo', 'apt', 'install', 'aria2', '-y'], check=True)
        safe_print("[✓] aria2 installed")
        return True
    except Exception as e:
        safe_print(f"[!] Failed to install aria2: {e}")
        return False

def _install_ytdlp():
    safe_print("[*] Installing yt-dlp...")
    try:
        subprocess.run(
            [sys.executable, '-m', 'pip', 'install', 'yt-dlp', '--break-system-packages', '-q'],
            check=True
        )
        safe_print("[✓] yt-dlp installed")
        return True
    except Exception as e:
        safe_print(f"[!] Failed to install yt-dlp: {e}")
        return False

def _update_ytdlp():
    """Silent yt-dlp update in background — runs alongside git pull on startup."""
    def _run():
        try:
            subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '--upgrade', 'yt-dlp',
                 '--break-system-packages', '-q'],
                check=True, capture_output=True
            )
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()

# ─── DOWNLOAD BACKENDS ────────────────────────────────────────
def download_with_aria2c(url, folder, filename, summary,
                         bandwidth_limit=0, current_process=None, retries=3, stop_flag=None):
    import shutil
    has_aria2c = shutil.which('aria2c') is not None
    if not has_aria2c:
        if not _install_aria2c():
            safe_print("[!] aria2c unavailable — falling back to requests")
            return download_with_requests(url, folder, filename, summary, stop_flag=stop_flag)
        has_aria2c = True

    os.makedirs(folder, exist_ok=True)
    safe_fname    = re.sub(r'[^\w]', '_', filename)[:30]
    session_file  = os.path.join(folder, f'.aria2_{safe_fname}.txt')
    filepath      = os.path.join(folder, filename)
    referer       = get_referer_for_url(url)

    safe_print(f"  [↓] Downloading: {filename}")

    for attempt in range(retries):
        try:
            cmd = [
                'aria2c',
                '-c',
                '--max-tries=0',
                '--retry-wait=30',
                '--timeout=120',
                '--connect-timeout=60',
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
                '-d', folder,
                '-o', filename,
            ]
            if bandwidth_limit > 0:
                cmd += ['--max-download-limit', f'{bandwidth_limit}K']
            cmd.append(url)

            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
            if current_process is not None:
                current_process[0] = proc
            proc.wait()
            code = proc.returncode
            if current_process is not None:
                current_process[0] = None

            if code == 0:
                if os.path.exists(filepath):
                    size = os.path.getsize(filepath)
                    if size < 100 * 1024:
                        safe_print(f"  [✗] File too small ({size/1024:.0f}KB) — likely error page")
                        try:
                            os.remove(filepath)
                        except Exception:
                            pass
                        if attempt < retries - 1:
                            safe_print(f"  [*] Retrying ({attempt+2}/{retries})...")
                            time.sleep(5)
                            continue
                        summary.add_failed(filename)
                        return False
                    size_mb = size / (1024 * 1024)
                    safe_print(f"  [✓] Done: {filename} ({size_mb:.1f}MB)")
                    try:
                        if os.path.exists(session_file):
                            os.remove(session_file)
                    except Exception:
                        pass
                    summary.add_success()
                    log_download(filename, url, filepath)
                    return True
                else:
                    safe_print(f"  [✗] File not found after download")
                    if attempt < retries - 1:
                        safe_print(f"  [*] Retrying ({attempt+2}/{retries})...")
                        time.sleep(5)
                        continue
                    summary.add_failed(filename)
                    return False
            else:
                safe_print(f"  [✗] aria2c failed (code {code})")
                if attempt < retries - 1:
                    safe_print(f"  [*] Retrying ({attempt+2}/{retries})...")
                    time.sleep(5)
                    continue
                summary.add_failed(filename)
                return False
        except Exception as e:
            safe_print(f"  [!] aria2c error: {e}")
            summary.add_failed(filename)
            return False
    return False

def download_with_requests(url, folder, filename, summary, stop_flag=None):
    import requests
    filepath = os.path.join(folder, filename)
    os.makedirs(folder, exist_ok=True)
    try:
        s = make_session()
        r = s.get(url, stream=True, timeout=30,
                  headers={**dict(s.headers), 'Referer': get_referer_for_url(url)})
        if r.status_code != 200:
            safe_print(f"  [!] HTTP {r.status_code}")
            summary.add_failed(filename)
            return False
        if 'text/html' in r.headers.get('content-type', ''):
            safe_print(f"  [!] Got HTML instead of video")
            summary.add_failed(filename)
            return False
        total      = int(r.headers.get('content-length', 0))
        downloaded = 0
        start      = time.time()
        with open(filepath, 'wb') as f:
            for chunk in r.iter_content(chunk_size=512 * 1024):
                if stop_flag and stop_flag[0]:
                    safe_print(f"\n  [!] Stopped")
                    break
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct   = downloaded * 100 // total
                        mb_d  = downloaded / (1024 * 1024)
                        mb_t  = total / (1024 * 1024)
                        ela   = time.time() - start
                        spd   = (downloaded / ela / 1024 / 1024) if ela > 0 else 0
                        eta   = int((total - downloaded) / (downloaded / ela)) if downloaded > 0 else 0
                        safe_print(f"\r  [↓] {pct}% — {mb_d:.1f}/{mb_t:.1f}MB — {spd:.1f}MB/s — ETA {eta}s",
                                   end='', flush=True)
        safe_print()
        if not os.path.exists(filepath) or os.path.getsize(filepath) < 100 * 1024:
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except Exception:
                pass
            safe_print(f"  [!] File too small — likely failed")
            summary.add_failed(filename)
            return False
        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        safe_print(f"  [✓] Done: {filename} ({size_mb:.1f}MB)")
        summary.add_success()
        log_download(filename, url, filepath)
        return True
    except Exception as e:
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass
        safe_print(f"  [!] requests error: {e}")
        summary.add_failed(filename)
        return False

def download_with_ytdlp(url, folder, filename, summary,
                        quality=None, current_process=None):
    import shutil
    has_ytdlp  = shutil.which('yt-dlp') is not None
    has_ffmpeg = shutil.which('ffmpeg') is not None
    has_aria2c = shutil.which('aria2c') is not None

    if not has_ytdlp:
        if not _install_ytdlp():
            safe_print(f"  [!] yt-dlp unavailable")
            summary.add_failed(filename)
            return False
    if not has_ffmpeg:
        safe_print(f"  [!] ffmpeg not found — install with: pkg install ffmpeg")
        summary.add_failed(filename)
        return False

    os.makedirs(folder, exist_ok=True)
    base         = re.sub(r'\.(mp4|mkv|m3u8)$', '', filename)
    out_template = os.path.join(folder, base + '.%(ext)s')
    quality_str  = quality or 'bestvideo[height<=480]+bestaudio/best[height<=480]'

    safe_print(f"  [↓] yt-dlp: {filename}")
    try:
        cmd = [
            'yt-dlp',
            '-f', quality_str,
            '--merge-output-format', 'mp4',
            '-o', out_template,
            '--no-playlist',
            '--retries', 'infinite',
            '--fragment-retries', 'infinite',
            '--retry-sleep', '10',
            '--quiet', '--no-warnings', '--progress', '--newline',
        ]
        if has_aria2c:
            cmd += [
                '--external-downloader', 'aria2c',
                '--external-downloader-args',
                'aria2c:-x 16 -s 16 -c --max-tries=0 --retry-wait=30 --timeout=120 '
                '--connect-timeout=60 --file-allocation=none --min-split-size=1M'
            ]
        cmd.append(url)
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        if current_process is not None:
            current_process[0] = proc
        proc.wait()
        code = proc.returncode
        if current_process is not None:
            current_process[0] = None

        if code == 0:
            for ext in ['mp4', 'mkv', 'webm']:
                p = os.path.join(folder, f"{base}.{ext}")
                if os.path.exists(p):
                    size_mb = os.path.getsize(p) / (1024 * 1024)
                    safe_print(f"  [✓] Done: {filename} ({size_mb:.1f}MB)")
                    summary.add_success()
                    log_download(filename, url, p)
                    return True
            safe_print(f"  [✓] Done: {filename}")
            summary.add_success()
            return True
        else:
            safe_print(f"  [✗] yt-dlp failed")
            summary.add_failed(filename)
            return False
    except Exception as e:
        safe_print(f"  [!] yt-dlp error: {e}")
        summary.add_failed(filename)
        return False

def download_social_ytdlp(url, folder, filename, summary, current_process=None,
                           quality_override=None, out_template=None):
    import shutil
    has_ytdlp  = shutil.which('yt-dlp') is not None
    has_aria2c = shutil.which('aria2c') is not None

    if not has_ytdlp:
        if not _install_ytdlp():
            safe_print(f"  [!] yt-dlp unavailable")
            summary.add_failed(filename)
            return False

    os.makedirs(folder, exist_ok=True)
    base = re.sub(r'\.(mp4|mkv|m3u8)$', '', filename)
    if not out_template:
        out_template = os.path.join(folder, base + '.%(ext)s')

    # If caller already picked a quality, use it directly with a best fallback.
    # Otherwise use the default auto chain.
    if quality_override:
        format_chain = [quality_override, 'bestvideo+bestaudio/best', 'best']
        safe_print(f"  [↓] yt-dlp: {filename}")
    else:
        format_chain = [
            'bestvideo[height<=720]+bestaudio/best[height<=720]',
            'bestvideo[height<=480]+bestaudio/best[height<=480]',
            'bestvideo[height<=360]+bestaudio/best[height<=360]',
            'bestvideo+bestaudio/best',
            'best',
        ]
        safe_print(f"  [↓] yt-dlp (720p auto): {filename}")

    for fmt in format_chain:
        try:
            cmd = [
                'yt-dlp', '-f', fmt,
                '--merge-output-format', 'mp4',
                '-o', out_template,
                '--no-playlist',
                '--retries', '3', '--fragment-retries', '3',
                '--quiet', '--no-warnings', '--progress', '--newline',
            ]
            if has_aria2c:
                cmd += [
                    '--external-downloader', 'aria2c',
                    '--external-downloader-args',
                    'aria2c:-x 16 -s 16 -c --max-tries=0 --retry-wait=30 '
                    '--timeout=120 --connect-timeout=60 --file-allocation=none --min-split-size=1M'
                ]
            cmd.append(url)
            result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            if result.returncode == 0:
                for ext in ['mp4', 'mkv', 'webm']:
                    p = os.path.join(folder, f"{base}.{ext}")
                    if os.path.exists(p):
                        size_mb = os.path.getsize(p) / (1024 * 1024)
                        safe_print(f"  [✓] Done: {filename} ({size_mb:.1f}MB)")
                        summary.add_success()
                        log_download(filename, url, p)
                        return True
                safe_print(f"  [✓] Done: {filename}")
                summary.add_success()
                return True
            err = result.stderr.lower()
            if 'requested format not available' in err or 'format' in err:
                continue
            safe_print(f"  [✗] yt-dlp failed: {result.stderr[:100]}")
            break
        except Exception as e:
            safe_print(f"  [!] yt-dlp error: {e}")
            break

    summary.add_failed(filename)
    return False

# ─── SMART DOWNLOAD FILE ──────────────────────────────────────
def download_file(url, folder, filename, summary,
                  check_expiry=True, series_url=None, series_name=None,
                  bandwidth_limit=0, quality=None,
                  current_process=None, stop_flag=None, paused_flag=None,
                  wait_fn=None):
    """
    Smart downloader — handles resume state, expiry check, disk space,
    and routes to the right backend.

    stop_flag:   list([False]) — set to True to abort
    paused_flag: list([False]) — set to True to pause
    wait_fn:     callable — blocks until unpaused
    """
    # Disk space check before every episode
    if not assert_disk_space():
        summary.add_failed(filename)
        return False

    done, _ = already_downloaded(folder, filename)
    if done:
        safe_print(f"  [✓] Already downloaded — skipping")
        summary.add_skipped()
        if series_url:
            mark_episode_done(series_url, series_name or folder, filename)
        return True

    if series_url and is_episode_done_in_state(series_url, filename):
        safe_print(f"  [✓] Done in previous session — skipping")
        summary.add_skipped()
        return True

    # Link expiry detection — re-extracts fresh link upstream if expired
    if check_expiry and not is_streaming_link(url):
        _s = make_session()
        status = check_url_alive(url, _s)
        if status == 'expired':
            safe_print(f"  [!] Link expired (403/404) — re-paste the series URL for fresh links")
            summary.add_failed(filename)
            return False

    # Pause/stop check
    if wait_fn:
        wait_fn()
    if stop_flag and stop_flag[0]:
        return False

    if series_url:
        mark_episode_current(series_url, series_name or folder, filename)

    if is_streaming_link(url):
        result = download_with_ytdlp(url, folder, filename, summary,
                                     quality=quality, current_process=current_process)
    else:
        result = download_with_aria2c(url, folder, filename, summary,
                                      bandwidth_limit=bandwidth_limit,
                                      current_process=current_process,
                                      stop_flag=stop_flag)

    if result and series_url:
        mark_episode_done(series_url, series_name or folder, filename)

    return result

# ─── PREFETCHER ───────────────────────────────────────────────
class Prefetcher:
    """Pre-fetches next episode link while current one downloads."""
    def __init__(self, fetch_fn):
        self.fetch_fn = fetch_fn
        self._result  = None
        self._thread  = None
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
        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def get(self, timeout=30):
        self._ready.wait(timeout=timeout)
        return self._result

# ─── BATCH DOWNLOADER ─────────────────────────────────────────
def download_batch(items, folder, summary, parallel=1,
                   series_url=None, series_name=None,
                   bandwidth_limit=0, quality=None,
                   current_process=None, stop_flag=None, wait_fn=None):
    if not items:
        return
    if parallel == 1:
        for url, filename in items:
            if stop_flag and stop_flag[0]:
                break
            download_file(url, folder, filename, summary,
                          series_url=series_url, series_name=series_name,
                          bandwidth_limit=bandwidth_limit, quality=quality,
                          current_process=current_process,
                          stop_flag=stop_flag, wait_fn=wait_fn)
    else:
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(
                    download_file, url, folder, filename, summary,
                    True, series_url, series_name,
                    bandwidth_limit, quality, current_process, stop_flag, None, wait_fn
                ): filename
                for url, filename in items
            }
            for future in as_completed(futures):
                fname = futures[future]
                try:
                    future.result()
                except Exception as e:
                    safe_print(f"  [!] Thread error for {fname}: {e}")
                    summary.add_failed(fname)
