"""
main.py — Download Toolkit entry point.
Handles: REPL, signal handling, settings, download queue, auto-update.
"""

import os

import sys
import json
import time
import shutil
import signal
import shlex
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


def detect_total_ram_gb():
    """Best-effort total physical RAM in GB, or None if it can't be read.

    Reads Linux/Termux /proc/meminfo (MemTotal, in kB). Falls back to None on
    any error so callers can pick a conservative default without crashing.
    """
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    kb = int(line.split()[1])
                    return kb / (1024 * 1024)
    except Exception:
        pass
    return None


def recommended_limits():
    """Return device-appropriate (parallel, aria2c_connections) defaults.

    Only tiers down on Android/Termux, where a weak phone gets OOM-killed by
    Android's Low-Memory-Killer when too many aria2c connections run at once
    (~16 connections per download, and parallel multiplies that). Desktop keeps
    the fast defaults. Unknown RAM on Android falls back to the safest tier.
    """
    if not IS_ANDROID:
        return 2, 16
    ram = detect_total_ram_gb()
    if ram is None:
        return 1, 4          # unknown phone → play it safe
    if ram < 3:
        return 1, 4          # low-end: one download, few connections
    if ram < 6:
        return 2, 8          # mid-range: two downloads, moderate connections
    return 2, 16             # high-end phone: closer to desktop

from src.downloader import CONFIG_DIR, ui_emit, ui_text
CONFIG_FILE = os.path.join(CONFIG_DIR, '.config.json')
QUEUE_FILE  = os.path.join(CONFIG_DIR, '.queue.json')
CONTROL_FILE = os.path.join(CONFIG_DIR, '.download_control')
AUTO_UPDATE_STATE_FILE = os.path.join(CONFIG_DIR, '.auto_update_state.json')

# ─── GLOBAL STATE ─────────────────────────────────────────────
from src.state import AppState

app = AppState()

def _record_pause_state():
    state = app.get_download_state()
    if not (state['series_url'] and state['episode_name'] and state['filepath']):
        return
    try:
        from src.downloader import DownloadReceipt, mark_episode_current
        progress_bytes = os.path.getsize(state['filepath']) if os.path.exists(state['filepath']) else 0
        episode_key = f"{state['series_url']}:{state['episode_name']}"
        DownloadReceipt.mark_paused(
            episode_key,
            state['filepath'],
            progress_bytes,
            state['expected_size'],
        )
        mark_episode_current(
            state['series_url'],
            state['series_name'] or 'Download',
            state['episode_name'],
        )
    except Exception as e:
        try:
            print(f"  [!] Could not save pause state: {e}")
        except Exception:
            pass

def _request_pause(source='control'):
    if app.pause.is_set() or not app.has_active_download():
        return
    app.pause.set()
    _record_pause_state()
    safe_source = 'tmux' if source == 'tmux' else source
    print(f"\n  [pause] Download paused ({safe_source}). Press Ctrl+P to resume.\n")

def _request_resume(source='control'):
    if not app.pause.is_set():
        return
    app.pause.clear()
    safe_source = 'tmux' if source == 'tmux' else source
    print(f"\n  [resume] Resuming download ({safe_source})...\n")

def _consume_control_request():
    """Read one tmux-issued pause/resume request without touching terminal input."""
    try:
        if not os.path.exists(CONTROL_FILE):
            return
        with open(CONTROL_FILE, 'r', encoding='utf-8') as f:
            request = f.read().strip().lower()
        os.remove(CONTROL_FILE)
    except OSError:
        return
    if request == 'pause':
        _request_pause('tmux')
    elif request == 'resume':
        _request_resume('tmux')
    elif request == 'toggle':
        if app.pause.is_set():
            _request_resume('tmux')
        else:
            _request_pause('tmux')

def start_termux_pause_controls():
    """Install session-only tmux controls without using raw terminal mode."""
    if not (IS_ANDROID and os.environ.get('TMUX') and shutil.which('tmux')):
        return False
    try:
        if os.path.exists(CONTROL_FILE):
            os.remove(CONTROL_FILE)
        target = shlex.quote(CONTROL_FILE)
        command = f"printf '%s\\n' toggle > {target}"
        result = subprocess.run(
            ['tmux', 'bind-key', '-n', 'C-p', 'run-shell', command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        if result.returncode != 0:
            return False
        app.control_stop.clear()
        t = threading.Thread(
            target=_watch_termux_controls,
            name='termux-pause-control',
            daemon=True,
        )
        t.start()
        app.tmux_active = True
        return True
    except Exception:
        return False

def _watch_termux_controls():
    while not app.control_stop.wait(0.2):
        _consume_control_request()

def stop_termux_pause_controls():
    app.control_stop.set()
    try:
        if os.path.exists(CONTROL_FILE):
            os.remove(CONTROL_FILE)
    except OSError:
        pass
    if app.tmux_active:
        try:
            subprocess.run(
                ['tmux', 'unbind-key', '-n', 'C-p'],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
        except Exception:
            pass
    app.tmux_active = False

# ─── CONFIG ───────────────────────────────────────────────────
_REC_PARALLEL, _REC_CONNECTIONS = recommended_limits()

DEFAULT_CONFIG = {
    # Download settings
    'quality':              '480p',
    'parallel':             _REC_PARALLEL,
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
                                         # Used only by the explicit `updateyt` command.
    
    # Parallel download settings
    'parallel_mode':        'queue',    # 'queue' (recommended) or 'thread' (legacy)
    'resolver_threads':     4,          # Parallel resolvers when using queue mode
    
    # Search settings
    'search_timeout':       45,         # Max seconds to wait for search results
    'search_workers':       6 if IS_ANDROID else 12,  # Lower burst load on Termux
    'search_cache':         True,       # Cache search results for 24h
    
    # Storage
    'storage_guard_gb':     1.0,        # Stop downloads if free space below this (GB)
    
    # App behaviour
    'auto_resume':          True,       # Show resume prompt on startup
    
    # Logging
    'enable_progress_log':  True,       # Log downloads to .download.log
    'log_level':            'normal',   # 'normal' or 'debug'
    'color':                'auto',     # 'auto' (color only on a TTY), 'always', or 'never'
    'auto_update_days':     7,          # Weekly auto-update cadence
    'social_quality':       '720p',     # Prefer 720p for non-YouTube social videos
    'enable_android_notifications': True,       # Toggle Termux system notifications
    'clipboard_check_interval_sec': 2,         # Clipboard watcher loop frequency in seconds
    'aria2c_connections': _REC_CONNECTIONS,    # -x flag (device-tiered on Android)
    'aria2c_splits': _REC_CONNECTIONS,         # -s flag (kept equal to connections)
    'aria2c_min_split_size': '1M',             # --min-split-size flag
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
                merged['_over_recommended'] = _config_exceeds_device(merged)
                return merged
    except Exception:
        pass
    return dict(DEFAULT_CONFIG)


def _config_exceeds_device(cfg):
    """True if saved parallel/connections are heavier than this device's
    recommended safe values. Used to warn existing installs (e.g. a phone
    that already saved 16 connections before the tiering existed)."""
    if not IS_ANDROID:
        return False
    rec_p, rec_c = recommended_limits()
    try:
        return (int(cfg.get('parallel', rec_p)) > rec_p or
                int(cfg.get('aria2c_connections', rec_c)) > rec_c)
    except (ValueError, TypeError):
        return False


def warn_if_over_recommended(cfg):
    """Print a one-time startup advisory if the saved config is heavier than
    the device can comfortably handle. Non-blocking — the user keeps their
    settings; this just explains the freeze/restart risk and how to fix it."""
    if not cfg.get('_over_recommended'):
        return
    rec_p, rec_c = recommended_limits()
    ram = detect_total_ram_gb()
    ram_txt = f"{ram:.1f} GB" if ram else "unknown"
    print("\n[!] Heads up: your download settings are heavy for this device.")
    print(f"    Device RAM: {ram_txt}  |  "
          f"parallel={cfg.get('parallel')} connections={cfg.get('aria2c_connections')}")
    print(f"    Recommended here: parallel={rec_p}, connections={rec_c}.")
    print("    High values can make the app freeze or get killed by Android")
    print("    after a few downloads. Lower them in Settings if that happens.\n")


def warn_override(kind, value):
    """Print a warning when the user sets `kind` ('parallel' or 'connections')
    above the device's recommended safe value. Advisory only — the value is
    still applied. No-op on desktop or when within the safe range."""
    if not IS_ANDROID:
        return
    rec_p, rec_c = recommended_limits()
    rec = rec_p if kind == 'parallel' else rec_c
    if value <= rec:
        return
    ram = detect_total_ram_gb()
    ram_txt = f"{ram:.1f} GB" if ram else "unknown"
    print(f"[!] Warning: {kind}={value} is above the recommended {rec} for "
          f"this device (RAM: {ram_txt}).")
    print("    Higher values may cause the app to freeze or be killed by")
    print("    Android after a few downloads. Setting it anyway.")


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        # Drop transient/computed keys so they never persist to disk.
        to_write = {k: v for k, v in cfg.items() if not k.startswith('_')}
        with open(CONFIG_FILE, 'w') as f:
            json.dump(to_write, f, indent=2)
    except Exception:
        pass

# ─── SIGNAL HANDLING (Ctrl+C) ─────────────────────────────────
def setup_signal_handler():
    def handler(sig, frame):
        # Always set stop flag so any download that starts will see it immediately
        app.stop.set()
        app.pause.clear()
        if not app.has_active_download():
            return
        _record_pause_state()
        try:
            from src.downloader import terminate_active_processes
            terminate_active_processes()
        except Exception:
            pass
        try:
            sys.stdout.write('\n\n  [stop] Download stopped and saved for resume.\n\n')
            sys.stdout.flush()
        except Exception:
            pass

    def sigterm_handler(sig, frame):
        """Called when Android kills Termux from notification or app switcher."""
        _record_pause_state()
        proc = app.current_process.proc
        if proc:
            try: proc.terminate()
            except Exception: pass
        try:
            from src.downloader import terminate_active_processes
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

# Raw mode listener removed — it caused terminal glitches on Termux.
# Ctrl+P pause control is handled by tmux; Ctrl+C cancels the active batch.

def _make_ctx(cfg):
    return app.make_ctx(cfg)

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
        os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
        with open(QUEUE_FILE, 'w') as f:
            json.dump(q, f, indent=2)
    except Exception:
        pass

def queue_add(url):
    q = load_queue()
    if url not in q:
        q.append(url)
        save_queue(q)
        ui_emit('queue_added')
        print("    " + ui_text('queue_start_hint', count=len(q)))
    else:
        ui_emit('already_in_queue')

def queue_add_many(urls):
    """Add multiple URLs to the queue in one pass.
    Loads/saves the queue once and dedups both against the existing
    queue and within the batch itself. Quiet (prints nothing) so the
    caller can render a single aggregate summary.
    Returns (added, already) counts.
    """
    q = load_queue()
    added = 0
    already = 0
    for url in urls:
        if url in q:
            already += 1
        else:
            q.append(url)
            added += 1
    if added:
        save_queue(q)
    return added, already

def queue_list():
    q = load_queue()
    if not q:
        ui_emit('queue_empty')
        return
    print(f"\n{'='*50}")
    print(f"  DOWNLOAD QUEUE  ({len(q)} item(s))")
    print(f"{'='*50}")
    for i, url in enumerate(q, 1):
        print(f"  [{i}] {url[:65]}")
    print(f"{'='*50}")

def queue_clear():
    save_queue([])
    ui_emit('queue_cleared')

def queue_remove(n):
    q = load_queue()
    if 1 <= n <= len(q):
        removed = q.pop(n - 1)
        save_queue(q)
        ui_emit('queue_removed', url=removed[:60])
    else:
        ui_emit('queue_bad_index')

def queue_run(session, cfg):
    q = load_queue()
    if not q:
        ui_emit('queue_empty')
        return
    print()
    ui_emit('queue_starting', count=len(q))
    from src.extractors import process_link_queue
    ctx = _make_ctx(cfg)
    remaining = []
    for index, url in enumerate(q):
        if app.stop.is_set():
            remaining.extend(q[index:])
            break
        outcomes = process_link_queue([url], session, ctx)
        outcome = outcomes[0] if outcomes else {'status': 'failed'}
        if outcome.get('status') != 'success':
            remaining.append(url)
    save_queue(remaining)
    if not remaining:
        ui_emit('queue_complete')
    else:
        ui_emit('queue_kept_unfinished', count=len(remaining))

# ─── SETTINGS ─────────────────────────────────────────────────
def handle_settings(parts, cfg):
    # Backwards compatibility: if user types "settings quality 720p" directly
    if len(parts) > 1:
        key = parts[1].lower()
        if key == 'quality' and len(parts) >= 3:
            q = parts[2].lower()
            if q in ('4k', '2160'):
                q = '2160p'
            if q in ('360p', '480p', '720p', '1080p', '2160p', 'best'):
                cfg['quality'] = q
                save_config(cfg)
                ui_emit('setting_saved', label='Quality', value=q)
            else:
                ui_emit('setting_invalid', hint='Valid values: 360p, 480p, 720p, 1080p, 2160p/4k, best')
        elif key == 'parallel' and len(parts) >= 3:
            try:
                n = int(parts[2])
                if 1 <= n <= 3:
                    warn_override('parallel', n)
                    cfg['parallel'] = n
                    save_config(cfg)
                    ui_emit('setting_saved', label='Parallel downloads', value=n)
                else:
                    ui_emit('setting_invalid', hint='Parallel must be between 1 and 3.')
            except ValueError:
                ui_emit('setting_invalid', hint='Enter a whole number.')
        elif key == 'bandwidth' and len(parts) >= 3:
            try:
                bw = int(parts[2])
                cfg['bandwidth'] = bw
                save_config(cfg)
                ui_emit('setting_saved', label='Bandwidth', value=('unlimited' if not bw else f'{bw}KB/s'))
            except ValueError:
                ui_emit('setting_invalid', hint='Enter bandwidth in KB/s (e.g. 500), or 0 for unlimited.')
        elif key == 'timeout' and len(parts) >= 3:
            try:
                secs = int(parts[2])
                if 30 <= secs <= 600:
                    cfg['download_timeout'] = secs
                    save_config(cfg)
                    ui_emit('setting_saved', label='Timeout', value=f'{secs}s')
                else:
                    ui_emit('setting_invalid', hint='Timeout must be between 30 and 600 seconds.')
            except ValueError:
                ui_emit('setting_invalid', hint='Enter a whole number.')
        elif key == 'retries' and len(parts) >= 3:
            try:
                r = int(parts[2])
                if 1 <= r <= 10:
                    cfg['download_retries'] = r
                    save_config(cfg)
                    ui_emit('setting_saved', label='Retries', value=f'{r} attempts')
                else:
                    ui_emit('setting_invalid', hint='Retries must be between 1 and 10.')
            except ValueError:
                ui_emit('setting_invalid', hint='Enter a whole number.')
        elif key in ('search-timeout', 'search_timeout') and len(parts) >= 3:
            try:
                s = int(parts[2])
                if 10 <= s <= 300:
                    cfg['search_timeout'] = s
                    save_config(cfg)
                    ui_emit('setting_saved', label='Search timeout', value=f'{s}s')
                else:
                    ui_emit('setting_invalid', hint='Search timeout must be between 10 and 300 seconds.')
            except ValueError:
                ui_emit('setting_invalid', hint='Enter a whole number.')
        elif key in ('search-cache', 'search_cache') and len(parts) >= 3:
            val = parts[2].lower()
            if val in ('on', 'off', 'true', 'false'):
                cfg['search_cache'] = val in ('on', 'true')
                save_config(cfg)
                ui_emit('setting_saved', label='Search cache', value=('Enabled' if cfg['search_cache'] else 'Disabled'))
            else:
                ui_emit('setting_invalid', hint="Use 'on' or 'off'.")
        elif key in ('auto-update', 'autoupdate') and len(parts) >= 3:
            try:
                days = int(parts[2])
                if 1 <= days <= 30:
                    cfg['auto_update_days'] = days
                    save_config(cfg)
                    ui_emit('setting_saved', label='Auto-update', value=f'every {days} day(s)')
                else:
                    ui_emit('setting_invalid', hint='Use 1-30 days.')
            except ValueError:
                ui_emit('setting_invalid', hint='Use days, e.g. settings auto-update 7')
        elif key in ('storage-guard', 'storage_guard') and len(parts) >= 3:
            try:
                gb = float(parts[2])
                if 0.1 <= gb <= 50.0:
                    cfg['storage_guard_gb'] = gb
                    save_config(cfg)
                    ui_emit('setting_saved', label='Storage guard threshold', value=f'{gb} GB')
                else:
                    ui_emit('setting_invalid', hint='Threshold must be between 0.1 and 50.0 GB.')
            except ValueError:
                ui_emit('setting_invalid', hint='Enter a number, e.g. 2.0')
        elif key in ('auto-resume', 'autoresume') and len(parts) >= 3:
            val = parts[2].lower()
            if val in ('on', 'off', 'true', 'false'):
                cfg['auto_resume'] = val in ('on', 'true')
                save_config(cfg)
                ui_emit('setting_saved', label='Auto resume', value=('Enabled' if cfg['auto_resume'] else 'Disabled'))
            else:
                ui_emit('setting_invalid', hint="Use 'on' or 'off'.")
        elif key in ('log', 'mode') and len(parts) >= 3:
            mode = parts[2].lower()
            if mode in ('normal', 'debug'):
                cfg['log_level'] = mode
                save_config(cfg)
                try:
                    from src.downloader import set_output_mode
                    set_output_mode(mode)
                except Exception:
                    pass
                ui_emit('setting_saved', label='Output mode', value=mode)
            else:
                ui_emit('setting_invalid', hint='Use normal or debug.')
        elif key in ('social-quality', 'social_quality') and len(parts) >= 3:
            q = parts[2].lower()
            if q in ('4k', '2160'):
                q = '2160p'
            if q in ('360p', '480p', '720p', '1080p', '2160p', 'best'):
                cfg['social_quality'] = q
                save_config(cfg)
                ui_emit('setting_saved', label='Social quality', value=q)
            else:
                ui_emit('setting_invalid', hint='Valid: 360p 480p 720p 1080p 2160p/4k best')
        elif key in ('anime-mode', 'anime_mode') and len(parts) >= 3:
            val = parts[2].lower()
            if val in ('sub', 'dub'):
                cfg['anime_mode'] = val
                save_config(cfg)
                ui_emit('setting_saved', label='Anime mode', value=val)
            else:
                ui_emit('setting_invalid', hint="Use 'sub' or 'dub'.")
        elif key == 'ytdlp-channel' and len(parts) >= 3:
            channel = parts[2].lower()
            if channel in ('master', 'stable'):
                cfg['ytdlp_channel'] = channel
                save_config(cfg)
                ui_emit('setting_saved', label='yt-dlp channel', value=channel)
            else:
                ui_emit('setting_invalid', hint='Use master or stable.')
        elif key == 'disable' and len(parts) >= 3:
            site = parts[2].lower()
            disabled = cfg.get('disabled_sites', [])
            if site not in disabled:
                disabled.append(site)
                cfg['disabled_sites'] = disabled
                save_config(cfg)
                ui_emit('setting_saved', label='Disabled site', value=site)
            else:
                print("[*] Already disabled")
        elif key == 'enable' and len(parts) >= 3:
            site = parts[2].lower()
            disabled = cfg.get('disabled_sites', [])
            if site in disabled:
                disabled.remove(site)
                cfg['disabled_sites'] = disabled
                save_config(cfg)
                ui_emit('setting_saved', label='Enabled site', value=site)
            else:
                print("[*] Not disabled")
        elif key in ('notifications', 'android_notifications', 'enable_android_notifications') and len(parts) >= 3:
            val = parts[2].lower()
            if val in ('on', 'off', 'true', 'false'):
                cfg['enable_android_notifications'] = val in ('on', 'true')
                save_config(cfg)
                ui_emit('setting_saved', label='Termux notifications', value=('Enabled' if cfg['enable_android_notifications'] else 'Disabled'))
            else:
                ui_emit('setting_invalid', hint="Use 'on' or 'off'.")
        elif key in ('watch-interval', 'clipboard_interval', 'clipboard_check_interval_sec') and len(parts) >= 3:
            try:
                sec = int(parts[2])
                if 1 <= sec <= 10:
                    cfg['clipboard_check_interval_sec'] = sec
                    save_config(cfg)
                    ui_emit('setting_saved', label='Clipboard check interval', value=f'{sec}s')
                else:
                    ui_emit('setting_invalid', hint='Interval must be between 1 and 10 seconds.')
            except ValueError:
                ui_emit('setting_invalid', hint='Enter a whole number.')
        else:
            ui_emit('setting_invalid', hint='Unknown setting. Type settings to open the menu.')
        return cfg

    # Interactive Wizard Mode (if user just types "settings")
    while True:
        _show_settings(cfg)
        try:
            choice = input("Select setting to change (0-17): ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            break

        if not choice or choice == '0':
            break

        if choice == '1':
            print("\n=== Change Download Quality ===")
            print("  1) 360p")
            print("  2) 480p")
            print("  3) 720p")
            print("  4) 1080p")
            print("  5) Best")
            print("  6) 4K (2160p)")
            print("  0) Back")
            try:
                opt = input("Select option (0-6): ").strip()
                if opt == '1': cfg['quality'] = '360p'
                elif opt == '2': cfg['quality'] = '480p'
                elif opt == '3': cfg['quality'] = '720p'
                elif opt == '4': cfg['quality'] = '1080p'
                elif opt == '5': cfg['quality'] = 'best'
                elif opt == '6': cfg['quality'] = '2160p'
                if opt in ('1','2','3','4','5','6'):
                    save_config(cfg)
                    print(f"[ok] Quality set to: {cfg['quality']}")
            except (KeyboardInterrupt, EOFError):
                pass

        elif choice == '2':
            try:
                p = input("Enter parallel downloads count (1-3): ").strip()
                n = int(p)
                if 1 <= n <= 3:
                    warn_override('parallel', n)
                    cfg['parallel'] = n
                    save_config(cfg)
                    print(f"[ok] Parallel set to: {n}")
                else:
                    print("[!] Must be between 1 and 3")
            except ValueError:
                print("[!] Invalid number")
            except (KeyboardInterrupt, EOFError):
                pass

        elif choice == '3':
            try:
                bw = input("Enter limit in KB/s (0 for unlimited): ").strip()
                cfg['bandwidth'] = int(bw)
                save_config(cfg)
                # Compute the label separately: a nested same-quote f-string
                # here is a SyntaxError on Python < 3.12 (Termux ships 3.11),
                # which would fail to parse the whole module at import.
                bw_label = 'unlimited' if not cfg['bandwidth'] else f"{cfg['bandwidth']}KB/s"
                print(f"[ok] Bandwidth: {bw_label}")
            except ValueError:
                print("[!] Invalid number")
            except (KeyboardInterrupt, EOFError):
                pass

        elif choice == '4':
            try:
                t = input("Enter stall timeout in seconds (30-600): ").strip()
                secs = int(t)
                if 30 <= secs <= 600:
                    cfg['download_timeout'] = secs
                    save_config(cfg)
                    print(f"[ok] Timeout set to: {secs}s")
                else:
                    print("[!] Timeout must be 30 to 600")
            except ValueError:
                print("[!] Invalid number")
            except (KeyboardInterrupt, EOFError):
                pass

        elif choice == '5':
            try:
                r = input("Enter retry limit (1-10): ").strip()
                ret = int(r)
                if 1 <= ret <= 10:
                    cfg['download_retries'] = ret
                    save_config(cfg)
                    print(f"[ok] Retry limit: {ret}")
                else:
                    print("[!] Must be 1 to 10")
            except ValueError:
                print("[!] Invalid number")
            except (KeyboardInterrupt, EOFError):
                pass

        elif choice == '6':
            try:
                st = input("Enter search timeout in seconds (10-300): ").strip()
                secs = int(st)
                if 10 <= secs <= 300:
                    cfg['search_timeout'] = secs
                    save_config(cfg)
                    print(f"[ok] Search timeout: {secs}s")
                else:
                    print("[!] Must be 10 to 300")
            except ValueError:
                print("[!] Invalid number")
            except (KeyboardInterrupt, EOFError):
                pass

        elif choice == '7':
            print("\n=== Search Cache ===")
            print("  1) Enable (Save results for 24h)")
            print("  2) Disable (Always search fresh)")
            print("  0) Back")
            try:
                opt = input("Select option (0-2): ").strip()
                if opt == '1':
                    cfg['search_cache'] = True
                    save_config(cfg)
                    print("[ok] Search Cache: Enabled")
                elif opt == '2':
                    cfg['search_cache'] = False
                    save_config(cfg)
                    print("[ok] Search Cache: Disabled")
            except (KeyboardInterrupt, EOFError):
                pass

        elif choice == '8':
            try:
                days = input("Enter auto-update interval in days (1-30): ").strip()
                d = int(days)
                if 1 <= d <= 30:
                    cfg['auto_update_days'] = d
                    save_config(cfg)
                    print(f"[ok] Auto-update: every {d} day(s)")
                else:
                    print("[!] Must be 1 to 30")
            except ValueError:
                print("[!] Invalid number")
            except (KeyboardInterrupt, EOFError):
                pass

        elif choice == '9':
            try:
                guard = input("Enter storage guard limit in GB (e.g. 1.0): ").strip()
                g = float(guard)
                if 0.1 <= g <= 50.0:
                    cfg['storage_guard_gb'] = g
                    save_config(cfg)
                    print(f"[ok] Storage Guard limit: {g} GB")
                else:
                    print("[!] Must be between 0.1 and 50.0 GB")
            except ValueError:
                print("[!] Invalid number")
            except (KeyboardInterrupt, EOFError):
                pass

        elif choice == '10':
            print("\n=== Auto Resume ===")
            print("  1) Enable (Auto-prompt to resume on startup)")
            print("  2) Disable")
            print("  0) Back")
            try:
                opt = input("Select option (0-2): ").strip()
                if opt == '1':
                    cfg['auto_resume'] = True
                    save_config(cfg)
                    print("[ok] Auto Resume: Enabled")
                elif opt == '2':
                    cfg['auto_resume'] = False
                    save_config(cfg)
                    print("[ok] Auto Resume: Disabled")
            except (KeyboardInterrupt, EOFError):
                pass

        elif choice == '11':
            print("\n=== Log Level ===")
            print("  1) Normal (Clean download progress bars)")
            print("  2) Debug (Full aria2c / yt-dlp details)")
            print("  0) Back")
            try:
                opt = input("Select option (0-2): ").strip()
                if opt in ('1', '2'):
                    mode = 'normal' if opt == '1' else 'debug'
                    cfg['log_level'] = mode
                    save_config(cfg)
                    try:
                        from src.downloader import set_output_mode
                        set_output_mode(mode)
                    except Exception:
                        pass
                    print(f"[ok] Output mode: {mode}")
            except (KeyboardInterrupt, EOFError):
                pass

        elif choice == '12':
            print("\n=== yt-dlp Channel ===")
            print("  1) Master (Pre-release, latest fixes)")
            print("  2) Stable (Standard releases)")
            print("  0) Back")
            try:
                opt = input("Select option (0-2): ").strip()
                if opt in ('1', '2'):
                    channel = 'master' if opt == '1' else 'stable'
                    cfg['ytdlp_channel'] = channel
                    save_config(cfg)
                    print(f"[ok] yt-dlp channel: {channel}")
            except (KeyboardInterrupt, EOFError):
                pass

        elif choice == '14':
            print("\n=== Anime Mode ===")
            print("  1) Sub (Subtitled - default)")
            print("  2) Dub (English dubbed)")
            print("  0) Back")
            try:
                opt = input("Select option (0-2): ").strip()
                if opt in ('1', '2'):
                    mode = 'sub' if opt == '1' else 'dub'
                    cfg['anime_mode'] = mode
                    save_config(cfg)
                    print(f"[ok] Anime mode: {mode}")
            except (KeyboardInterrupt, EOFError):
                pass
                
        elif choice == '15':
            try:
                val = input("Enter aria2c connections per server (1-16): ").strip()
                n = int(val)
                if 1 <= n <= 16:
                    warn_override('connections', n)
                    cfg['aria2c_connections'] = n
                    save_config(cfg)
                    print(f"[ok] aria2c connections set to: {n}")
                else:
                    print("[!] Must be between 1 and 16")
            except ValueError:
                print("[!] Invalid number")
            except (KeyboardInterrupt, EOFError):
                pass

        elif choice == '16':
            try:
                val = input("Enter aria2c splits per file (1-32): ").strip()
                n = int(val)
                if 1 <= n <= 32:
                    cfg['aria2c_splits'] = n
                    save_config(cfg)
                    print(f"[ok] aria2c splits set to: {n}")
                else:
                    print("[!] Must be between 1 and 32")
            except ValueError:
                print("[!] Invalid number")
            except (KeyboardInterrupt, EOFError):
                pass

        elif choice == '17':
            try:
                val = input("Enter aria2c min split size (e.g. 1M, 5M, 10M): ").strip().upper()
                if val and val.endswith('M') and val[:-1].isdigit():
                    cfg['aria2c_min_split_size'] = val
                    save_config(cfg)
                    print(f"[ok] aria2c min split size set to: {val}")
                else:
                    print("[!] Must be a number followed by M (e.g. 1M)")
            except (KeyboardInterrupt, EOFError):
                pass

        elif choice == '18':
            # Cycle color mode: auto -> always -> never -> auto
            from src.messages import set_color
            order = ['auto', 'always', 'never']
            cur = cfg.get('color', 'auto')
            nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else 'auto'
            cfg['color'] = nxt
            save_config(cfg)
            set_color(nxt)  # apply immediately so the confirmation reflects it
            print(f"[ok] Color output set to: {nxt}")

        elif choice == '13':
            # Manage Sites loop
            while True:
                disabled = cfg.get('disabled_sites', [])
                all_sites = ['nkiri', '9jarocks', 'plutomovies', 'dramakey', 'dramarain', 'socials']
                print("\n=== Disable/Enable Sites ===")
                for idx, s in enumerate(all_sites, 1):
                    status = "[Disabled]" if s in disabled else "[Enabled]"
                    print(f"  {idx}) {s.capitalize():<12} {status}")
                print("  0) Back")
                try:
                    s_choice = input("Enter site number to toggle (0-6): ").strip()
                except (KeyboardInterrupt, EOFError):
                    break
                if not s_choice or s_choice == '0':
                    break
                try:
                    s_idx = int(s_choice)
                    if 1 <= s_idx <= len(all_sites):
                        site = all_sites[s_idx - 1]
                        if site in disabled:
                            disabled.remove(site)
                            print(f"[ok] Enabled: {site}")
                        else:
                            disabled.append(site)
                            print(f"[ok] Disabled: {site}")
                        cfg['disabled_sites'] = disabled
                        save_config(cfg)
                    else:
                        print("[!] Invalid option")
                except ValueError:
                    print("[!] Invalid option")
        else:
            print("[!] Invalid choice")
            time.sleep(1)

    return cfg

def _show_settings(cfg):
    from src.downloader import get_free_space_gb
    bw  = cfg.get('bandwidth', 0)
    dis = cfg.get('disabled_sites', [])
    q   = cfg.get('quality', '480p')
    p   = cfg.get('parallel', 1)
    mode = cfg.get('log_level', 'normal')
    social_q = cfg.get('social_quality', '720p')
    auto_days = cfg.get('auto_update_days', 7)
    timeout = cfg.get('download_timeout', 120)
    retries = cfg.get('download_retries', 3)
    ytdlp_channel = cfg.get('ytdlp_channel', 'master')
    search_timeout = cfg.get('search_timeout', 45)
    search_cache = cfg.get('search_cache', True)
    guard = cfg.get('storage_guard_gb', 1.0)
    auto_resume = cfg.get('auto_resume', True)
    
    try:
        free_gb = get_free_space_gb()
        space_s = f"{free_gb:.1f} GB Free (Guard Active)"
    except Exception:
        space_s = "unknown"

    from src.messages import paint
    bar  = paint("=" * 50, 'cyan')
    dash = paint("-" * 50, 'cyan')
    def opt(n, label, val):
        return f"  {paint(f'{n:>2})', 'byellow')} {label:<21} {paint(f'[{val}]', 'bcyan')}"

    print()
    print(bar)
    print(f"  {paint('ANONRODE SETTINGS', 'bold', 'bcyan')}")
    print(bar)
    print(f"  {paint('Quality:', 'gray')}   {q:<17} {paint('Parallel:', 'gray')}  {p}")
    print(f"  {paint('Bandwidth:', 'gray')} {'unlimited' if not bw else f'{bw}KB/s':<17} {paint('Output:', 'gray')}    {mode}")
    print(f"  {paint('Social:', 'gray')}    {social_q:<17} {paint('Update:', 'gray')}    {auto_days} days")
    print(f"  {paint('Timeout:', 'gray')}   {str(timeout) + 's':<17} {paint('Channel:', 'gray')}   {ytdlp_channel}")
    print(f"  {paint('Save dir:', 'gray')}  {BASE_DIR}")
    print(f"  {paint('Storage:', 'gray')}   {space_s}")
    print(dash)
    ok = paint('[OK]', 'bgreen')
    print(f"  {ok} Parallel Search          {ok} Pause/Resume (Ctrl+P on Termux)")
    print(f"  {ok} Expired Link Refresh     {ok} Smart Queue")
    print(dash)
    print(f"  {paint('Searchable:', 'gray')}  NKiri | DramaKey | PlutoMovies | AllAnime")
    print(f"  {paint('Link Only:', 'gray')}   9JaRocks | DramaRain | Socials")
    print(bar)
    print(opt(1,  'Download Quality',   q))
    print(opt(2,  'Parallel Downloads', p))
    print(opt(3,  'Bandwidth Limit',    'unlimited' if not bw else f'{bw} KB/s'))
    print(opt(4,  'Stall Timeout',      f'{timeout}s'))
    print(opt(5,  'Max Retry Limit',    f'{retries} attempts'))
    print(opt(6,  'Search Timeout',     f'{search_timeout}s'))
    print(opt(7,  'Search Cache',       'Enabled' if search_cache else 'Disabled'))
    print(opt(8,  'Auto Update Days',   f'{auto_days} days'))
    print(opt(9,  'Storage Guard',      f'{guard} GB'))
    print(opt(10, 'Auto Resume',        'Enabled' if auto_resume else 'Disabled'))
    print(opt(11, 'Log level',          mode))
    print(opt(12, 'yt-dlp Channel',     ytdlp_channel))
    print(opt(13, 'Manage Sites',       f'{len(dis)} disabled'))
    print(opt(14, 'Anime Mode',         cfg.get('anime_mode', 'sub')))
    print(opt(15, 'aria2c Connections', cfg.get('aria2c_connections', 16)))
    print(opt(16, 'aria2c Splits',      cfg.get('aria2c_splits', 16)))
    print(opt(17, 'Min Split Size',     cfg.get('aria2c_min_split_size', '1M')))
    print(opt(18, 'Color Output',       cfg.get('color', 'auto')))
    print(f"  {paint(' 0)', 'byellow')} Back to command prompt")
    print(bar)

# ─── RESUME ───────────────────────────────────────────────────
def handle_resume_command(session, cfg):
    from src.downloader import show_resume_list, load_resume_state
    from src.extractors import process_link_queue
    items = show_resume_list()
    if not items:
        return
    try:
        choice = int(input("\n  Pick number to resume (0 to cancel): ").strip())
    except (ValueError, EOFError):
        return
    if choice < 1 or choice > len(items):
        return
    url = items[choice - 1][0]
    print(f"\n[*] Resuming: {url[:60]}")
    ctx = _make_ctx(cfg)
    process_link_queue([url], session, ctx)

# ─── GIT HELPERS ──────────────────────────────────────────────
def _git_update(script_dir, announce=False, force=False):
    """Single source of truth for the git fetch/merge/stamp dance shared by
    auto_update() and the manual `update` command. Never raises.

    Safety kept from the originals: refuses to touch a dirty tree (tracked,
    non-`??` changes), fast-forward only, every git call is non-interactive
    (stdin=DEVNULL) with a timeout, so it is Windows + Termux safe.

    force=True refreshes the 7-day auto-update stamp on a successful remote
    check (both real callers pass force=True). Returns a dict:
      reached_remote bool  – `git fetch` succeeded
      was_dirty      bool  – tracked changes present; merge refused
      dirty_lines    list  – porcelain lines behind was_dirty (for display)
      before_commit  str   – full HEAD before the merge ('' if unknown)
      after_commit   str   – full HEAD after the merge ('' if unknown)
      updated        bool  – HEAD actually advanced (a real fast-forward)
      behind_count   int   – commits HEAD is behind origin/main (0 if unknown)
      merge_ok       bool  – `git merge --ff-only` returned 0 (incl. no-op)
      merge_output   str   – merge stdout (for display)
      error          str   – first error text encountered ('' if none)
    """
    res = {
        'reached_remote': False, 'was_dirty': False, 'dirty_lines': [],
        'before_commit': '', 'after_commit': '', 'updated': False,
        'behind_count': 0, 'merge_ok': False, 'merge_output': '', 'error': '',
    }

    def _run(args, timeout):
        return subprocess.run(
            args, cwd=script_dir, capture_output=True,
            text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )

    def _commit():
        try:
            r = _run(['git', 'rev-parse', 'HEAD'], 5)
            return r.stdout.strip() if r.returncode == 0 else ''
        except Exception:
            return ''

    try:
        res['before_commit'] = _commit()

        # 1) Never wipe local work — refuse on tracked (non-??) changes.
        status = _run(['git', 'status', '--porcelain'], 5)
        res['dirty_lines'] = [
            l for l in status.stdout.splitlines() if l and not l.startswith('??')
        ]
        if res['dirty_lines']:
            res['was_dirty'] = True
            return res

        # 2) Reach the remote.
        fetch = _run(['git', 'fetch', 'origin', '-q'], 30)
        if fetch.returncode != 0:
            res['error'] = (fetch.stderr or fetch.stdout or '').strip()
            return res
        res['reached_remote'] = True

        # 3) How far behind origin/main (informational).
        try:
            rc = _run(['git', 'rev-list', '--count', 'HEAD..origin/main'], 10)
            if rc.returncode == 0 and rc.stdout.strip().isdigit():
                res['behind_count'] = int(rc.stdout.strip())
        except Exception:
            pass

        # 4) Fast-forward only — never a merge commit, never a rebase.
        merge = _run(['git', 'merge', '--ff-only', 'origin/main', '-q'], 30)
        res['merge_output'] = (merge.stdout or '').strip()
        res['merge_ok'] = (merge.returncode == 0)
        if not res['merge_ok']:
            res['error'] = (merge.stderr or merge.stdout or '').strip()
            return res

        res['after_commit'] = _commit()
        res['updated'] = bool(
            res['before_commit'] and res['after_commit']
            and res['before_commit'] != res['after_commit']
        )

        # 5) Refresh the auto-update stamp on a successful remote check.
        if force:
            try:
                os.makedirs(os.path.dirname(AUTO_UPDATE_STATE_FILE), exist_ok=True)
                open(AUTO_UPDATE_STATE_FILE, 'w').write(str(time.time()))
            except Exception:
                pass

        if announce and res['updated']:
            ui_emit('toolkit_updated')
    except Exception as e:
        res['error'] = res['error'] or str(e)

    return res


def _git_status_info(script_dir):
    """Read-only live inspection of the checkout vs origin/main for the
    `version` command. Fetches but never merges or stamps. Never raises.
    Returns commit(short), date, branch, behind(int|None), reached_remote(bool).
    """
    info = {
        'commit': '', 'date': '', 'branch': '',
        'behind': None, 'reached_remote': False,
    }

    def _run(args, timeout):
        try:
            return subprocess.run(
                args, cwd=script_dir, capture_output=True,
                text=True, timeout=timeout, stdin=subprocess.DEVNULL
            )
        except Exception:
            return None

    r = _run(['git', 'rev-parse', '--short', 'HEAD'], 5)
    if r and r.returncode == 0:
        info['commit'] = r.stdout.strip()
    r = _run(['git', 'log', '-1', '--format=%cd', '--date=format:%Y-%m-%d %H:%M'], 5)
    if r and r.returncode == 0:
        info['date'] = r.stdout.strip()
    r = _run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], 5)
    if r and r.returncode == 0:
        info['branch'] = r.stdout.strip()

    fetch = _run(['git', 'fetch', 'origin', '-q'], 30)
    if fetch and fetch.returncode == 0:
        info['reached_remote'] = True
        r = _run(['git', 'rev-list', '--count', 'HEAD..origin/main'], 10)
        if r and r.returncode == 0 and r.stdout.strip().isdigit():
            info['behind'] = int(r.stdout.strip())
    return info


# ─── AUTO UPDATE ──────────────────────────────────────────────
def auto_update(cfg=None, announce=True):
    script_dir  = os.path.dirname(os.path.abspath(__file__))
    stamp_file  = AUTO_UPDATE_STATE_FILE
    days = int((cfg or {}).get('auto_update_days', 7))
    pull_interval = max(1, days) * 24 * 60 * 60

    def _should_pull():
        try:
            if os.path.exists(stamp_file):
                last = float(open(stamp_file).read().strip())
                if time.time() - last < pull_interval:
                    return False
        except (ValueError, OSError):
            try:
                os.remove(stamp_file)
            except OSError:
                pass
        return True

    if not _should_pull():
        return  # pulled recently — skip entirely, start instantly

    # Single source of truth for the fetch/merge/stamp dance. force=True
    # refreshes the 7-day stamp on a successful remote check; the helper
    # refuses a dirty tree and announces only on a real update.
    _git_update(script_dir, announce=announce, force=True)

def schedule_auto_update(cfg):
    """Check the weekly repository update without delaying the first prompt."""
    def _run_quietly():
        try:
            auto_update(cfg, announce=False)
        except TypeError:
            # Keeps integrations that provide the original one-argument hook working.
            auto_update(cfg)

    worker = threading.Thread(
        target=_run_quietly,
        name='anonrode-auto-update',
        daemon=True,
    )
    worker.start()

# ─── ANDROID SETUP ────────────────────────────────────────────
def setup_android():
    if not IS_ANDROID:
        return None
    try:
        subprocess.run(
            ['termux-wake-lock'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
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
            ui_emit('tmux_missing')
    return True

# ─── BANNER ───────────────────────────────────────────────────
# Block-letter logo — pure ASCII (# fill), ~46 cols so it fits portrait phones.
_LOGO = r"""
  ___   _  _  ___  _  _ ___  ___  ___  ___
 / _ \ | \| |/ _ \| \| | _ \/ _ \|   \| __|
| (_) || .` | (_) | .` |   / (_) | |) | _|
 \___/ |_|\_|\___/|_|\_|_|_\\___/|___/|___|
""".strip("\n")


def print_banner(cfg):
    import shutil as _shutil
    from src.messages import paint
    try:
        columns, _ = _shutil.get_terminal_size(fallback=(80, 24))
    except Exception:
        columns = 80

    def bar(ch='='):
        print(paint(ch * 60, 'cyan'))

    def head(text):
        print("  " + paint(text, 'bold', 'bcyan'))

    def feature(name, desc):
        print("   " + paint("-", 'gray') + " " + paint(f"{name:<18}", 'bgreen') + paint(desc, 'gray'))

    _SPEED_COLOR = {'Very Fast': 'bgreen', 'Fast': 'green', 'Normal': 'byellow'}

    def site(name, speed, desc):
        tag = paint(f"[{speed}]", _SPEED_COLOR.get(speed, 'white'))
        print("   " + paint("-", 'gray') + f" {name:<12} {tag:<22} " + paint(desc, 'gray'))

    bar('=')
    for line in _LOGO.splitlines():
        print(paint(line, 'bcyan'))
    print("  " + paint("v2.2", 'gray') + "  " + paint("Anon's download toolkit", 'dim'))
    bar('=')
    head("KEY FEATURES")
    feature("Parallel Search", "Search all sites at once")
    feature("Pause & Resume", "Press Ctrl+P anytime")
    feature("Link Auto-Update", "Refreshes expired downloads")
    feature("Smart Queue", "Queue up multiple series")
    feature("Storage Guard", "Auto-pauses if space is full")
    bar('-')
    head("SUPPORTED SITES")
    is_landscape = columns >= 115
    if is_landscape:
        print("   " + paint("Movies & Series:", 'white'))
    site("NKiri", "Very Fast", "Korean & Nollywood")
    site("9JaRocks", "Very Fast", "Nollywood & Hollywood")
    site("PlutoMovies", "Fast", "Blockbusters & Shows")
    if is_landscape:
        print("   " + paint("Asian Dramas:", 'white'))
    site("DramaKey", "Normal", "Chinese & Korean")
    site("DramaRain", "Normal", "Chinese & Japanese")
    if is_landscape:
        print("   " + paint("Social Media:", 'white'))
    site("YouTube", "Fast", "Videos & Playlists")
    site("Pinterest", "Fast", "Pins & Boards")
    bar('=')

# make_session has been centralized and is imported from downloader.py

def handle_status(cfg):
    from src.downloader import get_status, load_resume_state, get_free_space_gb, ui_screen
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
    from src.downloader import get_free_space_gb, ui_screen
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
    from src.downloader import load_resume_state, ui_screen
    from src.extractors import process_link_queue
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
        if app.stop.is_set():
            break
        process_link_queue([url], session, ctx)

def handle_cleanup():
    from src.downloader import BASE_DIR, DownloadReceipt, ui_screen
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

# ─── ANIME ────────────────────────────────────────────────────
def cmd_anime(query, session, cfg):
    from src.extractors import search_allanime, _get_episode_list, extract_allanime


    print()
    ui_emit('anime_search_running', query=query)

    shows = search_allanime(query)
    if not shows:
        ui_emit('anime_no_results')
        return

    # ── Show list ──────────────────────────────────────────────
    print()
    print(f"  {'-'*50}")
    for i, show in enumerate(shows, 1):
        sub = show['sub_eps']
        dub = show['dub_eps']
        avail = []
        if sub: avail.append(f'sub:{sub}')
        if dub: avail.append(f'dub:{dub}')
        avail_str = ', '.join(avail) if avail else 'unknown'
        print(f"  [{i}] {show['name']} ({avail_str} eps)")
    print(f"  {'-'*50}")

    try:
        choice = input(f'  Pick a show (1-{len(shows)}) or 0 to cancel: ').strip()
        choice = int(choice)
    except (ValueError, EOFError, KeyboardInterrupt):
        return
    if choice == 0 or not (1 <= choice <= len(shows)):
        return

    selected  = shows[choice - 1]
    show_id   = selected['id']
    show_name = selected['name']
    mode      = cfg.get('anime_mode', 'sub')
    eps_count = selected['dub_eps'] if mode == 'dub' else selected['sub_eps']

    # Fallback: if chosen mode has 0 episodes, switch to the other
    if eps_count == 0:
        fallback = 'sub' if mode == 'dub' else 'dub'
        fallback_count = selected['sub_eps'] if mode == 'dub' else selected['dub_eps']
        if fallback_count > 0:
            ui_emit('anime_mode_fallback', mode=mode, fallback=fallback)
            mode      = fallback
            eps_count = fallback_count
        else:
            ui_emit('anime_no_episodes')
            return

    print()
    ui_emit('anime_show_selected', show=show_name, count=eps_count, mode=mode)

    # ── Fetch full episode list ────────────────────────────────
    ui_emit('fetching_episode_list')
    ep_list = _get_episode_list(show_id, mode=mode)
    if not ep_list:
        ui_emit('anime_ep_list_failed')
        return

    total = len(ep_list)
    ui_emit('anime_ep_count', count=total, first=ep_list[0], last=ep_list[-1])

    # ── All or specific ────────────────────────────────────────
    print()
    print('  [1] Download all episodes')
    print('  [2] Pick specific episode(s)')
    try:
        dl_choice = input('  Choice: ').strip()
    except (EOFError, KeyboardInterrupt):
        return

    if dl_choice == '1':
        if total >= 10:
            try:
                confirm = input(f'\n  [!] This will download {total} episodes. Continue? (y/n): ').strip().lower()
            except (EOFError, KeyboardInterrupt):
                return
            if confirm not in ('y', 'yes'):
                ui_emit('cancelled')
                return
        episodes = ep_list

    elif dl_choice == '2':
        ep_display = f'1-{total}' if total > 1 else '1'
        print(f'  Episodes available: {ep_display}')
        try:
            spec = input('  Enter episode(s) - e.g. 1, 1-5, 1-5,10: ').strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not spec:
            return
        try:
            selected_nums = _parse_episode_selection(spec)
        except (ValueError, Exception):
            ui_emit('invalid_range')
            return
        # Filter ep_list by selected numbers
        episodes = []
        for ep_str in ep_list:
            try:
                n = int(float(ep_str))
                if n in selected_nums:
                    episodes.append(ep_str)
            except ValueError:
                pass
        if not episodes:
            ui_emit('no_episodes_in_range')
            return
        ui_emit('downloading_count', count=len(episodes))
    else:
        return

    # ── Hand off to extractor ──────────────────────────────────
    ctx = _make_ctx(cfg)
    extract_allanime(show_id, show_name, episodes, mode=mode, ctx=ctx)


def watch_clipboard(session, cfg):
    ui_emit('clipboard_watch_start')
    ui_emit('clipboard_watch_exit_hint')
    last_text = ""
    # Clamp at read time: a hand-edited .config.json can hold 0 (or negative),
    # which would make the poll loop below spin at 100% CPU spawning the
    # clipboard probe with no sleep. The settings menu clamps 1-10; enforce
    # the floor here too since watch reads the raw value.
    interval = max(1, int(cfg.get('clipboard_check_interval_sec', 2) or 1))
    watch_stop = threading.Event()

    # Pre-load/install pyperclip on Windows if termux-clipboard-get is not present
    has_termux_clip = False
    try:
        # timeout guards against a same-named binary that blocks on a
        # clipboard service — otherwise watch hangs here at startup.
        subprocess.run(['termux-clipboard-get', '--help'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=2)
        has_termux_clip = True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
        
    if not has_termux_clip:
        try:
            import pyperclip
        except ImportError:
            ui_emit('pyperclip_installing')
            try:
                subprocess.run(
                    ['pip', 'install', 'pyperclip', '--break-system-packages', '-q'],
                    check=True, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                import pyperclip
            except Exception as e:
                print(f"[!] Auto-install failed for pyperclip: {e}")
                print("[!] Please install pyperclip manually or run in Termux with termux-api installed.")
                return

    def get_clip():
        if has_termux_clip:
            try:
                res = subprocess.run(['termux-clipboard-get'], capture_output=True, text=True, timeout=2)
                return res.stdout.strip()
            except Exception:
                return ""
        else:
            try:
                import pyperclip
                return pyperclip.paste().strip()
            except Exception:
                return ""

    try:
        last_text = get_clip()
    except Exception:
        pass

    while not watch_stop.is_set():
        try:
            text = get_clip()
            if text and text != last_text:
                last_text = text
                if text.startswith('http'):
                    from src.extractors import detect_site
                    extractor = detect_site(text)
                    if extractor:
                        print()
                        ui_emit('clipboard_link_found', url=text[:70])
                        app.reset_download_state()
                        ctx = _make_ctx(cfg)
                        from src.extractors import process_link_queue
                        process_link_queue([text], session, ctx)
                        try:
                            last_text = get_clip()
                        except Exception:
                            pass
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[!] Clipboard watch error: {e}")

        for _ in range(int(interval * 10)):
            if watch_stop.is_set():
                break
            time.sleep(0.1)

    print()
    ui_emit('clipboard_watch_stop')


# ─── MAIN REPL ────────────────────────────────────────────────
def main():
    setup_android()
    cfg = load_config()
    warn_if_over_recommended(cfg)

    def _release_wake_lock():
        stop_termux_pause_controls()
        try:
            subprocess.run(['termux-wake-unlock'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=3)
        except Exception:
            pass

    from src.downloader import show_history, register_state_callback, register_app_state, set_output_mode, make_session
    from src.extractors import process_link_queue
    from src.search import search, fsearch, clear_search_cache, ensure_async

    # One-time: pull in the aiohttp async search engine for users who installed
    # before it existed and never re-run setup.sh. No-op once present.
    ensure_async()

    register_state_callback(app.set_download_state)
    register_app_state(app)

    set_output_mode(cfg.get('log_level', 'normal'))
    from src.messages import set_color
    set_color(cfg.get('color', 'auto'))
    session = make_session()
    setup_signal_handler()
    termux_controls = start_termux_pause_controls()
    # _start_pause_listener() — removed (caused terminal glitches)
    
    print_banner(cfg)
    schedule_auto_update(cfg)
    if termux_controls:
        print("[*] Termux control: Ctrl+P = pause/resume")

    while True:
        app.reset_for_next_command()

        try:
            raw = input("\n> ").strip()
        except EOFError:
            print("\n[*] Exiting...")
            try:
                session.close()
            except Exception:
                pass
            _release_wake_lock()
            break
        except KeyboardInterrupt:
            # Ctrl+C at the prompt — just print a newline and stay in the REPL
            print()
            continue

        if not raw:
            continue

        lower = raw.lower()
        parts = raw.split()

        if lower in ('exit', 'quit', 'q'):
            print("[*] Goodbye")
            try:
                session.close()
            except Exception:
                pass
            _release_wake_lock()
            break

        elif lower in ('help', 'h', '?'):
            print(f"\n{'='*50}")
            print(f"  COMMANDS")
            print(f"{'='*50}")
            print(f"  anime <title>          - Search and download anime (AllAnime/sub)")
            print(f"  search <title>         - Find a show/movie across all search sites")
            print(f"  fsearch <title> [hint] - Fast search (korean|chinese|nollywood|etc)")
            print(f"  <url>                  - Paste direct URL to start downloading")
            print(f"  clip                   - Download link copied to clipboard")
            print(f"  watch                  - Start clipboard watcher mode")
            print(f"  resume                 - Select a paused download to resume")
            print(f"  status                 - Show current download speed & status")
            print(f"  queue add <url>        - Add a link to download queue")
            print(f"  queue list/clear/run   - Manage download queue")
            print(f"  qad/quad <url> ...     - Quick-add one or more links to queue")
            print(f"  qas                    - Start (run) the download queue")
            print(f"  settings               - Open interactive settings menu")
            print(f"  update                 - Update toolkit from GitHub")
            print(f"  version                - Show current version & update status")
            print(f"  updateyt               - Update yt-dlp only")

            print(f"  doctor                 - Check Termux dependencies")
            print(f"  cleanup                - Delete temporary/stale files")
            print(f"  history                - Show past download history")
            print(f"  exit                   - Quit app")
            print(f"{'='*50}")
            print(f"  Ctrl+P = pause/resume (Termux)   Ctrl+C = stop and save for resume")
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
            clipped = ''
            try:
                result  = subprocess.run(['termux-clipboard-get'],
                                         capture_output=True, text=True, timeout=5)
                clipped = result.stdout.strip()
            except FileNotFoundError:
                # Fallback to pyperclip on non-Termux platforms
                try:
                    import pyperclip
                    clipped = pyperclip.paste().strip()
                except ImportError:
                    print("[!] Install pyperclip (pip install pyperclip) or use Termux with termux-api")
                    continue
                except Exception as e:
                    print(f"[!] Clipboard error: {e}")
                    continue
            except Exception as e:
                print(f"[!] Clipboard error: {e}")
                continue
            if clipped and clipped.startswith('http'):
                ui_emit('clipboard_link_found', url=clipped[:70])
                app.reset_download_state()
                ctx = _make_ctx(cfg)
                process_link_queue([clipped], session, ctx)
            elif clipped:
                ui_emit('clipboard_not_url')
            else:
                print("[!] Clipboard is empty")

        elif lower in ('watch', '/watch', 'watcher'):
            watch_clipboard(session, cfg)

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

        elif parts[0].lower() in ('qad', 'quad'):
            tokens = parts[1:]
            urls = [t for t in tokens if t.lower().startswith('http')]
            if not urls:
                print("[!] Usage: qad <url> [url2 ...]  (paste one or more links to quick-add)")
            else:
                added, already = queue_add_many(urls)
                skipped = len(tokens) - len(urls)
                summary = f"[+] {added} added, {already} already in queue"
                if skipped:
                    summary += f", {skipped} skipped (not a URL)"
                total = len(load_queue())
                print(f"{summary} - {total} item(s) total, type 'qas' to start")

        elif lower == 'qas':
            queue_run(session, cfg)

        elif lower == 'update':
            script_dir = os.path.dirname(os.path.abspath(__file__))
            print("[*] Checking toolkit updates...")
            res = _git_update(script_dir, announce=False, force=True)

            if res['was_dirty']:
                ui_emit('update_dirty')
                for line in res['dirty_lines'][:8]:
                    print(f"      {line}")
            elif not res['reached_remote']:
                ui_emit('update_no_remote', debug=res['error'][:400] if res['error'] else None)
            elif not res['merge_ok']:
                ui_emit('update_merge_failed', debug=res['error'][:400] if res['error'] else None)
            elif res['updated']:
                old = (res['before_commit'] or '')[:8]
                new = (res['after_commit'] or '')[:8]
                ui_emit('update_applied', old=old, new=new)
                sys.stdout.flush()
                time.sleep(0.5)
                os.execv(sys.executable, [sys.executable] + sys.argv)
            else:
                head = (res['after_commit'] or res['before_commit'] or '')[:8]
                ui_emit('update_on_latest', head=head)

        elif lower in ('updateyt', 'update-yt', 'update ytdlp'):
            try:
                from src.downloader import _update_ytdlp
                channel = cfg.get('ytdlp_channel', 'master')
                print(f"[*] Updating yt-dlp ({channel})...")
                _update_ytdlp(channel=channel)
            except Exception as e:
                print(f"[!] yt-dlp update failed: {e}")

        elif lower == 'version':
            script_dir = os.path.dirname(os.path.abspath(__file__))
            print("[*] Checking version...")
            info = _git_status_info(script_dir)
            commit = info['commit'] or 'unknown'
            branch = info['branch'] or '?'
            print(f"  Version : {commit} ({branch})")
            if info['date']:
                print(f"  Date    : {info['date']}")
            if not info['reached_remote']:
                print("  Remote  : offline - could not check origin/main")
            elif info['behind'] is None:
                print("  Remote  : reached, but behind-count unavailable")
            elif info['behind'] == 0:
                print("  Remote  : up to date with origin/main")
            else:
                n = info['behind']
                s = 's' if n != 1 else ''
                print(f"  Remote  : {n} commit{s} behind - type 'update' to upgrade")

        elif lower.startswith('anime '):
            query = raw.split(' ', 1)[1].strip()
            if query:
                cmd_anime(query, session, cfg)
            else:
                print('[!] Usage: anime <title>')

        elif lower.startswith('search ') or lower.startswith('s '):
            query = raw.split(' ', 1)[1].strip()
            if query:
                url = search(query, session)
                if url:
                    print(f"\n[*] Downloading: {url[:60]}")
                    app.reset_download_state()
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
                    app.reset_download_state()
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
            app.reset_download_state()
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
