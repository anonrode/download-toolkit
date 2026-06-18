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
import signal
import threading
import subprocess

# ─── CONSTANTS ────────────────────────────────────────────────
IS_ANDROID  = os.path.exists('/storage/emulated/0')
BASE_DIR    = '/storage/emulated/0/Anon' if IS_ANDROID else os.path.join(os.path.expanduser('~'), 'Downloads', 'Anon')
CONFIG_FILE = os.path.join(BASE_DIR, '.config.json')
QUEUE_FILE  = os.path.join(BASE_DIR, '.queue.json')

# ─── GLOBAL STATE ─────────────────────────────────────────────
PAUSED          = False
_CTRL_C_COUNT   = [0]
CURRENT_PROCESS = [None]
STOP_FLAG       = [False]

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
    global PAUSED, _CTRL_C_COUNT, CURRENT_PROCESS, STOP_FLAG

    def handler(sig, frame):
        global PAUSED
        _CTRL_C_COUNT[0] += 1
        proc = CURRENT_PROCESS[0]
        if _CTRL_C_COUNT[0] == 1:
            PAUSED       = True
            STOP_FLAG[0] = False
            if proc:
                try: proc.terminate()
                except Exception: pass
            try:
                sys.stdout.write('\n\n  [pause] Paused — press Enter to resume, Ctrl+C again to exit\n\n')
                sys.stdout.flush()
            except Exception:
                pass
        else:
            PAUSED       = False
            STOP_FLAG[0] = True
            if proc:
                try: proc.terminate()
                except Exception: pass
            try:
                sys.stdout.write('\n\n  [stop] Stopping...\n\n')
                sys.stdout.flush()
            except Exception:
                pass

    signal.signal(signal.SIGINT, handler)

def wait_if_paused():
    global PAUSED, _CTRL_C_COUNT
    if not PAUSED or not sys.stdin.isatty():
        return
    try:
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass
    while PAUSED and not STOP_FLAG[0]:
        try:
            input()
            if PAUSED:
                PAUSED = False
                _CTRL_C_COUNT[0] = 0
                print('  [resume] Resuming...\n')
        except EOFError:
            STOP_FLAG[0] = True
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
        'disabled_sites':  cfg.get('disabled_sites', []),
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
    q = load_queue()
    if url not in q:
        q.append(url)
        save_queue(q)
        print(f"[+] Added to queue: {url[:60]}")
        print(f"[*] Queue: {len(q)} item(s) — type 'queue start' to begin")
    else:
        print("[*] Already in queue")

def queue_list():
    q = load_queue()
    if not q:
        print("[*] Queue is empty — add URLs with: queue add <url>")
        return
    print(f"\n{'='*50}")
    print(f"  DOWNLOAD QUEUE  ({len(q)} item(s))")
    print(f"{'='*50}")
    for i, url in enumerate(q, 1):
        print(f"  [{i}] {url[:65]}")
    print(f"{'='*50}")

def queue_clear():
    save_queue([])
    print("[*] Queue cleared")

def queue_remove(n):
    q = load_queue()
    if 1 <= n <= len(q):
        removed = q.pop(n - 1)
        save_queue(q)
        print(f"[-] Removed: {removed[:60]}")
    else:
        print("[!] Invalid index")

def queue_run(session, cfg):
    q = load_queue()
    if not q:
        print("[*] Queue is empty — add URLs with: queue add <url>")
        return
    print(f"\n[*] Starting queue — {len(q)} item(s)")
    from extractors import process_link_queue
    ctx = _make_ctx(cfg)
    completed = []
    for url in q:
        if STOP_FLAG[0]:
            break
        process_link_queue([url], session, ctx)
        completed.append(url)
    remaining = [u for u in q if u not in completed]
    save_queue(remaining)
    if not remaining:
        print("[✓] Queue complete — cleared")

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
            print(f"[ok] Quality: {q}")
        else:
            print("[!] Valid: 360p 480p 720p 1080p best")
    elif key == 'parallel' and len(parts) >= 3:
        try:
            n = int(parts[2])
            if 1 <= n <= 3:
                cfg['parallel'] = n
                save_config(cfg)
                print(f"[ok] Parallel: {n}")
            else:
                print("[!] Parallel must be 1-3")
        except ValueError:
            print("[!] Invalid number")
    elif key == 'bandwidth' and len(parts) >= 3:
        try:
            bw = int(parts[2])
            cfg['bandwidth'] = bw
            save_config(cfg)
            print(f"[ok] Bandwidth: {'unlimited' if not bw else f'{bw}KB/s'}")
        except ValueError:
            print("[!] Use KB/s number, e.g. 'settings bandwidth 500'")
    elif key == 'disable' and len(parts) >= 3:
        site     = parts[2].lower()
        disabled = cfg.get('disabled_sites', [])
        if site not in disabled:
            disabled.append(site)
            cfg['disabled_sites'] = disabled
            save_config(cfg)
            print(f"[ok] Disabled: {site}")
        else:
            print("[*] Already disabled")
    elif key == 'enable' and len(parts) >= 3:
        site     = parts[2].lower()
        disabled = cfg.get('disabled_sites', [])
        if site in disabled:
            disabled.remove(site)
            cfg['disabled_sites'] = disabled
            save_config(cfg)
            print(f"[ok] Enabled: {site}")
        else:
            print("[*] Not disabled")
    else:
        _show_settings(cfg)
    return cfg

def _show_settings(cfg):
    bw  = cfg.get('bandwidth', 0)
    dis = cfg.get('disabled_sites', [])
    q   = cfg.get('quality', '480p')
    p   = cfg.get('parallel', 1)
    print(f"\n{'='*50}")
    print(f"  SETTINGS")
    print(f"{'='*50}")
    print(f"  Quality:   {q}")
    print(f"  Parallel:  {p}")
    print(f"  Bandwidth: {'unlimited' if not bw else f'{bw}KB/s'}")
    print(f"  Disabled:  {', '.join(dis) if dis else 'none'}")
    print(f"  Save dir:  {BASE_DIR}")
    print(f"{'='*50}")
    print(f"  settings quality <360p|480p|720p|1080p>")
    print(f"  settings parallel <1|2|3>")
    print(f"  settings bandwidth <KB/s or 0=unlimited>")
    print(f"  settings disable/enable <site>")
    print(f"{'='*50}")

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
    print(f"\n[*] Resuming: {url[:60]}")
    ctx = _make_ctx(cfg)
    process_link_queue([url], session, ctx)

# ─── AUTO UPDATE ──────────────────────────────────────────────
def auto_update():
    from downloader import _update_ytdlp
    script_dir = os.path.dirname(os.path.abspath(__file__))

    ytdlp_thread = threading.Thread(target=_update_ytdlp, daemon=True)
    ytdlp_thread.start()

    if IS_ANDROID:
        try:
            stamp_file = os.path.join(script_dir, '.last_run_commit')
            def _get_commit():
                r = subprocess.run(
                    ['git', 'rev-parse', 'HEAD'],
                    cwd=script_dir, capture_output=True,
                    text=True, timeout=5, stdin=subprocess.DEVNULL
                )
                return r.stdout.strip()
            current = _get_commit()
            last = ''
            if os.path.exists(stamp_file):
                last = open(stamp_file).read().strip()
            if current and current != last:
                open(stamp_file, 'w').write(current)
                if last:
                    print("[ok] Updated — restarting...")
                    sys.stdout.flush()
                    ytdlp_thread.join(timeout=2)
                    time.sleep(0.5)
                    os.execv(sys.executable, [sys.executable] + sys.argv)
            elif current:
                open(stamp_file, 'w').write(current)
        except Exception:
            pass
    else:
        try:
            result = subprocess.run(
                ['git', 'pull'], cwd=script_dir,
                capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL
            )
            if result.returncode == 0 and 'Already up to date' not in result.stdout:
                print("[ok] Toolkit updated — restart to use latest version")
        except Exception:
            pass

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
            check = subprocess.run(
                ['tmux', 'has-session', '-t', 'download'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            if check.returncode == 0:
                try:
                    os.execvp('tmux', ['tmux', 'attach-session', '-t', 'download'])
                except Exception as e:
                    print(f"[!] tmux attach error: {e}")
            else:
                print("[*] Starting tmux session...")
                try:
                    os.execvp('tmux', ['tmux', 'new-session', '-s', 'download',
                                       sys.executable] + sys.argv)
                except Exception as e:
                    print(f"[!] tmux error: {e}")
        else:
            print("[!] tmux not found — install with: pkg install tmux")

# ─── BANNER ───────────────────────────────────────────────────
def print_banner(cfg):
    import shutil as _shutil
    from downloader import get_free_space_gb
    q         = cfg.get('quality', '480p')
    p         = cfg.get('parallel', 1)
    aria2c_ok = bool(_shutil.which('aria2c'))
    ytdlp_ok  = bool(_shutil.which('yt-dlp'))
    try:
        free_gb = get_free_space_gb()
        space_s = f"{free_gb:.1f}GB free"
    except Exception:
        space_s = "unknown"
    print("╔══════════════════════════════════════════════╗")
    print("║         DOWNLOAD TOOLKIT                     ║")
    print(f"║  Quality: {q:<6}   Parallel: {p}               ║")
    print(f"║  aria2c: {'✓' if aria2c_ok else '✗'}   yt-dlp: {'✓' if ytdlp_ok else '✗'}   Storage: {space_s:<10}║")
    print("╠══════════════════════════════════════════════╣")
    print("║  SITES:                                      ║")
    print("║  nkiri • dramakey • dramarain • naijavault   ║")
    print("║  plutomovies • anitaku • myasiantv           ║")
    print("║  naijaprey • 9jarocks • yt/ig/tiktok/fb      ║")
    print("╠══════════════════════════════════════════════╣")
    print("║  COMMANDS:                                   ║")
    print("║  search <title>  • fsearch <title> [hint]    ║")
    print("║  settings • history • resume • clip          ║")
    print("║  queue add/list/start • help • exit          ║")
    print("╚══════════════════════════════════════════════╝")
    print()

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
    global PAUSED, _CTRL_C_COUNT, STOP_FLAG

    setup_android()
    auto_update()

    from downloader import check_disk_space, show_history
    from extractors import process_link_queue
    from search import search, fsearch, rebuild_index_command, clear_search_cache

    cfg     = load_config()
    session = make_session()
    setup_signal_handler()
    check_disk_space()
    print_banner(cfg)

    while True:
        if STOP_FLAG[0]:
            print("\n[*] Exiting...")
            break
        PAUSED           = False
        _CTRL_C_COUNT[0] = 0
        STOP_FLAG[0]     = False

        try:
            raw = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[*] Exiting...")
            break

        if not raw:
            continue

        lower = raw.lower()
        parts = raw.split()

        if lower in ('exit', 'quit', 'q'):
            print("[*] Goodbye")
            break

        elif lower in ('help', 'h', '?'):
            print(f"\n{'='*50}")
            print(f"  COMMANDS")
            print(f"{'='*50}")
            print(f"  search <title>              find a show on NKiri / DramaKey")
            print(f"  fsearch <title> [hint]      fast search — proven patterns first")
            print(f"    hints: korean chinese thai nollywood japanese")
            print(f"  <url>                       paste any supported URL")
            print(f"  clip                        read URL from clipboard")
            print(f"  resume                      resume a paused download")
            print(f"  history                     show download history")
            print(f"  queue add <url>             add URL to queue")
            print(f"  queue list                  show queue")
            print(f"  queue start                 start downloading queue")
            print(f"  queue remove <n>            remove item from queue")
            print(f"  queue clear                 clear entire queue")
            print(f"  settings                    view / change settings")
            print(f"  cache clear                 clear search result cache")
            print(f"  exit                        quit")
            print(f"{'='*50}")
            print(f"  Ctrl+C once = pause   Ctrl+C twice = stop")
            print(f"{'='*50}")

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
                    print(f"[*] From clipboard: {clipped[:70]}")
                    ctx = _make_ctx(cfg)
                    process_link_queue([clipped], session, ctx)
                elif clipped:
                    print(f"[!] Not a URL: {clipped[:60]}")
                else:
                    print("[!] Clipboard is empty")
            except FileNotFoundError:
                print("[!] termux-clipboard-get not found — pkg install termux-api")
            except Exception as e:
                print(f"[!] Clipboard error: {e}")

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
                    print("[!] Usage: queue remove <number>")
            else:
                print("[*] queue add <url> | list | start | clear | remove <n>")

        elif lower == 'index rebuild':
            rebuild_index_command()

        elif lower.startswith('search ') or lower.startswith('s '):
            query = raw.split(' ', 1)[1].strip()
            if query:
                url = search(query, session)
                if url:
                    print(f"\n[*] Downloading: {url[:60]}")
                    ctx = _make_ctx(cfg)
                    process_link_queue([url], session, ctx)
            else:
                print("[!] Usage: search <title>")

        elif lower.startswith('fsearch ') or lower.startswith('fs '):
            query = raw.split(' ', 1)[1].strip()
            if query:
                url = fsearch(query, session)
                if url:
                    print(f"\n[*] Downloading: {url[:60]}")
                    ctx = _make_ctx(cfg)
                    process_link_queue([url], session, ctx)
            else:
                print("[!] Usage: fsearch <title> [korean|chinese|thai|nollywood]")

        elif lower in ('cache clear', 'search cache clear'):
            clear_search_cache()

        elif raw.startswith('http'):
            urls = [u.strip() for u in raw.split() if u.strip().startswith('http')]
            if not urls:
                print("[!] No valid URLs found")
                continue
            ctx = _make_ctx(cfg)
            if len(urls) > 3:
                print(f"[*] {len(urls)} URLs detected")
                ans = input("  Start now or add to queue? [now/queue]: ").strip().lower()
                if ans == 'queue':
                    for u in urls:
                        queue_add(u)
                    continue
            process_link_queue(urls, session, ctx)

        else:
            print(f"[!] Unknown: {raw[:40]}")
            print("[*] Type 'search <title>', paste a URL, or 'help'")

if __name__ == '__main__':
    main()
