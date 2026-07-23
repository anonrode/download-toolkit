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
    # Filler words users tack on that release names rarely carry — these
    # must NOT count against a match or "batman movie" kills "The Batman".
    'the', 'a', 'an', 'of', 'and',
    'movie', 'movies', 'film', 'show', 'series', 'tv', 'full',
    'free', 'download', 'watch', 'online', 'stream',
])


def _is_year(tok):
    """A 4-digit token that looks like a release year (1900-2099)."""
    return len(tok) == 4 and tok.isdigit() and tok[:2] in ('19', '20')


def _tokenize(text):
    return set(re.findall(r'[a-z0-9]+', text.lower()))


# Season/episode markers a user might type, most-specific first. Each yields
# (season, episode) with episode possibly None. Matched against the raw query.
_QUERY_SE_PATTERNS = [
    # s1e3, s01e03, s01 e03
    (re.compile(r'(?i)\bs(\d{1,2})\s*e(\d{1,3})\b'),
     lambda m: (int(m.group(1)), int(m.group(2)))),
    # 1x03
    (re.compile(r'(?i)\b(\d{1,2})x(\d{1,3})\b'),
     lambda m: (int(m.group(1)), int(m.group(2)))),
    # season 1 episode 3
    (re.compile(r'(?i)\bseason\s*(\d{1,2})\s*episode\s*(\d{1,3})\b'),
     lambda m: (int(m.group(1)), int(m.group(2)))),
    # season 1 / season1
    (re.compile(r'(?i)\bseason\s*(\d{1,2})\b'),
     lambda m: (int(m.group(1)), None)),
    # s1 / s01  (also catches a trailing "silos01" once split on the boundary)
    (re.compile(r'(?i)\bs(\d{1,2})\b'),
     lambda m: (int(m.group(1)), None)),
    # episode 3 / e03 with no season
    (re.compile(r'(?i)\b(?:episode|e)\s*(\d{1,3})\b'),
     lambda m: (None, int(m.group(1)))),
]


def _parse_query_intent(query):
    """Pull season/episode intent out of a free-text query.

    Returns dict {title, season, episode, raw}:
      - title:   the query with S/E markers stripped, for relevance scoring
      - season:  int or None
      - episode: int or None
      - raw:     the original query

    Handles 'silo s1', 'silo s01', 'silo season 1', 'silos01', 'silo s1e3',
    'silo 1x03', 'silo season 1 episode 3'. The stripped title is what the
    relevance filter scores against, so these spellings can't sink a match.
    """
    raw = query.strip()
    # Split a glued season suffix like 'silos01' -> 'silos 01' won't help;
    # instead insert a boundary before a trailing sNN so \b patterns catch it.
    work = re.sub(r'(?i)([a-z])(s\d{1,2}(?:e\d{1,3})?)\b', r'\1 \2', raw)

    season = episode = None
    title = work
    for pattern, extract in _QUERY_SE_PATTERNS:
        m = pattern.search(work)
        if m:
            s, e = extract(m)
            if season is None and s is not None:
                season = s
            if episode is None and e is not None:
                episode = e
            # Strip this marker from the title text.
            title = title[:m.start()] + ' ' + title[m.end():]
            # Keep scanning: 'season 1' + separate 'episode 3' both apply.
    title = re.sub(r'\s+', ' ', title).strip()
    if not title:
        title = raw  # query was nothing but markers; fall back to raw
    return {'title': title, 'season': season, 'episode': episode, 'raw': raw}


def _relevance_score(query_tokens, title):
    """Token-set subset alignment score. Returns 0.0-1.0+.

    Scoring is driven by the *meaningful* query words only — stop tokens
    (quality/codec/filler like 'the', 'movie') and year tokens are dropped
    from the denominator so they can't sink an otherwise-good match. A year
    that DOES appear in the title still helps (small bonus), it just never
    hurts when it's missing or different.
    """
    title_tokens = _tokenize(title) - _STOP_TOKENS
    clean_query = (query_tokens - _STOP_TOKENS)
    query_years = {t for t in clean_query if _is_year(t)}
    core_query = {t for t in clean_query if not _is_year(t)}

    if not core_query:
        # Query was nothing but filler/year — fall back to matching on years
        # if present, else treat as a match-all.
        if query_years:
            return 1.0 if (query_years & title_tokens) else 0.5
        return 1.0

    overlap = len(core_query & title_tokens)
    score = overlap / len(core_query)

    # Year present in BOTH query and title → small confidence bonus, never a
    # penalty for absence/mismatch.
    if query_years and (query_years & title_tokens):
        score += 0.15

    # Exact substring bonus — all core words appear verbatim in the title.
    if all(t in title.lower() for t in core_query):
        score += 0.3

    return score


# Keep matches that cover at least ~45% of the meaningful query words. Lower
# than the old 0.7 so partial-but-real matches survive; the results are still
# sorted best-first so junk sinks to the bottom rather than being hidden.
_RELEVANCE_MIN = 0.45
# If the strict pass finds nothing, show anything with at least one real
# word in common rather than returning an empty screen.
_RELEVANCE_FALLBACK = 0.15


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
        return ([], 0, f'search failed: {e}', None)

    # apibay returns [{"id":"0","name":"No results..."}] for no results
    if not data or (len(data) == 1 and data[0].get('id') == '0'):
        return ([], 0, None, None)

    # Keep only Video-category torrents (200-299). Drops ebooks (601),
    # audiobooks (102), music, games, etc. that match the title text.
    data = [d for d in data if _is_video_category(d.get('category', ''))]
    if not data:
        return ([], 0, None, None)

    # Security filter (layers 1-4)
    safe, blocked_count, block_reasons = filter_results(data)

    # Relevance filter. Parse season/episode intent out of the query first so
    # markers like 's1', 'season 1', 'silos01' don't sink the title match —
    # relevance scores only against the stripped title. Score everything, take
    # the strict set; if empty, fall back to a looser threshold so a real
    # search never returns a blank screen just because release names are messy.
    intent = _parse_query_intent(query)
    query_tokens = _tokenize(intent['title'])
    scored = []
    for r in safe:
        r['_relevance'] = _relevance_score(query_tokens, r['name'])
        scored.append(r)

    relevant = [r for r in scored if r['_relevance'] >= _RELEVANCE_MIN]
    if not relevant:
        relevant = [r for r in scored if r['_relevance'] >= _RELEVANCE_FALLBACK]

    # Enrich results with parsed metadata
    for r in relevant:
        _enrich_result(r)

    # Sort within the flat list; grouping/season-priority happens at display.
    # Keys, most-significant first:
    #   relevance bucket (loose fallback can't outrank a solid hit)
    #   season match      (asked-for season floats up; None intent = neutral)
    #   quality tier      (1080p over 720p, etc. — your main ask)
    #   episode ascending (E01, E02, E03 within a season/quality run)
    #   seeders           (final tiebreak)
    want_season = intent['season']

    def _season_rank(r):
        if want_season is None:
            return 0
        return 1 if r.get('_season_num') == want_season else -1

    def _episode_key(r):
        # Ascending episode order, but sort() is reverse=True below, so negate.
        ep = r.get('_episode_num')
        return -(ep if ep is not None else 9999)

    relevant.sort(
        key=lambda r: (round(r.get('_relevance', 0) / 0.15),
                       _season_rank(r),
                       r.get('_quality_tier', 0),
                       _episode_key(r),
                       int(r.get('seeders', 0))),
        reverse=True)

    return (relevant, blocked_count, None, intent)


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

    # Season / episode numbers for grouping + ordering (None if absent).
    r['_season_num'], r['_episode_num'] = _parse_release_se(name)

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


_RELEASE_SE_PATTERNS = [
    re.compile(r'(?i)\bS(\d{1,2})E(\d{1,3})\b'),          # S01E03
    re.compile(r'(?i)\b(\d{1,2})x(\d{2,3})\b'),           # 1x03
    re.compile(r'(?i)Season\s*(\d{1,2}).*?Episode\s*(\d{1,3})'),
]
_RELEASE_S_PATTERNS = [
    re.compile(r'(?i)\bS(\d{1,2})(?!E\d)\b'),             # S01 (no episode)
    re.compile(r'(?i)\bSeason\s*(\d{1,2})\b'),            # Season 1
]


def _parse_release_se(name):
    """Extract (season, episode) ints from a release name; None where absent."""
    for pat in _RELEASE_SE_PATTERNS:
        m = pat.search(name)
        if m:
            return (int(m.group(1)), int(m.group(2)))
    for pat in _RELEASE_S_PATTERNS:
        m = pat.search(name)
        if m:
            return (int(m.group(1)), None)
    return (None, None)


def _make_health_bar(pct):
    """ASCII health bar: [==========] 100%"""
    filled = pct // 10
    empty = 10 - filled
    return f'[{"=" * filled}{"." * empty}]'


# ─── DISPLAY ───────────────────────────────────────────────────

def present_torrent_results(results, blocked_count, intent=None):
    """Format and print search results grouped by scope.

    Caps per group to keep output scannable. When the query named a season
    (intent['season']), episodes/packs for that season lead and other seasons
    drop to a trailing group instead of vanishing. Returns the displayed
    subset (caller handles selection from this list).
    """
    if not results:
        return []

    intent = intent or {}
    want_season = intent.get('season')

    def _in_season(r):
        # No season asked → everything counts as "in scope".
        if want_season is None:
            return True
        return r.get('_season_num') == want_season

    # Group by scope. Episodes/packs get split by asked-for season when set.
    packs = [r for r in results if r['_scope'] == 'SEASON_PACK' and _in_season(r)][:6]
    episodes = [r for r in results if r['_scope'] == 'EPISODE' and _in_season(r)][:12]
    movies = [r for r in results if r['_scope'] == 'MOVIE_OR_GENERAL'][:6]
    other_season = []
    if want_season is not None:
        other_season = [
            r for r in results
            if r['_scope'] in ('SEASON_PACK', 'EPISODE') and not _in_season(r)
        ][:6]

    displayed = []
    groups = []
    ep_header = 'EPISODES'
    pack_header = 'SEASON PACKS'
    if want_season is not None:
        ep_header = f'EPISODES · SEASON {want_season}'
        pack_header = f'SEASON PACKS · SEASON {want_season}'
    if packs:
        groups.append((pack_header, packs))
    if episodes:
        groups.append((ep_header, episodes))
    if movies:
        groups.append(('MOVIES / GENERAL', movies))
    if other_season:
        groups.append(('OTHER SEASONS', other_season))

    # Echo what we understood, so the user can see the query was parsed.
    if intent.get('season') is not None or intent.get('episode') is not None:
        bits = []
        if intent.get('title'):
            bits.append(intent['title'].title())
        if intent.get('season') is not None:
            bits.append(f'Season {intent["season"]}')
        if intent.get('episode') is not None:
            bits.append(f'Episode {intent["episode"]}')
        print(f'  {paint("parsed:", "gray")} {paint(" · ".join(bits), "cyan")}')

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
