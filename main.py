"""
main.py — Download Toolkit entry point.
Handles: REPL, signal handling, settings, download queue, auto-update.
"""

import os
import re
import sys
import json
import time
import shutil
import threading
import subprocess

# ─── CONSTANTS ────────────────────────────────────────────────
IS_ANDROID  = os.path.exists('/storage/emulated/0')
BASE_DIR    = '/storage/emulated/0/Anon' if IS_ANDROID else os.path.join(os.path.expanduser('~'), 'Downloads', 'Anon')
CONFIG_FILE = os.path.join(BASE_DIR, '.config.json')
QUEUE_FILE  = os.path.join(BASE_DIR, '.queue.json')

# ─── GLOBAL STATE ─────────────────────────────────────────────
STOP            = False
PAUSED          = False
_CTRL_C_COUNT   = [0]
CURRENT_PROCESS = [None]
STOP_FLAG       = [False]   # mutable stop flag — shared with ctx so signal reaches extractor loops

# ─── CONFIG ───────────────────────────────────────────────────
DEFAULT_CONFIG = {
    'quality':        '480p',
    'parallel':       1,
    'bandwidth':      0,
    'disabled_sites': [],
}

def load_config():
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
                return {**DEFAULT_CONFIG, **cfg}
    except Exception:
        pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

# ─── SIGNAL HANDLING (Ctrl+C) ─────────────────────────────────
def setup_signal_handler():
    import signal
    global STOP, PAUSED, _CTRL_C_COUNT, CURRENT_PROCESS, STOP_FLAG

    def handler(sig, frame):
        global STOP, PAUSED, _CTRL_C_COUNT
        _CTRL_C_COUNT[0] += 1
        if _CTRL_C_COUNT[0] == 1:
            PAUSED = True
            STOP_FLAG[0] = False
            proc = CURRENT_PROCESS[0]
            if proc:
                try:
                    proc.terminate()
                except Exception:
                    pass
            from ui import paused; paused()
        else:
            STOP   = True
            PAUSED = False
            STOP_FLAG[0] = True
            proc   = CURRENT_PROCESS[0]
            if proc:
                try:
                    proc.terminate()
                except Exception:
                    pass
            from ui import stopped; stopped()

    signal.signal(signal.SIGINT, handler)

def wait_if_paused():
    global PAUSED, STOP, _CTRL_C_COUNT
    if not PAUSED or not sys.stdin.isatty():
        return
    try:
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass
    while PAUSED and not STOP:
        try:
            input()
            if PAUSED:
                PAUSED = False
                _CTRL_C_COUNT[0] = 0
                from ui import resuming; resuming()
        except EOFError:
            STOP = True
            break

def _quality_str(q):
    q = str(q)
    if '1080' in q: return 'bestvideo[height<=1080]+bestaudio/best[height<=1080]'
    if '720'  in q: return 'bestvideo[height<=720]+bestaudio/best[height<=720]'
    if '480'  in q: return 'bestvideo[height<=480]+bestaudio/best[height<=480]'
    if '360'  in q: return 'bestvideo[height<=360]+bestaudio/best[height<=360]'
    return 'bestvideo[height<=480]+bestaudio/best[height<=480]'

def _make_ctx(cfg):
    return {
        'stop':            STOP_FLAG,
        'wait':            wait_if_paused,
        'bandwidth':       cfg.get('bandwidth', 0),
        'quality':         _quality_str(cfg.get('quality', '480p')),
        'parallel':        cfg.get('parallel', 1),
        'current_process': CURRENT_PROCESS,
    }

# ─── QUEUE ────────────────────────────────────────────────────
def load_queue():
    try:
        if os.path.exists(QUEUE_FILE):
            with open(QUEUE_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return []

def save_queue(q):
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(QUEUE_FILE, 'w') as f:
            json.dump(q, f, indent=2)
    except Exception:
        pass

def queue_add(url):
    from ui import success, info
    q = load_queue()
    if url not in q:
        q.append(url)
        save_queue(q)
        success(f'added to queue: {url[:60]}')
        info(f'queue: {len(q)} item(s)  —  type queue start to begin')
    else:
        info('already in queue')

def queue_list():
    from ui import info, blank, sep, _w, GREY, RESET, WHITE, BCYAN
    q = load_queue()
    if not q:
        info('queue is empty — add URLs with: queue add <url>')
        return
    blank()
    _w(f'  {WHITE}QUEUE{RESET}  {GREY}·  {len(q)} item(s){RESET}')
    sep()
    for i, url in enumerate(q, 1):
        _w(f'  {GREY}[{i}]{RESET}  {BCYAN}{url[:65]}{RESET}')
    sep()

def queue_clear():
    from ui import info
    save_queue([])
    info('queue cleared')

def queue_remove(n):
    from ui import success, warn
    q = load_queue()
    if 1 <= n <= len(q):
        removed = q.pop(n - 1)
        save_queue(q)
        success(f'removed: {removed[:60]}')
    else:
        warn('invalid index')

def queue_run(session, cfg):
    from ui import info, success
    q = load_queue()
    if not q:
        info('queue is empty — add URLs with: queue add <url>')
        return
    info(f'starting queue — {len(q)} item(s)')
    from extractors import process_link_queue
    ctx = _make_ctx(cfg)
    process_link_queue(q, session, ctx)
    if not STOP:
        save_queue([])
        success('queue complete — cleared')

# ─── SETTINGS ─────────────────────────────────────────────────
def handle_settings(parts, cfg):
    if len(parts) == 1:
        _show_settings(cfg)
        return cfg
    key = parts[1].lower()
    if key == 'quality' and len(parts) >= 3:
        q = parts[2]
        if q in ('360p', '480p', '720p', '1080p', 'best'):
            cfg['quality'] = q
            save_config(cfg)
            from ui import success; success(f"quality set to {q}")
        else:
            from ui import warn; warn("valid options: 360p 480p 720p 1080p")
    elif key == 'parallel' and len(parts) >= 3:
        try:
            n = int(parts[2])
            if 1 <= n <= 3:
                cfg['parallel'] = n
                save_config(cfg)
                from ui import success; success(f"parallel set to {n}")
            else:
                from ui import warn; warn("parallel must be 1, 2 or 3")
        except ValueError:
            from ui import warn; warn("invalid number")
    elif key == 'bandwidth' and len(parts) >= 3:
        try:
            bw = int(parts[2])
            cfg['bandwidth'] = bw
            save_config(cfg)
            from ui import success; success(f"bandwidth set to {'unlimited' if not bw else f'{bw}KB/s'}")
        except ValueError:
            from ui import warn; warn("enter a number in KB/s, e.g. settings bandwidth 500")
    elif key == 'disable' and len(parts) >= 3:
        site     = parts[2].lower()
        disabled = cfg.get('disabled_sites', [])
        if site not in disabled:
            disabled.append(site)
            cfg['disabled_sites'] = disabled
            save_config(cfg)
            from ui import success; success(f"disabled: {site}")
        else:
            from ui import info; info("already disabled")
    elif key == 'enable' and len(parts) >= 3:
        site     = parts[2].lower()
        disabled = cfg.get('disabled_sites', [])
        if site in disabled:
            disabled.remove(site)
            cfg['disabled_sites'] = disabled
            save_config(cfg)
            from ui import success; success(f"enabled: {site}")
        else:
            from ui import info; info("not disabled")
    else:
        _show_settings(cfg)
    return cfg

def _show_settings(cfg):
    """Interactive settings — pick a number to change."""
    from ui import BCYAN, RESET, GREY, WHITE, BGREEN, YELLOW, _w, blank, sep

    QUALITY_OPTIONS = ['360p', '480p', '720p', '1080p']

    while True:
        bw = cfg.get('bandwidth', 0)
        q  = cfg.get('quality', '480p')
        p  = cfg.get('parallel', 1)

        blank()
        _w(f'  {WHITE}SETTINGS{RESET}')
        sep()
        _w(f'  {GREY}[1]{RESET}  Quality     {BCYAN}{q}{RESET}')
        _w(f'  {GREY}[2]{RESET}  Parallel    {BCYAN}{p} thread{"s" if p > 1 else ""}{RESET}')
        _w(f'  {GREY}[3]{RESET}  Bandwidth   {BCYAN}{"unlimited" if not bw else f"{bw}KB/s"}{RESET}')
        _w(f'  {GREY}[4]{RESET}  Save dir    {GREY}{BASE_DIR}{RESET}')
        sep()
        _w(f'  {GREY}pick a number to change, or Enter to go back{RESET}')

        try:
            choice = input('\n  › ').strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not choice:
            break

        if choice == '1':
            blank()
            _w(f'  {WHITE}QUALITY{RESET}')
            sep()
            hints = {'360p': 'faster, smaller files', '480p': 'default',
                     '720p': 'better quality', '1080p': 'best, largest files'}
            for i, opt in enumerate(QUALITY_OPTIONS, 1):
                marker = f'{BGREEN}←{RESET}' if opt == q else ''
                _w(f'  {GREY}[{i}]{RESET}  {opt}  {GREY}{hints[opt]}{RESET}  {marker}')
            sep()
            try:
                pick = input('\n  › ').strip()
                if pick.isdigit() and 1 <= int(pick) <= len(QUALITY_OPTIONS):
                    new_q = QUALITY_OPTIONS[int(pick) - 1]
                    cfg['quality'] = new_q
                    save_config(cfg)
                    from ui import after_quality_change
                    after_quality_change(new_q)
            except (EOFError, KeyboardInterrupt):
                pass

        elif choice == '2':
            blank()
            _w(f'  {WHITE}PARALLEL DOWNLOADS{RESET}')
            sep()
            for i, label, desc in [(1,'1 thread','one at a time (default)'),(2,'2 threads','two at once'),(3,'3 threads','three at once')]:
                marker = f'{BGREEN}←{RESET}' if p == i else ''
                _w(f'  {GREY}[{i}]{RESET}  {label}  {GREY}{desc}{RESET}  {marker}')
            sep()
            try:
                pick = input('\n  › ').strip()
                if pick in ('1', '2', '3'):
                    cfg['parallel'] = int(pick)
                    save_config(cfg)
                    _w(f'\n  {BGREEN}✓  parallel → {pick} thread{"s" if int(pick) > 1 else ""}{RESET}')
            except (EOFError, KeyboardInterrupt):
                pass

        elif choice == '3':
            blank()
            _w(f'  {WHITE}BANDWIDTH LIMIT{RESET}')
            sep()
            _w(f'  {GREY}enter a limit in KB/s, or 0 for unlimited{RESET}')
            _w(f'  {GREY}current: {"unlimited" if not bw else f"{bw}KB/s"}{RESET}')
            sep()
            try:
                pick = input('\n  › ').strip()
                if pick.isdigit():
                    cfg['bandwidth'] = int(pick)
                    save_config(cfg)
                    label = 'unlimited' if not int(pick) else f'{pick}KB/s'
                    _w(f'\n  {BGREEN}✓  bandwidth → {label}{RESET}')
            except (EOFError, KeyboardInterrupt):
                pass

# ─── RESUME ───────────────────────────────────────────────────
def handle_resume_command(session, cfg):
    from downloader import show_resume_list, load_resume_state
    from extractors import process_link_queue
    if not show_resume_list():
        return
    try:
        choice = int(input("\n  Pick number to resume (0 to cancel): ").strip())
    except (ValueError, EOFError):
        return
    state = load_resume_state()
    urls  = list(state.keys())
    if choice == 0 or choice > len(urls):
        return
    url = urls[choice - 1]
    from ui import info
    info(f'resuming: {url[:60]}')
    ctx = _make_ctx(cfg)
    process_link_queue([url], session, ctx)

# ─── AUTO UPDATE ──────────────────────────────────────────────
def auto_update():
    from downloader import _update_ytdlp
    _update_ytdlp()
    def _pull():
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            result = subprocess.run(
                ['git', 'pull'], cwd=script_dir,
                capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL
            )
            if result.returncode == 0 and 'Already up to date' not in result.stdout:
                from ui import success
                success('updated to latest version')
        except Exception:
            pass
    threading.Thread(target=_pull, daemon=True).start()

# ─── ANDROID SETUP ────────────────────────────────────────────
def setup_android():
    if not IS_ANDROID:
        return
    try:
        subprocess.Popen(['termux-wake-lock'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    if not os.environ.get('TMUX'):
        if shutil.which('tmux'):
            from ui import info; info("starting tmux session...")
            try:
                subprocess.run(['tmux', 'kill-session', '-t', 'download'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                os.execvp('tmux', ['tmux', 'new-session', '-s', 'download',
                                   sys.executable] + sys.argv)
            except Exception as e:
                from ui import warn; warn(f"tmux error: {e}")
        else:
            from ui import warn; warn("tmux not found — install with: pkg install tmux")

# ─── BANNER ───────────────────────────────────────────────────
def print_banner(cfg):
    import shutil
    from ui import print_splash
    from downloader import get_free_space_gb
    aria2c_ok = bool(shutil.which('aria2c'))
    ytdlp_ok  = bool(shutil.which('yt-dlp'))
    try:
        free_gb = get_free_space_gb()
    except Exception:
        free_gb = None
    print_splash(cfg, aria2c_ok=aria2c_ok, ytdlp_ok=ytdlp_ok, free_gb=free_gb)

# ─── SESSION FACTORY ──────────────────────────────────────────
def make_session():
    from downloader import UA_DESKTOP
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

# ─── MAIN REPL ────────────────────────────────────────────────
def main():
    global STOP, PAUSED, _CTRL_C_COUNT

    setup_android()
    auto_update()

    from downloader import check_disk_space, show_history
    from extractors import process_link_queue
    from search import search, rebuild_index_command

    cfg     = load_config()
    session = make_session()
    setup_signal_handler()
    check_disk_space()
    print_banner(cfg)

    while True:
        STOP             = False
        PAUSED           = False
        _CTRL_C_COUNT[0] = 0
        STOP_FLAG[0]     = False

        try:
            from ui import prompt_line
            prompt_line(cfg)
            raw = input('').strip()
        except (EOFError, KeyboardInterrupt):
            from ui import info
            info('exiting...')
            break

        if not raw:
            continue

        lower = raw.lower()
        parts = raw.split()

        if lower in ('exit', 'quit', 'q'):
            from ui import info
            info('goodbye')
            break

        elif lower == 'history':
            show_history()

        elif lower == 'resume':
            handle_resume_command(session, cfg)

        elif lower == 'clip':
            try:
                result  = subprocess.run(['termux-clipboard-get'],
                                         capture_output=True, text=True, timeout=5)
                clipped = result.stdout.strip()
                if clipped.startswith('http'):
                    from ui import info; info(f"from clipboard: {clipped[:70]}")
                    ctx = _make_ctx(cfg)
                    process_link_queue([clipped], session, ctx)
                elif clipped:
                    from ui import warn; warn(f"not a URL: {clipped[:60]}")
                else:
                    from ui import warn; warn("clipboard is empty")
            except FileNotFoundError:
                from ui import warn; warn("termux-clipboard-get not found — pkg install termux-api")
            except Exception as e:
                from ui import warn; warn(f"clipboard error: {e}")

        elif lower.startswith('settings'):
            cfg = handle_settings(parts, cfg)

        elif lower.startswith('queue'):
            if len(parts) == 1 or parts[1] == 'list':
                queue_list()
            elif parts[1] == 'add' and len(parts) >= 3:
                queue_add(parts[2])
            elif parts[1] == 'clear':
                queue_clear()
            elif parts[1] in ('start', 'run'):
                queue_run(session, cfg)
            elif parts[1] == 'remove' and len(parts) >= 3:
                try:
                    queue_remove(int(parts[2]))
                except ValueError:
                    from ui import warn; warn("usage: queue remove <number>")
            else:
                from ui import info; info("usage: queue add <url> | list | start | clear | remove <n>")

        elif lower == 'index rebuild':
            rebuild_index_command()

        elif lower.startswith('search ') or lower.startswith('s '):
            query = raw.split(' ', 1)[1].strip()
            if query:
                url = search(query, session)
                if url:
                    from ui import info
                    info(f'downloading: {url[:60]}')
                    ctx = _make_ctx(cfg)
                    process_link_queue([url], session, ctx)
            else:
                from ui import warn; warn("usage: search <title>")

        elif raw.startswith('http'):
            urls = [u.strip() for u in raw.split() if u.strip().startswith('http')]
            if not urls:
                from ui import warn; warn("no valid URLs found")
                continue
            ctx = _make_ctx(cfg)
            if len(urls) > 3:
                from ui import info; info(f"{len(urls)} URLs detected")
                ans = input("  Start now or add to queue? [now/queue]: ").strip().lower()
                if ans == 'queue':
                    for u in urls:
                        queue_add(u)
                    continue
            process_link_queue(urls, session, ctx)

        else:
            from ui import warn, info
            warn(f'unknown command: {raw[:40]}')
            info("type  search <title>  to find a show, or paste a URL")

if __name__ == '__main__':
    main()
