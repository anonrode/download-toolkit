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
    """Thread-safe write."""
    with _PRINT_LOCK:
        sys.stdout.write(text + end)
        sys.stdout.flush()

# ─── THEMED PRINTS ────────────────────────────────────────────

def info(msg):
    _w(f'  {GREY}·{RESET}  {msg}')

def success(msg):
    _w(f'  {BGREEN}✓{RESET}  {msg}')

def warn(msg):
    _w(f'  {YELLOW}!{RESET}  {msg}')

def error(msg):
    _w(f'  {RED}✗{RESET}  {msg}')

def downloading(msg):
    _w(f'  {BCYAN}↓{RESET}  {msg}')

def plain(msg):
    _w(f'  {msg}')

def blank():
    _w()

def sep():
    width = min(TERM_WIDTH - 4, 44)
    _w(f'  {GREY}{"─" * width}{RESET}')

# safe_print — drop-in replacement for the old one, routes through themed output
def safe_print(*args, **kwargs):
    msg = ' '.join(str(a) for a in args)
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
        _w('  ' + msg)

# ─── STARTUP SPLASH ───────────────────────────────────────────

def print_splash(cfg, aria2c_ok=True, ytdlp_ok=True, free_gb=None):
    """Clear screen and draw the Anonrode startup splash."""
    os.system('clear')
    q  = cfg.get('quality', '480p')
    p  = cfg.get('parallel', 1)
    W  = min(TERM_WIDTH - 4, 44)

    def _sep():
        _w(f'  {GREY}{"━" * W}{RESET}')

    _w()
    _w(f'  {WHITE}{BOLD}A N O N R O D E{RESET}  {GREY}v3.0{RESET}')
    _w()
    _sep()
    _w()

    # ── Sites ──────────────────────────────────────────────────
    _w(f'  {GREY}TV SERIES & MOVIES{RESET}')
    _w(f'  {CYAN}NKiri{RESET}  ·  {CYAN}NaijaVault{RESET}  ·  {CYAN}PlutoMovies{RESET}')
    _w(f'  {CYAN}9jaRocks{RESET}  ·  {CYAN}NaijaPrey{RESET}  ·  {CYAN}DramaKey{RESET}')
    _w(f'  {CYAN}DramaRain{RESET}  ·  {CYAN}MyAsianTV{RESET}  ·  {CYAN}Anitaku{RESET}')
    _w()
    _w(f'  {GREY}SOCIAL & VIDEO{RESET}')
    _w(f'  {CYAN}YouTube{RESET}  ·  {CYAN}Instagram{RESET}  ·  {CYAN}TikTok{RESET}')
    _w(f'  {CYAN}Facebook{RESET}  ·  {CYAN}Pinterest{RESET}')
    _w()
    _sep()
    _w()

    # ── How to use ─────────────────────────────────────────────
    _w(f'  {GREY}search{RESET} {WHITE}<show name>{RESET}    {GREY}find & download a show{RESET}')
    _w(f'  {GREY}<paste link>{RESET}           {GREY}download directly{RESET}')
    _w(f'  {GREY}settings{RESET}               {GREY}change quality & more{RESET}')
    _w()
    _sep()
    _w()

    # ── Status bar ─────────────────────────────────────────────
    status_parts = []
    gb_color = BGREEN if (free_gb or 0) > 2 else YELLOW if (free_gb or 0) > 0.5 else RED
    if free_gb is not None:
        status_parts.append(f'{gb_color}{free_gb:.1f}GB free{RESET}')
    status_parts.append(f'{GREY}{q}{RESET}')
    if not aria2c_ok:
        status_parts.append(f'{YELLOW}aria2c missing{RESET}')
    if not ytdlp_ok:
        status_parts.append(f'{RED}yt-dlp missing{RESET}')
    _w('  ' + f'  {GREY}·{RESET}  '.join(status_parts))
    _w()

# ─── PROMPT ───────────────────────────────────────────────────

def prompt_line(cfg):
    """Print the › prompt with current context."""
    q = cfg.get('quality', '480p')
    p = cfg.get('parallel', 1)
    context = f'{GREY}{q} · {p}t{RESET}'
    with _PRINT_LOCK:
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
    size_str = f'{size_mb:.0f}MB' if size_mb else ''
    _w(f'  {GREEN}✓{RESET}  {GREY}{name}{RESET}  {GREY}{DIM}{size_str}{RESET}')

def ep_skipped(n, name):
    _w(f'  {GREY}={RESET}  {GREY}{DIM}{name}  already downloaded{RESET}')

def ep_failed(name):
    _w(f'  {RED}✗{RESET}  {RED}{name}{RESET}')

def ep_start(n, total, name):
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
            bar     = self._bar(pct)
            pct_str = f'{YELLOW}{pct:3.0f}%{RESET}'
            spd_str = f'  {WHITE}{speed_mbs:.1f}MB/s{RESET}' if speed_mbs else ''
            eta_str = f'  {GREY}{eta}{RESET}' if eta else ''
            sys.stdout.write(f'\r     {bar}  {pct_str}{spd_str}{eta_str}   ')
            sys.stdout.flush()
            self._started = True

    def done(self, size_mb=None):
        self._done = True
        with _PRINT_LOCK:
            bar      = self._bar(100)
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
    _w(f'  {BGREEN}✓{RESET}  {GREY}{site}{RESET}')

def search_site_miss(site):
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

# ─── CONTEXT-AWARE MESSAGES ───────────────────────────────────

def after_download_done(name, count, size_gb=None, elapsed=None):
    """Smart message after a series/batch finishes."""
    blank()
    _w(f'  {BGREEN}✓  {name} — done{RESET}')
    parts = []
    if count:   parts.append(f'{count} episode{"s" if count != 1 else ""}')
    if size_gb: parts.append(f'{size_gb:.1f}GB')
    if elapsed:
        m, s = divmod(int(elapsed), 60)
        h, m = divmod(m, 60)
        parts.append(f'{h}h {m}m' if h else f'{m}m {s}s')
    if parts:
        _w(f'  {GREY}{" · ".join(parts)}{RESET}')
    _w()
    _w(f'  {GREY}search another show, paste a link, or type{RESET} {WHITE}queue{RESET} {GREY}to batch download{RESET}')

def after_search_not_found(query):
    blank()
    _w(f'  {RED}✗{RESET}  {GREY}nothing found for{RESET} {WHITE}{query}{RESET}')
    _w(f'  {GREY}try different spelling, or paste a direct link from NaijaVault / PlutoMovies{RESET}')

def after_link_expired():
    blank()
    _w(f'  {RED}✗  link expired{RESET}')
    _w(f'  {GREY}go back to the series page and paste that URL instead{RESET}')

def after_quality_change(new_q):
    blank()
    _w(f'  {BGREEN}✓  quality → {new_q}{RESET}')
    size_hint = {
        '360p':  '~300MB per 45min episode',
        '480p':  '~500MB per 45min episode',
        '720p':  '~900MB per 45min episode',
        '1080p': '~1.5GB per 45min episode',
    }
    hint = size_hint.get(new_q)
    if hint:
        _w(f'  {GREY}heads up — {hint}{RESET}')

def after_unknown_url(url):
    blank()
    _w(f'  {RED}✗  site not supported{RESET}')
    _w(f'  {GREY}supported: NKiri, NaijaVault, PlutoMovies, DramaKey, YouTube, Instagram, TikTok, Pinterest and more{RESET}')
    _w(f'  {GREY}try{RESET} {WHITE}search <title>{RESET} {GREY}to find the show instead{RESET}')

def paused():
    blank()
    _w(f'  {YELLOW}⏸  paused{RESET}  {GREY}·  press Enter to resume, Ctrl+C again to stop{RESET}')

def stopped():
    blank()
    _w(f'  {RED}■  stopped{RESET}')

def resuming():
    blank()
    _w(f'  {BGREEN}▶  resuming…{RESET}')
