import requests
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

from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# ─── SHARED STATE (imported from main) ────────────────────────
# These are set by main.py and read here. Imported at call time
# to avoid circular imports.

# ─── CONSTANTS ────────────────────────────────────────────────
IS_ANDROID   = os.path.exists('/storage/emulated/0')
BASE_DIR     = '/storage/emulated/0/Anon' if IS_ANDROID else os.path.join(os.path.expanduser('~'), 'Downloads', 'Anon')

if IS_ANDROID:
    CONFIG_DIR = os.path.expanduser('~/.config/anonrode')
else:
    CONFIG_DIR = os.path.join(os.path.expanduser('~'), '.config', 'anonrode')

if os.path.exists(os.path.join(BASE_DIR, '.config.json')) and not os.path.exists(os.path.join(CONFIG_DIR, '.config.json')):
    CONFIG_DIR = BASE_DIR
else:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
    except Exception:
        CONFIG_DIR = BASE_DIR

LOG_FILE     = os.path.join(CONFIG_DIR, '.download_history.json')
RESUME_FILE  = os.path.join(CONFIG_DIR, '.resume_state.json')
RECEIPT_FILE = os.path.join(CONFIG_DIR, '.download_receipts.json')
DIAG_LOG     = os.path.join(CONFIG_DIR, '.diag.log')
PROGRESS_LOG = os.path.join(CONFIG_DIR, '.download.log')  # NEW: Download progress log

UA_DESKTOP   = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

PRINT_LOCK   = threading.Lock()
STATE_LOCK   = threading.RLock()
PROCESS_LOCK = threading.Lock()
ACTIVE_PROCESSES = set()
_ARIA2C_AVAILABLE = None
_YTDLP_AVAILABLE  = None
_FFMPEG_AVAILABLE = None
_TOOL_LOCK        = threading.Lock()

class ProcessContainer:
    """A clean, object-oriented mutable wrapper for tracking active subprocesses."""
    def __init__(self, proc=None):
        self.proc = proc

def _is_stopped(flag):
    if flag is None:
        return False
    return flag.is_set() if hasattr(flag, 'is_set') else flag[0]

def _is_paused(flag):
    if flag is None:
        return False
    return flag.is_set() if hasattr(flag, 'is_set') else flag[0]

def check_connection() -> bool:
    try:
        r = requests.get("https://1.1.1.1", timeout=3, verify=False)
        return r.status_code == 200 or r.status_code == 204 or r.status_code == 302
    except Exception:
        return False

def wait_for_network(stop_flag=None):
    if not check_connection():
        safe_print("\n[📡] Network connection lost! Pausing active downloads and waiting for connection...")
        while not check_connection():
            if stop_flag and _is_stopped(stop_flag):
                break
            time.sleep(3)
        if not (stop_flag and _is_stopped(stop_flag)):
            safe_print("[📡] Network connection restored! Resuming...")

class ActiveDownloads:
    _lock = threading.Lock()
    _jobs = {}
    _last_printed_lines = 0

    @classmethod
    def update(cls, filename, pct, speed=None, eta=None, status="Downloading"):
        with cls._lock:
            cls._jobs[filename] = {
                'pct': pct,
                'speed': speed,
                'eta': eta,
                'status': status
            }
            cls._refresh_display()

    @classmethod
    def remove(cls, filename, status="Done", size_mb=None):
        with cls._lock:
            if filename in cls._jobs:
                cls._jobs[filename]['status'] = status
                cls._jobs[filename]['pct'] = 100.0
                if size_mb:
                    cls._jobs[filename]['speed'] = size_mb
            cls._refresh_display()

    @classmethod
    def _refresh_display(cls):
        import sys
        # Only draw if there are active downloads
        active_jobs = [j for j in cls._jobs.values() if j['status'] == 'Downloading']
        if not active_jobs:
            if cls._last_printed_lines > 0:
                cls._last_printed_lines = 0
            return
            
        lines_to_print = []
        lines_to_print.append("==================================================")
        lines_to_print.append("  ANONRODE DOWNLOAD DASHBOARD")
        lines_to_print.append("==================================================")
        
        for name, job in list(cls._jobs.items()):
            pct = job['pct']
            status = job['status']
            if status == 'Downloading':
                filled = int(pct / 100 * 15)
                bar = "█" * filled + "░" * (15 - filled)
                spd = f" | {job['speed']:.1f} MB/s" if job['speed'] is not None else ""
                eta = f" | ETA {job['eta']}" if job['eta'] else ""
                lines_to_print.append(f"  [↓] {name[:25]:<25} [{bar}] {pct:5.1f}%{spd}{eta}")
            elif status == 'Done':
                lines_to_print.append(f"  [✓] {name[:25]:<25} Done")
            elif status == 'Failed':
                lines_to_print.append(f"  [✗] {name[:25]:<25} Failed")
                
        lines_to_print.append("==================================================")
        
        # Clear previous print
        if cls._last_printed_lines > 0:
            sys.stdout.write(f"\033[{cls._last_printed_lines}A")
            
        # Print new lines
        for line in lines_to_print:
            sys.stdout.write(f"\r\033[K{line}\n")
        sys.stdout.flush()
        
        cls._last_printed_lines = len(lines_to_print)
def send_notification(title, message):
    """Trigger native Termux android notification if configured."""
    config = {}
    try:
        import json as _json
        config_path = os.path.join(CONFIG_DIR, '.config.json')
        if os.path.exists(config_path):
            with open(config_path) as _f:
                config = _json.load(_f)
    except Exception:
        pass
        
    if not config.get('enable_android_notifications', True):
        return
        
    try:
        # Run termux-notification in subprocess
        subprocess.run([
            'termux-notification',
            '-t', title,
            '-c', message,
            '--id', 'anonrode_dl'
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass
    except Exception:
        pass

OUTPUT_MODE = 'normal'
LAST_STATUS = {
    'screen': 'Ready',
    'status': 'Idle',
    'title': '',
    'source': '',
    'current': '',
    'progress': '',
}

# ─── STATE TRACKING CALLBACK (avoids circular import) ──────────────
# main.py registers a callback to be notified of current download state
_current_state_callback = None

def register_state_callback(callback):
    """Register callback for download state updates. Called by main.py."""
    global _current_state_callback
    _current_state_callback = callback

def _notify_current_state(series_url, series_name, episode_name, filepath, expected_size):
    """Thread-safe notification of current download state."""
    try:
        if _current_state_callback:
            _current_state_callback(series_url, series_name, episode_name, filepath, expected_size)
    except Exception:
        pass

def set_output_mode(mode):
    global OUTPUT_MODE
    OUTPUT_MODE = 'debug' if str(mode).lower() == 'debug' else 'normal'

def get_output_mode():
    return OUTPUT_MODE

def is_debug():
    return OUTPUT_MODE == 'debug'

def _is_noisy_line(text):
    t = text.strip().lower()
    if not t:
        return False
    noisy_starts = (
        '[*] resolver', '[*] checking', '[*] expected size',
        '[!] http', '[!] attempt', '[>] ', '[diag]',
        '    [>]', '  [>] ', '  [!] attempt',
    )
    noisy_contains = (
        ' resolved:', 'trying:', 'cdn url', 'available formats',
        'diagnostic', 'details written', 'resolver', 'pattern not found',
    )
    return t.startswith(noisy_starts) or any(x in t for x in noisy_contains)

def safe_print(*args, **kwargs):
    text = ' '.join(str(a) for a in args)
    if not is_debug() and _is_noisy_line(text):
        return
    with PRINT_LOCK:
        import sys
        if hasattr(ActiveDownloads, '_last_printed_lines') and ActiveDownloads._last_printed_lines > 0:
            sys.stdout.write(f"\033[{ActiveDownloads._last_printed_lines}A")
            for _ in range(ActiveDownloads._last_printed_lines):
                sys.stdout.write("\033[K\n")
            sys.stdout.write(f"\033[{ActiveDownloads._last_printed_lines}A")
            ActiveDownloads._last_printed_lines = 0
            sys.stdout.flush()
        try:
            print(*args, **kwargs)
        except UnicodeEncodeError:
            print(text.encode('ascii', 'replace').decode('ascii'), **kwargs)

def debug_print(*args, **kwargs):
    if is_debug():
        safe_print(*args, **kwargs)

def update_status(**kwargs):
    LAST_STATUS.update({k: v for k, v in kwargs.items() if v is not None})

def get_status():
    return dict(LAST_STATUS)

def ui_screen(title, rows=None, footer=None):
    """Compact normal-mode status block. In debug mode it still prints cleanly."""
    rows = rows or []
    width = 50
    with PRINT_LOCK:
        print()
        print("ANONRODE")
        print(title)
        print("-" * width)
        for key, value in rows:
            if value is None or value == '':
                continue
            print(f"{str(key) + ':':<12} {value}")
        if footer:
            print()
            print(footer)

def register_process(proc):
    """Track subprocesses so Ctrl+C can stop parallel downloads too."""
    if not proc:
        return
    with PROCESS_LOCK:
        ACTIVE_PROCESSES.add(proc)

def unregister_process(proc):
    if not proc:
        return
    with PROCESS_LOCK:
        ACTIVE_PROCESSES.discard(proc)

def terminate_active_processes():
    """Terminate all known live subprocesses. Returns how many were signalled."""
    with PROCESS_LOCK:
        procs = list(ACTIVE_PROCESSES)
    count = 0
    for proc in procs:
        try:
            if proc and proc.poll() is None:
                proc.terminate()
                count += 1
        except Exception:
            pass
    return count

def finish_process(proc, timeout=5):
    """Wait for a terminated process; kill it if it refuses to exit."""
    if not proc:
        return
    try:
        if proc.poll() is None:
            proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=timeout)
        except Exception:
            pass

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
        with STATE_LOCK:
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
            
        # Throttle updates to 2 Hz to prevent Git Bash / Mintty deadlocks with \r
        import time
        now = time.time()
        if hasattr(self, '_last_update') and now - self._last_update < 0.5 and pct < 100.0:
            return
        self._last_update = now

        self._started = True
        update_status(status='Downloading', current=self._name, progress=f'{pct:0.1f}%')
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
        update_status(status='Complete', current=self._name, progress='100%')
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
        update_status(status='Failed', current=self._name)
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
    try:
        config_path = os.path.join(CONFIG_DIR, '.config.json')
        with open(config_path) as f:
            min_gb = float(json.load(f).get('storage_guard_gb', 1.0))
    except Exception:
        min_gb = 1.0
    free_gb = get_free_space_gb()
    if free_gb < min_gb:
        safe_print(f"[!] Critically low disk space ({free_gb:.2f}GB free, limit is {min_gb:.2f}GB) — stopping")
        return False
    return True

# ─── DOWNLOAD HISTORY ─────────────────────────────────────────
def load_history():
    with HISTORY_LOCK:
        try:
            os.makedirs(BASE_DIR, exist_ok=True)
            if os.path.exists(LOG_FILE):
                with open(LOG_FILE, 'r') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

def save_history(history):
    with HISTORY_LOCK:
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
    with HISTORY_LOCK:
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
        os.makedirs(CONFIG_DIR, exist_ok=True)
        try:
            if os.path.exists(PROGRESS_LOG) and os.path.getsize(PROGRESS_LOG) > 5 * 1024 * 1024:
                backup = PROGRESS_LOG + '.old'
                if os.path.exists(backup):
                    os.remove(backup)
                os.rename(PROGRESS_LOG, backup)
        except Exception:
            pass
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


RESUME_LOCK  = threading.Lock()
HISTORY_LOCK = threading.RLock()

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
        if ep_filename in state[key].get('failed', []):
            state[key]['failed'].remove(ep_filename)
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
        if session:
            s = session
        else:
            s = _req.Session()
            s.headers['User-Agent'] = UA_DESKTOP
        r = s.head(url, timeout=10, allow_redirects=True)
        cl = r.headers.get('Content-Length') or r.headers.get('content-length')
        if cl and str(cl).isdigit():
            return int(cl)
    except Exception:
        pass
    return None

def already_downloaded(folder, filename, min_mb=1.0, series_url=None, url=None):
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
        
        # If done: return complete (only if no incomplete files exist on disk!)
        if receipt.get('status') == 'done':
            path = receipt.get('filepath')
            if path and os.path.exists(path):
                # If there are incomplete files on disk, it means the done status is corrupt/outdated
                if os.path.exists(path + '.aria2') or os.path.exists(path + '.part'):
                    with STATE_LOCK:
                        receipts = DownloadReceipt.load_all()
                        if ep_key in receipts:
                            receipts[ep_key]['status'] = 'paused'
                            DownloadReceipt.save_all(receipts)
                    safe_print(f"  [*] Incomplete download found — will resume")
                    return False, None
                else:
                    safe_print(f"  [✓] Already downloaded (receipt verified)")
                    return True, path
            # Receipt says done but file missing — clean up receipt and re-download
            DownloadReceipt.mark_failed(ep_key)
    
    # Fallback: filesystem check (for files without receipt records)
    base = re.sub(r'\.(mp4|mkv|m3u8|webm)$', '', filename)

    def _resolve_expected(filepath):
        """Get expected size: stored state first, HEAD request if needed."""
        if series_url:
            stored = get_episode_size(series_url, filename)
            if stored:
                return stored
        if url:
            fetched = fetch_expected_size(url)
            if fetched and series_url:
                save_episode_size(series_url, filename, fetched)
            return fetched
        return None
    
    # First try exact filename match
    if os.path.exists(os.path.join(folder, filename)):
        filepath = os.path.join(folder, filename)
        actual = os.path.getsize(filepath)
        expected = _resolve_expected(filepath)
        
        # Sidecar = ground truth. Check BEFORE size math so 99% files are never falsely skipped.
        if os.path.exists(filepath + '.aria2') or os.path.exists(filepath + '.part'):
            safe_print(f"  [*] Incomplete download found ({actual/(1024*1024):.1f}MB) — will resume")
            return False, None

        if expected:
            if actual >= expected * 0.99:
                safe_print(f"  [✓] Found existing file ({actual/(1024*1024):.1f}MB)")
                return True, filepath
            elif ep_key:
                receipt = DownloadReceipt.get_receipt(ep_key)
                if receipt.get('status') == 'paused':
                    safe_print(f"  [*] Resuming: {actual/(1024*1024):.1f}MB of {expected/(1024*1024):.1f}MB")
                    return False, None
            safe_print(f"  [!] Incomplete: {actual/(1024*1024):.1f}MB of {expected/(1024*1024):.1f}MB — re-downloading")
            try:
                os.remove(filepath)
            except Exception:
                pass
            return False, None
        else:
            min_bytes = max(5 * 1024 * 1024, min_mb * 1024 * 1024)
            if actual >= min_bytes:
                safe_print(f"  [✓] Found existing file ({actual/(1024*1024):.1f}MB)")
                return True, filepath
    
    # If exact filename not found, check by extension (but prefer receipt filepath if available)
    receipt_path = None
    if ep_key:
        receipt = DownloadReceipt.get_receipt(ep_key)
        if receipt and receipt.get('filepath'):
            receipt_path = receipt.get('filepath')
            if os.path.exists(receipt_path):
                actual = os.path.getsize(receipt_path)
                expected = _resolve_expected(receipt_path)
                # Sidecar = ground truth. Check BEFORE size math.
                if os.path.exists(receipt_path + '.aria2') or os.path.exists(receipt_path + '.part'):
                    safe_print(f"  [*] Incomplete download found ({actual/(1024*1024):.1f}MB) — will resume")
                    return False, None
                if expected:
                    if actual >= expected * 0.99:
                        safe_print(f"  [✓] Found existing file ({actual/(1024*1024):.1f}MB)")
                        return True, receipt_path
                else:
                    min_bytes = max(5 * 1024 * 1024, min_mb * 1024 * 1024)
                    if actual >= min_bytes:
                        safe_print(f"  [✓] Found existing file ({actual/(1024*1024):.1f}MB)")
                        return True, receipt_path
    
    # Last resort: scan by extension (only if no receipt path available)
    for ext in ['mp4', 'mkv', 'webm']:
        filepath = os.path.join(folder, f"{base}.{ext}")
        if os.path.exists(filepath):
            actual = os.path.getsize(filepath)
            expected = _resolve_expected(filepath)
            
            # Sidecar = ground truth. Check BEFORE size math.
            if os.path.exists(filepath + '.aria2') or os.path.exists(filepath + '.part'):
                if filename.endswith('.' + ext):
                    safe_print(f"  [*] Incomplete download found ({actual/(1024*1024):.1f}MB) — will resume")
                return False, None

            if expected:
                if actual >= expected * 0.99:
                    safe_print(f"  [✓] Found existing file ({actual/(1024*1024):.1f}MB)")
                    return True, filepath
                else:
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
                    return False, None
            else:
                min_bytes = max(5 * 1024 * 1024, min_mb * 1024 * 1024)
                if actual >= min_bytes:
                    safe_print(f"  [✓] Found existing file ({actual/(1024*1024):.1f}MB)")
                    return True, filepath
                else:
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
    try:
        from curl_cffi import requests as cf_requests
        s = cf_requests.Session(impersonate='chrome120')
        s.headers['User-Agent'] = UA_DESKTOP
        return s
    except ImportError:
        import requests
        s = requests.Session()
        s.headers['User-Agent'] = UA_DESKTOP
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

def _update_ytdlp(channel='stable'):
    """
    Update yt-dlp. channel='stable' (default) installs the latest stable
    release. channel='master' installs the latest pre-release/nightly
    build via --pre, which carries newer site fixes (e.g. Instagram) but
    is less tested.

    The silent background auto-update path always calls this with no
    args (stable) so an unattended update can't silently put a
    less-tested yt-dlp build in front of a running download. Only the
    explicit `update` command — where the user is watching and can react
    — respects the configured ytdlp_channel setting.
    """
    try:
        cmd = [sys.executable, '-m', 'pip', 'install', '--upgrade']
        if channel == 'master':
            cmd.append('--pre')
        cmd += ['yt-dlp', '--break-system-packages', '-q']
        subprocess.run(cmd, check=True, capture_output=True)
    except Exception:
        pass

def _check_aria2c_availability():
    global _ARIA2C_AVAILABLE
    with _TOOL_LOCK:
        if _ARIA2C_AVAILABLE is not None:
            return _ARIA2C_AVAILABLE
        import shutil
        if shutil.which('aria2c') is not None:
            _ARIA2C_AVAILABLE = True
        else:
            if _install_aria2c():
                _ARIA2C_AVAILABLE = True
            else:
                _ARIA2C_AVAILABLE = False
        return _ARIA2C_AVAILABLE

def _check_ytdlp_availability():
    global _YTDLP_AVAILABLE
    with _TOOL_LOCK:
        if _YTDLP_AVAILABLE is not None:
            return _YTDLP_AVAILABLE
        import shutil
        if shutil.which('yt-dlp') is not None:
            _YTDLP_AVAILABLE = True
        else:
            if _install_ytdlp():
                _YTDLP_AVAILABLE = True
            else:
                _YTDLP_AVAILABLE = False
        return _YTDLP_AVAILABLE

def _auto_install_system_pkg(pkg_name):
    """Install a system package via pkg (Termux) or apt (Linux)."""
    import shutil
    if shutil.which(pkg_name):
        return True
    safe_print(f'  [*] {pkg_name} not found — installing...')
    for installer in (['pkg', 'install', pkg_name, '-y'],
                      ['apt-get', 'install', pkg_name, '-y', '-q']):
        try:
            result = subprocess.run(
                installer,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode == 0 and shutil.which(pkg_name):
                safe_print(f'  [\u2713] {pkg_name} installed')
                return True
        except FileNotFoundError:
            continue
        except Exception:
            continue
    safe_print(f'  [!] Could not auto-install {pkg_name} — install manually: pkg install {pkg_name}')
    return False

def _check_ffmpeg_availability():
    global _FFMPEG_AVAILABLE
    with _TOOL_LOCK:
        if _FFMPEG_AVAILABLE is not None:
            return _FFMPEG_AVAILABLE
        import shutil
        if shutil.which('ffmpeg') is not None:
            _FFMPEG_AVAILABLE = True
        else:
            _FFMPEG_AVAILABLE = _auto_install_system_pkg('ffmpeg')
        return _FFMPEG_AVAILABLE

# ─── DOWNLOAD BACKENDS ────────────────────────────────────────
def download_with_aria2c(url, folder, filename, summary,
                         bandwidth_limit=0, current_process=None,
                         retries=3, stop_flag=None, pause_flag=None,
                         parallel_mode=False,
                         series_url=None, series_name=None,
                         expected_size=0, config=None):
    """
    Smart downloader with resumable downloads support.
    
    If a partial file exists:
      - Check receipt system
      - Use aria2c's --continue flag to resume from byte offset
      - Much faster than starting over
    """
    config = config or {}  # Config dict with resolver_timeout, etc.
    try:
        import json as _json
        config_path = os.path.join(CONFIG_DIR, '.config.json')
        if os.path.exists(config_path):
            with open(config_path) as _f:
                config = {**config, **_json.load(_f)}
    except Exception:
        pass
    retries = int(config.get('download_retries', 3))
    
    if not _check_aria2c_availability():
        safe_print("[!] aria2c unavailable — falling back to requests")
        return download_with_requests(
            url, folder, filename, summary,
            stop_flag=stop_flag,
            parallel_mode=parallel_mode,
            series_url=series_url,
            series_name=series_name,
            expected_size=expected_size
        )

    os.makedirs(folder, exist_ok=True)
    safe_fname    = re.sub(r'[^\w]', '_', filename)[:30]
    # Use file hash only (no thread_id) so aria2c can find its session
    # file across pause/resume cycles even if the thread changes.
    import hashlib
    file_hash = hashlib.md5(filename.encode()).hexdigest()[:8]
    session_file  = os.path.join(folder, f'.aria2_{safe_fname}_{file_hash}.txt')
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


    for attempt in range(retries):
        try:
            cmd = [
                'aria2c',
                '-c',  # Continue/resume support (key for resumable downloads!)
                f'--max-tries=0',
                '--retry-wait=10',
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

            # Set filepath for pause handler using callback
            _notify_current_state(series_url, series_name or folder, filename, filepath, expected_size)

            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
            register_process(proc)
            if current_process is not None:
                current_process.proc = proc

            # Poll instead of blocking wait — allows stop_flag to interrupt
            stopped = False
            stalled = False
            idle_timeout = max(60, int(config.get('download_timeout', 120)) * 2)
            last_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
            last_progress = time.time()
            while proc.poll() is None:
                if _is_stopped(stop_flag):
                    proc.terminate()
                    stopped = True
                    break
                # Pause: gracefully terminate aria2c instead of SIGSTOP.
                # SIGSTOP causes kernel TCP buffers to fill up and then
                # flush all at once on SIGCONT, creating a misleading
                # 40MB+ instant jump and often killing the connection.
                # aria2c's -c flag ensures it resumes from the partial
                # file when we re-launch it after unpause.
                if _is_paused(pause_flag):
                    safe_print("  [‖] Pausing download...")
                    proc.terminate()
                    finish_process(proc)
                    unregister_process(proc)
                    if current_process is not None:
                        current_process.proc = None
                    # Block until user unpauses
                    while _is_paused(pause_flag) and not (_is_stopped(stop_flag)):
                        time.sleep(0.3)
                    if _is_stopped(stop_flag):
                        stopped = True
                        break
                    # Re-launch aria2c — it picks up from the partial file
                    safe_print("  [▶] Resuming download...")
                    last_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
                    last_progress = time.time()
                    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
                    register_process(proc)
                    if current_process is not None:
                        current_process.proc = proc
                    continue
                current_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
                if current_size > last_size:
                    last_size = current_size
                    last_progress = time.time()
                elif time.time() - last_progress > idle_timeout:
                    proc.terminate()
                    stalled = True
                    break
                time.sleep(0.5)
            finish_process(proc)
            code = proc.returncode if proc.returncode is not None else -1
            unregister_process(proc)
            if current_process is not None:
                current_process.proc = None

            if stopped:
                progress.fail()
                _cleanup_session_file(session_file)
                return False
            if stalled:
                progress.fail()
                safe_print(f"[✗] aria2c stalled for {idle_timeout}s — moving to next episode")
                _cleanup_session_file(session_file)
                summary.add_failed(filename)
                return False

            # Detect user cancellation: aria2c code 7, Windows Ctrl+C code,
            # negative codes (terminated by signal), or stop_flag already set
            is_user_cancel = (
                code == 7 or
                code == -1073741510 or   # Windows STATUS_CONTROL_C_EXIT
                code < 0 or
                _is_stopped(stop_flag)
            )
            if is_user_cancel:
                progress.fail()
                safe_print(f"[*] Download cancelled by user")
                # Propagate stop to other parallel threads
                if stop_flag is not None and hasattr(stop_flag, 'set'):
                    stop_flag.set()
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
                        if attempt < retries - 1 and not _is_stopped(stop_flag):
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
                    if attempt < retries - 1 and not _is_stopped(stop_flag):
                        safe_print(f"[*] retrying ({attempt+2}/{retries})...")
                        time.sleep(5)
                        continue
                    _cleanup_session_file(session_file)
                    summary.add_failed(filename)
                    return False
            else:
                progress.fail()
                if code == 7:
                    # Code 7 = user pressed Ctrl+C — never retry
                    safe_print(f"[*] Download cancelled by user")
                    _cleanup_session_file(session_file)
                    summary.add_failed(filename)
                    return False
                safe_print(f"[✗] aria2c failed (code {code})")
                if attempt < retries - 1 and not _is_stopped(stop_flag):
                    safe_print(f"[*] retrying ({attempt+2}/{retries})...")
                    time.sleep(5)
                    continue
                _cleanup_session_file(session_file)
                summary.add_failed(filename)
                return False
        except Exception as e:
            progress.fail()
            try:
                unregister_process(proc)
            except Exception:
                pass
            if current_process is not None:
                current_process.proc = None
            _cleanup_session_file(session_file)
            safe_print(f"[✗] aria2c error: {e}")
            summary.add_failed(filename)
            return False
    _cleanup_session_file(session_file)
    return False

def download_with_requests(url, folder, filename, summary, stop_flag=None,
                           parallel_mode=False, series_url=None,
                           series_name=None, expected_size=0):

    filepath = os.path.join(folder, filename)
    os.makedirs(folder, exist_ok=True)
    safe_print(f"  [↓] Downloading: {filename}")
    progress = LiveProgress(filename, parallel=parallel_mode)
    
    s = make_session()
    retries_left = 5
    downloaded = 0
    total = expected_size
    
    _notify_current_state(series_url, series_name or folder, filename, filepath, expected_size)
    
    start_time = time.time()
    
    while retries_left > 0:
        if _is_stopped(stop_flag):
            progress.fail()
            return False
            
        try:
            existing_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
            headers = {**dict(s.headers), 'Referer': get_referer_for_url(url)}
            if existing_size > 0:
                headers['Range'] = f'bytes={existing_size}-'
                
            r = s.get(url, stream=True, timeout=30, headers=headers)
            
            if r.status_code == 416:
                # Range not satisfiable, file might be complete
                break
                
            if r.status_code not in (200, 206):
                # If we get a 403 or similar link expiry error, we can't resume
                progress.fail()
                safe_print(f"[!] HTTP {r.status_code} — cannot resume")
                summary.add_failed(filename)
                return False
                
            mode = 'ab' if existing_size and r.status_code == 206 else 'wb'
            if mode == 'wb':
                existing_size = 0
                
            if 'text/html' in r.headers.get('content-type', ''):
                progress.fail()
                safe_print("[!] got HTML instead of video")
                summary.add_failed(filename)
                return False
                
            content_length = int(r.headers.get('content-length', 0))
            if not total:
                total = existing_size + content_length if r.status_code == 206 else content_length
                
            downloaded = existing_size
            
            with open(filepath, mode) as f:
                for chunk in r.iter_content(chunk_size=512 * 1024):
                    if _is_stopped(stop_flag):
                        progress.fail()
                        safe_print("[!] stopped")
                        if series_url:
                            ep_key = f"{series_url}:{filename}"
                            DownloadReceipt.mark_paused(ep_key, filepath, os.path.getsize(filepath), total)
                        return False
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            pct = downloaded * 100 / total
                            ela = time.time() - start_time
                            spd = (downloaded / ela / 1024 / 1024) if ela > 0 else 0
                            eta_s = int((total - downloaded) / (downloaded / ela)) if downloaded > 0 else 0
                            eta = f'{eta_s // 60}:{eta_s % 60:02d}'
                            progress.update(pct, spd, eta)
            # Completed chunk loop successfully
            break
            
        except (requests.RequestException, ConnectionError, OSError) as e:
            retries_left -= 1
            safe_print(f"\n[📡] requests error: {e}. Retrying ({5 - retries_left}/5)...")
            if retries_left > 0:
                wait_for_network(stop_flag)
                if _is_stopped(stop_flag):
                    progress.fail()
                    return False
                time.sleep(3)
            else:
                progress.fail()
                safe_print("[✗] Connection failed permanently after 5 retries.")
                summary.add_failed(filename)
                return False
                
    # Final size checks
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

def download_with_ytdlp(url, folder, filename, summary,
                        quality=None, current_process=None, stop_flag=None,
                        pause_flag=None, parallel_mode=False):
    if not _check_ytdlp_availability():
        safe_print("[!] yt-dlp unavailable")
        summary.add_failed(filename)
        return False
    if not _check_ffmpeg_availability():
        summary.add_failed(filename)
        return False

    os.makedirs(folder, exist_ok=True)
    base        = re.sub(r'\.(mp4|mkv|m3u8)$', '', filename)
    out_template = os.path.join(folder, base + '.%(ext)s')
    quality_str  = quality or 'bestvideo[height<=480]+bestaudio/best[height<=480]'

    progress = LiveProgress(filename, parallel=parallel_mode)
    proc = None
    try:
        cmd = [
            'yt-dlp',
            '-f', quality_str,
            '--merge-output-format', 'mp4',
            '-o', out_template,
            '--no-playlist',
            '--retries', '3',
            '--fragment-retries', '3',
            '--retry-sleep', '10',
            '--no-warnings', '--progress', '--newline',
        ]
        if _check_aria2c_availability():
            cmd += [
                '--external-downloader', 'aria2c',
                '--external-downloader-args',
                'aria2c:-x 16 -s 16 -c --max-tries=0 --retry-wait=10 --timeout=120 '
                '--connect-timeout=60 --file-allocation=none --min-split-size=1M'
            ]
        cmd.append(url)
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        register_process(proc)
        if current_process is not None:
            current_process.proc = proc

        started = time.time()
        hard_timeout = 6 * 60 * 60
        while proc.poll() is None:
            if _is_stopped(stop_flag):
                proc.terminate()
                break
            # ── Pause/Resume support ───────────────────────────────
            if _is_paused(pause_flag):
                # Gracefully terminate so aria2c saves .aria2 state for resume
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                if current_process is not None:
                    current_process.proc = None
                safe_print("  [‖] Paused — press Ctrl+C to resume")
                # Block here until unpaused or stopped
                while _is_paused(pause_flag):
                    if _is_stopped(stop_flag):
                        break
                    time.sleep(0.2)
                if _is_stopped(stop_flag):
                    summary.add_failed(filename)
                    return False
                # Re-launch with -c (continue/resume) flag
                safe_print("  [▶] Resuming download...")
                # Add -c (continue) flag for aria2c
                resume_cmd = cmd[:]  # cmd already has -c in aria2c args above
                proc = subprocess.Popen(resume_cmd, stdin=subprocess.DEVNULL)
                register_process(proc)
                if current_process is not None:
                    current_process.proc = proc
                started = time.time()
                continue
            # ──────────────────────────────────────────────────────
            if time.time() - started > hard_timeout:
                proc.terminate()
                safe_print("[✗] yt-dlp timed out — moving on")
                break
            time.sleep(0.5)
        finish_process(proc)
        code = proc.returncode if proc.returncode is not None else -1
        unregister_process(proc)
        if current_process is not None:
            current_process.proc = None

        if code == 0:
            for ext in ['mp4', 'mkv', 'webm']:
                p = os.path.join(folder, f"{base}.{ext}")
                if os.path.exists(p):
                    size_mb = os.path.getsize(p) / (1024 * 1024)
                    progress.done(size_mb)
                    summary.add_success()
                    log_download(filename, url, p)
                    return True
            # yt-dlp exited 0 but no output file — likely failed
            progress.fail()
            safe_print("[✗] yt-dlp exited successfully but produced no file")
            summary.add_failed(filename)
            return False
        else:
            progress.fail()
            if _is_stopped(stop_flag):
                safe_print("[*] yt-dlp stopped")
            else:
                safe_print("[✗] yt-dlp failed")
                summary.add_failed(filename)
            return False
    except Exception as e:
        unregister_process(proc)
        if current_process is not None:
            current_process.proc = None
        progress.fail()
        safe_print(f"[✗] yt-dlp error: {e}")
        summary.add_failed(filename)
        return False


def _select_social_format(url, preferred_quality='720p'):
    """Inspect yt-dlp formats for social videos. Prefer 720p, else best available."""
    if str(preferred_quality).lower() == 'best':
        return 'bestvideo+bestaudio/best', 'best available'
    preferred_height = 720
    m = re.search(r'(\d+)', str(preferred_quality or '720p'))
    if m:
        preferred_height = int(m.group(1))
    try:
        result = subprocess.run(
            ['yt-dlp', '-J', '--no-playlist', '--no-warnings', url],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL
        )
        if result.returncode != 0 or not result.stdout:
            return 'bestvideo+bestaudio/best', 'best available'
        info = json.loads(result.stdout)
        formats = info.get('formats') or []
        heights = sorted({
            int(f.get('height')) for f in formats
            if isinstance(f.get('height'), int) and f.get('height') > 0
        })
        if heights:
            debug_print(f"[*] Available social formats: {', '.join(str(h) + 'p' for h in heights)}")
        if preferred_height in heights:
            debug_print(f"[*] Selected social format: {preferred_height}p")
            return (
                f'bestvideo[height={preferred_height}]+bestaudio/'
                f'best[height={preferred_height}]/best',
                f'{preferred_height}p'
            )
        if heights:
            best_height = max(heights)
            debug_print(f"[*] Selected social format: best available ({best_height}p)")
            return (
                f'bestvideo[height={best_height}]+bestaudio/'
                f'best[height={best_height}]/best',
                f'best available ({best_height}p)'
            )
    except Exception as e:
        debug_print(f"[*] Social format inspection failed: {e}")
    return 'bestvideo+bestaudio/best', 'best available'

def _find_recent_media(folder, since_time):
    try:
        candidates = []
        for name in os.listdir(folder):
            if not name.lower().endswith(('.mp4', '.mkv', '.webm', '.m4a')):
                continue
            path = os.path.join(folder, name)
            try:
                if os.path.getmtime(path) >= since_time - 2:
                    candidates.append(path)
            except Exception:
                pass
        if candidates:
            return max(candidates, key=lambda p: os.path.getmtime(p))
    except Exception:
        pass
    return None

def download_social_ytdlp(url, folder, filename, summary, current_process=None,
                           quality_override=None, out_template=None, stop_flag=None,
                           preferred_quality='720p', smart_select=True):
    if not _check_ytdlp_availability():
        safe_print("[!] yt-dlp unavailable")
        summary.add_failed(filename)
        return False

    os.makedirs(folder, exist_ok=True)
    base = re.sub(r'\.(mp4|mkv|m3u8)$', '', filename)
    if not out_template:
        out_template = os.path.join(folder, base + '.%(ext)s')

    selected_label = None
    if quality_override:
        format_chain = [quality_override, 'bestvideo+bestaudio/best', 'best']
        selected_label = 'custom'
    elif smart_select:
        selected_fmt, selected_label = _select_social_format(url, preferred_quality)
        format_chain = [selected_fmt, 'bestvideo+bestaudio/best', 'best']
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
        proc = None
        cmd = [
            'yt-dlp', '-f', fmt,
            '--merge-output-format', 'mp4',
            '-o', out_template,
            '--no-playlist',
            '--retries', '3', '--fragment-retries', '3',
            '--no-warnings', '--progress', '--newline',
        ]
        if _check_aria2c_availability():
            cmd += [
                '--external-downloader', 'aria2c',
                '--external-downloader-args',
                'aria2c:-x 16 -s 16 -c --max-tries=0 --retry-wait=10 '
                '--timeout=120 --connect-timeout=60 --file-allocation=none --min-split-size=1M'
            ]
        cmd.append(url)
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
            register_process(proc)
            if current_process is not None:
                current_process.proc = proc
            while proc.poll() is None:
                if _is_stopped(stop_flag):
                    proc.terminate()
                    break
                time.sleep(0.5)
            finish_process(proc)
            return proc.returncode if proc.returncode is not None else -1
        finally:
            unregister_process(proc)
            if current_process is not None:
                current_process.proc = None

    try:
        start_time = time.time()
        if selected_label:
            ui_screen('Social Download', [
                ('Quality', selected_label),
                ('Status', 'Downloading'),
            ])
        for fmt in format_chain:
            if _is_stopped(stop_flag):
                break
            code = _run_ytdlp(fmt)
            if _is_stopped(stop_flag):
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
                p = _find_recent_media(folder, start_time)
                if p:
                    size_mb = os.path.getsize(p) / (1024 * 1024)
                    progress.done(size_mb)
                    summary.add_success()
                    log_download(os.path.basename(p), url, p)
                    return True
                progress.done()
                summary.add_success()
                return True
        # All formats failed
        progress.fail()
        if _is_stopped(stop_flag):
            safe_print("[*] yt-dlp stopped")
        else:
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
                  check_expiry=False, series_url=None, series_name=None,
                  bandwidth_limit=0, quality=None,
                  current_process=None, stop_flag=None, pause_flag=None,
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

    done, _ = already_downloaded(folder, filename, series_url=series_url, url=url)
    if done:
        safe_print(f"  [✓] Already downloaded — skipping")
        summary.add_skipped()
        if series_url:
            mark_episode_done(series_url, series_name or folder, filename)
        return True
    else:
        # Self-healing: if file is actually incomplete on disk, remove it from completed lists
        if series_url:
            with RESUME_LOCK:
                try:
                    state = _load_resume_state_unlocked()
                    if series_url in state and 'done' in state[series_url]:
                        if filename in state[series_url]['done']:
                            state[series_url]['done'].remove(filename)
                            _save_resume_state_unlocked(state)
                except Exception:
                    pass
            with STATE_LOCK:
                try:
                    receipts = DownloadReceipt.load_all()
                    ep_key = f"{series_url}:{filename}"
                    if ep_key in receipts and receipts[ep_key].get('status') == 'done':
                        receipts[ep_key]['status'] = 'paused'
                        DownloadReceipt.save_all(receipts)
                except Exception:
                    pass

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
    if _is_stopped(stop_flag):
        return False

    # Set globals for pause handler (Ctrl+C) - use callback for thread safety
    _notify_current_state(series_url, series_name or folder, filename, None, 0)

    if series_url:
        mark_episode_current(series_url, series_name or folder, filename)

    # Fetch and store expected file size before download starts
    # so resume checks can verify completeness precisely
    expected = 0
    if series_url and not is_streaming_link(url):
        expected = get_episode_size(series_url, filename)
        if not expected:
            expected = fetch_expected_size(url)
            if expected:
                save_episode_size(series_url, filename, expected)
                safe_print(f"  [*] Expected size: {expected/(1024*1024):.1f} MB")
        expected = expected or 0

    if is_streaming_link(url):
        result = download_with_ytdlp(url, folder, filename, summary,
                                     quality=quality,
                                     current_process=current_process,
                                     stop_flag=stop_flag,
                                     pause_flag=pause_flag,
                                     parallel_mode=parallel_mode)
    else:
        result = download_with_aria2c(url, folder, filename, summary,
                                      bandwidth_limit=bandwidth_limit,
                                      current_process=current_process,
                                      stop_flag=stop_flag,
                                      pause_flag=pause_flag,
                                      parallel_mode=parallel_mode,
                                      series_url=series_url,
                                      series_name=series_name or folder,
                                      expected_size=expected)

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
        paused = DownloadReceipt.get_paused_download(ep_key)
        if paused:
            return False
        DownloadReceipt.mark_failed(ep_key)
        # Also clear from resume state so it doesn't show as "current"
        with RESUME_LOCK:
            state = _load_resume_state_unlocked()
            if series_url in state:
                state[series_url]['current'] = None
                if filename not in state[series_url].get('failed', []):
                    if 'failed' not in state[series_url]:
                        state[series_url]['failed'] = []
                    state[series_url]['failed'].append(filename)
                _save_resume_state_unlocked(state)

    if result:
        send_notification("Download Complete", f"Finished downloading {filename}")
    else:
        if not _is_stopped(stop_flag) and not _is_paused(pause_flag):
            send_notification("Download Failed", f"Could not download {filename}")

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
                   current_process=None, stop_flag=None,
                   pause_flag=None, wait_fn=None):
    if not items:
        return
    if parallel == 1:
        for url, filename in items:
            if _is_stopped(stop_flag):
                break
            download_file(url, folder, filename, summary,
                          series_url=series_url, series_name=series_name,
                          bandwidth_limit=bandwidth_limit, quality=quality,
                          current_process=current_process,
                          stop_flag=stop_flag, pause_flag=pause_flag,
                          wait_fn=wait_fn,
                          parallel_mode=False)
    else:
        # Divide bandwidth evenly across threads so total stays within limit
        per_thread_bw = (bandwidth_limit // parallel) if bandwidth_limit else 0
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {}
            for url, filename in items:
                # Each thread gets its own current_process slot so
                # Ctrl+C can terminate ALL active subprocesses, not just one
                thread_proc = ProcessContainer()
                f = executor.submit(
                    download_file,
                    url, folder, filename, summary,
                    check_expiry=False,
                    series_url=series_url,
                    series_name=series_name,
                    bandwidth_limit=per_thread_bw,
                    quality=quality,
                    current_process=thread_proc,
                    stop_flag=stop_flag,
                    pause_flag=pause_flag,
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
