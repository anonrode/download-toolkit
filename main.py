import requests
import re
import time
import sys
import os
import platform
import subprocess
import shutil
import json
import threading
import signal
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote_plus

try:
    from curl_cffi import requests as cf_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

# ─── CONFIG ───────────────────────────────────────────────────
UA_DESKTOP   = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
UA_MOBILE    = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36'
PLUTO_BASE   = 'https://plutomovies.com'
ANITAKU_BASE = 'https://anitaku.com.ro'
EP_KEYWORDS  = ['-e', 'episode', 's0', 's1', 's2', 's3', 's4', 's5', 's6', 's7', 's8', 's9']

# ─── OS DETECTION ─────────────────────────────────────────────
IS_ANDROID = os.path.exists('/storage/emulated/0')
BASE_DIR   = '/storage/emulated/0/Anon' if IS_ANDROID else os.path.join(os.path.expanduser('~'), 'Downloads', 'Anon')
LOG_FILE   = os.path.join(BASE_DIR, '.download_history.json')

# ─── TOOL AVAILABILITY ────────────────────────────────────────
HAS_ARIA2C = shutil.which('aria2c') is not None
HAS_YTDLP  = shutil.which('yt-dlp') is not None
HAS_FFMPEG = shutil.which('ffmpeg') is not None

# ─── PARALLEL DOWNLOADS ───────────────────────────────────────
PARALLEL_COUNT = 1  # default, user selects at startup

# ─── PRINT LOCK for clean parallel output ─────────────────────
PRINT_LOCK       = threading.Lock()
PAUSED           = False
STOP             = False
CURRENT_PROCESS  = [None]  # tracks active aria2c/yt-dlp subprocess
_CTRL_C_COUNT    = [0]     # global so wait_if_paused() can reset it after resume

def safe_print(*args, **kwargs):
    with PRINT_LOCK:
        print(*args, **kwargs)

def kill_current_process():
    """Kill the currently running download process."""
    proc = CURRENT_PROCESS[0]
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            time.sleep(0.5)
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        CURRENT_PROCESS[0] = None

def setup_signal_handler():
    """
    Handle Ctrl+C cleanly:
    - First Ctrl+C: kills active download process, pauses
    - Second Ctrl+C (including while paused): exits completely
    Counter resets to 0 on resume so user can pause multiple times.
    """
    global PAUSED, STOP

    def handler(sig, frame):
        _CTRL_C_COUNT[0] += 1
        if _CTRL_C_COUNT[0] == 1:
            kill_current_process()
            PAUSED = True
            safe_print("\n[⏸] Download paused — press Enter to continue with next episode")
            safe_print("[⏸] Press Ctrl+C again to exit completely")
        else:
            STOP = True
            PAUSED = False
            kill_current_process()
            safe_print("\n[✗] Exiting...")
            sys.exit(0)

    signal.signal(signal.SIGINT, handler)

def wait_if_paused():
    """Block until user presses Enter after a Ctrl+C pause."""
    global PAUSED
    if not PAUSED:
        return
    # Flush stdin — aria2c/yt-dlp may have left chars in the buffer when killed,
    # which would cause input() to return immediately and auto-resume.
    try:
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass
    # Loop: if input() returns unexpectedly (stale buffer read or signal interrupt),
    # keep waiting until the user actually presses Enter or a second Ctrl+C exits.
    while PAUSED and not STOP:
        try:
            input()
            PAUSED = False
            _CTRL_C_COUNT[0] = 0  # reset so user can pause again later in the session
            safe_print("[▶] Continuing...")
            return
        except EOFError:
            # stdin was closed/redirected — just resume silently
            PAUSED = False
            _CTRL_C_COUNT[0] = 0
            return
        except Exception:
            time.sleep(0.1)  # interrupted but not by a valid keypress — retry

# ─── RESUME STATE ─────────────────────────────────────────────
RESUME_FILE = os.path.join(BASE_DIR, '.resume_state.json')

def load_resume_state():
    """Load all paused series states."""
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        if os.path.exists(RESUME_FILE):
            with open(RESUME_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_resume_state(state):
    """Save resume state to central file."""
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(RESUME_FILE, 'w') as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass

def mark_episode_done(series_url, series_name, ep_filename):
    """Mark an episode as successfully downloaded."""
    state = load_resume_state()
    key = series_url
    if key not in state:
        state[key] = {'name': series_name, 'done': [], 'failed': [], 'current': None}
    if ep_filename not in state[key]['done']:
        state[key]['done'].append(ep_filename)
    state[key]['current'] = None
    save_resume_state(state)

def mark_episode_current(series_url, series_name, ep_filename):
    """Mark episode as currently downloading."""
    state = load_resume_state()
    key = series_url
    if key not in state:
        state[key] = {'name': series_name, 'done': [], 'failed': [], 'current': None}
    state[key]['current'] = ep_filename
    state[key]['name'] = series_name
    save_resume_state(state)

def mark_series_complete(series_url):
    """Remove series from resume state when fully done."""
    state = load_resume_state()
    if series_url in state:
        del state[series_url]
        save_resume_state(state)

def is_episode_done_in_state(series_url, ep_filename):
    """Check if episode was already downloaded in a previous session."""
    state = load_resume_state()
    if series_url in state:
        return ep_filename in state[series_url].get('done', [])
    return False

def show_resume_list():
    """Show all paused/incomplete series."""
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

def handle_resume_command(session):
    """Handle resume command — show paused series and let user pick."""
    state = load_resume_state()
    if not state:
        print("[*] No paused downloads to resume")
        return

    entries = list(state.items())
    show_resume_list()

    if len(entries) == 1:
        url = entries[0][0]
        name = entries[0][1].get('name', 'Unknown')
        print(f"\n[*] Resuming: {name}")
        process_link_queue([url], session)
    else:
        print(f"\nPick a series to resume (1-{len(entries)}) or 0 to cancel:")
        try:
            choice = int(input("> ").strip())
            if 1 <= choice <= len(entries):
                url  = entries[choice-1][0]
                name = entries[choice-1][1].get('name', 'Unknown')
                print(f"[*] Resuming: {name}")
                process_link_queue([url], session)
            else:
                print("[*] Cancelled")
        except (ValueError, EOFError):
            print("[*] Cancelled")

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

def log_download(name, url, filepath):
    history = load_history()
    if name not in history:
        history[name] = []
    entry = {'url': url, 'file': filepath, 'time': time.strftime('%Y-%m-%d %H:%M')}
    if entry not in history[name]:
        history[name].append(entry)
    save_history(history)

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

# ─── DISK SPACE CHECK ─────────────────────────────────────────
def check_disk_space(min_gb=1.0):
    try:
        if not hasattr(os, 'statvfs'):
            return  # Windows — skip
        stat = os.statvfs(BASE_DIR if IS_ANDROID else os.path.expanduser('~'))
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024**3)
        if free_gb < min_gb:
            safe_print(f"[!] Low disk space: {free_gb:.1f}GB free. Downloads may fail.")
        else:
            safe_print(f"[✓] Disk space: {free_gb:.1f}GB free")
    except Exception:
        pass

# ─── AUTO UPDATE ──────────────────────────────────────────────
def auto_update():
    """Silent git pull in background on startup."""
    def _pull():
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            result = subprocess.run(
                ['git', 'pull'],
                cwd=script_dir,
                capture_output=True,
                text=True,
                timeout=15
            )
            if 'Already up to date' not in result.stdout and result.returncode == 0:
                safe_print(f"[✓] Auto-updated to latest version")
        except Exception:
            pass
    threading.Thread(target=_pull, daemon=True).start()

# ─── ANDROID SETUP ────────────────────────────────────────────
def setup_android():
    if not IS_ANDROID:
        return
    if shutil.which('termux-wake-lock'):
        try:
            subprocess.Popen(['termux-wake-lock'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("[✓] Wake lock enabled — screen can go off safely")
        except Exception as e:
            print(f"[!] Wake lock failed: {e}")
    else:
        print("[!] termux-wake-lock not found — install with: pkg install termux-api")
    if not os.environ.get('TMUX'):
        if shutil.which('tmux'):
            print("[*] Starting fresh tmux session...")
            try:
                # Kill any leftover session so reopening Termux always
                # starts clean — runs .bashrc, pulls updates, fresh state.
                subprocess.run(['tmux', 'kill-session', '-t', 'download'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                os.execvp('tmux', ['tmux', 'new-session', '-s', 'download', sys.executable] + sys.argv)
            except Exception as e:
                print(f"[!] Could not start tmux: {e}")
        else:
            print("[!] tmux not found — install with: pkg install tmux")

# ─── STARTUP SETTINGS ─────────────────────────────────────────
QUALITY_MAP = {
    '1': ('360p',  'bestvideo[height<=360]+bestaudio/best[height<=360]'),
    '2': ('480p',  'bestvideo[height<=480]+bestaudio/best[height<=480]'),
    '3': ('720p',  'bestvideo[height<=720]+bestaudio/best[height<=720]'),
    '4': ('1080p', 'bestvideo[height<=1080]+bestaudio/best[height<=1080]'),
}
SELECTED_QUALITY = ('480p', 'bestvideo[height<=480]+bestaudio/best[height<=480]')
BANDWIDTH_LIMIT  = 0  # 0 = no limit, in KB/s

CONFIG_FILE = os.path.join(BASE_DIR, '.config.json')

DEFAULT_CONFIG = {
    'quality': '480p',
    'parallel': 2,
    'bandwidth': 0,
}

def load_config():
    """Load settings from config file, use defaults if not found."""
    global SELECTED_QUALITY, PARALLEL_COUNT, BANDWIDTH_LIMIT
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
        else:
            cfg = DEFAULT_CONFIG
            save_config(cfg)
    except Exception:
        cfg = DEFAULT_CONFIG

    # Apply quality
    q = cfg.get('quality', '480p')
    for label, fmt in QUALITY_MAP.values():
        if label == q:
            SELECTED_QUALITY = (label, fmt)
            break

    PARALLEL_COUNT = int(cfg.get('parallel', 2))
    BANDWIDTH_LIMIT = int(cfg.get('bandwidth', 0))

def save_config(cfg=None):
    """Save current settings to config file."""
    if cfg is None:
        q_label = SELECTED_QUALITY[0] if SELECTED_QUALITY else '480p'
        cfg = {
            'quality': q_label,
            'parallel': PARALLEL_COUNT,
            'bandwidth': BANDWIDTH_LIMIT,
        }
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

def show_settings():
    """Display current settings."""
    q = SELECTED_QUALITY[0] if SELECTED_QUALITY else '480p'
    bw = f"{BANDWIDTH_LIMIT} KB/s" if BANDWIDTH_LIMIT > 0 else "unlimited"
    print(f"\n  Current settings:")
    print(f"  • quality   = {q}  (streaming sites)")
    print(f"  • parallel  = {PARALLEL_COUNT} download(s) at once")
    print(f"  • bandwidth = {bw}")
    print(f"\n  To change: settings quality 720p | settings parallel 2 | settings bandwidth 500")

def handle_settings_command(cmd):
    """Handle settings command — e.g. 'settings quality 720p'"""
    global SELECTED_QUALITY, PARALLEL_COUNT, BANDWIDTH_LIMIT
    parts = cmd.strip().split()
    if len(parts) == 1:
        show_settings()
        return
    if len(parts) < 3:
        print("  Usage: settings quality 720p | settings parallel 2 | settings bandwidth 500")
        return
    setting = parts[1].lower()
    value = parts[2].lower()
    if setting == 'quality':
        found = False
        for label, fmt in QUALITY_MAP.values():
            if label == value:
                SELECTED_QUALITY = (label, fmt)
                found = True
                break
        if found:
            print(f"  [✓] Quality set to {value}")
            save_config()
        else:
            print(f"  [!] Valid options: 360p, 480p, 720p, 1080p")
    elif setting == 'parallel':
        try:
            n = int(value)
            if 1 <= n <= 3:
                PARALLEL_COUNT = n
                print(f"  [✓] Parallel downloads set to {n}")
                save_config()
            else:
                print("  [!] Valid options: 1, 2, 3")
        except ValueError:
            print("  [!] Enter a number: 1, 2, or 3")
    elif setting == 'bandwidth':
        try:
            n = int(value)
            BANDWIDTH_LIMIT = n
            bw = f"{n} KB/s" if n > 0 else "unlimited"
            print(f"  [✓] Bandwidth limit set to {bw}")
            save_config()
        except ValueError:
            print("  [!] Enter KB/s number (0 = unlimited)")
    else:
        print("  [!] Unknown setting. Use: quality, parallel, bandwidth")

def ask_startup_settings():
    """No longer shows prompts — loads from config file."""
    load_config()

# ─── TOOL INSTALLERS ──────────────────────────────────────────
def install_aria2c():
    global HAS_ARIA2C
    print("[*] Installing aria2...")
    try:
        if IS_ANDROID:
            env = os.environ.copy()
            env['DEBIAN_FRONTEND'] = 'noninteractive'
            subprocess.run(['pkg', 'install', 'aria2', '-y'], check=True, env=env)
        elif platform.system() == 'Windows':
            print("[!] Install aria2 manually from https://github.com/aria2/aria2/releases")
            return False
        else:
            subprocess.run(['sudo', 'apt', 'install', 'aria2', '-y'], check=True)
        HAS_ARIA2C = True
        print("[✓] aria2 installed")
        return True
    except Exception as e:
        print(f"[!] Failed to install aria2: {e}")
        return False

def install_ytdlp():
    global HAS_YTDLP
    print("[*] Installing yt-dlp...")
    try:
        subprocess.run([sys.executable, '-m', 'pip', 'install', 'yt-dlp', '--break-system-packages', '-q'], check=True)
        HAS_YTDLP = True
        print("[✓] yt-dlp installed")
        return True
    except Exception as e:
        print(f"[!] Failed to install yt-dlp: {e}")
        return False

# ─── SESSION FACTORY ──────────────────────────────────────────
def make_session(mobile=False):
    s = requests.Session()
    s.headers.update({'User-Agent': UA_MOBILE if mobile else UA_DESKTOP})
    return s

def make_cf_session():
    if HAS_CURL_CFFI:
        return cf_requests.Session(impersonate='chrome120')
    return None

def make_best_session(mobile=False):
    """
    Cloudflare bypass as default — uses curl_cffi if available,
    falls back to regular requests. This handles Cloudflare-protected
    sites transparently without any extra configuration.
    """
    if HAS_CURL_CFFI:
        s = cf_requests.Session(impersonate='chrome120')
        return s
    return make_session(mobile)

# ─── HELPERS ──────────────────────────────────────────────────
def safe_get(session, url, timeout=20, referer=None, retries=3):
    for attempt in range(retries):
        try:
            # curl_cffi sessions accept headers= just like requests
            # but we update separately to avoid issues with some versions
            if referer:
                session.headers['Referer'] = referer
            r = session.get(url, timeout=timeout)
            return r
        except Exception as e:
            safe_print(f"  [!] Attempt {attempt+1}/{retries} failed: {e}")
            if attempt < retries - 1:
                time.sleep(2)
    return None

def find_direct_video(text):
    for ext in [r'\.m3u8', r'\.mp4', r'\.mkv']:
        found = re.findall(r'https?://[^\s"\'<>,\\]+' + ext + r'[^\s"\'<>,\\]*', text)
        if found:
            return found[0].rstrip('.,;)')
    return None

def clean_name(slug):
    name = re.sub(r'[-_]+', ' ', slug)
    name = re.sub(r'\s+', ' ', name).strip()
    return name.title()

def safe_filename(name):
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = re.sub(r'\s+', ' ', name)
    name = name.strip().rstrip('.')
    return name

def clean_ep_name(raw):
    name = re.sub(r'\([\w\s]+p\)', '', raw)
    name = re.sub(r'\[[\w\s]+\]', '', name)
    name = re.sub(r'download', '', name, flags=re.IGNORECASE)
    name = re.sub(r'[-–|]+', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name or raw

def is_streaming_link(url):
    return '.m3u8' in url or 'manifest' in url.lower()

def base_domain(url):
    m = re.search(r'(https?://[^/]+)', url)
    return m.group(1) if m else ''

def check_url_alive(url, session):
    """
    Check if a download URL is still valid before downloading.
    Returns: 'ok', 'expired', or 'unknown'
    """
    try:
        r = session.head(url, timeout=10, allow_redirects=True)
        if r.status_code in (403, 404, 410):
            return 'expired'
        if r.status_code == 200:
            return 'ok'
        return 'unknown'
    except Exception:
        return 'unknown'

def diagnose_page(soup, url, expected_pattern=None):
    """
    Auto site structure detection — when extraction fails,
    print all links found grouped by domain so we can see what changed.
    """
    safe_print(f"\n[!] STRUCTURE DIAGNOSTIC for: {url[:60]}")
    safe_print(f"[!] Expected pattern: {expected_pattern or 'unknown'}")

    # Group all links by domain
    domain_links = {}
    for a in soup.find_all('a', href=True):
        href = a['href']
        if href.startswith('http'):
            dom = base_domain(href)
        elif href.startswith('/'):
            dom = '[relative]'
        else:
            continue
        if dom not in domain_links:
            domain_links[dom] = []
        domain_links[dom].append(href)

    safe_print(f"[!] Links found by domain:")
    for dom, links in sorted(domain_links.items(), key=lambda x: -len(x[1])):
        safe_print(f"  {dom}: {len(links)} links")
        for l in links[:3]:
            safe_print(f"    • {l[:80]}")
    safe_print(f"[!] Report this output if the site has changed structure")

def get_free_space_gb():
    try:
        if not hasattr(os, 'statvfs'):
            return 999  # Windows — assume enough space
        stat = os.statvfs(BASE_DIR if IS_ANDROID else os.path.expanduser('~'))
        return (stat.f_bavail * stat.f_frsize) / (1024**3)
    except Exception:
        return 999

# ─── DOWNLOAD SUMMARY ─────────────────────────────────────────
class DownloadSummary:
    def __init__(self):
        self.success  = 0
        self.skipped  = 0
        self.failed   = 0
        self._lock    = threading.Lock()
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

# ─── DOWNLOADER ───────────────────────────────────────────────
def already_downloaded(folder, filename):
    """Exact filename match — no prefix matching."""
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

def get_referer_for_url(url):
    if 'vikingfile.com' in url or 'vkng' in url:
        return 'https://vikingfile.com/'
    if 'kissorgrab.com' in url:
        return 'https://plutomovies.com/'
    if 'kwik.cx' in url or 'animepahe' in url:
        return 'https://anitaku.com.ro/'
    return base_domain(url) + '/'

def download_with_aria2c(url, folder, filename, summary, retries=3):
    global BANDWIDTH_LIMIT
    if not HAS_ARIA2C:
        if not install_aria2c():
            safe_print("[!] aria2c unavailable — falling back to requests")
            return download_with_requests(url, folder, filename, summary)

    os.makedirs(folder, exist_ok=True)
    safe_fname = re.sub(r'[^\w]', '_', filename)[:30]
    session_file = os.path.join(folder, f'.aria2_{safe_fname}.txt')
    filepath = os.path.join(folder, filename)
    referer = get_referer_for_url(url)

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
                '-x', '16',
                '-s', '16',
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
            if BANDWIDTH_LIMIT > 0:
                cmd += ['--max-download-limit', f'{BANDWIDTH_LIMIT}K']
            cmd.append(url)

            # Track process so Ctrl+C can kill it.
            # stdin=DEVNULL prevents aria2c from inheriting the terminal stdin,
            # which avoids leaving stray chars in the buffer when it's killed.
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
            CURRENT_PROCESS[0] = proc
            proc.wait()
            result_code = proc.returncode
            CURRENT_PROCESS[0] = None
            if result_code == 0:
                if os.path.exists(filepath):
                    size = os.path.getsize(filepath)
                    size_mb = size / (1024 * 1024)
                    if size < 1024 * 100:
                        safe_print(f"  [✗] File too small ({size_mb:.2f}MB) — likely error page")
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
                safe_print(f"  [✗] aria2c failed (code {result_code})")
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

def download_with_requests(url, folder, filename, summary):
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
        content_type = r.headers.get('content-type', '')
        if 'text/html' in content_type:
            safe_print(f"  [!] Got HTML instead of video")
            summary.add_failed(filename)
            return False
        total = int(r.headers.get('content-length', 0))
        downloaded = 0
        start_time = time.time()
        with open(filepath, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024 * 512):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded * 100 // total
                        mb_done = downloaded / (1024 * 1024)
                        mb_total = total / (1024 * 1024)
                        elapsed = time.time() - start_time
                        speed = (downloaded / elapsed / 1024 / 1024) if elapsed > 0 else 0
                        eta = int((total - downloaded) / (downloaded / elapsed)) if downloaded > 0 else 0
                        safe_print(f"\r  [↓] {pct}% — {mb_done:.1f}/{mb_total:.1f}MB — {speed:.1f}MB/s — ETA {eta}s", end='', flush=True)
        safe_print()
        if not os.path.exists(filepath) or os.path.getsize(filepath) < 1024 * 100:
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

def download_with_ytdlp(url, folder, filename, summary):
    if not HAS_YTDLP:
        if not install_ytdlp():
            safe_print(f"  [!] yt-dlp unavailable")
            summary.add_failed(filename)
            return False
    if not HAS_FFMPEG:
        safe_print(f"  [!] ffmpeg not found — install with: pkg install ffmpeg")
        summary.add_failed(filename)
        return False
    os.makedirs(folder, exist_ok=True)
    base = re.sub(r'\.(mp4|mkv|m3u8)$', '', filename)
    out_template = os.path.join(folder, base + '.%(ext)s')
    quality_label, format_str = SELECTED_QUALITY
    safe_print(f"  [↓] yt-dlp ({quality_label}): {filename}")
    try:
        cmd = [
            'yt-dlp',
            '-f', format_str,
            '--merge-output-format', 'mp4',
            '-o', out_template,
            '--no-playlist',
            '--retries', 'infinite',
            '--fragment-retries', 'infinite',
            '--retry-sleep', '10',
            '--quiet',
            '--no-warnings',
            '--progress',
            '--newline',
        ]
        if HAS_ARIA2C:
            cmd += [
                '--external-downloader', 'aria2c',
                '--external-downloader-args',
                'aria2c:-x 16 -s 16 -c --max-tries=0 --retry-wait=30 --timeout=120 --connect-timeout=60 --file-allocation=none --min-split-size=1M'
            ]
        cmd.append(url)
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        CURRENT_PROCESS[0] = proc
        proc.wait()
        result_code = proc.returncode
        CURRENT_PROCESS[0] = None
        if result_code == 0:
            # Find the downloaded file
            final_file = None
            for ext in ['mp4', 'mkv', 'webm']:
                p = os.path.join(folder, f"{base}.{ext}")
                if os.path.exists(p):
                    final_file = p
                    break
            if final_file:
                size_mb = os.path.getsize(final_file) / (1024 * 1024)
                safe_print(f"  [✓] Done: {filename} ({size_mb:.1f}MB)")
                summary.add_success()
                log_download(filename, url, final_file)
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

def download_file(url, folder, filename, summary,
                  check_expiry=True, series_url=None, series_name=None):
    """
    Smart downloader with resume state tracking.
    series_url/series_name: if provided, tracks progress for resume-after-restart.
    """
    done, _ = already_downloaded(folder, filename)
    if done:
        safe_print(f"  [✓] Already downloaded — skipping")
        summary.add_skipped()
        # Mark done in state too
        if series_url:
            mark_episode_done(series_url, series_name or folder, filename)
        return True

    # Check resume state — was this done in a previous session?
    if series_url and is_episode_done_in_state(series_url, filename):
        safe_print(f"  [✓] Done in previous session — skipping")
        summary.add_skipped()
        return True

    # Link expiry detection
    if check_expiry and not is_streaming_link(url):
        _check_s = make_session()
        status = check_url_alive(url, _check_s)
        if status == 'expired':
            safe_print(f"  [!] Link expired (403/404) — re-paste the series URL for fresh links")
            summary.add_failed(filename)
            return False

    # Pause/resume check
    wait_if_paused()
    if STOP:
        return False

    # Mark as currently downloading
    if series_url:
        mark_episode_current(series_url, series_name or folder, filename)

    if is_streaming_link(url):
        result = download_with_ytdlp(url, folder, filename, summary)
    else:
        result = download_with_aria2c(url, folder, filename, summary)

    # Update state based on result
    if result and series_url:
        mark_episode_done(series_url, series_name or folder, filename)

    return result

# ─── PREFETCH SYSTEM ──────────────────────────────────────────
class Prefetcher:
    """
    Pre-fetches the next episode download link while current is downloading.
    When current reaches ~90%, starts fetching next link in background.
    Zero gap between episodes.
    """
    def __init__(self, fetch_fn):
        self.fetch_fn   = fetch_fn   # function to call to get next link
        self._result    = None
        self._thread    = None
        self._ready     = threading.Event()

    def prefetch(self, *args, **kwargs):
        """Start fetching next link in background."""
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
        """Get the prefetched result, waiting if not ready yet."""
        self._ready.wait(timeout=timeout)
        return self._result

# ─── PARALLEL DOWNLOAD MANAGER ────────────────────────────────
def download_batch(items, folder, summary, series_url=None, series_name=None):
    """
    Download a list of (url, filename) tuples in parallel.
    items: list of (url, filename)
    Uses PARALLEL_COUNT threads.
    series_url/series_name: forwarded to download_file for resume state tracking.
    """
    if not items:
        return

    if PARALLEL_COUNT == 1:
        for url, filename in items:
            download_file(url, folder, filename, summary,
                         series_url=series_url, series_name=series_name)
    else:
        with ThreadPoolExecutor(max_workers=PARALLEL_COUNT) as executor:
            futures = {
                executor.submit(download_file, url, folder, filename, summary,
                               True, series_url, series_name): filename
                for url, filename in items
            }
            for future in as_completed(futures):
                fname = futures[future]
                try:
                    future.result()
                except Exception as e:
                    safe_print(f"  [!] Thread error for {fname}: {e}")
                    summary.add_failed(fname)

# ─── FILE HOST RESOLVERS ──────────────────────────────────────

def resolve_downloadwella(url, session):
    try:
        r = safe_get(session, url, timeout=20)
        if not r:
            return None
        soup = BeautifulSoup(r.text, 'html.parser')
        form = soup.find('form')
        if not form:
            return None
        data = {inp.get('name'): inp.get('value', '')
                for inp in form.find_all('input') if inp.get('name')}
        data['method_free'] = 'Free Download'
        r2 = session.post(url, data=data, timeout=20)
        return find_direct_video(r2.text)
    except Exception as e:
        safe_print(f"  [!] Downloadwella: {e}")
        return None

def resolve_loadedfiles(url, session):
    try:
        r1 = safe_get(session, url, referer='https://9jarocks.net/')
        if not r1:
            return None
        m1 = re.search(r"var downloadUrl = '(https://loadedfiles\.org/[^']+)'", r1.text)
        if not m1:
            return None
        r2 = safe_get(session, m1.group(1), referer='https://loadedfiles.org/')
        if not r2:
            return None
        m2 = re.search(r"var downloadUrl = '(https://loadedfiles\.org/[^']+)'", r2.text)
        if not m2:
            return None
        try:
            r3 = session.get(m2.group(1), timeout=20, allow_redirects=False)
            return r3.headers.get('location')
        except Exception as e:
            safe_print(f"  [!] Loadedfiles redirect: {e}")
            return None
    except Exception as e:
        safe_print(f"  [!] Loadedfiles: {e}")
        return None

def resolve_wildshare(url):
    if not HAS_CURL_CFFI:
        safe_print("  [!] Wildshare requires curl_cffi — pip install curl_cffi --break-system-packages")
        return None
    try:
        s = make_cf_session()
        if not s:
            return None
        r = s.get(url, timeout=20)
        if not r or r.status_code != 200:
            return None
        pt = re.search(r'pt=([A-Za-z0-9%+=/]+)', r.text)
        if not pt:
            return None
        parts = url.rstrip('/').split('/')
        file_id = next((p for p in reversed(parts) if not p.endswith(('.mkv', '.mp4', '.m3u8'))), parts[-1])
        pt_url = f'https://wildshare.net/{file_id}?{pt.group(0)}'
        r2 = s.get(pt_url, timeout=20, allow_redirects=False)
        return r2.headers.get('location')
    except Exception as e:
        safe_print(f"  [!] Wildshare: {e}")
        return None

def resolve_streamtape(url, session):
    try:
        r = safe_get(session, url, referer='https://watchadsontape.com/')
        if not r or r.status_code == 404:
            return None
        for line in r.text.split('\n'):
            if "getElementById('robotlink')" in line and 'substring' in line:
                m = re.search(r"innerHTML\s*=\s*'([^']+)'\s*\+\s*\('([^']+)'\)", line.strip())
                if m:
                    base, raw = m.group(1), m.group(2)
                    for n in re.findall(r'\.substring\((\d+)\)', line):
                        raw = raw[int(n):]
                    get_url = 'https:' + base + raw
                    r2 = session.get(get_url, timeout=20, allow_redirects=False)
                    loc = r2.headers.get('location')
                    if loc:
                        return loc
        v = find_direct_video(r.text)
        if v:
            return v
        return None
    except Exception as e:
        safe_print(f"  [!] Streamtape: {e}")
        return None

def resolve_vidmoly(embed_url, session):
    try:
        r = safe_get(session, embed_url, referer='https://myasiantv9.com.ro/')
        if not r:
            return None
        m3u8 = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', r.text)
        if m3u8:
            return m3u8[0]
        mp4 = re.findall(r'https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*', r.text)
        if mp4:
            return mp4[0]
        return None
    except Exception as e:
        safe_print(f"  [!] Vidmoly: {e}")
        return None

VIDBASIC_BLOCKED   = ['asianload', 'dood', 'streamvid']
VIDBASIC_PREFERRED = ['watchadsontape.com', 'streamtape']

def resolve_vidbasic(embed_url, session):
    BLOCKED_HOSTS   = VIDBASIC_BLOCKED
    PREFERRED_HOSTS = VIDBASIC_PREFERRED
    for attempt in range(2):
        try:
            r = safe_get(session, embed_url, referer='https://myasiantv9.com.ro/')
            if not r:
                continue
            raw_servers = re.findall(r'data-video="(https?://[^"]+)"', r.text)
            servers = [u for u in raw_servers if not any(h in u for h in BLOCKED_HOSTS)]
            if not servers:
                safe_print(f"  [!] No usable servers (attempt {attempt+1})")
                time.sleep(3)
                continue
            ordered = sorted(servers, key=lambda u: 0 if any(h in u for h in PREFERRED_HOSTS) else 1)
            for sv_url in ordered:
                safe_print(f"    [>] Trying: {sv_url[:60]}...")
                if 'watchadsontape.com' in sv_url or 'streamtape' in sv_url:
                    result = resolve_streamtape(sv_url, session)
                    if result:
                        return result
                else:
                    try:
                        r2 = safe_get(session, sv_url, referer=embed_url, timeout=15)
                        if r2:
                            v = find_direct_video(r2.text)
                            if v:
                                return v
                    except Exception as e:
                        safe_print(f"    [!] Server error: {e}")
                        continue
            v = find_direct_video(r.text)
            if v:
                return v
        except Exception as e:
            safe_print(f"  [!] Vidbasic attempt {attempt+1}: {e}")
            time.sleep(3)
    return None

def resolve_embed(src, session):
    if 'vidmoly' in src:
        return resolve_vidmoly(src, session)
    elif 'vidbasic' in src:
        return resolve_vidbasic(src, session)
    else:
        safe_print(f"    [>] Unknown embed, trying generic: {src[:60]}...")
        r = safe_get(session, src)
        return find_direct_video(r.text) if r else None

def resolve_drip_waffi(url, session):
    try:
        referer = 'https://dramakey.cc/' if 'dramakey.cc' in url else 'https://dramarain.com/'
        r = safe_get(session, url, referer=referer)
        if not r:
            return None
        # Pattern 1: JS redirect
        m = re.search(r'window\.location\.href\s*=\s*"([^"]+)"', r.text)
        if m:
            return m.group(1)
        # Pattern 2: already a drip link
        if 'drip.waffi.cloud' in url:
            return url
        # Pattern 3: drip link embedded in page
        m2 = re.search(r'https://drip[.]waffi[.]cloud/\S+', r.text)
        if m2:
            return m2.group(0)
        return None
    except Exception as e:
        safe_print(f"  [!] Drip: {e}")
        return None

def resolve_vikingfile(url, session):
    """
    Resolve a vikingfile.com URL to the actual CDN download URL.
    
    Two formats exist:
    - Old: vikingfile.com/{long-id}  — 2-hop redirect to CDN
    - New: vikingfile.com/f/{id}     — may redirect OR serve a landing page
                                       with the CDN link embedded in HTML
    
    Always uses a plain requests session (not curl_cffi) for reliable
    allow_redirects=False behaviour.
    """
    try:
        # Use a plain requests session — curl_cffi may not honour allow_redirects=False
        s = requests.Session()
        s.headers.update({
            'User-Agent': UA_DESKTOP,
            'Referer': 'https://www.naijavault.com/',
        })

        # ── Hop 1 ──────────────────────────────────────────────────────
        for attempt in range(3):
            try:
                r1 = s.get(url, timeout=15, allow_redirects=False)
                break
            except Exception:
                if attempt < 2:
                    time.sleep(2)
                else:
                    raise

        loc1 = r1.headers.get('location')

        if loc1:
            # Classic redirect path — follow hop 2
            for attempt in range(3):
                try:
                    r2 = s.get(loc1, timeout=15, allow_redirects=False)
                    break
                except Exception:
                    if attempt < 2:
                        time.sleep(2)
                    else:
                        raise
            loc2 = r2.headers.get('location')
            if loc2:
                return loc2   # 2-hop redirect → CDN URL
            # loc1 itself might already be the CDN URL
            if any(x in loc1 for x in ['.mp4', '.mkv', 'cdn', 'download']):
                return loc1
            # loc1 is another page — scan it for a CDN link
            cdn = find_direct_video(r2.text)
            if cdn:
                return cdn
            return loc1

        # ── No redirect — new /f/{id} landing page ─────────────────────
        # Page has the CDN link embedded as a direct download anchor or
        # in a script tag. Scan for it.
        if r1.status_code == 200:
            # Try following with allow_redirects=True — some /f/ URLs
            # redirect on second request after a cookie is set
            r1b = s.get(url, timeout=15, allow_redirects=True)
            final_url = r1b.url
            if final_url != url and any(x in final_url for x in ['.mp4', '.mkv', 'cdn', 'download']):
                return final_url

            # Scan page text for direct video link
            cdn = find_direct_video(r1b.text)
            if cdn:
                return cdn

            # Scan for any CDN-looking URL
            for pattern in [
                r'https?://[^\s"\'<>]*cdn[^\s"\'<>]*\.(?:mp4|mkv)',
                r'https?://[^\s"\'<>]+\.(?:mp4|mkv)\b',
                r'"(https?://[^\s"\'<>]+(?:download|file)[^\s"\'<>]*)"',
            ]:
                m = re.search(pattern, r1b.text, re.IGNORECASE)
                if m:
                    return m.group(0).strip('"')

        safe_print(f"  [!] VikingFile: could not resolve {url[:60]}")
        return None
    except Exception as e:
        safe_print(f"  [!] VikingFile: {e}")
        return None

# ─── SHARED DOWNLOADWELLA EXTRACTOR ───────────────────────────
def _extract_downloadwella_site(url, session, site_label, name_cleaner):
    safe_print(f"[*] {site_label} mode")
    slug = url.rstrip('/').split('/')[-1]
    name = name_cleaner(slug)
    name = clean_name(name)
    safe_print(f"[*] Series: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))
    r = safe_get(session, url)
    if not r:
        return
    soup = BeautifulSoup(r.text, 'html.parser')
    links = list(dict.fromkeys(
        a['href'] for a in soup.find_all('a', href=True)
        if 'downloadwella.com' in a['href']
    ))
    if not links:
        safe_print(f"[!] No downloadwella links found on page")
        diagnose_page(soup, url, expected_pattern="downloadwella.com links")
        return
    safe_print(f"[*] Found {len(links)} episode(s) — saving to: {folder}")
    summary = DownloadSummary()

    # Use prefetcher — fetch next link while current downloads
    pf = Prefetcher(resolve_downloadwella)
    next_direct = [None]

    for i, ep_url in enumerate(links, 1):
        if STOP:
            break
        wait_if_paused()

        ep_name = ep_url.split('/')[-1].replace('.html', '')
        safe_print(f"\n[{i}/{len(links)}] {ep_name}")

        # Get direct link — use prefetched if available
        if next_direct[0] is not None:
            direct = next_direct[0]
            next_direct[0] = None
        else:
            direct = resolve_downloadwella(ep_url, session)

        # Prefetch next episode link in background
        if i < len(links):
            pf.prefetch(links[i], session)

        if direct:
            ext = 'mkv' if '.mkv' in direct else 'mp4'
            fname = safe_filename(f"{ep_name}.{ext}")
            download_file(direct, folder, fname, summary,
                         series_url=url, series_name=name)
            # Collect prefetched result after download starts
            if i < len(links):
                next_direct[0] = pf.get(timeout=60)
        else:
            safe_print(f"  [✗] Could not extract link")
            summary.add_failed(ep_name)
            if i < len(links):
                next_direct[0] = pf.get(timeout=60)

    # Mark series complete only if fully done (not user-aborted, no failures)
    if summary.failed == 0 and not STOP:
        mark_series_complete(url)
    summary.report()

# ─── SITE EXTRACTORS ──────────────────────────────────────────

def extract_nkiri(url, session):
    _extract_downloadwella_site(
        url, session,
        site_label='NKIRI/Thenkiri',
        name_cleaner=lambda s: re.sub(r'-s\d+.*$', '', s, flags=re.IGNORECASE)
    )

def extract_dramakey_com(url, session):
    def cleaner(s):
        s = re.sub(r'-s\d+.*$', '', s, flags=re.IGNORECASE)
        s = re.sub(r'-(season|episode|complete).*$', '', s, flags=re.IGNORECASE)
        return s
    _extract_downloadwella_site(url, session, site_label='DramaKey.com', name_cleaner=cleaner)

def extract_9jarocks(url, session):
    safe_print("[*] 9jaRocks mode")
    slug = url.rstrip('/').split('/')[-1]
    name = re.sub(r'-id\d+.*$', '', slug)
    name = clean_name(name)
    safe_print(f"[*] Title: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))
    r = safe_get(session, url, referer='https://9jarocks.net/')
    if not r:
        return
    soup = BeautifulSoup(r.text, 'html.parser')
    lf_links = list(dict.fromkeys(
        a['href'] for a in soup.find_all('a', href=True)
        if 'loadedfiles.org' in a['href']
    ))
    safe_print(f"[*] Found {len(lf_links)} file(s) — saving to: {folder}")
    summary = DownloadSummary()
    for i, lf_url in enumerate(lf_links, 1):
        if STOP:
            break
        wait_if_paused()
        fname = lf_url.split('/')[-1][:60]
        safe_print(f"\n[{i}/{len(lf_links)}] {fname}")
        direct = resolve_loadedfiles(lf_url, session)
        if direct:
            ext = 'mkv' if '.mkv' in direct else 'mp4'
            download_file(direct, folder, safe_filename(f"{fname}.{ext}"), summary)
        else:
            safe_print(f"  [✗] Could not extract: {fname}")
            summary.add_failed(fname)
        time.sleep(0.5)
    summary.report()

def extract_naijaprey(url, session):
    safe_print("[*] NaijaPrey mode")
    slug = url.rstrip('/').split('/')[-1]
    name = clean_name(slug)
    safe_print(f"[*] Title: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))
    r = safe_get(session, url, referer='https://www.naijaprey.tv/')
    if not r:
        return
    soup = BeautifulSoup(r.text, 'html.parser')
    ep_links = list(dict.fromkeys(
        a['href'] for a in soup.find_all('a', href=True)
        if 'vdl.np-downloader.com' in a['href']
    ))
    safe_print(f"[*] Found {len(ep_links)} episode(s) — saving to: {folder}")
    summary = DownloadSummary()
    for i, ep_url in enumerate(ep_links, 1):
        if STOP:
            break
        wait_if_paused()
        ep_name = ep_url.rstrip('/').split('/')[-1]
        safe_print(f"\n[{i}/{len(ep_links)}] {ep_name}")
        try:
            r2 = safe_get(session, ep_url, referer='https://www.naijaprey.tv/')
            if not r2:
                summary.add_failed(ep_name)
                continue
            soup2 = BeautifulSoup(r2.text, 'html.parser')
            ws_url = next((a['href'] for a in soup2.find_all('a', href=True)
                          if 'wildshare.net' in a['href']), None)
            if ws_url:
                direct = resolve_wildshare(ws_url)
                if direct:
                    ext = 'mkv' if '.mkv' in direct else 'mp4'
                    download_file(direct, folder, safe_filename(f"{ep_name}.{ext}"), summary)
                else:
                    safe_print(f"  [✗] Wildshare failed")
                    summary.add_failed(ep_name)
            else:
                safe_print(f"  [!] No wildshare link found")
                summary.add_failed(ep_name)
        except Exception as e:
            safe_print(f"  [!] Error: {e}")
            summary.add_failed(ep_name)
        time.sleep(1)
    summary.report()

def extract_myasiantv(url, session):
    safe_print("[*] MyAsianTV mode")
    slug = url.rstrip('/').split('/')[-1]
    name = re.sub(r'-episode-\d+.*$', '', slug)
    name = re.sub(r'-\d{4}.*$', '', name)
    name = clean_name(name)
    safe_print(f"[*] Series: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))
    bd = base_domain(url)
    summary = DownloadSummary()
    if 'episode-' in url:
        ep_links = [url]
        safe_print(f"[*] Saving to: {folder}")
    else:
        safe_print("[*] Fetching episode list...")
        r = safe_get(session, url, referer=bd + '/', timeout=30)
        if not r:
            return
        soup = BeautifulSoup(r.text, 'html.parser')
        show_slug = re.sub(r'-\d{4}.*$', '', slug)
        ep_links = list(dict.fromkeys(
            a['href'] for a in soup.find_all('a', href=True)
            if ('episode-' in a['href'] and bd in a['href'] and show_slug in a['href'])
        ))
        if not ep_links:
            safe_print("[!] No episode links found")
            return
        ep_links.sort(key=lambda u: int(m.group(1)) if (m := re.search(r'episode-(\d+)', u)) else 0)
        safe_print(f"[*] Found {len(ep_links)} episode(s) — saving to: {folder}")
    for i, ep_url in enumerate(ep_links, 1):
        if STOP:
            break
        wait_if_paused()
        ep_name = ep_url.rstrip('/').split('/')[-1]
        safe_print(f"\n[{i}/{len(ep_links)}] {ep_name}")
        r = safe_get(session, ep_url, referer=bd + '/', timeout=30)
        if not r:
            safe_print(f"  [✗] Could not fetch episode page")
            summary.add_failed(ep_name)
            continue
        soup = BeautifulSoup(r.text, 'html.parser')
        iframe = soup.find('iframe', src=re.compile(r'vidbasic|vidmoly'))
        if not iframe:
            iframe = soup.find('iframe', src=True)
        if not iframe:
            safe_print(f"  [!] No iframe found")
            summary.add_failed(ep_name)
            continue
        src = iframe.get('src', '')
        if not src.startswith('http'):
            src = 'https:' + src
        direct = resolve_embed(src, session)
        if direct:
            download_file(direct, folder, safe_filename(f"{ep_name}.mp4"), summary)
        else:
            safe_print(f"  [✗] Could not extract video")
            summary.add_failed(ep_name)
        time.sleep(1)
    summary.report()

def extract_dramarain(url, session):
    site = 'DramaKey.cc' if 'dramakey.cc' in url else 'DramaRain'
    safe_print(f"[*] {site} mode")
    slug = url.rstrip('/').split('/')[-1]
    name = re.sub(r'-(chinese|korean|thai|japanese|drama|tvshows|movies?).*$', '', slug, flags=re.IGNORECASE)
    name = clean_name(name)
    safe_print(f"[*] Title: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))
    summary = DownloadSummary()

    # Use site-specific referer
    site_referer = 'https://dramakey.cc/' if 'dramakey.cc' in url else 'https://dramarain.com/'
    session.headers['Referer'] = site_referer
    r = safe_get(session, url, referer=site_referer)
    if not r:
        return
    soup = BeautifulSoup(r.text, 'html.parser')

    # Method 1: Direct drip.waffi.cloud links already in page
    drip_links = [(a.text.strip(), a['href']) for a in soup.find_all('a', href=True)
                  if 'drip.waffi.cloud' in a['href']]
    if drip_links:
        safe_print(f"[*] Found {len(drip_links)} direct link(s) — saving to: {folder}")
        for i, (label, link) in enumerate(drip_links, 1):
            if STOP: break
            wait_if_paused()
            fname = safe_filename(f"{label or f'episode-{i}'}.mp4")
            safe_print(f"\n[{i}/{len(drip_links)}] {fname}")
            download_file(link, folder, fname, summary)
        summary.report()
        return

    # Method 2: Download page links that redirect to drip
    dl_links = [(a.text.strip(), a['href']) for a in soup.find_all('a', href=True)
                if any(x in a['href'] for x in ['dramarain.com/download', 'dramakey.cc/download', 'drip.waffi.cloud'])]
    if dl_links:
        safe_print(f"[*] Found {len(dl_links)} episode(s) — saving to: {folder}")
        for i, (label, dl_url) in enumerate(dl_links, 1):
            if STOP: break
            wait_if_paused()
            fname = safe_filename(f"{label or f'episode-{i}'}.mp4")
            safe_print(f"\n[{i}/{len(dl_links)}] {fname}")
            if 'drip.waffi.cloud' in dl_url:
                direct = dl_url
            else:
                # Update referer to the download page before resolving
                session.headers['Referer'] = site_referer
                direct = resolve_drip_waffi(dl_url, session)
            if direct:
                download_file(direct, folder, fname, summary)
            else:
                safe_print(f"  [✗] Could not resolve link")
                summary.add_failed(fname)
            time.sleep(0.5)
        summary.report()
        return

    all_links = [a['href'] for a in soup.find_all('a', href=True)]
    safe_print(f"[!] No download links found. Page has {len(all_links)} total links.")
    diagnose_page(soup, url, expected_pattern="drip.waffi.cloud links")

def extract_naijavault(url, session):
    safe_print("[*] NaijaVault mode")
    slug = url.rstrip('/').split('/')[-1]
    name = re.sub(r'-\d{4}.*$', '', slug)
    name = re.sub(r'-season-\d+.*$', '', name, flags=re.IGNORECASE)
    name = clean_name(name)
    safe_print(f"[*] Title: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))

    session.headers['Referer'] = 'https://www.naijavault.com/'
    r = safe_get(session, url, timeout=30)
    if not r:
        return
    soup = BeautifulSoup(r.text, 'html.parser')
    summary = DownloadSummary()

    # Check for ZIP file first — if found, download it and skip individual episodes
    zip_links = [a['href'] for a in soup.find_all('a', href=True)
                 if a['href'].endswith('.zip') or 'zip' in a.get_text(strip=True).lower()]
    if zip_links:
        safe_print(f"[*] ZIP file found — downloading ZIP and skipping individual episodes")
        zip_url = zip_links[0]
        zip_name = zip_url.split('/')[-1] or f"{name}.zip"
        download_file(zip_url, folder, safe_filename(zip_name), summary)
        summary.report()
        return

    # Find all /dl- episode links
    seen_dl = set()
    dl_links = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if '/dl-' in href and 'naijavault.com' in href and href not in seen_dl:
            seen_dl.add(href)
            dl_links.append((a.text.strip(), href))

    # Handle single movie/dl page — user pasted a /dl- or /temp/ URL directly.
    # Detect both old format (var downloadURL) and new format (vikingfile.com anchor).
    if not dl_links:
        is_dl_page = (
            re.search(r'var downloadURL = "([^"]+)"', r.text) or
            re.search(r'https?://vikingfile\.com/[^\s"\'<>]+', r.text) or
            re.search(r'[?&]nj_download=', r.text)
        )
        if is_dl_page:
            # Use page title as label if available, else slug
            page_title = soup.find('title')
            label = page_title.get_text(strip=True) if page_title else slug
            dl_links = [(label, url)]

    if not dl_links:
        safe_print("[!] No episode links found")
        diagnose_page(soup, url, expected_pattern="/dl- links or vikingfile.com anchor")
        return

    safe_print(f"[*] Found {len(dl_links)} episode(s) — saving to: {folder}")

    items = []
    for i, (label, dl_url) in enumerate(dl_links, 1):
        if STOP:
            break
        ep_name = safe_filename(clean_ep_name(label) or f"episode-{i}")
        safe_print(f"\n[{i}/{len(dl_links)}] Extracting: {ep_name}")

        session.headers.update({'Referer': url})
        r2 = safe_get(session, dl_url, timeout=20)
        if not r2:
            safe_print(f"  [✗] Could not fetch download page")
            summary.add_failed(ep_name)
            continue

        # ── Pattern 1 (NEW): vikingfile.com anchor directly in page ─────────
        # New /dl- pages are minimal HTML with:
        #   <a href="https://vikingfile.com/f/{id}">⬇ DOWNLOAD</a>
        # This runs first because it's now the primary format.
        vf_anchor = re.search(r'https?://vikingfile\.com/[^\s"\'<>]+', r2.text)
        if vf_anchor:
            vf_url = vf_anchor.group(0).rstrip('.,;)')
            safe_print(f"  [*] VikingFile anchor found")
            direct = resolve_vikingfile(vf_url, session)
            if direct:
                ext = 'mkv' if '.mkv' in direct else 'mp4'
                items.append((direct, safe_filename(f"{ep_name}.{ext}")))
            else:
                safe_print(f"  [✗] VikingFile resolution failed")
                summary.add_failed(ep_name)
            time.sleep(0.5)
            continue

        # ── Pattern 2 (OLD): var downloadURL = "..." in JavaScript ──────────
        # Kept as fallback for any older /dl- pages still using the old format.
        vf_match = re.search(r'var downloadURL = "([^"]+)"', r2.text)
        if vf_match:
            vf_url = vf_match.group(1)
            if 'vikingfile.com' in vf_url:
                direct = resolve_vikingfile(vf_url, session)
            else:
                direct = vf_url
            if direct:
                ext = 'mkv' if '.mkv' in direct else 'mp4'
                items.append((direct, safe_filename(f"{ep_name}.{ext}")))
            else:
                safe_print(f"  [✗] VikingFile resolution failed")
                summary.add_failed(ep_name)
            time.sleep(0.5)
            continue

        # ── Pattern 3: cdn.filevault.com.ng direct link ─────────────────────
        fv = re.findall(r'https?://cdn\.filevault\.com\.ng/[^\s"\'<>]+', r2.text)
        if fv:
            direct = fv[0]
            ext = 'mkv' if '.mkv' in direct else 'mp4'
            items.append((direct, safe_filename(f"{ep_name}.{ext}")))
            time.sleep(0.5)
            continue

        # ── Pattern 4: ?nj_download= direct NaijaVault redirect ─────────────
        # New pages also expose a NaijaVault-side redirect link like:
        #   https://www.naijavault.com/?nj_download=wura-s04e01-mkv
        # Follow it — NaijaVault redirects it to the actual CDN URL.
        nj_dl = re.search(r'https?://[^\s"\'<>]*naijavault\.com[^\s"\'<>]*[?&]nj_download=[^\s"\'<>]+', r2.text)
        if nj_dl:
            nj_url = nj_dl.group(0).rstrip('.,;)')
            safe_print(f"  [*] nj_download link found — following redirect")
            try:
                rr = session.get(nj_url, timeout=15, allow_redirects=False)
                cdn = rr.headers.get('location')
                if cdn and cdn.startswith('http'):
                    ext = 'mkv' if '.mkv' in cdn else 'mp4'
                    items.append((cdn, safe_filename(f"{ep_name}.{ext}")))
                    time.sleep(0.5)
                    continue
            except Exception as e:
                safe_print(f"  [!] nj_download redirect failed: {e}")

        safe_print(f"  [✗] No download URL found on page")
        summary.add_failed(ep_name)
        time.sleep(0.5)

    safe_print(f"\n[*] Starting {len(items)} download(s)...")
    for dl_url, dl_fname in items:
        if STOP:
            break
        wait_if_paused()
        download_file(dl_url, folder, dl_fname, summary)
    summary.report()

def extract_anitaku(url, session):
    safe_print("[*] Anitaku mode")
    slug = url.rstrip('/').split('/')[-1]
    is_episode = 'episode-' in slug

    if is_episode:
        name = re.sub(r'-episode-\d+.*$', '', slug)
    else:
        name = slug
    name = clean_name(name)
    safe_print(f"[*] Title: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))
    summary = DownloadSummary()

    def download_episode(ep_url, ep_name):
        """
        Extract and download a single anitaku episode.
        Anitaku only uses tamilembed.lol — the embed URL is in the page as a plain link.
        We extract it directly and pass to yt-dlp.
        """
        r = safe_get(session, ep_url, referer=ANITAKU_BASE + '/', timeout=30)
        if not r:
            safe_print(f"  [✗] Could not fetch: {ep_name}")
            summary.add_failed(ep_name)
            return

        # Extract tamilembed URL from page — it appears as a plain link in the HTML
        # Pattern: https://tamilembed.lol/embed/stream/{token}
        tamil_match = re.search(r"""(https://tamilembed\.lol/embed/[^\s"'<>]+)""", r.text)
        if tamil_match:
            embed_url = tamil_match.group(1)
            safe_print(f"  [*] Found tamilembed stream")
            download_with_ytdlp(embed_url, folder, safe_filename(f"{ep_name}.mp4"), summary)
            return

        # Fallback: check iframe src
        soup = BeautifulSoup(r.text, 'html.parser')
        iframe = soup.find('iframe', src=re.compile(r'tamilembed|embed'))
        if iframe:
            src = iframe.get('src', '')
            if not src.startswith('http'):
                src = 'https:' + src
            safe_print(f"  [*] Found embed via iframe")
            download_with_ytdlp(src, folder, safe_filename(f"{ep_name}.mp4"), summary)
            return

        # Last resort: try yt-dlp on the episode page itself
        safe_print(f"  [*] Trying yt-dlp on episode page directly")
        result = download_with_ytdlp(ep_url, folder, safe_filename(f"{ep_name}.mp4"), summary)
        if not result:
            safe_print(f"  [✗] All methods failed for: {ep_name}")
            diagnose_page(soup, ep_url, "tamilembed.lol embed URL")

    if is_episode:
        ep_name = safe_filename(slug)
        safe_print(f"[*] Single episode — saving to: {folder}")
        download_episode(url, ep_name)
    else:
        # Series page — fetch episode list
        safe_print("[*] Fetching episode list...")
        r = safe_get(session, url, referer=ANITAKU_BASE + '/', timeout=30)
        if not r:
            safe_print("[!] Could not fetch series page")
            return

        soup = BeautifulSoup(r.text, 'html.parser')

        # Episodes listed on page as "Episode N" links
        ep_links = []
        seen = set()
        for a in soup.find_all('a', href=True):
            href = a['href']
            text = a.get_text(strip=True)
            if 'episode-' in href and ANITAKU_BASE in href and href not in seen:
                # Must belong to this anime
                ep_slug = href.rstrip('/').split('/')[-1]
                anime_base = slug.rstrip('/')
                if ep_slug.startswith(anime_base) or anime_base in ep_slug:
                    seen.add(href)
                    ep_links.append((href, text or ep_slug))

        if not ep_links:
            # Fallback: look for any episode links on page
            for a in soup.find_all('a', href=True):
                href = a['href']
                if 'episode-' in href and href not in seen:
                    text = a.get_text(strip=True)
                    seen.add(href)
                    ep_links.append((href, text or href.split('/')[-1]))

        if not ep_links:
            safe_print("[!] No episode links found")
            return

        # Sort chronologically
        def ep_num(item):
            m = re.search(r'episode-(\d+)', item[0])
            return int(m.group(1)) if m else 0
        ep_links.sort(key=ep_num)

        safe_print(f"[*] Found {len(ep_links)} episode(s) — saving to: {folder}")

        for i, (ep_url, ep_text) in enumerate(ep_links, 1):
            if STOP:
                break
            wait_if_paused()
            ep_name = safe_filename(ep_url.rstrip('/').split('/')[-1])
            safe_print(f"\n[{i}/{len(ep_links)}] {ep_name}")
            done, _ = already_downloaded(folder, f"{ep_name}.mp4")
            if done:
                safe_print(f"  [✓] Already downloaded — skipping")
                summary.add_skipped()
                continue
            download_episode(ep_url, ep_name)
            time.sleep(1)

    summary.report()

# ─── PLUTOMOVIES EXTRACTOR ────────────────────────────────────

def resolve_plutomovies_dl(dl_url, session):
    try:
        session.headers.update({'Referer': PLUTO_BASE + '/'})
        r = safe_get(session, dl_url, timeout=15)
        if not r:
            return None
        m = re.search(
            r"getElementById\('downloadButton'\)\.onclick\s*=\s*function\(\)\s*\{"
            r"\s*location\.href\s*=\s*'(https://[^']+)'",
            r.text, re.DOTALL
        )
        if m:
            return m.group(1)
        safe_print(f"  [!] DL pattern not found — page size: {len(r.text)} bytes")
        return None
    except Exception as e:
        safe_print(f"  [!] PlutoMovies DL: {e}")
        return None

def pluto_get_ep_name(a_tag):
    text = a_tag.get_text(strip=True)
    if text and len(text) > 3:
        return safe_filename(text)
    img = a_tag.find('img')
    if img:
        alt = img.get('alt', '').strip()
        if alt and len(alt) > 3:
            return safe_filename(alt)
    return safe_filename(a_tag['href'].rstrip('/').split('/')[-1])

def extract_plutomovies(url, session):
    safe_print("[*] PlutoMovies mode")
    is_movie = '/movie/' in url
    slug = url.rstrip('/').split('/')[-1]
    name = re.sub(r'-\d{4}.*$', '', slug).replace('-', ' ').title()
    safe_print(f"[*] Title: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))
    summary = DownloadSummary()

    session.headers.update({'Referer': PLUTO_BASE + '/'})
    r = safe_get(session, url, timeout=30)
    if not r:
        return

    soup = BeautifulSoup(r.text, 'html.parser')

    dl_link = next((a['href'] for a in soup.find_all('a', href=True)
                   if 'dl.plutomovies.com' in a['href']), None)

    if is_movie or dl_link:
        if dl_link:
            safe_print(f"[*] Direct link found — saving to: {folder}")
            direct = resolve_plutomovies_dl(dl_link, session)
            if direct:
                ext = 'mkv' if 'mkv' in direct.lower() else 'mp4'
                download_file(direct, folder, safe_filename(f"{name}.{ext}"), summary)
            else:
                safe_print("[✗] Could not resolve download link")
                summary.add_failed(name)
        else:
            safe_print("[✗] No download link found on page")
            summary.add_failed(name)
        summary.report()
        return

    season_links = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if '/series/' in href and 'season' in href.lower() and '#' not in href:
            full_url = urljoin(PLUTO_BASE, href)
            if full_url != url and full_url not in season_links:
                season_links.append(full_url)

    if not season_links:
        season_links = [url]

    safe_print(f"[*] Found {len(season_links)} season(s)")

    for season_url in season_links:
        season_name = season_url.rstrip('/').split('/')[-1]
        for a in soup.find_all('a', href=True):
            href = a['href'].split('#')[0]
            if urljoin(PLUTO_BASE, href) == season_url:
                txt = a.get_text(strip=True)
                if txt:
                    season_name = txt
                break
        safe_print("\n[*] Season: " + season_name)
        page = 1
        seen_eps = set()

        # Step 1: Collect ALL episodes across ALL pages first
        safe_print(f"  [*] Scanning all pages...")
        all_season_eps = []

        while True:
            page_url = season_url if page == 1 else f"{season_url}/page/{page}"
            r2 = safe_get(session, page_url, timeout=30)
            if not r2 or r2.status_code == 404:
                break

            soup2 = BeautifulSoup(r2.text, 'html.parser')

            ep_items = []
            for a in soup2.find_all('a', href=True):
                href = a['href'].split('#')[0]
                if '/series/' not in href:
                    continue
                full_url = urljoin(PLUTO_BASE, href)
                if full_url == season_url or full_url in seen_eps:
                    continue
                if not any(x in href.lower() for x in EP_KEYWORDS):
                    continue
                ep_name = pluto_get_ep_name(a)
                ep_items.append((full_url, ep_name))

            seen_urls = set()
            unique_eps = []
            for ep_url, ep_name in ep_items:
                if ep_url not in seen_urls:
                    seen_urls.add(ep_url)
                    unique_eps.append((ep_url, ep_name))

            if not unique_eps:
                break

            for ep_url, _ in unique_eps:
                seen_eps.add(ep_url)

            safe_print(f"  [*] Page {page}: {len(unique_eps)} episode(s) found")
            all_season_eps.extend(unique_eps)
            page += 1
            time.sleep(0.5)

        if not all_season_eps:
            safe_print(f"  [!] No episodes found for this season")
            continue

        # Step 2: Sort numerically EP1 → EP last
        def ep_sort_key(item):
            ep_url, ep_name = item
            # Try to get episode number from name (S01 E05 → 5)
            m = re.search(r'[Ee](?:pisode\s*)?(\d+)', ep_name)
            if m:
                return int(m.group(1))
            # Fallback: from URL
            m = re.search(r'-e(\d+)', ep_url.lower())
            if m:
                return int(m.group(1))
            return 0

        all_season_eps.sort(key=ep_sort_key)
        safe_print(f"  [*] Total: {len(all_season_eps)} episode(s) — sorted EP1→EP{len(all_season_eps)}")

        # Step 3: Extract download links for all episodes
        items = []
        for i, (ep_url, ep_name) in enumerate(all_season_eps, 1):
            if STOP:
                break
            wait_if_paused()
            safe_print(f"\n  [{i}/{len(all_season_eps)}] Extracting: {ep_name}")

            r3 = safe_get(session, ep_url, timeout=30)
            if not r3:
                safe_print(f"  [✗] Could not fetch episode page")
                summary.add_failed(ep_name)
                continue

            soup3 = BeautifulSoup(r3.text, 'html.parser')
            dl_link = next((a['href'] for a in soup3.find_all('a', href=True)
                           if 'dl.plutomovies.com' in a['href']), None)

            if not dl_link:
                safe_print(f"  [✗] No download link on episode page")
                summary.add_failed(ep_name)
                continue

            direct = resolve_plutomovies_dl(dl_link, session)
            if direct:
                ext = 'mkv' if 'mkv' in direct.lower() else 'mp4'
                items.append((direct, safe_filename(f"{ep_name}.{ext}")))
            else:
                safe_print(f"  [✗] Could not resolve download link")
                summary.add_failed(ep_name)

            time.sleep(0.5)

        # Step 4: Download all extracted links
        if items:
            safe_print(f"\n  [*] Starting download of {len(items)} episode(s)...")
            download_batch(items, folder, summary, series_url=url, series_name=name)

    summary.report()

# ─── SOCIAL MEDIA / CATCH-ALL ─────────────────────────────────
SOCIAL_DOMAINS = [
    'facebook.com', 'fb.watch', 'instagram.com', 'twitter.com', 'x.com',
    'tiktok.com', 'youtube.com', 'youtu.be', 'vimeo.com', 'dailymotion.com',
    'twitch.tv', 'reddit.com', 'pinterest.com', 'snapchat.com'
]

def download_social_ytdlp(url, folder, filename, summary):
    """
    Social media download — 720p default with automatic quality fallback.
    Tries 720p → 480p → 360p → best available. No quality prompt.
    """
    if not HAS_YTDLP:
        if not install_ytdlp():
            safe_print(f"  [!] yt-dlp unavailable")
            summary.add_failed(filename)
            return False

    os.makedirs(folder, exist_ok=True)
    base = re.sub(r'\.(mp4|mkv|m3u8)$', '', filename)
    out_template = os.path.join(folder, base + '.%(ext)s')

    # Quality fallback chain for social media
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
                'yt-dlp',
                '-f', fmt,
                '--merge-output-format', 'mp4',
                '-o', out_template,
                '--no-playlist',
                '--retries', '3',
                '--fragment-retries', '3',
                '--quiet',
                '--no-warnings',
                '--progress',
                '--newline',
            ]
            if HAS_ARIA2C:
                cmd += [
                    '--external-downloader', 'aria2c',
                    '--external-downloader-args',
                    'aria2c:-x 16 -s 16 -c --max-tries=0 --retry-wait=30 --timeout=120 --connect-timeout=60 --file-allocation=none --min-split-size=1M'
                ]
            cmd.append(url)
            result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            if result.returncode == 0:
                # Find downloaded file
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
            # Check if it was a format error — try next quality
            if 'requested format not available' in result.stderr.lower() or 'format' in result.stderr.lower():
                continue
            # Other error — print and fail
            safe_print(f"  [✗] yt-dlp failed: {result.stderr[:100]}")
            break
        except Exception as e:
            safe_print(f"  [!] yt-dlp error: {e}")
            break

    summary.add_failed(filename)
    return False

def extract_social(url, session):
    domain = base_domain(url).replace('https://', '').replace('www.', '')
    safe_print(f"[*] Social/Generic mode: {domain}")
    name = domain.split('.')[0].title()
    folder = os.path.join(BASE_DIR, 'Social', safe_filename(name))
    summary = DownloadSummary()

    slug = url.rstrip('/').split('/')[-1] or 'video'
    slug = re.sub(r'[^\w-]', '_', slug)[:50]
    filename = safe_filename(f"{slug}.mp4")

    safe_print(f"[*] Downloading: {filename}")
    safe_print(f"[*] Saving to: {folder}")
    download_social_ytdlp(url, folder, filename, summary)
    summary.report()

# ─── SEARCH WITHIN SCRIPT ────────────────────────────────────

def _pluto_results(soup):
    seen = set()
    results = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        title = a.get_text(strip=True)
        if (('/series/' in href or '/movie/' in href) and
                'plutomovies.com' in href and
                href not in seen and len(title) > 5):
            seen.add(href)
            results.append((title, href))
    return results

def _naijavault_results(soup):
    seen = set()
    results = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        title = a.get_text(strip=True)
        if ('naijavault.com' in href and
                '/dl-' not in href and
                '/?s=' not in href and
                href.count('/') >= 4 and  # must have a real path, not just domain
                href not in seen and len(title) > 5):
            seen.add(href)
            results.append((title, href))
    return results

def _nkiri_results(soup):
    seen = set()
    results = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        title = a.get_text(strip=True)
        if 'nkiri.com' in href and href not in seen and len(title) > 5:
            seen.add(href)
            results.append((title, href))
    return results

def _dramarain_results(soup):
    seen = set()
    results = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        title = a.get_text(strip=True)
        if 'dramarain.com' in href and href not in seen and len(title) > 5:
            seen.add(href)
            results.append((title, href))
    return results

def _myasian_results(soup):
    seen = set()
    results = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        title = a.get_text(strip=True)
        if 'myasiantv9' in href and href not in seen and len(title) > 5:
            seen.add(href)
            results.append((title, href))
    return results

def _anitaku_results(soup):
    seen = set()
    results = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        title = a.get_text(strip=True)
        # Anitaku search results are in /anime/{slug} format
        if '/anime/' in href and 'category' not in href:
            full = href if href.startswith('http') else 'https://anitaku.com.ro' + href
            if full not in seen and len(title) > 3:
                seen.add(full)
                results.append((title, full))
    return results

SEARCH_ENGINES = {
    'plutomovies.com':   {'url': 'https://plutomovies.com/?s={query}',           'fn': _pluto_results},
    'naijavault.com':    {'url': 'https://www.naijavault.com/?s={query}',         'fn': _naijavault_results},
    'nkiri.com':         {'url': 'https://nkiri.com/?s={query}',                  'fn': _nkiri_results},
    'dramarain.com':     {'url': 'https://dramarain.com/?s={query}',              'fn': _dramarain_results},
    'myasiantv9.com.ro': {'url': 'https://myasiantv9.com.ro/?s={query}',          'fn': _myasian_results},
    'anitaku.com.ro':    {'url': 'https://anitaku.com.ro/search.html?keyword={query}', 'fn': _anitaku_results},
}

def search_sites(query, session):
    """
    Search all supported sites for a show.
    Returns list of (site, title, url) tuples.
    """
    encoded = quote_plus(query)
    all_results = []

    safe_print(f"\n[*] Searching for: {query}")
    safe_print(f"{'─'*50}")

    for site, config in SEARCH_ENGINES.items():
        try:
            search_url = config['url'].format(query=encoded)
            r = safe_get(session, search_url, timeout=15)
            if not r or r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, 'html.parser')
            results = config['fn'](soup)

            # Deduplicate by URL
            seen = set()
            clean = []
            for title, url in results:
                if url not in seen and title and len(title) > 3:
                    seen.add(url)
                    clean.append((title, url))

            if clean:
                safe_print(f"\n  [{site}]")
                for title, url in clean[:5]:
                    all_results.append((site, title, url))
                    safe_print(f"  [{len(all_results)}] {title[:50]}")
                    safe_print(f"       {url[:70]}")
        except Exception as e:
            safe_print(f"  [!] Search failed for {site}: {e}")

    return all_results

def handle_search(query, session):
    """Handle search command and let user pick a result to download."""
    results = search_sites(query, session)

    if not results:
        safe_print("\n[!] No results found")
        return

    safe_print(f"\n{'─'*50}")
    safe_print(f"Pick a result (1-{len(results)}) or 0 to cancel:")
    try:
        choice = input("> ").strip()
        idx = int(choice)
        if idx == 0 or idx > len(results):
            safe_print("[*] Cancelled")
            return
        site, title, url = results[idx - 1]
        safe_print(f"\n[*] Selected: {title}")
        process_link_queue([url], session)
    except (ValueError, EOFError):
        safe_print("[*] Cancelled")

# ─── SITE DETECTION ───────────────────────────────────────────
SITE_MAP = {
    'thenkiri.com':      extract_nkiri,
    'nkiri.com':         extract_nkiri,
    'dramakey.com':      extract_dramakey_com,
    'dramakey.cc':       extract_dramarain,
    'dramarain.com':     extract_dramarain,
    '9jarocks.net':      extract_9jarocks,
    'naijaprey.tv':      extract_naijaprey,
    'myasiantv9.com.ro': extract_myasiantv,
    'myasiantv9.com':    extract_myasiantv,
    'naijavault.com':    extract_naijavault,
    'anitaku.com.ro':    extract_anitaku,
    'plutomovies.com':   extract_plutomovies,
}

def detect_site(url):
    for domain, extractor in SITE_MAP.items():
        if domain in url:
            return extractor
    # Social media catch-all
    for domain in SOCIAL_DOMAINS:
        if domain in url:
            return extract_social
    return None

# ─── LINK QUEUE ───────────────────────────────────────────────
def process_link_queue(links, session):
    """Process a queue of links one by one."""
    for i, url in enumerate(links, 1):
        if STOP:
            safe_print("[*] Stopped by user")
            break
        wait_if_paused()
        if len(links) > 1:
            safe_print(f"\n{'─'*50}")
            safe_print(f"  Queue [{i}/{len(links)}]: {url[:60]}")
            safe_print(f"{'─'*50}")
        extractor = detect_site(url)
        if not extractor:
            safe_print(f"[!] Unsupported site: {url}")
            safe_print(f"[!] Supported: {', '.join(SITE_MAP.keys())}")
            continue
        try:
            extractor(url, session)
        except Exception as e:
            safe_print(f"\n[!] Unexpected error: {e}")
            safe_print("[!] Please check the URL and try again")

# ─── MAIN ─────────────────────────────────────────────────────
def main():
    setup_android()
    auto_update()

    session = make_best_session()  # uses curl_cffi if available for Cloudflare bypass

    # Non-interactive mode
    if len(sys.argv) >= 2:
        url = sys.argv[1].strip()
        extractor = detect_site(url)
        if not extractor:
            print(f"[!] Unsupported site: {url}")
            sys.exit(1)
        try:
            extractor(url, session)
        except Exception as e:
            print(f"\n[!] Unexpected error: {e}")
        return

    # Interactive mode
    check_disk_space()
    ask_startup_settings()
    setup_signal_handler()

    q = SELECTED_QUALITY[0] if SELECTED_QUALITY else '480p'
    print("╔══════════════════════════════════════════════╗")
    print("║         DOWNLOAD TOOLKIT v2.0                ║")
    print(f"║  Quality: {q:<6}  Parallel: {PARALLEL_COUNT}                   ║")
    print("╠══════════════════════════════════════════════╣")
    print("║  SITES:                                      ║")
    print("║  nkiri • dramakey • dramarain • naijavault   ║")
    print("║  plutomovies • anitaku • myasiantv           ║")
    print("║  naijaprey • 9jarocks • +yt/ig/tiktok/fb     ║")
    print("╠══════════════════════════════════════════════╣")
    print("║  COMMANDS (type and press Enter):             ║")
    print("║  search <title>  • settings  • history       ║")
    print("║  resume  • clip (paste URL)  • exit          ║")
    print("╚══════════════════════════════════════════════╝")

    while True:
        if STOP:
            print("\nBye!")
            break

        try:
            raw = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not raw:
            continue

        # ── Command routing ──────────────────────────────
        lower = raw.lower()

        if lower == 'exit':
            print("Bye!")
            break

        elif lower == 'history':
            show_history()

        elif lower == 'settings' or lower.startswith('settings '):
            handle_settings_command(raw)

        elif lower.startswith('search '):
            query = raw[7:].strip()
            if query:
                handle_search(query, session)
            else:
                print("[!] Usage: search <show name>")

        elif lower == 'resume':
            handle_resume_command(session)

        elif lower == 'clip':
            # Read URL from Termux clipboard — use when keyboard paste is glitchy
            try:
                result = subprocess.run(['termux-clipboard-get'], capture_output=True, text=True, timeout=5)
                clipped = result.stdout.strip()
                if clipped.startswith('http'):
                    print(f"[*] From clipboard: {clipped[:70]}")
                    process_link_queue([clipped], session)
                elif clipped:
                    print(f"[!] Clipboard doesn't look like a URL: {clipped[:60]}")
                else:
                    print("[!] Clipboard is empty")
            except FileNotFoundError:
                print("[!] termux-clipboard-get not found — install with: pkg install termux-api")
            except Exception as e:
                print(f"[!] Clipboard error: {e}")

        elif raw.startswith('http'):
            # Could be multiple URLs pasted — split by whitespace/newline
            urls = [u.strip() for u in re.split(r'[\s]+', raw) if u.strip().startswith('http')]
            if urls:
                process_link_queue(urls, session)
            else:
                print("[!] No valid URLs found")

        else:
            print(f"[!] Unknown command: {raw[:40]}")
            print("[!] Type: search <title> | settings | history | exit | paste a URL")

if __name__ == '__main__':
    main()
