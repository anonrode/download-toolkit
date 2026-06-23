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
STOP_FLAG       = [False]   # stops current batch — extractor loops check this
EXIT_FLAG       = [False]   # exits entire script — REPL loop checks this

# Track current download so we can mark_paused() on Ctrl+C
CURRENT_SERIES_URL   = [None]
CURRENT_FILEPATH     = [None]
CURRENT_EXPECTED_SIZE = [0]

# ─── CONFIG ───────────────────────────────────────────────────
DEFAULT_CONFIG = {
    # Download settings
    'quality':              '480p',
    'parallel':             1,
    'bandwidth':            0,
    'disabled_sites':       [],
    
    # Network monitoring
    'network_check_interval': 20,      # Check network every N seconds
    
    # Resolver settings
    'resolver_timeout':     15,         # Max seconds per resolver attempt
    'resolver_retries':     3,          # Max attempts per resolver
    'resolver_backoff_sec': 2,          # Wait between resolver retries
    
    # Download settings
    'download_retries':     3,          # Max attempts per download
    'download_timeout':     120,        # HTTP timeout in seconds
    'min_file_size_mb':     5,          # Minimum file size to consider complete
    'resumable_downloads':  True,       # Resume from byte offset on reconnect
    
    # Parallel download settings
    'parallel_mode':        'queue',    # 'queue' (recommended) or 'thread' (legacy)
    'resolver_threads':     4,          # Parallel resolvers when using queue mode
    
    # Logging
    'enable_progress_log':  True,       # Log downloads to .download.log
    'log_level':            'info',     # 'debug', 'info', 'warn'
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
    global PAUSED, _CTRL_C_COUNT, CURRENT_PROCESS, STOP_FLAG, EXIT_FLAG
    global CURRENT_SERIES_URL, CURRENT_FILEPATH, CURRENT_EXPECTED_SIZE

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
            
            # Mark download as paused in receipt system
            if CURRENT_SERIES_URL[0] and CURRENT_FILEPATH[0]:
                try:
                    from downloader import DownloadReceipt
                    progress_bytes = os.path.getsize(CURRENT_FILEPATH[0]) if os.path.exists(CURRENT_FILEPATH[0]) else 0
                    DownloadReceipt.mark_paused(
                        CURRENT_SERIES_URL[0],
                        CURRENT_FILEPATH[0],
                        progress_bytes,
                        CURRENT_EXPECTED_SIZE[0]
                    )
                except Exception:
                    pass
            
            try:
                sys.stdout.write('\n\n  [pause] Paused — press Enter to resume, Ctrl+C again to stop batch\n\n')
                sys.stdout.flush()
            except Exception:
                pass

        elif _CTRL_C_COUNT[0] == 2:
            PAUSED       = False
            STOP_FLAG[0] = True
            EXIT_FLAG[0] = False
            if proc:
                try: proc.terminate()
                except Exception: pass
            try:
                sys.stdout.write('\n\n  [stop] Batch stopped — back to prompt. Ctrl+C again to exit.\n\n')
                sys.stdout.flush()
            except Exception:
                pass

        else:
            PAUSED       = False
            STOP_FLAG[0] = True
            EXIT_FLAG[0] = True
            if proc:
                try: proc.terminate()
                except Exception: pass
            try:
                sys.stdout.write('\n\n  [exit] Exiting...\n\n')
                sys.stdout.flush()
            except Exception:
                pass

    def sigterm_handler(sig, frame):
        """Called when Android kills Termux from notification or app switcher."""
        proc = CURRENT_PROCESS[0]
        if proc:
            try: proc.terminate()
            except Exception: pass
        # Release wake lock so Termux foreground service stops
        try:
            subprocess.run(['termux-wake-unlock'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=3)
        except Exception:
            pass
        # Kill tmux session so next open starts fresh
        try:
            subprocess.run(['tmux', 'kill-session', '-t', 'download'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=3)
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, handler)
    try:
        signal.signal(signal.SIGTERM, sigterm_handler)
    except Exception:
        pass

def _monitor_network(stop_flag, check_interval=20):
    """
    Background thread: checks network every N seconds.
    DEBOUNCED: requires 2 consecutive checks to confirm state change.
    Prevents rapid pause/resume flapping on flaky connections.
    """
    global PAUSED
    import socket
    
    last_was_online = None  # None = unknown, True = online, False = offline
    fail_count = 0
    success_count = 0
    DEBOUNCE_THRESHOLD = 2
    
    while not stop_flag[0]:
        try:
            # Try TCP connection to Google DNS
            try:
                sock = socket.create_connection(('8.8.8.8', 53), timeout=3)
                sock.close()
                is_online = True
            except OSError:
                is_online = False
            
            # Accumulate success/fail counts — only transition after threshold
            if is_online:
                fail_count = 0
                success_count += 1
                # Transition: 2+ successes and we were not online
                if success_count >= DEBOUNCE_THRESHOLD and last_was_online != True:
                    PAUSED = False
                    from downloader import safe_print
                    safe_print("\n  [✓] Network recovered — resuming downloads\n")
                    last_was_online = True
            else:
                success_count = 0
                fail_count += 1
                # Transition: 2+ failures and we were online
                if fail_count >= DEBOUNCE_THRESHOLD and last_was_online != False:
                    PAUSED = True
                    from downloader import safe_print
                    safe_print("\n  [!] Network down — pausing downloads (will auto-resume when back)\n")
                    last_was_online = False
            
            time.sleep(check_interval)
        except Exception:
            time.sleep(check_interval)

def start_network_monitor(stop_flag):
    """Start network monitoring in background."""
    monitor_thread = threading.Thread(
        target=_monitor_network,
        args=(stop_flag,),
        daemon=True
    )
    monitor_thread.start()
    return monitor_thread

def wait_if_paused():
    global PAUSED, _CTRL_C_COUNT
    if not PAUSED or not sys.stdin.isatty():
        return
    try:
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass
    while PAUSED and not STOP_FLAG[0] and not EXIT_FLAG[0]:
        try:
            input()
            if PAUSED:
                PAUSED = False
                _CTRL_C_COUNT[0] = 0
                print('  [resume] Resuming...\n')
        except EOFError:
            EXIT_FLAG[0] = True
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
        'exit':            EXIT_FLAG,
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
    completed = set()
    for url in q:
        if STOP_FLAG[0]:
            break
        process_link_queue([url], session, ctx)
        completed.add(url)
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
    script_dir  = os.path.dirname(os.path.abspath(__file__))
    stamp_file  = os.path.join(script_dir, '.last_pull')
    pull_interval = 30 * 60  # 30 minutes in seconds

    ytdlp_thread = threading.Thread(target=_update_ytdlp, daemon=True)
    ytdlp_thread.start()

    def _should_pull():
        try:
            if os.path.exists(stamp_file):
                last = float(open(stamp_file).read().strip())
                if time.time() - last < pull_interval:
                    return False
        except Exception:
            pass
        return True

    def _stamp_pull():
        try:
            open(stamp_file, 'w').write(str(time.time()))
        except Exception:
            pass

    def _get_commit():
        try:
            r = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                cwd=script_dir, capture_output=True,
                text=True, timeout=5, stdin=subprocess.DEVNULL
            )
            return r.stdout.strip()
        except Exception:
            return ''

    if not _should_pull():
        return  # pulled recently — skip entirely, start instantly

    if IS_ANDROID:
        try:
            before = _get_commit()
            subprocess.run(
                ['git', 'pull', '-q'],
                cwd=script_dir, capture_output=True,
                text=True, timeout=30, stdin=subprocess.DEVNULL
            )
            _stamp_pull()
            after = _get_commit()
            if before and after and before != after:
                print("[ok] Updated — restarting...")
                sys.stdout.flush()
                ytdlp_thread.join(timeout=2)
                time.sleep(0.5)
                os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception:
            pass
    else:
        try:
            result = subprocess.run(
                ['git', 'pull'], cwd=script_dir,
                capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL
            )
            _stamp_pull()
            if result.returncode == 0 and 'Already up to date' not in result.stdout:
                print("[ok] Toolkit updated — restart to use latest version")
        except Exception:
            pass

# ─── ANDROID SETUP ────────────────────────────────────────────
def setup_android():
    if not IS_ANDROID:
        return None
    wake_proc = None
    try:
        wake_proc = subprocess.Popen(
            ['termux-wake-lock'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        pass
    if not os.environ.get('TMUX'):
        if shutil.which('tmux'):
            # Always kill existing session and start fresh
            subprocess.run(
                ['tmux', 'kill-session', '-t', 'download'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            try:
                os.execvp('tmux', ['tmux', 'new-session', '-s', 'download',
                                   sys.executable] + sys.argv)
            except Exception as e:
                print(f"[!] tmux error: {e}")
        else:
            print("[!] tmux not found — install with: pkg install tmux")
    return wake_proc

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
    print("║              ANONRODE                        ║")
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
    global PAUSED, _CTRL_C_COUNT, STOP_FLAG, EXIT_FLAG

    wake_proc = setup_android()
    auto_update()

    def _release_wake_lock():
        if wake_proc:
            try:
                wake_proc.terminate()
            except Exception:
                pass
        try:
            subprocess.run(['termux-wake-unlock'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=3)
        except Exception:
            pass

    from downloader import check_disk_space, show_history
    from extractors import process_link_queue
    from search import search, fsearch, rebuild_index_command, clear_search_cache

    cfg     = load_config()
    session = make_session()
    setup_signal_handler()
    
    # Start network monitoring thread
    start_network_monitor(EXIT_FLAG)
    
    check_disk_space()
    print_banner(cfg)

    while True:
        if EXIT_FLAG[0]:
            print("\n[*] Exiting...")
            _release_wake_lock()
            break
        # Reset batch stop and pause between commands — not exit flag
        PAUSED           = False
        _CTRL_C_COUNT[0] = 0
        STOP_FLAG[0]     = False

        try:
            raw = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[*] Exiting...")
            _release_wake_lock()
            break

        if not raw:
            continue

        lower = raw.lower()
        parts = raw.split()

        if lower in ('exit', 'quit', 'q'):
            print("[*] Goodbye")
            _release_wake_lock()
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
            print(f"  update                      force update from GitHub")
            print(f"  exit                        quit")
            print(f"{'='*50}")
            print(f"  Ctrl+C once = pause   Ctrl+C twice = stop batch   Ctrl+C 3x = exit")
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

        elif lower == 'update':
            script_dir = os.path.dirname(os.path.abspath(__file__))
            stamp_file = os.path.join(script_dir, '.last_pull')
            print("[*] Checking for updates...")
            try:
                before = subprocess.run(
                    ['git', 'rev-parse', 'HEAD'],
                    cwd=script_dir, capture_output=True,
                    text=True, timeout=5, stdin=subprocess.DEVNULL
                ).stdout.strip()
                subprocess.run(
                    ['git', 'pull', '-q'],
                    cwd=script_dir, capture_output=True,
                    text=True, timeout=30, stdin=subprocess.DEVNULL
                )
                try:
                    open(stamp_file, 'w').write(str(time.time()))
                except Exception:
                    pass
                after = subprocess.run(
                    ['git', 'rev-parse', 'HEAD'],
                    cwd=script_dir, capture_output=True,
                    text=True, timeout=5, stdin=subprocess.DEVNULL
                ).stdout.strip()
                if before and after and before != after:
                    print("[ok] Updated — restarting...")
                    sys.stdout.flush()
                    time.sleep(0.5)
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                else:
                    print("[*] Already up to date")
            except Exception as e:
                print(f"[!] Update failed: {e}")

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
