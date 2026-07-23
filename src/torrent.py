"""
torrent.py — TPB search, result parsing, season/episode/quality bucketing.

User flow: `torrent <query>` -> search apibay.org -> security filter ->
hierarchical display -> user picks -> build magnet -> hand to download_file().

Incomplete torrents are tracked in a small JSON file so `torrent resume`
(and the main resume menu) can list and continue them.
"""

import os
import re
import json
import urllib.parse

from .security import (
    filter_results, sanitize_magnet, check_infohash,
    TRUST_VIP, TRUST_TRUSTED, TRUST_MEMBER,
)
from .downloader import CONFIG_DIR
from .messages import render as render_message, paint

# Lazy requests — only loaded when actually searching
class _LazyRequests:
    _mod = None
    def _load(self):
        if _LazyRequests._mod is None:
            import requests as _r
            _LazyRequests._mod = _r
        return _LazyRequests._mod
    def __getattr__(self, name):
        return getattr(self._load(), name)

requests = _LazyRequests()


# ─── CONSTANTS ──────────────────────────────────────────────────

TPB_API = 'https://apibay.org/q.php'
TPB_TIMEOUT = 12  # seconds


def _is_video_category(cat):
    """TPB category 200-299 = Video (movies, TV, HD, 4K, etc.)."""
    try:
        return 200 <= int(cat) < 300
    except (ValueError, TypeError):
        return False

# Scope classification patterns
_SEASON_PACK_RE = re.compile(
    r'(?i)(?:'
    r'S\d{1,2}(?!E\d)'             # S01 without E01
    r'|Season\s*\d+'               # Season 1
    r'|Complete\s+Season'          # Complete Season
    r'|(?:Full|All)\s+Season'      # Full Season / All Season(s)
    r'|S\d{1,2}\s*-\s*S\d{1,2}'   # S01-S03 (multi-season)
    r')'
)

_EPISODE_RE = re.compile(
    r'(?i)(?:'
    r'S\d{1,2}E\d{1,3}'          # S01E03
    r'|Season\s*\d+\s*Episode\s*\d+'  # Season 1 Episode 3
    r'|\d{1,2}x\d{2,3}'          # 1x03
    r'|E\d{2,3}(?!\d)'           # E03 standalone
    r'|Episode\s*\d+'            # Episode 3
    r')'
)

# Quality tier detection
_QUALITY_PATTERNS = [
    (4, re.compile(r'(?i)(?:2160p|4K|UHD)')),
    (3, re.compile(r'(?i)(?:1080p|FHD)')),
    (2, re.compile(r'(?i)(?:720p|HD(?!TV|R))')),
    (1, re.compile(r'(?i)(?:480p|SD|360p)')),
]

_QUALITY_LABELS = {
    4: '4K UHD',
    3: '1080p FHD',
    2: '720p HD',
    1: 'SD',
    0: 'Unknown',
}

# Codec detection
_CODEC_PATTERNS = [
    ('x265/HEVC', re.compile(r'(?i)(?:x265|h\.?265|HEVC)')),
    ('x264/AVC', re.compile(r'(?i)(?:x264|h\.?264|AVC)')),
    ('AV1', re.compile(r'(?i)\bAV1\b')),
    ('VP9', re.compile(r'(?i)\bVP9\b')),
]

# Audio detection — order matters (most specific first)
_AUDIO_PATTERNS = [
    ('Atmos', re.compile(r'(?i)Atmos')),
    ('TrueHD', re.compile(r'(?i)TrueHD')),
    ('DTS-HD', re.compile(r'(?i)DTS[\-\.]?HD')),
    ('DTS', re.compile(r'(?i)\bDTS\b')),
    ('DD+ 5.1', re.compile(r'(?i)(?:DDP\.?5\.?1|EAC3|E-?AC-?3)')),
    ('DD 5.1', re.compile(r'(?i)(?:DD\.?5\.?1|AC3\.?5\.?1|AC-?3)')),
    ('AAC', re.compile(r'(?i)\bAAC')),
]

# Source detection
_SOURCE_PATTERNS = [
    ('BluRay', re.compile(r'(?i)(?:BluRay|BDRip|BRRip)')),
    ('WEB-DL', re.compile(r'(?i)(?:WEB[\-\.]?DL|WEBDL|WEBRip|AMZN|NF|DSNP)')),
    ('HDTV', re.compile(r'(?i)(?:HDTV|PDTV)')),
    ('HDRip', re.compile(r'(?i)HDRip')),
    ('CAM', re.compile(r'(?i)(?:CAM|TS|HDTS|TELESYNC)')),
]

# Trust tier display badges
_TRUST_BADGE = {
    TRUST_VIP: '[VIP]',
    TRUST_TRUSTED: '[Trusted]',
    TRUST_MEMBER: '[New]',
}

_TRUST_COLOR = {
    TRUST_VIP: ('bgreen',),
    TRUST_TRUSTED: ('byellow',),
    TRUST_MEMBER: ('gray',),
}


# ─── TOKEN-SET RELEVANCE FILTER ────────────────────────────────

_STOP_TOKENS = frozenset([
    's01', 's02', 's03', 's04', 's05', 's06', 's07', 's08', 's09', 's10',
    'e01', 'e02', 'e03', 'season', 'episode', 'complete', 'pack',
    '1080p', '720p', '2160p', '480p', '4k', 'uhd', 'fhd', 'hd', 'sd',
    'x264', 'x265', 'hevc', 'avc', 'web', 'dl', 'bluray', 'webrip',
    'aac', 'dts', 'atmos', 'ddp', 'dd', 'ac3', 'eac3',
    'mkv', 'mp4', 'avi',
])


def _tokenize(text):
    return set(re.findall(r'[a-z0-9]+', text.lower()))


def _relevance_score(query_tokens, title):
    """Token-set subset alignment score. Returns 0.0-1.0+."""
    title_tokens = _tokenize(title) - _STOP_TOKENS
    clean_query = query_tokens - _STOP_TOKENS

    if not clean_query:
        return 1.0  # empty query matches everything

    overlap = len(clean_query & title_tokens)
    score = overlap / len(clean_query)

    # Exact substring bonus
    if all(t in title.lower() for t in clean_query):
        score += 0.3

    return score


_RELEVANCE_MIN = 0.7  # strict — must match at least 70% of query tokens


# ─── SEARCH ────────────────────────────────────────────────────

def search_tpb(query):
    """Search The Pirate Bay via apibay.org.

    Returns:
        (results: list[dict], blocked_count: int, error: str or None)
        Each result dict has: name, info_hash, seeders, leechers, size,
        username, status, _trust_tier, _scope, _quality_tier, _quality_label,
        _codec, _audio, _source, _size_str, _health_pct, _health_bar.
    """
    try:
        resp = requests.get(
            TPB_API,
            params={'q': query},
            timeout=TPB_TIMEOUT,
            headers={'User-Agent': 'Mozilla/5.0'},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return ([], 0, f'search failed: {e}')

    # apibay returns [{"id":"0","name":"No results..."}] for no results
    if not data or (len(data) == 1 and data[0].get('id') == '0'):
        return ([], 0, None)

    # Keep only Video-category torrents (200-299). Drops ebooks (601),
    # audiobooks (102), music, games, etc. that match the title text.
    data = [d for d in data if _is_video_category(d.get('category', ''))]
    if not data:
        return ([], 0, None)

    # Security filter (layers 1-4)
    safe, blocked_count, block_reasons = filter_results(data)

    # Relevance filter
    query_tokens = _tokenize(query)
    relevant = []
    for r in safe:
        score = _relevance_score(query_tokens, r['name'])
        if score >= _RELEVANCE_MIN:
            r['_relevance'] = score
            relevant.append(r)

    # Enrich results with parsed metadata
    for r in relevant:
        _enrich_result(r)

    # Sort: quality tier desc, seeders desc
    relevant.sort(key=lambda r: (r.get('_quality_tier', 0), int(r.get('seeders', 0))),
                  reverse=True)

    return (relevant, blocked_count, None)


# ─── RESULT ENRICHMENT ──────────────────────────────────────────

def _enrich_result(r):
    """Parse release name and add metadata fields."""
    name = r.get('name', '')

    # Scope classification
    if _EPISODE_RE.search(name):
        r['_scope'] = 'EPISODE'
    elif _SEASON_PACK_RE.search(name):
        r['_scope'] = 'SEASON_PACK'
    else:
        r['_scope'] = 'MOVIE_OR_GENERAL'

    # Quality tier
    r['_quality_tier'] = 0
    r['_quality_label'] = _QUALITY_LABELS[0]
    for tier, pattern in _QUALITY_PATTERNS:
        if pattern.search(name):
            r['_quality_tier'] = tier
            r['_quality_label'] = _QUALITY_LABELS[tier]
            break

    # Codec
    r['_codec'] = ''
    for label, pattern in _CODEC_PATTERNS:
        if pattern.search(name):
            r['_codec'] = label
            break

    # Audio
    r['_audio'] = ''
    for label, pattern in _AUDIO_PATTERNS:
        if pattern.search(name):
            r['_audio'] = label
            break

    # Source
    r['_source'] = ''
    for label, pattern in _SOURCE_PATTERNS:
        if pattern.search(name):
            r['_source'] = label
            break

    # Size formatting
    size_bytes = int(r.get('size', 0))
    if size_bytes >= 1073741824:  # >= 1 GB
        r['_size_str'] = f'{size_bytes / 1073741824:.1f} GB'
    elif size_bytes >= 1048576:   # >= 1 MB
        r['_size_str'] = f'{size_bytes / 1048576:.0f} MB'
    else:
        r['_size_str'] = f'{size_bytes / 1024:.0f} KB'

    # Health bar (seeder:leecher ratio)
    seeders = int(r.get('seeders', 0))
    leechers = int(r.get('leechers', 0))
    total = seeders + leechers
    if total > 0:
        r['_health_pct'] = int(seeders / total * 100)
    else:
        r['_health_pct'] = 0
    r['_health_bar'] = _make_health_bar(r['_health_pct'])


def _make_health_bar(pct):
    """ASCII health bar: [==========] 100%"""
    filled = pct // 10
    empty = 10 - filled
    return f'[{"=" * filled}{"." * empty}]'


# ─── DISPLAY ───────────────────────────────────────────────────

def present_torrent_results(results, blocked_count):
    """Format and print search results grouped by scope.

    Caps at 5 results per group (15 total max) to keep output scannable.
    Returns the displayed subset (caller handles selection from this list).
    """
    if not results:
        return []

    # Group by scope
    packs = [r for r in results if r['_scope'] == 'SEASON_PACK'][:5]
    episodes = [r for r in results if r['_scope'] == 'EPISODE'][:5]
    movies = [r for r in results if r['_scope'] == 'MOVIE_OR_GENERAL'][:5]

    displayed = []
    groups = []
    if packs:
        groups.append(('SEASON PACKS', packs))
    if episodes:
        groups.append(('SINGLE EPISODES', episodes))
    if movies:
        groups.append(('MOVIES / GENERAL', movies))

    print()
    idx = 1
    for group_name, items in groups:
        header = f'--- {group_name} '
        header += '-' * (60 - len(header))
        print(f'  {paint(header, "bold")}')
        print()

        for r in items:
            _print_result(r, idx)
            displayed.append(r)
            idx += 1
            print()

    # Footer
    sep = '-' * 62
    print(f'  {sep}')
    if blocked_count > 0:
        print(f'  {paint("[!]", "byellow")} {blocked_count} result(s) blocked by security filter')
    total_available = len(results)
    if total_available > len(displayed):
        print(f'  Showing top {len(displayed)} of {total_available} results')
    print(f'  {sep}')

    return displayed


def _print_result(r, idx):
    """Print a single torrent result card."""
    # Title line
    name = r.get('name', '?')
    trust_tier = r.get('_trust_tier', TRUST_MEMBER)
    badge = paint(_TRUST_BADGE[trust_tier], *_TRUST_COLOR[trust_tier])

    print(f'  {paint(f"[{idx}]", "bold")}  {name}')

    # Metadata line 1: quality | codec | source
    meta1_parts = []
    if r['_quality_label']:
        meta1_parts.append(r['_quality_label'])
    if r['_source']:
        meta1_parts.append(r['_source'])
    if r['_codec']:
        meta1_parts.append(r['_codec'])
    if r['_audio']:
        meta1_parts.append(r['_audio'])
    meta1 = '  |  '.join(meta1_parts) if meta1_parts else 'Unknown quality'
    print(f'       {paint(meta1, "cyan")}')

    # Metadata line 2: size | seeds | leech | badge
    seeders = r.get('seeders', '0')
    leechers = r.get('leechers', '0')
    size_str = r.get('_size_str', '?')
    health = r.get('_health_bar', '[..........]')
    health_pct = r.get('_health_pct', 0)

    # Color the health bar
    if health_pct >= 80:
        health_colored = paint(health, 'bgreen')
    elif health_pct >= 50:
        health_colored = paint(health, 'byellow')
    else:
        health_colored = paint(health, 'bred')

    print(f'       {size_str}  |  Seeds: {seeders}  |  Leech: {leechers}  |  {badge}')
    print(f'       Health: {health_colored} {health_pct}%')


# ─── SELECTION + MAGNET BUILD ──────────────────────────────────

def select_and_build_magnet(results):
    """Let the user pick a result and build a sanitized magnet URI.

    Returns:
        (magnet_uri: str, result_dict: dict) or (None, None) if cancelled.
    """
    total = len(results)
    print()
    try:
        choice = input(f"  Pick a number (1-{total}) or 'b' to go back: ").strip()
    except (EOFError, KeyboardInterrupt):
        return (None, None)

    if choice.lower() in ('b', 'back', 'q', '0', ''):
        return (None, None)

    try:
        idx = int(choice)
    except ValueError:
        print(f'  {render_message("invalid_selection")}')
        return (None, None)

    if not (1 <= idx <= total):
        print(f'  {render_message("invalid_selection")}')
        return (None, None)

    selected = results[idx - 1]

    # Build sanitized magnet
    magnet, reason = sanitize_magnet(
        selected['info_hash'],
        selected['name']
    )
    if not magnet:
        print(f'  {paint("[X]", "bred")} Security block: {reason}')
        return (None, None)

    return (magnet, selected)


# ─── INCOMPLETE-TORRENT STATE (for resume) ─────────────────────

_TORRENT_STATE_FILE = os.path.join(CONFIG_DIR, '.torrent_state.json')


def _load_torrent_state():
    try:
        if os.path.exists(_TORRENT_STATE_FILE):
            with open(_TORRENT_STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_torrent_state(state):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = _TORRENT_STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, _TORRENT_STATE_FILE)
    except Exception:
        pass


def record_torrent_start(info_hash, name, magnet, folder):
    """Remember an in-progress torrent so it can be resumed later.

    Keyed by info_hash so re-adding the same torrent updates one entry.
    """
    state = _load_torrent_state()
    state[info_hash.lower()] = {
        'name': name,
        'magnet': magnet,
        'folder': folder,
        'status': 'downloading',
    }
    _save_torrent_state(state)


def mark_torrent_complete(info_hash):
    """Drop a torrent from the resume list once it finishes."""
    state = _load_torrent_state()
    if info_hash.lower() in state:
        del state[info_hash.lower()]
        _save_torrent_state(state)


def mark_torrent_stopped(info_hash):
    """Flag a torrent as stopped/incomplete so it shows in the resume menu."""
    state = _load_torrent_state()
    key = info_hash.lower()
    if key in state:
        state[key]['status'] = 'stopped'
        _save_torrent_state(state)


def list_incomplete_torrents():
    """Return [(info_hash, info_dict), ...] for all incomplete torrents."""
    return list(_load_torrent_state().items())


def show_torrent_resume_list():
    """Print the incomplete-torrent menu. Returns the list or False if empty."""
    items = list_incomplete_torrents()
    if not items:
        print(f'  {render_message("no_paused_downloads")}')
        return False

    print(f"\n{'='*50}")
    print(f"  INCOMPLETE TORRENTS")
    print(f"{'='*50}")
    for i, (ih, inf) in enumerate(items, 1):
        name = inf.get('name', 'Unknown')[:60]
        status = inf.get('status', 'downloading')
        tag = paint('[stopped]', 'byellow') if status == 'stopped' else paint('[partial]', 'bcyan')
        print(f"  [{i}] {name}")
        print(f"       {tag}  {ih[:16]}...")
    print(f"{'='*50}")
    return items
