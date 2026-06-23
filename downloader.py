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
import tempfile
import hashlib
from queue import Queue
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── SHARED STATE (imported from main) ────────────────────────
# These are set by main.py and read here. Imported at call time
# to avoid circular imports.

# ─── CONSTANTS ────────────────────────────────────────────────
IS_ANDROID   = os.path.exists('/storage/emulated/0')
BASE_DIR     = '/storage/emulated/0/Anon' if IS_ANDROID else os.path.join(os.path.expanduser('~'), 'Downloads', 'Anon')
LOG_FILE     = os.path.join(BASE_DIR, '.download_history.json')
RESUME_FILE  = os.path.join(BASE_DIR, '.resume_state.json')
RECEIPT_FILE = os.path.join(BASE_DIR, '.download_receipts.json')
DIAG_LOG     = os.path.join(BASE_DIR, '.diag.log')
PROGRESS_LOG = os.path.join(BASE_DIR, '.download.log')  # NEW: Download progress log

UA_DESKTOP   = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

PRINT_LOCK   = threading.Lock()
STATE_LOCK   = threading.Lock()

def safe_print(*args, **kwargs):
    with PRINT_LOCK:
        print(*args, **kwargs)

# ─── ATOMIC FILE WRITES (Safe state persistence) ─────────────────
def _atomic_write_json(filepath, data):
    """Write JSON atomically: temp file → rename."""
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(filepath), prefix='.tmp_')
        try:
            with os.fdopen(temp_fd, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(temp_path, filepath)  # Atomic rename
            return True
        except Exception:
            try:
                os.unlink(temp_path)
            except Exception:
                pass
            raise
    except Exception as e:
        safe_print(f"  [!] Failed to write {filepath}: {e}")
        return False

def _atomic_read_json(filepath, default=None):
    """Read JSON safely, return default if corrupted."""
    try:
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError):
        safe_print(f"  [!] Corrupted JSON: {filepath}, resetting")
    return default or {}

# ─── DOWNLOAD RECEIPT SYSTEM (Single source of truth) ─────────────
class DownloadReceipt:
    """Track per-episode download state precisely."""
    
    @staticmethod
    def load_all():
        """Load all receipts."""
        return _atomic_read_json(RECEIPT_FILE, {})
    
    @staticmethod
    def save_all(receipts):
        """Save all receipts atomically."""
        with STATE_LOCK:
            _atomic_write_json(RECEIPT_FILE, receipts)
    
    @staticmethod
    def get_receipt(episode_url):
        """Get receipt for one episode, return status."""
        receipts = DownloadReceipt.load_all()
        return receipts.get(episode_url, {})
    
    @staticmethod
    def mark_in_progress(episode_url, filename, expected_size=0):
        """Mark episode as being downloaded."""
        with STATE_LOCK:
            receipts = DownloadReceipt.load_all()
            receipts[episode_url] = {
                'status': 'in-progress',
                'filename': filename,
                'expected_size': expected_size,
                'timestamp': time.time()
            }
            DownloadReceipt.save_all(receipts)
    
    @staticmethod
    def mark_complete(episode_url, filepath, actual_size):
        """Mark episode as fully downloaded."""
        with STATE_LOCK:
            receipts = DownloadReceipt.load_all()
            receipts[episode_url] = {
                'status': 'done',
                'filepath': filepath,
                'filename': os.path.basename(filepath),
                'expected_size': 0,
                'actual_size': actual_size,
                'timestamp': time.time()
            }
            DownloadReceipt.save_all(receipts)
    
    @staticmethod
    def mark_failed(episode_url):
        """Mark episode as failed."""
        with STATE_LOCK:
            receipts = DownloadReceipt.load_all()
            if episode_url in receipts:
                receipts[episode_url]['status'] = 'failed'
                DownloadReceipt.save_all(receipts)
    
    @staticmethod
    def mark_partial(episode_url, filepath, actual_size, expected_size):
        """Mark episode as partially downloaded."""
        with STATE_LOCK:
            receipts = DownloadReceipt.load_all()
            receipts[episode_url] = {
                'status': 'partial',
                'filepath': filepath,
                'filename': os.path.basename(filepath),
                'actual_size': actual_size,
                'expected_size': expected_size,
                'timestamp': time.time()
            }
            DownloadReceipt.save_all(receipts)
    
    @staticmethod
    def is_complete(episode_url):
        """Check if episode is fully downloaded."""
        receipt = DownloadReceipt.get_receipt(episode_url)
        return receipt.get('status') == 'done'

    @staticmethod
    def mark_paused(episode_url, filepath, progress_bytes, expected_size):
        """Mark episode as paused (for resumable downloads)."""
        with STATE_LOCK:
            receipts = DownloadReceipt.load_all()
            receipts[episode_url] = {
                'status': 'paused',
                'filepath': filepath,
                'filename': os.path.basename(filepath),
                'progress_bytes': progress_bytes,
                'expected_size': expected_size,
                'timestamp': time.time()
            }
            DownloadReceipt.save_all(receipts)
    
    @staticmethod
    def get_paused_download(episode_url):
        """Get paused download info (for resuming)."""
        receipt = DownloadReceipt.get_receipt(episode_url)
        if receipt.get('status') == 'paused':
            return {
                'filepath': receipt.get('filepath'),
                'progress_bytes': receipt.get('progress_bytes', 0),
                'expected_size': receipt.get('expected_size', 0)
            }
        return None


class LiveProgress:
    """
    Single-line \r progress display.
    parallel=True switches to static newline output to avoid garbling.
    """
    def __init__(self, filename, parallel=False):
        self._name     = filename[:50] if len(filename) > 50 else filename
        self._parallel = parallel
        self._started  = False
        self._done     = False

    def update(self, pct, spd_mbps=None, eta=None):
        if self._done:
            return
        self._started = True
        pct_s = f'{pct:5.1f}%'
        spd_s = f' — {spd_mbps:.1f} MB/s' if spd_mbps is not None else ''
        eta_s = f' — ETA {eta}'            if eta          else ''
        line  = f'  [↓] {self._name}  {pct_s}{spd_s}{eta_s}'
        try:
            if self._parallel:
                if int(pct) % 10 == 0:
                    sys.stdout.write(line + '\n')
                    sys.stdout.flush()
            else:
                sys.stdout.write('\r' + line + '   ')
                sys.stdout.flush()
        except Exception:
            pass

    def done(self, size_mb=None):
        if self._done:
            return
        self._done = True
        size_s = f' ({size_mb:.1f} MB)' if size_mb is not None else ''
        line   = f'  [✓] Done: {self._name}{size_s}'
        try:
            if self._parallel or not self._started:
                sys.stdout.write(line + '\n')
            else:
                sys.stdout.write('\r' + line + ' ' * 20 + '\n')
            sys.stdout.flush()
        except Exception:
            pass

    def fail(self):
        if self._done:
            return
        self._done = True
        line = f'  [✗] Failed: {self._name}'
        try:
            if self._parallel or not self._started:
                sys.stdout.write(line + '\n')
            else:
                sys.stdout.write('\r' + line + ' ' * 20 + '\n')
            sys.stdout.flush()
        except Exception:
            pass

# ─── DISK SPACE ───────────────────────────────────────────────
def get_free_space_gb():
    try:
        import shutil as _shutil
        path = BASE_DIR if (IS_ANDROID and os.path.exists(BASE_DIR)) else os.path.expanduser('~')
        usage = _shutil.disk_usage(path)
        return usage.free / (1024 ** 3)
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
        _atomic_write_json(LOG_FILE, history)
    except Exception:
        pass

def _media_scan(filepath):
    """Trigger Android media scanner on the file's folder so WhatsApp picks it up fast."""
    if not IS_ANDROID:
        return
    try:
        folder = os.path.dirname(filepath)
        subprocess.Popen(
            ['termux-media-scan', '-r', folder],
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
        safe_print("[*] No download history yet")
        return
    print(f"\n{'='*50}")
    print(f"  DOWNLOAD HISTORY")
    print(f"{'='*50}")
    for name, entries in list(history.items())[-20:]:
        print(f"\n  {name}  ({len(entries)} file(s))")
        for e in entries[-3:]:
            print(f"    ·  {e['time']}  —  {os.path.basename(e['file'])}")
    print(f"{'='*50}")

# ─── PROGRESS LOGGING ─────────────────────────────────────────
def log_progress(filename, url, status, size_mb=None, reason=None, speed_mbps=None, duration_sec=None, retries=0):
    """Log download progress to .download.log for history and debugging."""
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        log_entry = {
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'filename': filename,
            'url': url[:80],  # Truncate long URLs
            'status': status,  # 'success', 'failed', 'paused', 'resumed'
        }
        if size_mb is not None:
            log_entry['size_mb'] = round(size_mb, 1)
        if speed_mbps is not None:
            log_entry['speed_mbps'] = round(speed_mbps, 1)
        if duration_sec is not None:
            log_entry['duration_sec'] = duration_sec
        if reason is not None:
            log_entry['reason'] = reason
        if retries > 0:
            log_entry['retries'] = retries
        
        with open(PROGRESS_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry) + '\n')
    except Exception:
        pass

# ─── ERROR CATEGORIZATION ─────────────────────────────────────
def categorize_error(exception, status_code=None):
    """
    Categorize download/network errors.
    Returns: (category, should_retry, description)
    
    Categories:
      - 'transient': retry (timeout, connection reset, 5xx)
      - 'permanent': don't retry (404, 410, bad URL)
      - 'rate_limit': retry with backoff (429, 503)
      - 'auth': don't retry (401, 403 — unless verified)
      - 'unknown': retry cautiously
    """
    err_str = str(exception).lower()
    
    # Network timeouts — always transient
    if any(x in err_str for x in ['timeout', 'timed out', 'read timed out']):
        return ('transient', True, 'Network timeout — will retry')
    
    # Connection errors — transient
    if any(x in err_str for x in ['connection reset', 'connection refused', 'broken pipe', 'remote end closed']):
        return ('transient', True, 'Connection reset — will retry')
    
    # DNS failures — transient
    if any(x in err_str for x in ['getaddrinfo failed', 'name resolution', 'no address']):
        return ('transient', True, 'DNS failure — will retry')
    
    # HTTP status codes
    if status_code:
        if status_code == 404 or status_code == 410:
            return ('permanent', False, f'Link expired ({status_code}) — skip')
        if status_code == 401:
            return ('auth', False, '401 Unauthorized — auth required')
        if status_code == 403:
            return ('auth', False, '403 Forbidden — may need verification')
        if status_code == 429:
            return ('rate_limit', True, '429 Too Many Requests — back off and retry')
        if status_code >= 500 and status_code < 600:
            return ('transient', True, f'{status_code} Server error — will retry')
        if status_code >= 400 and status_code < 500:
            return ('permanent', False, f'{status_code} Client error — skip')
        if status_code == 200:
            return ('success', False, 'OK')
    
    # Generic fallback
    return ('unknown', True, 'Unknown error — will retry')


RESUME_LOCK = threading.Lock()

def _load_resume_state_unlocked():
    return _atomic_read_json(RESUME_FILE, {})

def _save_resume_state_unlocked(state):
    _atomic_write_json(RESUME_FILE, state)

def load_resume_state():
    with RESUME_LOCK:
        return _load_resume_state_unlocked()

def save_resume_state(state):
    with RESUME_LOCK:
        _save_resume_state_unlocked(state)

def mark_episode_done(series_url, series_name, ep_filename):
    with RESUME_LOCK:
        state = _load_resume_state_unlocked()
        key = series_url
        if key not in state:
            state[key] = {'name': series_name, 'done': [], 'failed': [], 'current': None}
        if ep_filename not in state[key]['done']:
            state[key]['done'].append(ep_filename)
        state[key]['current'] = None
        _save_resume_state_unlocked(state)

def mark_episode_current(series_url, series_name, ep_filename):
    with RESUME_LOCK:
        state = _load_resume_state_unlocked()
        key = series_url
        if key not in state:
            state[key] = {'name': series_name, 'done': [], 'failed': [], 'current': None}
        state[key]['current'] = ep_filename
        state[key]['name'] = series_name
        _save_resume_state_unlocked(state)

def mark_series_complete(series_url):
    with RESUME_LOCK:
        state = _load_resume_state_unlocked()
        if series_url in state:
            del state[series_url]
            _save_resume_state_unlocked(state)

def is_episode_done_in_state(series_url, ep_filename):
    with RESUME_LOCK:
        state = _load_resume_state_unlocked()
        if series_url in state:
            return ep_filename in state[series_url].get('done', [])
        return False

def save_episode_size(series_url, ep_filename, expected_bytes):
    """Store expected file size in resume state before download starts."""
    with RESUME_LOCK:
        try:
            state = _load_resume_state_unlocked()
            if series_url not in state:
                state[series_url] = {'name': '', 'done': [], 'failed': [], 'current': None, 'sizes': {}}
            if 'sizes' not in state[series_url]:
                state[series_url]['sizes'] = {}
            if ep_filename not in state[series_url]['sizes']:
                state[series_url]['sizes'][ep_filename] = expected_bytes
                _save_resume_state_unlocked(state)
        except Exception:
            pass

def get_episode_size(series_url, ep_filename):
    """Retrieve stored expected size. Returns None if not stored."""
    with RESUME_LOCK:
        try:
            state = _load_resume_state_unlocked()
            return state.get(series_url, {}).get('sizes', {}).get(ep_filename)
        except Exception:
            return None

def show_resume_list():
    state = load_resume_state()
    if not state:
        safe_print("[*] No paused downloads found")
        return False
    print(f"\n{'='*50}")
    print(f"  PAUSED DOWNLOADS")
    print(f"{'='*50}")
    for i, (url, inf) in enumerate(state.items(), 1):
        name    = inf.get('name', 'Unknown')
        done    = len(inf.get('done', []))
        current = inf.get('current', None)
        status  = f'paused at: {current}' if current else f'{done} episode(s) done'
        print(f"  [{i}] {name}")
        print(f"       {status}")
        print(f"       {url[:60]}")
    print(f"{'='*50}")
    return True

# ─── DOWNLOAD SUMMARY ─────────────────────────────────────────
class DownloadSummary:
    def __init__(self):
        self._lock       = threading.Lock()
        self.success     = 0
        self.skipped     = 0
        self.failed      = 0
        self.failed_list = []
        self.start_time  = time.time()

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

    def report(self, name=''):
        total = self.success + self.skipped + self.failed
        if total == 0:
            return []
        elapsed = time.time() - self.start_time
        mins    = int(elapsed // 60)
        secs    = int(elapsed % 60)
        t_s     = f'{mins}m {secs}s' if mins else f'{secs}s'
        print(f"\n{'='*50}")
        print(f"  {name or 'DOWNLOAD'}")
        print(f"{'='*50}")
        print(f"  Done: {self.success}   Skipped: {self.skipped}   Failed: {self.failed}   ({t_s})")
        if self.failed_list:
            print(f"  Failed:")
            for f in self.failed_list:
                print(f"    · {f}")
        print(f"{'='*50}")
        if IS_ANDROID:
            if self.failed == 0:
                msg = f'{name} — {self.success}/{total} done ✓'
            else:
                msg = f'{name} — {self.success} done, {self.failed} failed'
            _notify('Anonrode — Complete', msg)
        return list(self.failed_list)

    def prompt_retry(self):
        """Ask user if they want to retry failed episodes. Returns True if yes."""
        if not self.failed_list:
            return False
        try:
            ans = input(f"\n  Retry {len(self.failed_list)} failed episode(s)? [y/N]: ").strip().lower()
            return ans in ('y', 'yes')
        except (EOFError, KeyboardInterrupt):
            return False

# ─── NOTIFICATION ─────────────────────────────────────────────
def _notify(title, message, vibrate=True):
    if not IS_ANDROID:
        return
    try:
        cmd = [
            'termux-notification',
            '--title', title,
            '--content', message,
            '--id', '42',          # fixed ID so notifications replace each other
            '--priority', 'high',
        ]
        if vibrate:
            cmd += ['--vibrate', '500']
        subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        pass

def _notify_start(name, count):
    """Notify when a batch download starts."""
    if count > 1:
        _notify('Anonrode — Downloading', f'{name} ({count} episodes)', vibrate=False)
    else:
        _notify('Anonrode — Downloading', name, vibrate=False)

# ─── HELPERS ──────────────────────────────────────────────────
def fetch_expected_size(url, session=None):
    """
    Get Content-Length from server via HEAD request.
    Returns size in bytes or None if unavailable.
    """
    try:
        import requests as _req
        s = _req.Session()
        s.headers['User-Agent'] = UA_DESKTOP
        r = s.head(url, timeout=10, allow_redirects=True)
        cl = r.headers.get('Content-Length') or r.headers.get('content-length')
        if cl and str(cl).isdigit():
            return int(cl)
    except Exception:
        pass
    return None

def already_downloaded(folder, filename, min_mb=1.0, series_url=None):
    """
    Check if file is complete using receipt system (primary) + filesystem check (fallback).
    Returns: (is_complete, filepath)
    
    Priority:
    1. If series_url: check receipt for "paused" status FIRST (don't delete paused files!)
    2. If series_url: check receipt for "done" status
    3. Else: check filesystem for file existence and size
    """
    # Per-episode receipt key: series_url + filename avoids all episodes sharing one slot
    ep_key = f"{series_url}:{filename}" if series_url else None

    # CRITICAL: Check receipt for "paused" FIRST (before any file checks)
    if ep_key:
        receipt = DownloadReceipt.get_receipt(ep_key)
        
        # If paused: return paused info WITHOUT deleting the file
        if receipt.get('status') == 'paused':
            filepath = receipt.get('filepath')
            if filepath and os.path.exists(filepath):
                actual = os.path.getsize(filepath)
                expected = receipt.get('expected_size', 0)
                safe_print(f"  [*] Paused: {actual/(1024*1024):.1f}MB of {expected/(1024*1024):.1f}MB")
                return False, None  # Not complete, but don't delete!
            # File missing but receipt says paused - clear it
            DownloadReceipt.mark_failed(ep_key)
        
        # If done: return complete
        if receipt.get('status') == 'done':
            path = receipt.get('filepath')
            if path and os.path.exists(path):
                safe_print(f"  [✓] Already downloaded (receipt verified)")
                return True, path
            # Receipt says done but file missing — clean up receipt and re-download
            DownloadReceipt.mark_failed(ep_key)
    
    # Fallback: filesystem check (for files without receipt records)
    base = re.sub(r'\.(mp4|mkv|m3u8|webm)$', '', filename)
    for ext in ['mp4', 'mkv', 'webm']:
        filepath = os.path.join(folder, f"{base}.{ext}")
        if os.path.exists(filepath):
            actual = os.path.getsize(filepath)
            expected = get_episode_size(series_url, filename) if series_url else None
            
            if expected:
                # Verify against expected size (allow 1% tolerance)
                if actual >= expected * 0.99:
                    safe_print(f"  [✓] Found existing file ({actual/(1024*1024):.1f}MB)")
                    return True, filepath
                else:
                    # Incomplete file — only delete if genuinely broken, NOT paused
                    # Check again if receipt says paused (shouldn't reach here but be safe)
                    if ep_key:
                        receipt = DownloadReceipt.get_receipt(ep_key)
                        if receipt.get('status') == 'paused':
                            safe_print(f"  [*] Resuming: {actual/(1024*1024):.1f}MB of {expected/(1024*1024):.1f}MB")
                            return False, None  # Don't delete!
                    
                    safe_print(f"  [!] Incomplete: {actual/(1024*1024):.1f}MB of {expected/(1024*1024):.1f}MB — re-downloading")
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
                    if ep_key:
                        DownloadReceipt.mark_partial(ep_key, filepath, actual, expected)
                    return False, None
            else:
                # No expected size: use file size heuristic
                # If file is large enough, consider it complete and skip
                min_bytes = max(5 * 1024 * 1024, min_mb * 1024 * 1024)
                if actual >= min_bytes:
                    safe_print(f"  [✓] Found existing file ({actual/(1024*1024):.1f}MB)")
                    return True, filepath
                else:
                    # File too small - likely corrupted, safe to delete
                    safe_print(f"  [!] File too small ({actual/(1024*1024):.1f}MB) — likely incomplete or corrupted")
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
    """
    Returns 'ok', 'expired', or 'unknown'.
    Uses a ranged GET (bytes=0-0) instead of HEAD — many CDNs return 403
    to HEAD requests even for valid files, but serve correctly on GET.
    404/410 are definitive expiry signals; 403 is ambiguous, so we treat
    it as 'unknown' and let the download attempt proceed.
    """
    try:
        r = session.get(url, timeout=10, allow_redirects=True,
                        headers={'Range': 'bytes=0-0'})
        if r.status_code in (404, 410):
            return 'expired'
        if r.status_code in (200, 206):
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
    """Silent yt-dlp update — called in a daemon thread from auto_update()."""
    try:
        subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '--upgrade', 'yt-dlp',
             '--break-system-packages', '-q'],
            check=True, capture_output=True
        )
    except Exception:
        pass

# ─── DOWNLOAD BACKENDS ────────────────────────────────────────
def download_with_aria2c(url, folder, filename, summary,
                         bandwidth_limit=0, current_process=None,
                         retries=3, stop_flag=None, parallel_mode=False,
                         series_url=None, config=None):
    """
    Smart downloader with resumable downloads support.
    
    If a partial file exists:
      - Check receipt system
      - Use aria2c's --continue flag to resume from byte offset
      - Much faster than starting over
    """
    import shutil
    config = config or {}  # Config dict with resolver_timeout, etc.
    
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
    
    # Check if this is a resumed download (for user-facing print only)
    if series_url:
        ep_key = f"{series_url}:{filename}"
        paused_info = DownloadReceipt.get_paused_download(ep_key)
        if paused_info and os.path.exists(paused_info['filepath']):
            current_size = os.path.getsize(paused_info['filepath'])
            expected_size = paused_info.get('expected_size', 0)
            if expected_size > 0 and current_size < expected_size:
                safe_print(f"  [*] Resuming from {current_size/(1024*1024):.1f}MB of {expected_size/(1024*1024):.1f}MB")

    def _cleanup_session_file(sf):
        try:
            if os.path.exists(sf):
                os.remove(sf)
        except Exception:
            pass

    progress = LiveProgress(filename, parallel=parallel_mode)
    start_time = time.time()

    for attempt in range(retries):
        try:
            cmd = [
                'aria2c',
                '-c',  # Continue/resume support (key for resumable downloads!)
                '--max-tries=0',
                '--retry-wait=30',
                '--timeout=' + str(config.get('download_timeout', 120)),
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
                '--check-certificate=false',
                '-d', folder,
                '-o', filename,
            ]
            if bandwidth_limit > 0:
                cmd += ['--max-download-limit', f'{bandwidth_limit}K']
            cmd.append(url)

            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
            if current_process is not None:
                current_process[0] = proc

            # Poll instead of blocking wait — allows stop_flag to interrupt
            stopped = False
            while proc.poll() is None:
                if stop_flag and stop_flag[0]:
                    proc.terminate()
                    stopped = True
                    break
                time.sleep(0.5)
            code = proc.returncode if proc.returncode is not None else -1
            if current_process is not None:
                current_process[0] = None

            if stopped:
                progress.fail()
                _cleanup_session_file(session_file)
                summary.add_failed(filename)
                return False

            if code == 0:
                if os.path.exists(filepath):
                    size = os.path.getsize(filepath)
                    if size < 100 * 1024:
                        progress.fail()
                        safe_print(f"[✗] file too small ({size/1024:.0f}KB) — likely error page")
                        try:
                            os.remove(filepath)
                        except Exception:
                            pass
                        if attempt < retries - 1:
                            safe_print(f"[*] retrying ({attempt+2}/{retries})...")
                            time.sleep(5)
                            continue
                        _cleanup_session_file(session_file)
                        summary.add_failed(filename)
                        return False
                    size_mb = size / (1024 * 1024)
                    progress.done(size_mb)
                    _cleanup_session_file(session_file)
                    summary.add_success()
                    log_download(filename, url, filepath)
                    return True
                else:
                    progress.fail()
                    safe_print("[✗] file not found after download")
                    if attempt < retries - 1:
                        safe_print(f"[*] retrying ({attempt+2}/{retries})...")
                        time.sleep(5)
                        continue
                    _cleanup_session_file(session_file)
                    summary.add_failed(filename)
                    return False
            else:
                progress.fail()
                safe_print(f"[✗] aria2c failed (code {code})")
                if attempt < retries - 1:
                    safe_print(f"[*] retrying ({attempt+2}/{retries})...")
                    time.sleep(5)
                    continue
                _cleanup_session_file(session_file)
                summary.add_failed(filename)
                return False
        except Exception as e:
            progress.fail()
            _cleanup_session_file(session_file)
            safe_print(f"[✗] aria2c error: {e}")
            summary.add_failed(filename)
            return False
    _cleanup_session_file(session_file)
    return False

def download_with_requests(url, folder, filename, summary, stop_flag=None, parallel_mode=False):
    import requests
    filepath = os.path.join(folder, filename)
    os.makedirs(folder, exist_ok=True)
    safe_print(f"  [↓] Downloading: {filename}")
    progress = LiveProgress(filename, parallel=parallel_mode)
    try:
        s = make_session()
        r = s.get(url, stream=True, timeout=30,
                  headers={**dict(s.headers), 'Referer': get_referer_for_url(url)})
        if r.status_code != 200:
            progress.fail()
            safe_print(f"[!] HTTP {r.status_code}")
            summary.add_failed(filename)
            return False
        if 'text/html' in r.headers.get('content-type', ''):
            progress.fail()
            safe_print("[!] got HTML instead of video")
            summary.add_failed(filename)
            return False
        total      = int(r.headers.get('content-length', 0))
        downloaded = 0
        start      = time.time()
        with open(filepath, 'wb') as f:
            for chunk in r.iter_content(chunk_size=512 * 1024):
                if stop_flag and stop_flag[0]:
                    progress.fail()
                    safe_print("[!] stopped")
                    try:
                        if os.path.exists(filepath):
                            os.remove(filepath)
                    except Exception:
                        pass
                    summary.add_failed(filename)
                    return False
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded * 100 / total
                        ela = time.time() - start
                        spd = (downloaded / ela / 1024 / 1024) if ela > 0 else 0
                        eta_s = int((total - downloaded) / (downloaded / ela)) if downloaded > 0 else 0
                        eta = f'{eta_s // 60}:{eta_s % 60:02d}'
                        progress.update(pct, spd, eta)
        if not os.path.exists(filepath) or os.path.getsize(filepath) < 100 * 1024:
            progress.fail()
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except Exception:
                pass
            safe_print("[!] file too small — likely failed")
            summary.add_failed(filename)
            return False
        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        progress.done(size_mb)
        summary.add_success()
        log_download(filename, url, filepath)
        return True
    except Exception as e:
        progress.fail()
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass
        safe_print(f"[✗] requests error: {e}")
        summary.add_failed(filename)
        return False

def download_with_ytdlp(url, folder, filename, summary,
                        quality=None, current_process=None, stop_flag=None, parallel_mode=False):
    import shutil
    has_ytdlp  = shutil.which('yt-dlp') is not None
    has_ffmpeg = shutil.which('ffmpeg') is not None
    has_aria2c = shutil.which('aria2c') is not None

    if not has_ytdlp:
        if not _install_ytdlp():
            safe_print("[!] yt-dlp unavailable")
            summary.add_failed(filename)
            return False
    if not has_ffmpeg:
        safe_print("[!] ffmpeg not found — install with: pkg install ffmpeg")
        summary.add_failed(filename)
        return False

    os.makedirs(folder, exist_ok=True)
    base        = re.sub(r'\.(mp4|mkv|m3u8)$', '', filename)
    out_template = os.path.join(folder, base + '.%(ext)s')
    quality_str  = quality or 'bestvideo[height<=480]+bestaudio/best[height<=480]'

    progress = LiveProgress(filename, parallel=parallel_mode)
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
            '--no-warnings', '--progress', '--newline',
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

        while proc.poll() is None:
            if stop_flag and stop_flag[0]:
                proc.terminate()
                break
            time.sleep(0.5)
        code = proc.returncode if proc.returncode is not None else -1
        if current_process is not None:
            current_process[0] = None

        if code == 0:
            for ext in ['mp4', 'mkv', 'webm']:
                p = os.path.join(folder, f"{base}.{ext}")
                if os.path.exists(p):
                    size_mb = os.path.getsize(p) / (1024 * 1024)
                    progress.done(size_mb)
                    summary.add_success()
                    log_download(filename, url, p)
                    return True
            progress.done()
            summary.add_success()
            return True
        else:
            progress.fail()
            safe_print("[✗] yt-dlp failed")
            summary.add_failed(filename)
            return False
    except Exception as e:
        progress.fail()
        safe_print(f"[✗] yt-dlp error: {e}")
        summary.add_failed(filename)
        return False

def download_social_ytdlp(url, folder, filename, summary, current_process=None,
                           quality_override=None, out_template=None, stop_flag=None):
    import shutil
    has_ytdlp  = shutil.which('yt-dlp') is not None
    has_aria2c = shutil.which('aria2c') is not None

    if not has_ytdlp:
        if not _install_ytdlp():
            safe_print("[!] yt-dlp unavailable")
            summary.add_failed(filename)
            return False

    os.makedirs(folder, exist_ok=True)
    base = re.sub(r'\.(mp4|mkv|m3u8)$', '', filename)
    if not out_template:
        out_template = os.path.join(folder, base + '.%(ext)s')

    # Quality chain: caller-specified → 720p → 480p → 360p → 1080p → best
    if quality_override:
        format_chain = [quality_override, 'bestvideo+bestaudio/best', 'best']
    else:
        format_chain = [
            'bestvideo[height<=720]+bestaudio/best[height<=720]',
            'bestvideo[height<=480]+bestaudio/best[height<=480]',
            'bestvideo[height<=360]+bestaudio/best[height<=360]',
            'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
            'bestvideo+bestaudio/best',
            'best',
        ]

    progress = LiveProgress(filename)

    def _run_ytdlp(fmt):
        cmd = [
            'yt-dlp', '-f', fmt,
            '--merge-output-format', 'mp4',
            '-o', out_template,
            '--no-playlist',
            '--retries', '3', '--fragment-retries', '3',
            '--no-warnings', '--progress', '--newline',
        ]
        if has_aria2c:
            cmd += [
                '--external-downloader', 'aria2c',
                '--external-downloader-args',
                'aria2c:-x 16 -s 16 -c --max-tries=0 --retry-wait=30 '
                '--timeout=120 --connect-timeout=60 --file-allocation=none --min-split-size=1M'
            ]
        cmd.append(url)
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        if current_process is not None:
            current_process[0] = proc
        while proc.poll() is None:
            if stop_flag and stop_flag[0]:
                proc.terminate()
                break
            time.sleep(0.5)
        if current_process is not None:
            current_process[0] = None
        return proc.returncode if proc.returncode is not None else -1

    try:
        for fmt in format_chain:
            if stop_flag and stop_flag[0]:
                break
            code = _run_ytdlp(fmt)
            if stop_flag and stop_flag[0]:
                break
            if code == 0:
                for ext in ['mp4', 'mkv', 'webm', 'm4a']:
                    p = os.path.join(folder, f'{base}.{ext}')
                    if os.path.exists(p):
                        size_mb = os.path.getsize(p) / (1024 * 1024)
                        progress.done(size_mb)
                        summary.add_success()
                        log_download(filename, url, p)
                        return True
                progress.done()
                summary.add_success()
                return True
        # All formats failed
        progress.fail()
        safe_print("[✗] yt-dlp failed — no compatible format found")
        summary.add_failed(filename)
        return False
    except Exception as e:
        progress.fail()
        safe_print(f"[✗] yt-dlp error: {e}")
        summary.add_failed(filename)
        return False

# ─── SMART DOWNLOAD FILE ──────────────────────────────────────
def download_file(url, folder, filename, summary,
                  check_expiry=True, series_url=None, series_name=None,
                  bandwidth_limit=0, quality=None,
                  current_process=None, stop_flag=None, paused_flag=None,
                  wait_fn=None, parallel_mode=False):
    """
    Smart downloader — handles resume state, expiry check, disk space,
    and routes to the right backend.

    stop_flag:    list([False]) — set to True to abort
    paused_flag:  list([False]) — set to True to pause
    wait_fn:      callable — blocks until unpaused
    parallel_mode: True when running inside download_batch with parallel>1
                   — switches LiveProgress to static line mode to avoid
                   interleaved \r corruption
    """
    # Disk space check before every episode
    if not assert_disk_space():
        summary.add_failed(filename)
        return False

    done, _ = already_downloaded(folder, filename, series_url=series_url)
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

    # Link expiry detection
    if check_expiry and not is_streaming_link(url):
        _s = make_session()
        status = check_url_alive(url, _s)
        if status == 'expired':
            safe_print(f"  [!] Link expired (404) — re-paste the series URL for fresh links")
            summary.add_failed(filename)
            return False

    # Pause/stop check
    if wait_fn:
        wait_fn()
    if stop_flag and stop_flag[0]:
        return False

    if series_url:
        mark_episode_current(series_url, series_name or folder, filename)

    # Fetch and store expected file size before download starts
    # so resume checks can verify completeness precisely
    if series_url and not is_streaming_link(url):
        expected = get_episode_size(series_url, filename)
        if not expected:
            expected = fetch_expected_size(url)
            if expected:
                save_episode_size(series_url, filename, expected)
                safe_print(f"  [*] Expected size: {expected/(1024*1024):.1f} MB")

    if is_streaming_link(url):
        result = download_with_ytdlp(url, folder, filename, summary,
                                     quality=quality,
                                     current_process=current_process,
                                     stop_flag=stop_flag,
                                     parallel_mode=parallel_mode)
    else:
        result = download_with_aria2c(url, folder, filename, summary,
                                      bandwidth_limit=bandwidth_limit,
                                      current_process=current_process,
                                      stop_flag=stop_flag,
                                      parallel_mode=parallel_mode)

    # Mark receipt if successful
    if result and series_url:
        # Find the actual downloaded file and record it
        ep_key = f"{series_url}:{filename}"
        base = re.sub(r'\.(mp4|mkv|m3u8|webm)$', '', filename)
        for ext in ['mp4', 'mkv', 'webm', 'm4a']:
            p = os.path.join(folder, f'{base}.{ext}')
            if os.path.exists(p):
                actual_size = os.path.getsize(p)
                DownloadReceipt.mark_complete(ep_key, p, actual_size)
                break
        mark_episode_done(series_url, series_name or folder, filename)
    elif not result and series_url:
        ep_key = f"{series_url}:{filename}"
        DownloadReceipt.mark_failed(ep_key)

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
            except Exception as e:
                self._result = None
                safe_print(f"  [!] Prefetch error: {e}")
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
                          stop_flag=stop_flag, wait_fn=wait_fn,
                          parallel_mode=False)
    else:
        # Divide bandwidth evenly across threads so total stays within limit
        per_thread_bw = (bandwidth_limit // parallel) if bandwidth_limit else 0
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {}
            for url, filename in items:
                # Each thread gets its own current_process slot so
                # Ctrl+C can terminate ALL active subprocesses, not just one
                thread_proc = [None]
                f = executor.submit(
                    download_file,
                    url, folder, filename, summary,
                    check_expiry=True,
                    series_url=series_url,
                    series_name=series_name,
                    bandwidth_limit=per_thread_bw,
                    quality=quality,
                    current_process=thread_proc,
                    stop_flag=stop_flag,
                    wait_fn=wait_fn,
                    parallel_mode=True,
                )
                futures[f] = filename
            for future in as_completed(futures):
                fname = futures[future]
                try:
                    future.result()
                except Exception as e:
                    safe_print(f"  [!] Thread error for {fname}: {e}")
                    summary.add_failed(fname)
