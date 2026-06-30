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

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# ─── CONSTANTS ────────────────────────────────────────────────
IS_ANDROID  = os.path.exists('/storage/emulated/0')
BASE_DIR    = '/storage/emulated/0/Anon' if IS_ANDROID else os.path.join(os.path.expanduser('~'), 'Downloads', 'Anon')
CONFIG_FILE = os.path.join(BASE_DIR, '.config.json')
QUEUE_FILE  = os.path.join(BASE_DIR, '.queue.json')

# ─── GLOBAL STATE ─────────────────────────────────────────────
_CTRL_C_COUNT   = [0]
CURRENT_PROCESS = [None]
STOP_FLAG       = [False]   # stops current batch — extractor loops check this
EXIT_FLAG       = [False]   # exits entire script — REPL loop checks this
PAUSE_FLAG      = [False]   # toggles aria2c SIGSTOP/SIGCONT — Ctrl+P

# Track current download so we can mark_paused() on Ctrl+C
CURRENT_SERIES_URL   = [None]
CURRENT_SERIES_NAME  = [None]
CURRENT_FILEPATH     = [None]
CURRENT_EPISODE_NAME = [None]
CURRENT_EXPECTED_SIZE = [0]

# Lock for thread-safe access to CURRENT_* globals
CURRENT_STATE_LOCK = threading.Lock()

def _set_current_state(series_url, series_name, episode_name, filepath, expected_size):
    """Safely set CURRENT_* globals with locking for parallel downloads."""
    global CURRENT_SERIES_URL, CURRENT_SERIES_NAME, CURRENT_FILEPATH, CURRENT_EPISODE_NAME, CURRENT_EXPECTED_SIZE
    with CURRENT_STATE_LOCK:
        CURRENT_SERIES_URL[0] = series_url
        CURRENT_SERIES_NAME[0] = series_name
        CURRENT_EPISODE_NAME[0] = episode_name
        CURRENT_FILEPATH[0] = filepath
        CURRENT_EXPECTED_SIZE[0] = expected_size or 0

def _reset_current_state():
    """Reset all CURRENT_* tracking variables between downloads."""
    global CURRENT_SERIES_URL, CURRENT_SERIES_NAME, CURRENT_FILEPATH, CURRENT_EPISODE_NAME, CURRENT_EXPECTED_SIZE
    CURRENT_SERIES_URL[0]   = None
    CURRENT_SERIES_NAME[0]  = None
    CURRENT_FILEPATH[0]     = None
    CURRENT_EPISODE_NAME[0] = None
    CURRENT_EXPECTED_SIZE[0] = 0

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
    'ytdlp_channel':        'master',   # 'master' (--pre, newest fixes) or 'stable'
                                         # — only affects the explicit `update` command;
                                         # silent background auto-update always uses stable
    
    # Parallel download settings
    'parallel_mode':        'queue',    # 'queue' (recommended) or 'thread' (legacy)
    'resolver_threads':     4,          # Parallel resolvers when using queue mode
    
    # Logging
    'enable_progress_log':  True,       # Log downloads to .download.log
    'log_level':            'normal',   # 'normal' or 'debug'
    'auto_update_days':     7,          # Weekly auto-update cadence
    'social_quality':       '720p',     # Prefer 720p for non-YouTube social videos
}

def load_config():
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
                merged = {**DEFAULT_CONFIG, **cfg}
                if merged.get('log_level') not in ('normal', 'debug'):
                    merged['log_level'] = 'normal'
                return merged
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
    global _CTRL_C_COUNT, CURRENT_PROCESS, STOP_FLAG, EXIT_FLAG
    global CURRENT_SERIES_URL, CURRENT_FILEPATH, CURRENT_EXPECTED_SIZE

    def handler(sig, frame):
        _CTRL_C_COUNT[0] += 1
        proc = CURRENT_PROCESS[0]

        if _CTRL_C_COUNT[0] == 1:
            STOP_FLAG[0] = True
            if proc:
                try: proc.terminate()
                except Exception: pass
            try:
                from downloader import terminate_active_processes
                terminate_active_processes()
            except Exception:
                pass
            
            # Mark download as paused in both receipt and resume state
            if CURRENT_SERIES_URL[0] and CURRENT_EPISODE_NAME[0] and CURRENT_FILEPATH[0]:
                try:
                    from downloader import DownloadReceipt, mark_episode_current
                    progress_bytes = os.path.getsize(CURRENT_FILEPATH[0]) if os.path.exists(CURRENT_FILEPATH[0]) else 0
                    episode_key = f"{CURRENT_SERIES_URL[0]}:{CURRENT_EPISODE_NAME[0]}"
                    
                    # Update receipt (per-episode)
                    DownloadReceipt.mark_paused(
                        episode_key,
                        CURRENT_FILEPATH[0],
                        progress_bytes,
                        CURRENT_EXPECTED_SIZE[0]
                    )
                    
                    # Update resume state (series-level) so it shows in resume list
                    if CURRENT_SERIES_NAME[0] and CURRENT_EPISODE_NAME[0]:
                        mark_episode_current(
                            CURRENT_SERIES_URL[0],
                            CURRENT_SERIES_NAME[0],
                            CURRENT_EPISODE_NAME[0]
                        )
                except Exception:
                    pass
            
            try:
                sys.stdout.write('\n\n  [pause] Paused — use the resume command to continue. Ctrl+C again to exit.\n\n')
                sys.stdout.flush()
            except Exception:
                pass

        elif _CTRL_C_COUNT[0] == 2:
            STOP_FLAG[0] = True
            EXIT_FLAG[0] = False
            if proc:
                try: proc.terminate()
                except Exception: pass
            try:
                from downloader import terminate_active_processes
                terminate_active_processes()
            except Exception:
                pass
            try:
                sys.stdout.write('\n\n  [stop] Batch stopped — back to prompt. Ctrl+C again to exit.\n\n')
                sys.stdout.flush()
            except Exception:
                pass

        else:
            STOP_FLAG[0] = True
            EXIT_FLAG[0] = True
            if proc:
                try: proc.terminate()
                except Exception: pass
            try:
                from downloader import terminate_active_processes
                terminate_active_processes()
            except Exception:
                pass
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
        try:
            from downloader import terminate_active_processes
            terminate_active_processes()
        except Exception:
            pass
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

def _start_pause_listener():
    """
    Background thread that reads raw keypresses from /dev/tty.
    Ctrl+P (0x10) toggles SIGSTOP/SIGCONT on the active aria2c process.
    Exits cleanly when EXIT_FLAG is set.
    Only active on platforms that have /dev/tty (Termux/Linux).
    """
    import os
    if not os.path.exists('/dev/tty'):
        return  # Git Bash / Windows — silently skip

    def _reader():
        import termios, tty, select
        while not EXIT_FLAG[0]:
            # Wait until there is an active download process before capturing keystrokes
            if CURRENT_PROCESS[0] is None:
                time.sleep(0.1)
                continue

            try:
                fd = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY)
                old = termios.tcgetattr(fd)
                tty.setraw(fd)
                try:
                    while CURRENT_PROCESS[0] is not None and not EXIT_FLAG[0]:
                        # Non-blocking read — poll every 100ms
                        r, _, _ = select.select([fd], [], [], 0.1)
                        if not r:
                            continue
                        ch = os.read(fd, 1)
                        if ch == b'\x10':  # Ctrl+P
                            proc = CURRENT_PROCESS[0]
                            if proc is None:
                                continue
                            if PAUSE_FLAG[0]:
                                # Currently paused — resume
                                PAUSE_FLAG[0] = False
                                try:
                                    import signal as _sig
                                    os.kill(proc.pid, _sig.SIGCONT)
                                except Exception:
                                    pass
                                try:
                                    sys.stdout.write('\n  [▶] Resumed\n')
                                    sys.stdout.flush()
                                except Exception:
                                    pass
                            else:
                                # Currently running — pause
                                PAUSE_FLAG[0] = True
                                try:
                                    import signal as _sig
                                    os.kill(proc.pid, _sig.SIGSTOP)
                                except Exception:
                                    pass
                                try:
                                    sys.stdout.write('\n  [‖] Paused — Ctrl+P to resume\n')
                                    sys.stdout.flush()
                                except Exception:
                                    pass
                finally:
                    # Restore cooked terminal mode as soon as the download finishes
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                    os.close(fd)
            except Exception:
                time.sleep(0.5)  # Avoid tight loop in case of errors

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

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
        'pause':           PAUSE_FLAG,
        'bandwidth':       cfg.get('bandwidth', 0),
        'quality':         _quality_str(cfg.get('quality', '480p')),
        'social_quality':  cfg.get('social_quality', '720p'),
        'log_level':       cfg.get('log_level', 'normal'),
        'parallel':        cfg.get('parallel', 1),
        'current_process': CURRENT_PROCESS,
        'disabled_sites':  cfg.get('disabled_sites', []),
    }

def _parse_episode_selection(spec):
    selected = set()
    for part in spec.replace(' ', '').split(','):
        if not part:
            continue
        if '-' in part:
            a, b = part.split('-', 1)
            start, end = int(a), int(b)
            if start <= 0 or end < start:
                raise ValueError
            selected.update(range(start, end + 1))
        else:
            n = int(part)
            if n <= 0:
                raise ValueError
            selected.add(n)
    if not selected:
        raise ValueError
    return selected

def _ctx_with_episode_filter(cfg, spec):
    ctx = _make_ctx(cfg)
    ctx['episode_filter'] = _parse_episode_selection(spec)
    return ctx

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
    elif key in ('log', 'mode') and len(parts) >= 3:
        mode = parts[2].lower()
        if mode in ('normal', 'debug'):
            cfg['log_level'] = mode
            save_config(cfg)
            try:
                from downloader import set_output_mode
                set_output_mode(mode)
            except Exception:
                pass
            print(f"[ok] Output mode: {mode}")
        else:
            print("[!] Valid: settings log normal | settings log debug")
    elif key in ('social-quality', 'social_quality') and len(parts) >= 3:
        q = parts[2].lower()
        if q in ('360p', '480p', '720p', '1080p', 'best'):
            cfg['social_quality'] = q
            save_config(cfg)
            print(f"[ok] Social quality: {q}")
        else:
            print("[!] Valid: 360p 480p 720p 1080p best")
    elif key in ('auto-update', 'autoupdate') and len(parts) >= 3:
        try:
            days = int(parts[2])
            if 1 <= days <= 30:
                cfg['auto_update_days'] = days
                save_config(cfg)
                print(f"[ok] Auto-update: every {days} day(s)")
            else:
                print("[!] Use 1-30 days")
        except ValueError:
            print("[!] Use days, e.g. settings auto-update 7")
    elif key == 'timeout' and len(parts) >= 3:
        try:
            secs = int(parts[2])
            if 30 <= secs <= 600:
                cfg['download_timeout'] = secs
                save_config(cfg)
                print(f"[ok] Timeout: {secs}s")
            else:
                print("[!] Use 30-600 seconds")
        except ValueError:
            print("[!] Use seconds, e.g. settings timeout 180")
    elif key == 'ytdlp-channel' and len(parts) >= 3:
        channel = parts[2].lower()
        if channel in ('master', 'stable'):
            cfg['ytdlp_channel'] = channel
            save_config(cfg)
            print(f"[ok] yt-dlp channel: {channel}")
            if channel == 'stable':
                print("[*] Run 'update' to switch the installed yt-dlp back to stable now")
        else:
            print("[!] Use master or stable")
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
    mode = cfg.get('log_level', 'normal')
    social_q = cfg.get('social_quality', '720p')
    auto_days = cfg.get('auto_update_days', 7)
    timeout = cfg.get('download_timeout', 120)
    ytdlp_channel = cfg.get('ytdlp_channel', 'master')
    print(f"\n{'='*50}")
    print(f"  SETTINGS")
    print(f"{'='*50}")
    print(f"  Quality:   {q}")
    print(f"  Parallel:  {p}")
    print(f"  Bandwidth: {'unlimited' if not bw else f'{bw}KB/s'}")
    print(f"  Output:    {mode}")
    print(f"  Social:    {social_q} auto")
    print(f"  Update:    every {auto_days} day(s)")
    print(f"  Timeout:   {timeout}s (aria2c stall limit)")
    print(f"  yt-dlp:    {ytdlp_channel} channel (used by 'update' command)")
    print(f"  Disabled:  {', '.join(dis) if dis else 'none'}")
    print(f"  Save dir:  {BASE_DIR}")
    print(f"{'='*50}")
    print(f"  settings quality <360p|480p|720p|1080p>")
    print(f"  settings parallel <1|2|3>")
    print(f"  settings bandwidth <KB/s or 0=unlimited>")
    print(f"  settings log <normal|debug>")
    print(f"  settings social-quality <360p|480p|720p|1080p|best>")
    print(f"  settings auto-update <days>")
    print(f"  settings timeout <seconds (30-600)>")
    print(f"  settings ytdlp-channel <master|stable>")
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
def auto_update(cfg=None):
    from downloader import _update_ytdlp
    script_dir  = os.path.dirname(os.path.abspath(__file__))
    stamp_file  = os.path.join(script_dir, '.last_pull')
    days = int((cfg or {}).get('auto_update_days', 7))
    pull_interval = max(1, days) * 24 * 60 * 60

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

    ytdlp_thread = threading.Thread(target=_update_ytdlp, daemon=True)
    ytdlp_thread.start()

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
    from downloader import get_free_space_gb, ui_screen, update_status
    q         = cfg.get('quality', '480p')
    p         = cfg.get('parallel', 1)
    aria2c_ok = bool(_shutil.which('aria2c'))
    ytdlp_ok  = bool(_shutil.which('yt-dlp'))
    try:
        free_gb = get_free_space_gb()
        space_s = f"{free_gb:.1f}GB free"
    except Exception:
        space_s = "unknown"
    update_status(screen='Ready', status='Idle')
    ui_screen('Ready', [
        ('Quality', q),
        ('Parallel', p),
        ('Output', cfg.get('log_level', 'normal')),
        ('Social', f"{cfg.get('social_quality', '720p')} auto"),
        ('aria2c', 'OK' if aria2c_ok else 'Missing'),
        ('yt-dlp', 'OK' if ytdlp_ok else 'Missing'),
        ('Storage', space_s),
    ], footer="Type help for commands.")

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

def handle_status(cfg):
    from downloader import get_status, load_resume_state, get_free_space_gb, ui_screen
    q = load_queue()
    resume = load_resume_state()
    failed = sum(len(v.get('failed', [])) for v in resume.values())
    paused = sum(1 for v in resume.values() if v.get('current'))
    st = get_status()
    ui_screen('Status', [
        ('State', st.get('status', 'Idle')),
        ('Screen', st.get('screen', 'Ready')),
        ('Title', st.get('title', '')),
        ('Current', st.get('current', '')),
        ('Progress', st.get('progress', '')),
        ('Queue', f'{len(q)} waiting'),
        ('Paused', paused),
        ('Failed', failed),
        ('Storage', f'{get_free_space_gb():.1f}GB free'),
        ('Output', cfg.get('log_level', 'normal')),
    ])

def handle_doctor(cfg):
    from downloader import get_free_space_gb, ui_screen
    checks = []
    def ok_missing(name, ok, fix=''):
        checks.append((name, 'OK' if ok else f'Missing{(" - " + fix) if fix else ""}'))

    ok_missing('Python', True)
    ok_missing('yt-dlp', bool(shutil.which('yt-dlp')), 'pip install yt-dlp')
    ok_missing('aria2c', bool(shutil.which('aria2c')), 'pkg install aria2')
    ok_missing('ffmpeg', bool(shutil.which('ffmpeg')), 'pkg install ffmpeg')
    try:
        import requests  # noqa
        ok_missing('requests', True)
    except Exception:
        ok_missing('requests', False, 'pip install requests')
    try:
        import bs4  # noqa
        ok_missing('bs4', True)
    except Exception:
        ok_missing('bs4', False, 'pip install beautifulsoup4')
    ok_missing('Storage', get_free_space_gb() > 1.0, f'{get_free_space_gb():.1f}GB free')
    if IS_ANDROID:
        ok_missing('Termux API', bool(shutil.which('termux-clipboard-get')), 'pkg install termux-api')
    try:
        import requests
        r = requests.get('https://example.com', timeout=8)
        ok_missing('Internet', r.status_code < 500)
    except Exception:
        ok_missing('Internet', False, 'check network')
    ui_screen('Doctor', checks)

def handle_retry_failed(session, cfg):
    from downloader import load_resume_state, ui_screen
    from extractors import process_link_queue
    state = load_resume_state()
    failed_urls = [(u, v) for u, v in state.items() if v.get('failed')]
    if not failed_urls:
        ui_screen('Retry Failed', [('Status', 'No failed episodes found')])
        return
    ui_screen('Retry Failed', [
        ('Found', f'{len(failed_urls)} series with failed episodes'),
        ('Action', 'Rechecking pages and retrying failed/missing files'),
    ])
    ctx = _make_ctx(cfg)
    for url, _ in failed_urls:
        if STOP_FLAG[0]:
            break
        process_link_queue([url], session, ctx)

def handle_cleanup():
    from downloader import BASE_DIR, RECEIPT_FILE, DownloadReceipt, ui_screen
    removed = 0
    bytes_removed = 0
    if os.path.exists(BASE_DIR):
        for root, _, files in os.walk(BASE_DIR):
            for name in files:
                path = os.path.join(root, name)
                remove = name.startswith('.aria2_') or name.endswith('.aria2')
                if not remove and name.lower().endswith(('.mp4', '.mkv', '.webm', '.m4a')):
                    try:
                        remove = os.path.getsize(path) < 100 * 1024
                    except Exception:
                        remove = False
                if remove:
                    try:
                        size = os.path.getsize(path)
                        os.remove(path)
                        removed += 1
                        bytes_removed += size
                    except Exception:
                        pass
    receipts = DownloadReceipt.load_all()
    cleaned_receipts = {}
    for key, receipt in receipts.items():
        fp = receipt.get('filepath')
        if fp and receipt.get('status') in ('done', 'paused') and not os.path.exists(fp):
            continue
        cleaned_receipts[key] = receipt
    if cleaned_receipts != receipts:
        DownloadReceipt.save_all(cleaned_receipts)
    ui_screen('Cleanup', [
        ('Files', f'{removed} removed'),
        ('Space', f'{bytes_removed / (1024 * 1024):.1f} MB'),
        ('Receipts', f'{len(receipts) - len(cleaned_receipts)} stale removed'),
    ])

# ─── MAIN REPL ────────────────────────────────────────────────
def main():
    global _CTRL_C_COUNT, STOP_FLAG, EXIT_FLAG

    wake_proc = setup_android()
    cfg = load_config()
    auto_update(cfg)

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

    from downloader import show_history, register_state_callback, set_output_mode
    from extractors import process_link_queue
    from search import search, fsearch, rebuild_index_command, clear_search_cache

    # Register callback for download state tracking
    register_state_callback(_set_current_state)

    set_output_mode(cfg.get('log_level', 'normal'))
    session = make_session()
    setup_signal_handler()
    _start_pause_listener()
    
    print_banner(cfg)

    while True:
        if EXIT_FLAG[0]:
            print("\n[*] Exiting...")
            _release_wake_lock()
            break
        # Reset batch state between commands
        _reset_current_state()
        _CTRL_C_COUNT[0] = 0
        STOP_FLAG[0]     = False
        PAUSE_FLAG[0]    = False

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
            print(f"  status                      show current app/download state")
            print(f"  doctor                      check dependencies and environment")
            print(f"  history                     show download history")
            print(f"  retry failed                retry failed downloads")
            print(f"  cleanup                     remove stale helper/tiny files")
            print(f"  download <range> <url>      download selected episodes, e.g. 1-5,8")
            print(f"  queue add <url>             add URL to queue")
            print(f"  queue list                  show queue")
            print(f"  queue start                 start downloading queue")
            print(f"  queue remove <n>            remove item from queue")
            print(f"  queue clear                 clear entire queue")
            print(f"  settings                    view / change settings")
            print(f"  settings log normal|debug   clean output or detailed logs")
            print(f"  cache clear                 clear search result cache")
            print(f"  update                      force update from GitHub")
            print(f"  exit                        quit")
            print(f"{'='*50}")
            print(f"  Ctrl+C once = pause   Ctrl+C twice = stop batch   Ctrl+C 3x = exit")
            print(f"{'='*50}")

        elif lower == 'history':
            show_history()

        elif lower == 'status':
            handle_status(cfg)

        elif lower == 'doctor':
            handle_doctor(cfg)

        elif lower == 'cleanup':
            handle_cleanup()

        elif lower in ('retry failed', 'retry'):
            handle_retry_failed(session, cfg)

        elif lower == 'resume':
            handle_resume_command(session, cfg)

        elif lower == 'clip':
            try:
                result  = subprocess.run(['termux-clipboard-get'],
                                         capture_output=True, text=True, timeout=5)
                clipped = result.stdout.strip()
                if clipped.startswith('http'):
                    print(f"[*] From clipboard: {clipped[:70]}")
                    _reset_current_state()
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

        elif lower.startswith('download ') or lower.startswith('range '):
            try:
                _, rest = raw.split(' ', 1)
                spec, url_part = rest.strip().split(' ', 1)
                urls = [u.strip() for u in url_part.split() if u.strip().startswith('http')]
                if not urls:
                    print("[!] Usage: download <range> <url>")
                    continue
                ctx = _ctx_with_episode_filter(cfg, spec)
                print(f"[*] Episode filter: {spec}")
                process_link_queue(urls, session, ctx)
            except ValueError:
                print("[!] Usage: download <range> <url>  e.g. download 1-5,8 https://...")

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
                from downloader import _update_ytdlp
                before = subprocess.run(
                    ['git', 'rev-parse', 'HEAD'],
                    cwd=script_dir, capture_output=True,
                    text=True, timeout=5, stdin=subprocess.DEVNULL
                ).stdout.strip()

                # Check for local changes BEFORE pulling — these silently
                # block `git pull` even with exit code 0, causing false
                # "Already up to date" reports.
                dirty = subprocess.run(
                    ['git', 'status', '--porcelain'],
                    cwd=script_dir, capture_output=True,
                    text=True, timeout=5, stdin=subprocess.DEVNULL
                ).stdout.strip()

                if dirty:
                    print("[!] Local changes detected — these block updates:")
                    for line in dirty.splitlines()[:10]:
                        print(f"      {line}")
                    print("[!] Run 'git stash' or 'git checkout -- .' in the toolkit folder, then update again")
                else:
                    pull = subprocess.run(
                        ['git', 'pull', '--ff-only'],
                        cwd=script_dir, capture_output=True,
                        text=True, timeout=30, stdin=subprocess.DEVNULL
                    )
                    try:
                        open(stamp_file, 'w').write(str(time.time()))
                    except Exception:
                        pass

                    if pull.returncode != 0:
                        print("[!] git pull failed:")
                        err = (pull.stderr or pull.stdout or '').strip()
                        print(f"      {err[:400]}")
                    else:
                        channel = cfg.get('ytdlp_channel', 'master')
                        print(f"[*] Updating yt-dlp ({channel})...")
                        _update_ytdlp(channel=channel)
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
                    _reset_current_state()
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
                    _reset_current_state()
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
            _reset_current_state()
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
