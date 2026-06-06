"""
config.py — Runtime constants, app state, logging, HTTP sessions, and settings I/O.

Merged from: core/constants.py, core/state.py, core/logger.py,
             core/session.py, core/config.py
"""

import json
import logging
import os
import threading
import time

import requests as _requests

try:
    from curl_cffi import requests as _cf
    _HAS_CF = True
except ImportError:
    _HAS_CF = False


# ─── Constants ────────────────────────────────────────────────────

UA_DESKTOP = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)
UA_MOBILE = (
    'Mozilla/5.0 (Linux; Android 10; K) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/139.0.0.0 Mobile Safari/537.36'
)

PLUTO_BASE   = 'https://plutomovies.com'
ANITAKU_BASE = 'https://anitaku.com.ro'

EP_KEYWORDS = ['-e', 'episode', 's0', 's1', 's2', 's3', 's4', 's5', 's6', 's7', 's8', 's9']

IS_ANDROID = os.path.exists('/storage/emulated/0')
BASE_DIR   = (
    '/storage/emulated/0/Anon'
    if IS_ANDROID
    else os.path.join(os.path.expanduser('~'), 'Downloads', 'Anon')
)
LOG_FILE    = os.path.join(BASE_DIR, '.download_history.json')
RESUME_FILE = os.path.join(BASE_DIR, '.resume_state.json')
CONFIG_FILE = os.path.join(BASE_DIR, '.config.json')

QUALITY_MAP = {
    '360p':  'bestvideo[height<=360]+bestaudio/best[height<=360]',
    '480p':  'bestvideo[height<=480]+bestaudio/best[height<=480]',
    '720p':  'bestvideo[height<=720]+bestaudio/best[height<=720]',
    '1080p': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
}

DEFAULT_CONFIG = {
    'quality':   '480p',
    'parallel':  2,
    'bandwidth': 0,
}

SOCIAL_DOMAINS = [
    'facebook.com', 'fb.watch', 'instagram.com', 'twitter.com', 'x.com',
    'tiktok.com', 'youtube.com', 'youtu.be', 'vimeo.com', 'dailymotion.com',
    'twitch.tv', 'reddit.com', 'pinterest.com', 'snapchat.com',
]

SEARCH_SITES = [
    'naijavault.com',
    'plutomovies.com',
    'nkiri.com',
    'dramarain.com',
    'myasiantv9.com.ro',
    'anitaku.com.ro',
]

NOLLYWOOD_HINTS   = ['nkiri', 'naijavault', 'naijaprey', '9jarocks']
ANIME_SITES       = ['anitaku.com.ro']
ASIAN_DRAMA_SITES = ['myasiantv9.com.ro', 'dramarain.com']
NOLLYWOOD_SITES   = ['naijavault.com', 'nkiri.com', 'naijaprey.tv', '9jarocks.net']


# ─── App state ────────────────────────────────────────────────────

class AppState:
    def __init__(self):
        self.paused          = False
        self.stop            = False
        self.ctrl_c_count    = 0
        self.current_process = None
        self._lock           = threading.Lock()

        self.quality_label   = '480p'
        self.quality_fmt     = 'bestvideo[height<=480]+bestaudio/best[height<=480]'
        self.parallel_count  = 2
        self.bandwidth_limit = 0

        self.has_aria2c    = False
        self.has_ytdlp     = False
        self.has_ffmpeg    = False
        self.has_curl_cffi = False

    def set_paused(self, value: bool):
        with self._lock:
            self.paused = value

    def set_stop(self, value: bool):
        with self._lock:
            self.stop = value

    def increment_ctrl_c(self) -> int:
        with self._lock:
            self.ctrl_c_count += 1
            return self.ctrl_c_count

    def reset_ctrl_c(self):
        with self._lock:
            self.ctrl_c_count = 0

    def set_process(self, proc):
        with self._lock:
            self.current_process = proc

    def get_process(self):
        with self._lock:
            return self.current_process


# ─── Logging ──────────────────────────────────────────────────────

_LOG_PATH = os.path.join(BASE_DIR, '.toolkit.log')

def _setup_logger() -> logging.Logger:
    os.makedirs(BASE_DIR, exist_ok=True)
    logger = logging.getLogger('toolkit')
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return logger

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter('%(message)s'))

    try:
        fh = logging.FileHandler(_LOG_PATH, encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        logger.addHandler(fh)
    except Exception:
        pass

    logger.addHandler(ch)
    return logger

log = _setup_logger()


# ─── HTTP sessions ────────────────────────────────────────────────

def make_session(mobile: bool = False):
    s = _requests.Session()
    s.headers['User-Agent'] = UA_MOBILE if mobile else UA_DESKTOP
    return s


def make_plain_session():
    """Always plain requests — use where curl_cffi causes issues."""
    s = _requests.Session()
    s.headers['User-Agent'] = UA_DESKTOP
    return s


def make_best_session(mobile: bool = False):
    """curl_cffi (Chrome120 TLS impersonation) if available, else plain requests."""
    if _HAS_CF:
        return _cf.Session(impersonate='chrome120')
    return make_session(mobile)


def safe_get(session, url: str, timeout: int = 20,
             referer: str = None, retries: int = 3):
    """GET with automatic retry and optional Referer. Returns Response or None."""
    for attempt in range(retries):
        try:
            if referer:
                session.headers['Referer'] = referer
            r = session.get(url, timeout=timeout)
            log.debug('GET %s → %d', url[:80], r.status_code)
            return r
        except Exception as e:
            log.debug('Attempt %d/%d failed for %s: %s', attempt + 1, retries, url[:60], e)
            if attempt < retries - 1:
                time.sleep(2)
    log.warning('All %d attempts failed for %s', retries, url[:60])
    return None


# ─── Settings I/O ────────────────────────────────────────────────

def load_config(state):
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
        else:
            cfg = DEFAULT_CONFIG
            _write_config(cfg)
    except Exception:
        cfg = DEFAULT_CONFIG

    q = cfg.get('quality', '480p')
    if q in QUALITY_MAP:
        state.quality_label = q
        state.quality_fmt   = QUALITY_MAP[q]

    state.parallel_count  = int(cfg.get('parallel', 2))
    state.bandwidth_limit = int(cfg.get('bandwidth', 0))
    log.debug('Config loaded: quality=%s parallel=%d bw=%d',
              state.quality_label, state.parallel_count, state.bandwidth_limit)


def save_config(state):
    _write_config({
        'quality':   state.quality_label,
        'parallel':  state.parallel_count,
        'bandwidth': state.bandwidth_limit,
    })


def _write_config(cfg: dict):
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log.warning('Could not save config: %s', e)


# ─── Download history ─────────────────────────────────────────────

def load_history() -> dict:
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_history(history: dict):
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(LOG_FILE, 'w') as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        log.warning('Could not save history: %s', e)


def log_download(name: str, url: str, filepath: str):
    history = load_history()
    if name not in history:
        history[name] = []
    entry = {'url': url, 'file': filepath, 'time': time.strftime('%Y-%m-%d %H:%M')}
    if entry not in history[name]:
        history[name].append(entry)
    save_history(history)


# ─── Resume state ─────────────────────────────────────────────────

def load_resume() -> dict:
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        if os.path.exists(RESUME_FILE):
            with open(RESUME_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_resume(state_dict: dict):
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(RESUME_FILE, 'w') as f:
            json.dump(state_dict, f, indent=2)
    except Exception as e:
        log.warning('Could not save resume state: %s', e)


def mark_episode_done(series_url: str, series_name: str, ep_filename: str):
    s = load_resume()
    if series_url not in s:
        s[series_url] = {'name': series_name, 'done': [], 'current': None}
    if ep_filename not in s[series_url]['done']:
        s[series_url]['done'].append(ep_filename)
    s[series_url]['current'] = None
    save_resume(s)


def mark_episode_current(series_url: str, series_name: str, ep_filename: str):
    s = load_resume()
    if series_url not in s:
        s[series_url] = {'name': series_name, 'done': [], 'current': None}
    s[series_url]['current'] = ep_filename
    s[series_url]['name']    = series_name
    save_resume(s)


def mark_series_complete(series_url: str):
    s = load_resume()
    if series_url in s:
        del s[series_url]
        save_resume(s)


def is_episode_done(series_url: str, ep_filename: str) -> bool:
    s = load_resume()
    return ep_filename in s.get(series_url, {}).get('done', [])
