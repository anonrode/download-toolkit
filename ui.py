"""
ui.py — Minimal terminal output layer.
No threads, no locks, no ANSI trickery beyond basic colors.
LiveProgress uses \r in serial mode and plain static lines in parallel mode.
"""

import sys
import threading

# ─── COLORS ───────────────────────────────────────────────────
GREY   = '\033[90m'
RESET  = '\033[0m'
WHITE  = '\033[97m'
BCYAN  = '\033[96m'
BGREEN = '\033[92m'
YELLOW = '\033[93m'
BRED   = '\033[91m'

# ─── PRINT LOCK ───────────────────────────────────────────────
_LOCK = threading.Lock()

def _p(msg):
    with _LOCK:
        print(msg, flush=True)

# ─── BASIC HELPERS ────────────────────────────────────────────
def safe_print(msg):   _p(str(msg))
def info(msg):         _p(f'[*] {msg}')
def warn(msg):         _p(f'[!] {msg}')
def error(msg):        _p(f'[✗] {msg}')
def success(msg):      _p(f'[✓] {msg}')
def downloading(msg):  _p(f'[↓] {msg}')
def plain(msg):        _p(str(msg))
def blank():           _p('')
def _w(msg):           _p(str(msg))
def sep():             _p('  ' + '─' * 46)

# ─── ONE-SHOT DISPLAY FUNCTIONS ───────────────────────────────
def paused():
    _p('\n[~] Paused — press Enter to resume')

def stopped():
    _p('\n[✗] Stopped')

def resuming():
    _p('[*] Resuming...')

def after_quality_change(q):
    _p(f'[✓] quality → {q}')

def after_unknown_url(url):
    _p(f'[!] Unknown site — no extractor for: {url[:70]}')
    _p('[!] Supported: NKiri, DramaKey, DramaRain, NaijaVault, 9jaRocks,')
    _p('[!]            NaijaPrey, MyAsianTV, Anitaku, PlutoMovies, YouTube,')
    _p('[!]            Instagram, TikTok, Facebook, Pinterest, and more.')

def after_download_done(name='', count=0, elapsed=0):
    mins = int(elapsed) // 60
    secs = int(elapsed) % 60
    t = f'{mins}m {secs:02d}s' if mins else f'{secs}s'
    _p(f'[✓] {count} episode(s) downloaded in {t}')

def print_summary(name='', success=0, skipped=0, failed=0,
                  failed_list=None, elapsed=0):
    mins = int(elapsed) // 60
    secs = int(elapsed) % 60
    t    = f'{mins}m {secs:02d}s' if mins else f'{secs}s'
    blank()
    sep()
    _p(f'  {name or "download"}')
    _p(f'  Done: {success}   Skipped: {skipped}   Failed: {failed}   Time: {t}')
    if failed_list:
        for f in failed_list:
            _p(f'  [✗] {f}')
    sep()

def print_splash(cfg, aria2c_ok=True, ytdlp_ok=True, free_gb=None):
    q  = cfg.get('quality',  '480p')
    p  = cfg.get('parallel', 1)
    a  = '\u2713' if aria2c_ok else '\u2717'
    y  = '\u2713' if ytdlp_ok  else '\u2717'
    gb = f'{free_gb:.1f}GB' if free_gb is not None else '?'
    blank()
    sep()
    _p(f'  {WHITE}ANONRODE{RESET}  Download Toolkit')
    _p(f'  quality: {BCYAN}{q}{RESET}  parallel: {BCYAN}{p}{RESET}')
    _p(f'  aria2c: {a}  yt-dlp: {y}  space: {gb}')
    sep()
    blank()

def prompt_line(cfg):
    q = cfg.get('quality', '480p')
    with _LOCK:
        sys.stdout.write(f'\n{GREY}[{q}]{RESET} \u203a ')
        sys.stdout.flush()

# ─── SEARCH DISPLAY ───────────────────────────────────────────
def search_start(query):
    _p(f'[*] Searching: "{query}"')

def search_site_found(site):
    _p(f'[*] Found on {site}')

def search_found_one(site, url):
    blank()
    _p(f'[\u2713] Found on {site}')
    _p(f'    {url}')

def search_not_found():
    _p('[!] Not found')

def after_search_not_found(query):
    _p(f'[!] Nothing found for "{query}"')
    _p('[!] Try a different spelling or paste the URL directly')

def search_results(results):
    blank()
    sep()
    for i, (site, url) in enumerate(results, 1):
        _p(f'  [{i}] {site}')
        _p(f'      {url}')
    sep()

# ─── LIVE PROGRESS ────────────────────────────────────────────
class LiveProgress:
    """
    Serial mode  (parallel=False): overwrites same line with \r.
    Parallel mode (parallel=True):  static milestone lines only.
    Callers in download_batch pass parallel=True when workers > 1.
    """

    def __init__(self, filename, parallel=False):
        self._name     = filename[:45] + '\u2026' if len(filename) > 45 else filename
        self._parallel = parallel
        self._started  = False
        self._done     = False

    def _label(self, pct=None, spd=None, eta=None):
        parts = [f'[\u2193] {self._name}']
        if pct is not None:
            parts.append(f'{pct:.0f}%')
        if spd is not None:
            parts.append(f'{spd:.1f} MB/s')
        if eta is not None:
            parts.append(f'ETA {eta}')
        return '  '.join(parts)

    def update(self, pct, spd=None, eta=None):
        if self._done:
            return
        line = self._label(pct, spd, eta)
        if self._parallel:
            milestones = (0, 25, 50, 75, 100)
            if not self._started or (pct is not None and
                    any(abs(pct - m) < 2 for m in milestones)):
                with _LOCK:
                    print(line, flush=True)
                self._started = True
        else:
            with _LOCK:
                sys.stdout.write('\r' + line + '    ')
                sys.stdout.flush()
            self._started = True

    def done(self, size_mb=None):
        if self._done:
            return
        self._done = True
        suffix = f'  ({size_mb:.1f} MB)' if size_mb is not None else ''
        msg    = f'[\u2713] {self._name}{suffix}'
        with _LOCK:
            if not self._parallel and self._started:
                sys.stdout.write('\r' + ' ' * (len(self._label(100)) + 6) + '\r')
                sys.stdout.flush()
            print(msg, flush=True)

    def fail(self):
        if self._done:
            return
        self._done = True
        msg = f'[\u2717] {self._name}  failed'
        with _LOCK:
            if not self._parallel and self._started:
                sys.stdout.write('\r' + ' ' * (len(self._label(100)) + 6) + '\r')
                sys.stdout.flush()
            print(msg, flush=True)
