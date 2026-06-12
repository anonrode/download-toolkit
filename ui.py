"""
ui.py — Anonrode display layer.
Handles: startup splash, live progress bar, episode table, status bar, themed prints.
Uses ANSI escape codes only — no external libraries needed.
"""

import os
import sys
import shutil
import threading
import time

# ─── ANSI ─────────────────────────────────────────────────────
ESC = '\033'
def _c(code):     return f'{ESC}[{code}m'
def _mv(n):       return f'{ESC}[{n}A'   # move cursor up n lines
def _clr():       return f'{ESC}[2K\r'   # clear current line

RESET   = _c(0)
BOLD    = _c(1)
DIM     = _c(2)
GREEN   = _c(32)
CYAN    = _c(36)
YELLOW  = _c(33)
RED     = _c(31)
WHITE   = _c(97)
BGREEN  = _c(92)   # bright green
BCYAN   = _c(96)   # bright cyan
GREY    = _c(90)   # dark grey

IS_ANDROID = os.path.exists('/storage/emulated/0')
TERM_WIDTH = shutil.get_terminal_size((60, 24)).columns

# ─── LOCK ─────────────────────────────────────────────────────
_PRINT_LOCK = threading.Lock()

def _w(text='', end='\n'):
    """Raw write — always use within lock."""
    sys.stdout.write(text + end)
    sys.stdout.flush()

# ─── THEMED PRINTS ────────────────────────────────────────────

def info(msg):
    with _PRINT_LOCK:
        _w(f'  {GREY}·{RESET}  {msg}')

def success(msg):
    with _PRINT_LOCK:
        _w(f'  {BGREEN}✓{RESET}  {msg}')

def warn(msg):
    with _PRINT_LOCK:
        _w(f'  {YELLOW}!{RESET}  {msg}')

def error(msg):
    with _PRINT_LOCK:
        _w(f'  {RED}✗{RESET}  {msg}')

def downloading(msg):
    with _PRINT_LOCK:
        _w(f'  {BCYAN}↓{RESET}  {msg}')

def plain(msg):
    with _PRINT_LOCK:
        _w(f'  {msg}')

def blank():
    with _PRINT_LOCK:
        _w()

def sep():
    with _PRINT_LOCK:
        width = min(TERM_WIDTH - 4, 44)
        _w(f'  {GREY}{"─" * width}{RESET}')

# safe_print — drop-in replacement for the old one, routes through themed output
def safe_print(*args, **kwargs):
    msg = ' '.join(str(a) for a in args)
    # Route based on prefix
    if msg.strip().startswith('[✓]') or msg.strip().startswith('✓'):
        success(msg.replace('[✓]', '').replace('✓', '').strip())
    elif msg.strip().startswith('[✗]') or msg.strip().startswith('✗'):
        error(msg.replace('[✗]', '').replace('✗', '').strip())
    elif msg.strip().startswith('[!]'):
        warn(msg.replace('[!]', '').strip())
    elif msg.strip().startswith('[↓]'):
        downloading(msg.replace('[↓]', '').strip())
    elif msg.strip().startswith('[*]'):
        info(msg.replace('[*]', '').strip())
    else:
        with _PRINT_LOCK:
            sys.stdout.write('  ' + msg + '\n')
            sys.stdout.flush()

# ─── STARTUP SPLASH ───────────────────────────────────────────

def print_splash(cfg, aria2c_ok=True, ytdlp_ok=True, free_gb=None):
    """Clear screen and draw the Anonrode startup splash."""
    os.system('clear')
    q = cfg.get('quality', '480p')
    p = cfg.get('parallel', 1)
    width = min(TERM_WIDTH, 48)

    _w()
    _w(f'  {WHITE}{BOLD}{"ANONRODE":^{width-4}}{RESET}')
    _w(f'  {GREY}{"download toolkit  ·  v3.0":^{width-4}}{RESET}')
    _w()

    # System checks
    checks = []
    checks.append(f'{BGREEN}✓{RESET} {GREY}aria2c{RESET}' if aria2c_ok else f'{RED}✗{RESET} {GREY}aria2c{RESET}')
    checks.append(f'{BGREEN}✓{RESET} {GREY}yt-dlp{RESET}' if ytdlp_ok else f'{RED}✗{RESET} {GREY}yt-dlp{RESET}')
    if free_gb is not None:
        gb_color = BGREEN if free_gb > 2 else YELLOW if free_gb > 0.5 else RED
        checks.append(f'{gb_color}{free_gb:.1f}GB{RESET} {GREY}free{RESET}')

    _w('  ' + f'   '.join(checks))
    _w()
    _w(f'  {GREY}{"─" * (width - 4)}{RESET}')
    _w(f'  {GREY}ready  ·  {q}  ·  {p} thread{"s" if p > 1 else ""}{RESET}')
    _w()

# ─── PROMPT ───────────────────────────────────────────────────

def prompt_line(cfg):
    """Print the › prompt with current context."""
    q = cfg.get('quality', '480p')
    p = cfg.get('parallel', 1)
    context = f'{GREY}{q} · {p}t{RESET}'
    sys.stdout.write(f'\n  {context}  {BCYAN}›{RESET} ')
    sys.stdout.flush()

# ─── SERIES HEADER ────────────────────────────────────────────

def series_header(name, total, folder):
    blank()
    _w(f'  {WHITE}{BOLD}{name}{RESET}  {GREY}·  {total} episode{"s" if total != 1 else ""}{RESET}')
    _w(f'  {GREY}{folder}{RESET}')
    sep()

# ─── EPISODE ROW ──────────────────────────────────────────────

def ep_done(n, name, size_mb):
    with _PRINT_LOCK:
        size_str = f'{size_mb:.0f}MB' if size_mb else ''
        _w(f'  {GREEN}✓{RESET}  {GREY}{name}{RESET}  {GREY}{DIM}{size_str}{RESET}')

def ep_skipped(n, name):
    with _PRINT_LOCK:
        _w(f'  {GREY}={RESET}  {GREY}{DIM}{name}  already downloaded{RESET}')

def ep_failed(name):
    with _PRINT_LOCK:
        _w(f'  {RED}✗{RESET}  {RED}{name}{RESET}')

def ep_start(n, total, name):
    with _PRINT_LOCK:
        counter = f'{GREY}[{n}/{total}]{RESET} ' if total else ''
        _w(f'\n  {BCYAN}↓{RESET}  {counter}{WHITE}{name}{RESET}')

# ─── LIVE PROGRESS BAR ────────────────────────────────────────

class LiveProgress:
    """
    Draws and redraws a single progress line in place using \\r.
    Call .update(pct, speed, eta) as data comes in.
    Call .done(size_mb) when finished.
    Call .fail() on error.
    """
    BAR_WIDTH = 20

    def __init__(self, filename):
        self.filename = filename
        self._lock    = threading.Lock()
        self._started = False
        self._done    = False

    def _bar(self, pct):
        filled = int(self.BAR_WIDTH * pct / 100)
        empty  = self.BAR_WIDTH - filled
        return f'{GREEN}{"█" * filled}{GREY}{"░" * empty}{RESET}'

    def update(self, pct, speed_mbs=None, eta=None):
        if self._done:
            return
        with _PRINT_LOCK:
            bar  = self._bar(pct)
            pct_str = f'{YELLOW}{pct:3.0f}%{RESET}'
            spd_str = f'  {WHITE}{speed_mbs:.1f}MB/s{RESET}' if speed_mbs else ''
            eta_str = f'  {GREY}{eta}{RESET}' if eta else ''
            line = f'\r     {bar}  {pct_str}{spd_str}{eta_str}   '
            sys.stdout.write(line)
            sys.stdout.flush()
            self._started = True

    def done(self, size_mb=None):
        self._done = True
        with _PRINT_LOCK:
            bar     = self._bar(100)
            size_str = f'  {GREY}{size_mb:.0f}MB{RESET}' if size_mb else ''
            sys.stdout.write(f'\r     {bar}  {BGREEN}100%{RESET}{size_str}\n')
            sys.stdout.flush()

    def fail(self):
        self._done = True
        with _PRINT_LOCK:
            if self._started:
                sys.stdout.write('\n')
            sys.stdout.flush()


# ─── DOWNLOAD SUMMARY ─────────────────────────────────────────

def print_summary(name, success, skipped, failed, failed_list=None, elapsed=None):
    blank()
    sep()
    if failed == 0:
        _w(f'  {BGREEN}✓  all done{RESET}  {GREY}·  {name}{RESET}')
    else:
        _w(f'  {YELLOW}done with errors{RESET}  {GREY}·  {name}{RESET}')

    parts = []
    if success:  parts.append(f'{BGREEN}{success} downloaded{RESET}')
    if skipped:  parts.append(f'{GREY}{skipped} skipped{RESET}')
    if failed:   parts.append(f'{RED}{failed} failed{RESET}')
    _w('  ' + '  ·  '.join(parts))

    if elapsed:
        m, s = divmod(int(elapsed), 60)
        h, m = divmod(m, 60)
        t = f'{h}h {m}m' if h else f'{m}m {s}s'
        _w(f'  {GREY}took {t}{RESET}')

    if failed_list:
        blank()
        for name in failed_list:
            _w(f'  {RED}  · {name}{RESET}')
    sep()

# ─── SEARCH OUTPUT ────────────────────────────────────────────

def search_start(query):
    blank()
    _w(f'  {GREY}searching:{RESET}  {WHITE}{query}{RESET}')
    sep()

def search_site_found(site):
    with _PRINT_LOCK:
        _w(f'  {BGREEN}✓{RESET}  {GREY}{site}{RESET}')

def search_site_miss(site):
    with _PRINT_LOCK:
        _w(f'  {GREY}·  {site}  —  not found{RESET}')

def search_results(results):
    blank()
    for i, (site, url) in enumerate(results, 1):
        short_url = url.replace('https://', '').replace('http://', '')
        short_url = short_url[:55] + '…' if len(short_url) > 55 else short_url
        _w(f'  {GREY}[{i}]{RESET}  {BCYAN}{site:<10}{RESET}  {GREY}{short_url}{RESET}')
    sep()

def search_found_one(site, url):
    blank()
    short_url = url.replace('https://', '').replace('http://', '')
    _w(f'  {BGREEN}✓{RESET}  {BCYAN}{site}{RESET}')
    _w(f'     {GREY}{short_url}{RESET}')

def search_not_found(query):
    blank()
    warn(f'nothing found for: {query}')
    info('try different spelling or paste URL directly')

# ─── PAUSE / STOP MESSAGES ────────────────────────────────────

def paused():
    blank()
    _w(f'  {YELLOW}⏸  paused{RESET}  {GREY}·  press Enter to resume, Ctrl+C again to stop{RESET}')

def stopped():
    blank()
    _w(f'  {RED}■  stopped{RESET}')

def resuming():
    blank()
    _w(f'  {BGREEN}▶  resuming…{RESET}')
