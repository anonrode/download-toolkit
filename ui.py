"""
ui.py — Banner, display helpers, settings handler, and main REPL loop.

Merged from: ui/display.py, ui/settings.py, ui/repl.py
"""

import os
import re
import subprocess

from config import (
    QUALITY_MAP, SEARCH_SITES, SOCIAL_DOMAINS,
    save_config, load_history, load_resume, mark_series_complete,
)
from core import (
    get_free_space_gb, wait_if_paused,
    DownloadSummary, download_file,
)
from search import normalise, sites_for_query, search_all_sites, score_and_group, display_results


# ─── Display ─────────────────────────────────────────────────────

def print_banner(state):
    q  = state.quality_label
    pc = state.parallel_count
    print('╔══════════════════════════════════════════════╗')
    print('║         DOWNLOAD TOOLKIT v3.0                ║')
    print(f'║  Quality: {q:<6}  Parallel: {pc}                   ║')
    print('╠══════════════════════════════════════════════╣')
    print('║  SITES:                                      ║')
    print('║  nkiri • dramakey • dramarain • naijavault   ║')
    print('║  plutomovies • anitaku • myasiantv           ║')
    print('║  naijaprey • 9jarocks • +yt/ig/tiktok/fb     ║')
    print('╠══════════════════════════════════════════════╣')
    print('║  COMMANDS (type and press Enter):             ║')
    print('║  search <title>  • settings  • history       ║')
    print('║  resume  • clip (paste URL)  • exit          ║')
    print('╚══════════════════════════════════════════════╝')


def check_disk_space():
    free = get_free_space_gb()
    if free < 1.0:
        print(f'[!] Low disk space: {free:.1f}GB free. Downloads may fail.')
    else:
        print(f'[✓] Disk space: {free:.1f}GB free')


def show_history():
    history = load_history()
    if not history:
        print('[*] No download history yet')
        return
    print(f"\n{'='*50}")
    print('  DOWNLOAD HISTORY')
    print(f"{'='*50}")
    for name, entries in list(history.items())[-20:]:
        print(f'\n  {name} ({len(entries)} file(s))')
        for e in entries[-3:]:
            print(f"    • {e['time']} — {os.path.basename(e['file'])}")
    print(f"{'='*50}")


def show_resume_list(state_dict: dict) -> bool:
    if not state_dict:
        print('[*] No paused downloads found')
        return False
    print(f"\n{'='*50}")
    print('  PAUSED DOWNLOADS')
    print(f"{'='*50}")
    for i, (url, info) in enumerate(state_dict.items(), 1):
        name    = info.get('name', 'Unknown')
        done    = len(info.get('done', []))
        current = info.get('current')
        status  = f'paused at: {current}' if current else f'{done} episode(s) done'
        print(f'  [{i}] {name}')
        print(f'       {status}')
        print(f'       {url[:60]}')
    print(f"{'='*50}")
    return True


# ─── Settings ────────────────────────────────────────────────────

def handle_settings(cmd: str, state):
    parts = cmd.strip().split()

    if len(parts) == 1:
        _show_settings(state)
        return

    if len(parts) < 3:
        print('  Usage: settings quality 720p | settings parallel 2 | settings bandwidth 500')
        return

    setting = parts[1].lower()
    value   = parts[2].lower()

    if setting == 'quality':
        if value in QUALITY_MAP:
            state.quality_label = value
            state.quality_fmt   = QUALITY_MAP[value]
            print(f'  [✓] Quality set to {value}')
            save_config(state)
        else:
            print(f'  [!] Valid options: {", ".join(QUALITY_MAP.keys())}')

    elif setting == 'parallel':
        try:
            n = int(value)
            if 1 <= n <= 3:
                state.parallel_count = n
                print(f'  [✓] Parallel downloads set to {n}')
                save_config(state)
            else:
                print('  [!] Valid range: 1–3')
        except ValueError:
            print('  [!] Enter a number: 1, 2, or 3')

    elif setting == 'bandwidth':
        try:
            n = int(value)
            state.bandwidth_limit = n
            bw = f'{n} KB/s' if n > 0 else 'unlimited'
            print(f'  [✓] Bandwidth limit set to {bw}')
            save_config(state)
        except ValueError:
            print('  [!] Enter KB/s number (0 = unlimited)')

    else:
        print('  [!] Unknown setting. Use: quality, parallel, bandwidth')


def _show_settings(state):
    bw = f'{state.bandwidth_limit} KB/s' if state.bandwidth_limit > 0 else 'unlimited'
    print(f'\n  Current settings:')
    print(f'  • quality   = {state.quality_label}')
    print(f'  • parallel  = {state.parallel_count} download(s) at once')
    print(f'  • bandwidth = {bw}')
    print(f'\n  To change: settings quality 720p | settings parallel 2 | settings bandwidth 500')


# ─── Site routing ────────────────────────────────────────────────

def _build_site_map():
    from sites import nkiri, dramakey, dramarain, jarocks
    from sites import naijaprey, myasiantv, naijavault, anitaku, plutomovies, social

    SITE_MAP = {
        'thenkiri.com':      nkiri.extract,
        'nkiri.com':         nkiri.extract,
        'dramakey.com':      dramakey.extract,
        'dramakey.cc':       dramarain.extract,
        'dramarain.com':     dramarain.extract,
        '9jarocks.net':      jarocks.extract,
        'naijaprey.tv':      naijaprey.extract,
        'myasiantv9.com.ro': myasiantv.extract,
        'myasiantv9.com':    myasiantv.extract,
        'naijavault.com':    naijavault.extract,
        'anitaku.com.ro':    anitaku.extract,
        'plutomovies.com':   plutomovies.extract,
    }
    return SITE_MAP, social.extract


_SITE_MAP, _extract_social = _build_site_map()


def detect_extractor(url: str):
    for domain, fn in _SITE_MAP.items():
        if domain in url:
            return fn
    for domain in SOCIAL_DOMAINS:
        if domain in url:
            return _extract_social
    return None


def process_links(urls: list[str], session, state):
    for i, url in enumerate(urls, 1):
        if state.stop:
            print('[*] Stopped by user')
            break
        wait_if_paused(state)
        if len(urls) > 1:
            print(f"\n{'─'*50}")
            print(f'  Queue [{i}/{len(urls)}]: {url[:60]}')
            print(f"{'─'*50}")
        fn = detect_extractor(url)
        if not fn:
            print(f'[!] Unsupported site: {url}')
            print(f"[!] Supported: {', '.join(_SITE_MAP.keys())}")
            continue
        try:
            fn(url, session, state)
        except Exception as e:
            print(f'\n[!] Unexpected error: {e}')
            print('[!] Please check the URL and try again')


# ─── Search command ──────────────────────────────────────────────

def handle_search(raw_query: str, session, state):
    query = normalise(raw_query)
    sites = sites_for_query(query, SEARCH_SITES)

    print(f'\n[*] Searching: "{query}"')
    print(f'[*] Sites: {", ".join(sites)}')
    print(f"{'─'*52}")

    raw     = search_all_sites(query, sites)
    scored  = score_and_group(raw, query)
    numbered = display_results(scored)

    if not numbered:
        return

    if len(numbered) >= 1 and numbered[0]['score'] >= 0.80:
        top = numbered[0]
        print(f"\n[*] Best match: {top['title']}")
        print(f"    {top['site']}")
        try:
            ans = input('Download this? [Y/n]: ').strip().lower()
            if ans in ('', 'y', 'yes'):
                process_links([top['url']], session, state)
                return
        except (EOFError, KeyboardInterrupt):
            return

    print(f'\nPick a result (1-{len(numbered)}) or 0 to cancel:')
    try:
        idx = int(input('> ').strip())
        if idx == 0 or idx > len(numbered):
            print('[*] Cancelled')
            return
        chosen = numbered[idx - 1]
        print(f"\n[*] Selected: {chosen['title']}")
        process_links([chosen['url']], session, state)
    except (ValueError, EOFError):
        print('[*] Cancelled')


# ─── Resume command ──────────────────────────────────────────────

def handle_resume(session, state):
    resume_state = load_resume()
    if not resume_state:
        print('[*] No paused downloads to resume')
        return

    entries = list(resume_state.items())
    show_resume_list(resume_state)

    if len(entries) == 1:
        url  = entries[0][0]
        name = entries[0][1].get('name', 'Unknown')
        print(f'\n[*] Resuming: {name}')
        process_links([url], session, state)
    else:
        print(f'\nPick a series to resume (1-{len(entries)}) or 0 to cancel:')
        try:
            choice = int(input('> ').strip())
            if 1 <= choice <= len(entries):
                url  = entries[choice - 1][0]
                name = entries[choice - 1][1].get('name', 'Unknown')
                print(f'[*] Resuming: {name}')
                process_links([url], session, state)
            else:
                print('[*] Cancelled')
        except (ValueError, EOFError):
            print('[*] Cancelled')


# ─── Main REPL loop ──────────────────────────────────────────────

def run(session, state):
    while True:
        if state.stop:
            print('\nBye!')
            break

        try:
            raw = input('\n> ').strip()
        except (EOFError, KeyboardInterrupt):
            print('\nBye!')
            break

        if not raw:
            continue

        lower = raw.lower()

        if lower == 'exit':
            print('Bye!')
            break

        elif lower == 'history':
            show_history()

        elif lower == 'settings' or lower.startswith('settings '):
            handle_settings(raw, state)

        elif lower.startswith('search '):
            query = raw[7:].strip()
            if query:
                handle_search(query, session, state)
            else:
                print('[!] Usage: search <show name>')

        elif lower == 'resume':
            handle_resume(session, state)

        elif lower == 'clip':
            try:
                result  = subprocess.run(
                    ['termux-clipboard-get'],
                    capture_output=True, text=True, timeout=5
                )
                clipped = result.stdout.strip()
                if clipped.startswith('http'):
                    print(f'[*] From clipboard: {clipped[:70]}')
                    process_links([clipped], session, state)
                elif clipped:
                    print(f'[!] Clipboard content: {clipped[:60]}')
                    print('[!] Does not look like a URL')
                else:
                    print('[!] Clipboard is empty')
            except FileNotFoundError:
                print('[!] termux-clipboard-get not found — install: pkg install termux-api')
            except Exception as e:
                print(f'[!] Clipboard error: {e}')

        elif raw.startswith('http'):
            urls = [u.strip() for u in re.split(r'\s+', raw) if u.strip().startswith('http')]
            if urls:
                process_links(urls, session, state)
            else:
                print('[!] No valid URLs found')

        else:
            print(f'[!] Unknown command: {raw[:40]}')
            print('[!] Type: search <title> | settings | history | resume | clip | exit')
