"""User-facing Anonrode message helpers.

Business logic should emit message IDs; this layer decides how Anon says it.
"""

import os
import sys

# ─── COLOR ────────────────────────────────────────────────────
# Plain ANSI escape codes only — no Unicode box-art or emoji, which render
# as mojibake on many Termux setups. ANSI SGR codes are pure ASCII and are
# well supported on Termux, so color is the one safe way to add richness.
CODES = {
    'reset': '\033[0m', 'bold': '\033[1m', 'dim': '\033[2m',
    'red': '\033[31m', 'green': '\033[32m', 'yellow': '\033[33m',
    'blue': '\033[34m', 'magenta': '\033[35m', 'cyan': '\033[36m',
    'white': '\033[37m', 'gray': '\033[90m',
    'bred': '\033[91m', 'bgreen': '\033[92m', 'byellow': '\033[93m',
    'bcyan': '\033[96m',
}

_COLOR_ON = False


def _detect_color():
    # Honor the NO_COLOR convention (https://no-color.org) and only emit color
    # when stdout is a real terminal — piping to a file/log stays clean ASCII.
    if os.environ.get('NO_COLOR'):
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _enable_windows_vt():
    # Windows 10+ consoles need VT processing turned on before ANSI works;
    # best-effort so the dev machine isn't full of raw escape codes.
    if os.name != 'nt':
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def set_color(mode='auto'):
    """mode: 'auto' (TTY + NO_COLOR aware), 'always', or 'never'."""
    global _COLOR_ON
    if mode == 'always':
        _COLOR_ON = True
    elif mode == 'never':
        _COLOR_ON = False
    else:
        _COLOR_ON = _detect_color()
    if _COLOR_ON:
        _enable_windows_vt()
    return _COLOR_ON


def color_enabled():
    return _COLOR_ON


def paint(text, *names):
    """Wrap text in the named ANSI codes, or return it untouched if color is off."""
    if not _COLOR_ON or not names:
        return text
    prefix = ''.join(CODES.get(n, '') for n in names)
    return f"{prefix}{text}{CODES['reset']}"


LABELS = {
    'ok': '[OK]',
    'info': '[..]',
    'paused': '[PAUSED]',
    'warn': '[!]',
    'fail': '[X]',
    'debug': '[debug]',
}

# Color per label level — applied only to the bracket tag, not the message body,
# so text stays readable and copy-pasteable.
LABEL_COLORS = {
    'ok': ('bgreen',),
    'info': ('bcyan',),
    'paused': ('byellow',),
    'warn': ('byellow',),
    'fail': ('bred',),
    'debug': ('gray',),
}

MESSAGES = {
    'already_saved': ('ok', 'Already saved - skipping this one.'),
    'download_start': ('info', 'Anon is fetching: {filename}'),
    'download_done': ('ok', 'Done - {filename} is saved.'),
    'download_failed': ('fail', 'Anon could not finish this download.'),
    'resume_from_size': ('info', 'Resuming safely from {size} MB.'),
    'paused_saved': ('paused', 'Paused safely - your progress is saved.'),
    'paused_at_size': ('paused', 'Paused safely at {size} MB - your progress is saved.'),
    'resume_start': ('info', 'Resuming from where Anon stopped...'),
    'stopped_saved': ('paused', 'Stopped - Anon saved what can be resumed.'),
    'network_lost': ('paused', 'Network dropped - Anon paused the download safely.'),
    'network_restored': ('info', 'Network is back - resuming...'),
    'link_expired': ('warn', 'Link expired - paste a fresh one to continue.'),
    'fresh_link_start': ('info', 'Link expired - Anon is fetching a fresh one.'),
    'fresh_link_found': ('ok', 'Fresh link found - resuming now.'),
    'fresh_link_failed': ('warn', 'Anon could not refresh this link. Paste a fresh one to continue.'),
    'no_download_link': ('fail', 'Anon could not find a working download link on this page.'),
    'no_episodes_found': ('warn', 'Anon could not find episodes on this page. Try the main series page.'),
    'page_fetch_failed': ('fail', 'Anon could not open this page. Check the link and try again.'),
    'queue_added': ('ok', 'Added to queue. Anon will keep it ready.'),
    'queue_empty': ('warn', 'Your queue is empty. Add a link with: queue add <url>'),
    'queue_complete': ('ok', 'Queue complete - everything finished.'),
    'queue_kept_unfinished': ('paused', 'Anon kept {count} unfinished item(s) so you can resume later.'),
    'clipboard_watch_start': ('info', 'Clipboard watch is on. Copy a link and Anon will pick it up.'),
    'clipboard_watch_stop': ('ok', 'Stopped watching clipboard.'),
    'clipboard_link_found': ('info', 'Anon spotted a link on your clipboard: {url}'),
    'clipboard_not_url': ('warn', "That clipboard text is not a link."),
    'missing_tool': ('fail', 'Anon is missing a download tool: {tool}. Install it with: {command}'),
    'setting_saved': ('ok', 'Done - {label} is now {value}.'),
    'setting_invalid': ('warn', '{hint}'),
    'already_in_queue': ('info', 'That one is already in your queue.'),
    'queue_cleared': ('ok', 'Queue cleared.'),
    'queue_removed': ('ok', 'Removed from queue: {url}'),
    'queue_bad_index': ('warn', "That queue number doesn't exist."),
    'update_success': ('ok', 'Anonrode is updated. Restart the app to use the latest version.'),
    'update_failed': ('fail', 'Update failed - looks like the internet took a quick nap. Check your connection and try again.'),
    'update_dirty': ('warn', 'You have local changes. Commit or stash them before Anon can update:'),
    'update_no_remote': ('warn', 'Anon could not reach GitHub, so nothing was checked. Check your connection and try again.'),
    'update_merge_failed': ('fail', 'Anon could not apply the update.'),
    'update_applied': ('ok', 'Updated {old} -> {new}. Anon is restarting...'),
    'update_on_latest': ('ok', "You're on the latest (origin/main @ {head})."),
    'site_mode': ('info', 'Anon is on it - {site}.'),
    'fetching_episode_list': ('info', 'Anon is fetching the episode list...'),
    'no_episode_links': ('warn', 'Anon could not find episode links here. Try the main series page.'),
    'no_episodes_in_range': ('warn', 'No episodes matched that range. Try different numbers.'),
    'no_episodes_to_download': ('warn', 'Nothing to download here.'),
    'invalid_range': ('warn', 'That range looks off - use a format like 5-10.'),
    'invalid_selection': ('warn', "That selection didn't work. Try again."),
    'invalid_items': ('warn', 'That list looks off - use a format like 1,3,7.'),
    'stopped_by_user': ('paused', 'Stopped - Anon halted here.'),
    'check_url_retry': ('warn', 'Check the link and try again.'),
    'unsupported_site': ('warn', "Anon doesn't know this site yet."),
    'supported_sites': ('info', 'Supported: {sites}'),
    'resolving_provider': ('info', 'Anon is resolving the provider...'),
    'no_provider_found': ('warn', 'Anon could not find a usable source for this episode.'),
    'season_label': ('info', 'Season: {season}'),
    'search_running': ('info', 'Anon is searching for: {query}'),
    'fast_search_running': ('info', 'Anon is running a fast search for: {query}'),
    'fast_search_running_hint': ('info', 'Anon is running a fast search ({hint}) for: {query}'),
    'search_found_on': ('ok', 'Found it on {site}.'),
    'search_cached': ('info', 'Anon already had this saved: {query}'),
    'search_empty_query': ('warn', 'Type something for Anon to search.'),
    'search_nothing_found': ('warn', 'Anon found nothing for: {query}'),
    'search_try_again': ('info', 'Try a different spelling, or paste the URL directly.'),
    'search_no_index': ('info', 'Anon searches by probing links directly - no index needed.'),
    'cache_cleared': ('ok', 'Search cache cleared.'),
    'cache_none': ('info', 'No cache file to clear.'),
    'cache_clear_failed': ('fail', 'Anon could not clear the cache: {error}'),
    'anime_search_running': ('info', 'Anon is searching AllAnime for "{query}"...'),
    'anime_no_results': ('warn', 'No results - try a different spelling.'),
    'anime_mode_fallback': ('warn', 'No {mode} available - Anon is switching to {fallback}.'),
    'anime_no_episodes': ('warn', 'No episodes available for this show.'),
    'anime_show_selected': ('ok', '{show} - {count} episode(s) available ({mode}).'),
    'anime_ep_list_failed': ('warn', 'Anon could not fetch the episode list.'),
    'anime_ep_count': ('ok', '{count} episode(s) found (Episodes: {first}-{last}).'),
    'cancelled': ('info', 'Cancelled.'),
    'downloading_count': ('info', 'Anon is downloading {count} episode(s).'),
    # downloader.py
    'disk_space_ok': ('ok', 'Disk space: {free} GB free.'),
    'disk_space_low': ('warn', 'Low disk space: {free} GB free. Downloads may fail.'),
    'disk_space_critical': ('fail', 'Critically low disk space ({free} GB free, limit is {limit} GB) - stopping.'),
    'history_empty': ('info', 'No download history yet.'),
    'no_paused_downloads': ('info', 'No paused downloads found.'),
    'incomplete_resume': ('info', 'Incomplete download found - Anon will resume it.'),
    'incomplete_resume_size': ('info', 'Incomplete download found ({size} MB) - Anon will resume it.'),
    'already_downloaded_verified': ('ok', 'Already downloaded (receipt verified) - skipping.'),
    'found_existing_file': ('ok', 'Found existing file ({size} MB).'),
    'installing_aria2': ('info', 'Anon is installing aria2...'),
    'aria2_installed': ('ok', 'aria2 installed.'),
    'aria2_install_failed': ('fail', 'Anon could not install aria2: {error}'),
    'aria2_manual': ('warn', 'Install aria2 manually from https://github.com/aria2/aria2/releases'),
    'installing_ytdlp': ('info', 'Anon is installing yt-dlp...'),
    'ytdlp_installed': ('ok', 'yt-dlp installed.'),
    'ytdlp_install_failed': ('fail', 'Anon could not install yt-dlp: {error}'),
    'pkg_installing': ('info', '{pkg} not found - Anon is installing it...'),
    'pkg_installed': ('ok', '{pkg} installed.'),
    'pkg_install_failed': ('warn', 'Anon could not auto-install {pkg}. Install it manually: pkg install {pkg}'),
    'file_too_small_kept': ('warn', 'File is still very small - keeping it so you can resume.'),
    'ytdlp_unavailable': ('warn', 'yt-dlp is unavailable.'),
    'download_paused_ctrlp': ('paused', 'Paused - press Ctrl+P to resume.'),
    'download_resuming': ('info', 'Resuming download...'),
    'ytdlp_timeout_moving_on': ('fail', 'yt-dlp timed out - Anon is moving on.'),
    'ytdlp_no_output': ('fail', 'yt-dlp finished but produced no file.'),
    'ytdlp_stopped': ('info', 'yt-dlp stopped.'),
    'ytdlp_failed': ('fail', 'yt-dlp could not finish this download.'),
    'ytdlp_failed_no_format': ('fail', 'yt-dlp failed - no compatible format found.'),
    'ytdlp_error': ('fail', 'yt-dlp error: {error}'),
    'backend_timeout': ('fail', '{label} timed out.'),
    'backend_error': ('fail', '{label} error: {error}'),
    'already_downloaded_skip': ('ok', 'Already downloaded - skipping.'),
    'done_prev_session_skip': ('ok', 'Done in a previous session - skipping.'),
    'link_expired_repaste': ('warn', 'Link expired (404) - re-paste the series URL for fresh links.'),
    'retrying_requests': ('warn', 'Connection hiccup: {error}. Retrying ({attempt}/5)...'),
    'write_failed': ('warn', 'Anon could not write {path}: {error}'),
    'corrupted_json_moved': ('warn', 'Corrupted file moved to {backup}: {error}'),
    'corrupted_json_kept': ('warn', 'Corrupted file left untouched: {path}: {error}'),
    'prefetch_error': ('warn', 'Prefetch error: {error}'),
    'thread_error': ('warn', 'Thread error for {name}: {error}'),
    # main.py
    'queue_start_hint': ('info', "Queue: {count} item(s) - type 'queue start' to begin."),
    'queue_starting': ('info', 'Anon is starting the queue - {count} item(s).'),
    'toolkit_updated': ('ok', 'Toolkit updated - restart to use the latest version.'),
    'tmux_missing': ('warn', 'tmux not found - install it with: pkg install tmux'),
    'clipboard_watch_exit_hint': ('info', 'Press Ctrl+C once to exit watch mode and return to the main menu.'),
    'pyperclip_installing': ('info', 'pyperclip not found - Anon is installing it...'),
}


def render(message_id, **values):
    level, template = MESSAGES[message_id]
    label = paint(LABELS[level], *LABEL_COLORS.get(level, ()))
    return f"{label} {template.format(**values)}"


def emit(printer, message_id, debug=None, is_debug=False, **values):
    printer(render(message_id, **values))
    if debug and is_debug:
        printer(f"{LABELS['debug']} {debug}")
