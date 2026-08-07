"""
security.py — 7-Layer Anti-Malware & Torrent Security Shield.

Filters torrent search results and validates downloaded files before they
reach the user. Every layer is independent — a result must pass ALL to be
presented. Layers:

  1. Uploader Trust & Reputation    — VIP/trusted priority, seeder floor
  2. Extension & Double-Extension    — blacklist dangerous file types
  3. InfoHash SHA1 Validation        — reject malformed hashes
  4. Magnet & Shell Injection Guard  — sanitize URIs + subprocess args
  5. Path Traversal Guard            — keep downloads inside base dir
  6. (Skipped — no pre-download file metadata from apibay.org)
  7. Magic-Byte Container Inspector  — verify file headers post-download
"""

import os
import re
import struct

from .messages import render as render_message, paint


# ─── CONSTANTS ──────────────────────────────────────────────────

# Layer 1 — uploader trust tiers and seeder thresholds
TRUST_VIP = 'vip'
TRUST_TRUSTED = 'trusted'
TRUST_MEMBER = 'member'
# Minimum seeders for each trust tier — paranoid-strict defaults
MIN_SEEDERS = {
    TRUST_VIP: 1,         # VIP uploaders have track records
    TRUST_TRUSTED: 3,     # trusted need a few seeds to confirm
    TRUST_MEMBER: 15,     # unknown uploaders need social proof
}
MIN_SEEDERS_DEFAULT = 15  # anything unrecognized = untrusted

# Layer 2 — dangerous file extensions (lowercase, with dot)
BLOCKED_EXTENSIONS = frozenset([
    '.exe', '.dll', '.lnk', '.bat', '.cmd', '.vbs', '.vbe',
    '.js', '.jse', '.wsf', '.wsh', '.ps1', '.ps2', '.msc',
    '.msi', '.msp', '.scr', '.iso', '.img', '.inf', '.reg',
    '.hta', '.cpl', '.jar', '.com', '.pif', '.application',
    '.gadget', '.appref-ms', '.sct', '.ws', '.mst', '.chm',
])

# Layer 2 — safe media extensions we expect
SAFE_MEDIA_EXTENSIONS = frozenset([
    '.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm',
    '.m4v', '.ts', '.mpg', '.mpeg', '.3gp', '.ogv', '.m2ts',
    '.srt', '.sub', '.ass', '.ssa', '.vtt', '.idx',   # subtitles
    '.nfo', '.txt', '.jpg', '.jpeg', '.png',           # info/art
])

# Layer 3 — infohash regex (40-char hex SHA1 or 32-char base32)
_INFOHASH_HEX = re.compile(r'^[a-fA-F0-9]{40}$')
_INFOHASH_B32 = re.compile(r'^[A-Z2-7]{32}$')

# Layer 4 — shell metacharacters that MUST NOT appear in release NAMES
_SHELL_DANGER = re.compile(r'[;&|`$\r\n\x00-\x1f]')

# Stricter check for magnet URI — & is legitimate in URIs, only block
# truly dangerous chars that could escape subprocess even with shell=False
_MAGNET_DANGER = re.compile(r'[;|`$\r\n\x00-\x1f]')

# Layer 7 — magic byte signatures
_MAGIC_MKV = b'\x1a\x45\xdf\xa3'               # EBML header
_MAGIC_MP4 = b'ftyp'                            # ISO BMFF
_MAGIC_AVI_RIFF = b'RIFF'
_MAGIC_AVI_TAG = b'AVI '
_MAGIC_EXE_MZ = b'MZ'                           # Windows PE
_MAGIC_ELF = b'\x7fELF'                         # Linux ELF
_MAGIC_MACH_O = [b'\xfe\xed\xfa\xce',           # Mach-O 32
                 b'\xfe\xed\xfa\xcf',            # Mach-O 64
                 b'\xce\xfa\xed\xfe',            # Mach-O 32 reverse
                 b'\xcf\xfa\xed\xfe']            # Mach-O 64 reverse


# ─── LAYER 1: UPLOADER TRUST ───────────────────────────────────

def check_uploader_trust(result):
    """Check uploader reputation and enforce seeder floor.

    Args:
        result: dict with 'status' (vip/trusted/member), 'seeders' (str/int),
                'username' (str).

    Returns:
        (passed: bool, reason: str, trust_tier: str)
    """
    status = (result.get('status') or '').lower().strip()
    raw_seeders = result.get('seeders')
    seeders = int(raw_seeders) if raw_seeders is not None else 1
    uploader = result.get('username', 'anonymous')

    # Map API status to our tiers
    if status == 'vip':
        tier = TRUST_VIP
    elif status == 'trusted':
        tier = TRUST_TRUSTED
    else:
        tier = TRUST_MEMBER

    min_seeds = MIN_SEEDERS.get(tier, MIN_SEEDERS_DEFAULT)

    if seeders < min_seeds:
        return (False,
                f'too few seeders ({seeders}) for {tier} uploader "{uploader}" '
                f'(need {min_seeds}+)',
                tier)

    return (True, '', tier)


# ─── LAYER 2: EXTENSION SHIELD ─────────────────────────────────

def check_extensions(name):
    """Reject releases with dangerous extensions or double-extensions.

    Checks:
      - Final extension against blacklist
      - ALL intermediate segments for hidden executables (Movie.mp4.exe)
      - Names with no extension at all (suspicious for media)

    Args:
        name: torrent release name string.

    Returns:
        (passed: bool, reason: str)
    """
    if not name or not name.strip():
        return (False, 'empty release name')

    name_lower = name.lower().strip()
    parts = name_lower.rsplit('.', 1)

    # Check final extension
    if len(parts) > 1:
        final_ext = '.' + parts[1]
        if final_ext in BLOCKED_EXTENSIONS:
            return (False, f'blocked extension: {final_ext}')

    # Double-extension attack: check every segment pair
    # e.g. "Movie.mp4.exe" -> segments ["movie", "mp4", "exe"]
    segments = name_lower.split('.')
    if len(segments) >= 3:
        for i in range(1, len(segments)):
            seg_ext = '.' + segments[i]
            if seg_ext in BLOCKED_EXTENSIONS:
                return (False,
                        f'hidden executable in name: ...{segments[i-1]}.{segments[i]}')

    # Reject names that are ONLY an extension or suspiciously short
    base = segments[0] if segments else ''
    if len(base) < 2:
        return (False, 'suspiciously short release name')

    return (True, '')


# ─── LAYER 3: INFOHASH VALIDATION ──────────────────────────────

def check_infohash(info_hash):
    """Validate info_hash is a well-formed SHA1 hex or Base32 string.

    Args:
        info_hash: string from API.

    Returns:
        (passed: bool, reason: str)
    """
    if not info_hash or not isinstance(info_hash, str):
        return (False, 'missing or non-string info_hash')

    h = info_hash.strip()

    if _INFOHASH_HEX.match(h):
        return (True, '')
    if _INFOHASH_B32.match(h):
        return (True, '')

    return (False, f'malformed info_hash: {h[:20]}...')


# ─── LAYER 4: MAGNET & INJECTION GUARD ─────────────────────────

# Standard tracker list — pre-encoded, trusted.
#
# These only affect PEER DISCOVERY (who we announce to and get peer IPs from),
# never content integrity: the infohash in the magnet cryptographically pins
# every byte we accept, and finished files still pass validate_file(). So a
# fatter, fresher list is a pure download-speed win with no security cost.
# Curated from the high-uptime public set (ngosang/trackerslist "best"),
# dropping the dead popcorn-tracker/bittor.pw entries that were slowing the
# announce round-trip. More live trackers => more seeders found faster.
TRACKERS = [
    'udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce',
    'udp%3A%2F%2Fopen.stealth.si%3A80%2Fannounce',
    'udp%3A%2F%2Ftracker.torrent.eu.org%3A451%2Fannounce',
    'udp%3A%2F%2Fexodus.desync.com%3A6969%2Fannounce',
    'udp%3A%2F%2Ftracker.openbittorrent.com%3A6969%2Fannounce',
    'udp%3A%2F%2Fopentracker.i2p.rocks%3A6969%2Fannounce',
    'udp%3A%2F%2Ftracker.dler.org%3A6969%2Fannounce',
    'udp%3A%2F%2Fexplodie.org%3A6969%2Fannounce',
    'udp%3A%2F%2Ftracker1.bt.moack.co.kr%3A80%2Fannounce',
    'udp%3A%2F%2Fopen.demonii.com%3A1337%2Fannounce',
    'udp%3A%2F%2Ftracker.tiny-vps.com%3A6969%2Fannounce',
    'udp%3A%2F%2Fwww.torrent.eu.org%3A451%2Fannounce',
    'https%3A%2F%2Ftracker.tamersunion.org%3A443%2Fannounce',
]


def sanitize_magnet(info_hash, name):
    """Build a sanitized magnet URI from validated components.

    NEVER builds from a raw user/API magnet string — always constructs
    from scratch using our own tracker list.

    Args:
        info_hash: validated hex/base32 hash (must pass check_infohash first).
        name: release name for display (dn= parameter).

    Returns:
        (magnet_uri: str or None, reason: str)
    """
    # Re-validate (defense in depth)
    passed, reason = check_infohash(info_hash)
    if not passed:
        return (None, reason)

    # Sanitize name — strip shell metacharacters
    clean_name = _SHELL_DANGER.sub('', name).strip()
    if not clean_name:
        clean_name = info_hash  # fallback to hash as display name

    # URL-encode the name for the magnet URI
    import urllib.parse
    encoded_name = urllib.parse.quote(clean_name, safe='')

    tracker_str = '&'.join(f'tr={t}' for t in TRACKERS)
    magnet = f'magnet:?xt=urn:btih:{info_hash}&dn={encoded_name}&{tracker_str}'

    # Final paranoia — scan the assembled URI for injection chars
    # (& is legitimate in magnet URIs, so use the URI-safe regex)
    if _MAGNET_DANGER.search(magnet):
        return (None, 'assembled magnet contains dangerous characters')

    return (magnet, '')


def sanitize_subprocess_args(args):
    """Validate a subprocess arg list for safety.

    Ensures no argument contains shell metacharacters that could break
    out of a Popen(shell=False) call. This is belt-and-suspenders —
    shell=False already prevents injection, but we catch it early.

    Args:
        args: list of strings to pass to Popen.

    Returns:
        (clean_args: list, reason: str) — reason is empty if OK.
    """
    clean = []
    for arg in args:
        s = str(arg)
        # Null bytes are always dangerous
        if '\x00' in s:
            return ([], f'null byte in argument: {s[:30]}')
        clean.append(s)
    return (clean, '')


# ─── LAYER 5: PATH TRAVERSAL GUARD ─────────────────────────────

def check_path_safe(filepath, base_dir):
    """Ensure a file path resolves inside the allowed base directory.

    Args:
        filepath: the candidate path (may be relative or absolute).
        base_dir: the allowed download directory.

    Returns:
        (passed: bool, resolved_path: str, reason: str)
    """
    try:
        resolved = os.path.realpath(os.path.abspath(filepath))
        base = os.path.realpath(os.path.abspath(base_dir))

        # commonpath raises ValueError if paths are on different drives (Windows)
        common = os.path.commonpath([resolved, base])
        if common != base:
            return (False, resolved,
                    f'path escapes base dir: {resolved} is outside {base}')

        return (True, resolved, '')
    except (ValueError, OSError) as e:
        return (False, filepath, f'path validation error: {e}')


# ─── LAYER 7: MAGIC-BYTE HEADER INSPECTOR ──────────────────────

def check_file_header(filepath, delete_if_dangerous=True):
    """Inspect the first 512 bytes of a downloaded file.

    Checks:
      - Windows PE executable (MZ header)
      - Linux ELF executable
      - macOS Mach-O executable
      - Valid media containers (MKV EBML, MP4 ftyp, AVI RIFF)

    Args:
        filepath: path to the downloaded file.
        delete_if_dangerous: if True, deletes files with executable headers.

    Returns:
        (safe: bool, file_type: str, reason: str)
          file_type: 'mkv', 'mp4', 'avi', 'executable', 'unknown'
    """
    try:
        with open(filepath, 'rb') as f:
            header = f.read(512)
    except (OSError, IOError) as e:
        return (False, 'unreadable', f'cannot read file: {e}')

    if len(header) < 4:
        return (False, 'too_small', 'file too small to identify')

    # ── DANGER: executable headers ──
    if header[:2] == _MAGIC_EXE_MZ:
        if delete_if_dangerous:
            _safe_delete(filepath)
        return (False, 'executable',
                'BLOCKED: Windows executable (MZ header) disguised as media')

    if header[:4] == _MAGIC_ELF:
        if delete_if_dangerous:
            _safe_delete(filepath)
        return (False, 'executable',
                'BLOCKED: Linux ELF binary disguised as media')

    for sig in _MAGIC_MACH_O:
        if header[:4] == sig:
            if delete_if_dangerous:
                _safe_delete(filepath)
            return (False, 'executable',
                    'BLOCKED: macOS Mach-O binary disguised as media')

    # ── SAFE: known media containers ──
    if header[:4] == _MAGIC_MKV:
        return (True, 'mkv', '')

    if _MAGIC_MP4 in header[:32]:
        return (True, 'mp4', '')

    if header[:4] == _MAGIC_AVI_RIFF and _MAGIC_AVI_TAG in header[:12]:
        return (True, 'avi', '')

    # Unknown but not an executable — allow with a note
    return (True, 'unknown',
            'file header not recognized as standard media but not executable')


def _safe_delete(filepath):
    """Delete a dangerous file, ignoring errors."""
    try:
        os.remove(filepath)
    except OSError:
        pass


# Negative filter patterns: CAM/TS rips and password/compressed scams
_CAM_RE = re.compile(r'(?i)\b(?:CAM|HDTS|TELESYNC|TS[\-\.]?RIP|HDCAM)\b')
_SUSPICIOUS_CONTENT_RE = re.compile(r'(?i)\b(?:password|passcode|pass\s*in\s*txt)\b|\.(?:rar|zip|7z)\b')
# Dangerous executable extensions that should never appear in video torrents.
_EXECUTABLE_RE = re.compile(r'(?i)\.(?:exe|bat|cmd|scr|msi|ps1|vbs|js|com|pif)(?:\b|$)')
# Opt-in marker: only a standalone "cam" in the query allows CAM releases.
_CAM_INTENT_RE = re.compile(r'(?i)\bcam\b')

# x265/HEVC reaches the same quality at roughly half the bitrate of x264, so the
# thresholds below (which assume x264) reject perfectly good HEVC encodes -- a
# 1080p x265 movie at 750-800MB is exactly what QxR and Tigole ship. Scale the
# floor down for HEVC rather than lowering it for everything, which would let
# genuine fakes through.
_HEVC_RE = re.compile(r'(?i)(?:x265|h\.?265|HEVC)')
_HEVC_SIZE_FACTOR = 0.6

# Minimum plausible sizes (bytes)
_MIN_SIZE_MOVIE = {
    4: 3 * 1024**3,      # 4K movie / pack: 3GB+
    3: 900 * 1024**2,    # 1080p movie / pack: 900MB+
    2: 500 * 1024**2,    # 720p movie / pack: 500MB+
}
_MIN_SIZE_EPISODE = {
    4: 600 * 1024**2,    # 4K single episode: 600MB+
    3: 180 * 1024**2,    # 1080p single episode: 180MB+
    2: 90 * 1024**2,     # 720p single episode: 90MB+
}
# Maximum plausible sizes — catches absurdly oversized fakes.
_MAX_SIZE_MOVIE = {
    4: 90 * 1024**3,     # 4K movie: 90GB ceiling (covers large remuxes)
    3: 40 * 1024**3,     # 1080p movie: 40GB ceiling
    2: 25 * 1024**3,     # 720p movie: 25GB ceiling
}
_MAX_SIZE_EPISODE = {
    4: 50 * 1024**3,     # 4K episode: 50GB ceiling
    3: 25 * 1024**3,     # 1080p episode: 25GB ceiling
    2: 15 * 1024**3,     # 720p episode: 15GB ceiling
}
# Season / multi-season packs hold dozens of episodes, so a per-item ceiling
# would throw away exactly the releases people want most. A 9-season 1080p pack
# legitimately runs into the hundreds of GB. Rather than guess an episode count
# we cannot see at filter time, cap packs only where the size stops being
# physically plausible for any real release.
_MAX_SIZE_PACK = {
    4: 2000 * 1024**3,   # 4K pack: 2TB
    3: 1000 * 1024**3,   # 1080p pack: 1TB
    2: 600 * 1024**3,    # 720p pack: 600GB
}
# Pack detection by NAME, because _scope is not available here: filter_results
# runs from search_tpb BEFORE _enrich_result assigns _scope, so result['_scope']
# is absent on this path and cannot be relied on.
#
# Match pack STRUCTURE, not pack vocabulary. Bare words are title text as often
# as they are release metadata -- "Season of the Witch", "A Complete Unknown"
# and "Four Seasons" are all movies, and treating them as packs would hand them
# the 1TB ceiling and let real fakes through. So every alternative below needs
# a season NUMBER or an explicit range/complete-series phrase.
_PACK_RE = re.compile(
    r'(?i)(?:\bS\d{1,2}\s*-\s*S?\d{1,2}\b'          # S01-S05, S01-5
    # S01 with no episode after it. The \b is load-bearing: without it the
    # greedy \d{1,2} backtracks on "S01E03" and matches a bare "S0", leaving
    # "1E03" ahead so the lookahead never sees the E and every episode reads
    # as a pack.
    r'|\bS\d{1,2}\b(?![\s\.\-_]*E\d)'
    # Season 1, Seasons.1-10. The (?!\d) stops the year in "Four Seasons 2018"
    # from being read as season 20 -- that would give a movie the pack ceiling.
    r'|\bseasons?[\s\.\-_]*\d{1,2}(?!\d)'
    r'|\bcomplete[\s\.\-_]*(?:series|collection|seasons?)\b'      # Complete Series
    r'|\b(?:full|entire)[\s\.\-_]*series\b'
    r'|\bbox[\s\.\-_]?set\b)')

def check_negative_filters(name, user_query=''):
    """Filter out CAM/TS rips and passworded/archive scams.

    If user explicitly searched for "cam", CAM releases are allowed.
    """
    if not name:
        return (True, '')

    # Exclude CAM unless the query explicitly asks for cam. Word-boundary, not
    # substring: a plain `'cam' in query` silently disabled this whole filter
    # for any title containing those letters ("Camelot", "Cameron", "camp").
    if not _CAM_INTENT_RE.search(user_query or ''):
        if _CAM_RE.search(name):
            return (False, 'excluded low-quality CAM/TS release')

    if _SUSPICIOUS_CONTENT_RE.search(name):
        return (False, 'excluded passworded/archive media release')

    if _EXECUTABLE_RE.search(name):
        return (False, 'excluded dangerous executable extension')

    return (True, '')


def check_size_plausible(result):
    """Scope-aware size validation to catch impossible/fake sizes.

    Allows 180MB+ for 1080p single episodes while requiring 900MB+ for movies.
    """
    try:
        size = int(result.get('size', 0))
    except (ValueError, TypeError):
        return (True, '')

    if size <= 0:
        return (False, 'zero or negative file size')

    quality_tier = result.get('_quality_tier', 0)
    if not quality_tier:
        name = result.get('name', '')
        if re.search(r'(?i)(?:2160p|4K|UHD)', name):
            quality_tier = 4
        elif re.search(r'(?i)(?:1080p|FHD)', name):
            quality_tier = 3
        elif re.search(r'(?i)(?:720p|HD)', name):
            quality_tier = 2

    # Check if single episode vs movie/pack. Compare case-insensitively:
    # _enrich_result writes '_scope' as 'EPISODE', so testing for lowercase
    # 'episode' never matched and every single episode was judged against the
    # movie thresholds -- which threw out legitimate sub-900MB 1080p rips.
    name = result.get('name', '')
    scope = str(result.get('_scope') or '').upper()
    is_ep = scope == 'EPISODE' or bool(re.search(r'(?i)(?:S\d{1,2}E\d{1,3}|\d{1,2}x\d{2,3}|episode\s*\d+)', name))
    # A pack is the not-an-episode case: "S01E03" wins over "S01", so check
    # is_ep first or every episode also looks like a pack.
    is_pack = not is_ep and (scope == 'SEASON_PACK' or bool(_PACK_RE.search(name)))

    min_map = _MIN_SIZE_EPISODE if is_ep else _MIN_SIZE_MOVIE
    min_bytes = min_map.get(quality_tier, 0)

    if min_bytes > 0 and _HEVC_RE.search(name):
        min_bytes = int(min_bytes * _HEVC_SIZE_FACTOR)

    if min_bytes > 0 and size < min_bytes:
        tier_label = {4: '4K', 3: '1080p', 2: '720p'}.get(quality_tier, '')
        return (False, f'size too small for claimed quality {tier_label} ({size // (1024*1024)}MB < {min_bytes // (1024*1024)}MB)')

    # Upper bound — catches absurdly oversized fakes. Packs get their own, far
    # higher ceiling: judged as movies they were all rejected (a 45GB single
    # season read as a 45GB "movie" and blew the 40GB cap), which silently
    # deleted every season pack from results.
    max_map = (_MAX_SIZE_PACK if is_pack
               else _MAX_SIZE_EPISODE if is_ep else _MAX_SIZE_MOVIE)
    max_bytes = max_map.get(quality_tier, 0)
    if max_bytes > 0 and size > max_bytes:
        tier_label = {4: '4K', 3: '1080p', 2: '720p'}.get(quality_tier, '')
        return (False, f'size too large for claimed quality {tier_label} ({size // (1024**3)}GB > {max_bytes // (1024**3)}GB)')

    return (True, '')


# ─── FULL PIPELINE ──────────────────────────────────────────────

def filter_result(result, user_query=''):
    """Run pre-download security layers on a single API result dict.

    Args:
        result: dict with keys: name, info_hash, seeders, leechers,
                status, username, size.
        user_query: optional user search string for intent checks.

    Returns:
        (passed: bool, reasons: list[str], trust_tier: str)
          reasons contains all failure messages (may be multiple).
    """
    reasons = []
    trust_tier = TRUST_MEMBER

    # Layer 1 — uploader trust + seeder floor
    passed, reason, trust_tier = check_uploader_trust(result)
    if not passed:
        reasons.append(f'[Layer 1] {reason}')

    # Layer 2 — extension blacklist
    passed, reason = check_extensions(result.get('name', ''))
    if not passed:
        reasons.append(f'[Layer 2] {reason}')

    # Layer 3 — infohash validation
    passed, reason = check_infohash(result.get('info_hash', ''))
    if not passed:
        reasons.append(f'[Layer 3] {reason}')

    # Layer 4 — shell metacharacters in release name
    name = result.get('name', '')
    if _SHELL_DANGER.search(name):
        reasons.append(f'[Layer 4] shell metacharacters in release name')

    # Layer 5 — negative filters (CAM/password/archive)
    passed, reason = check_negative_filters(name, user_query=user_query)
    if not passed:
        reasons.append(f'[Layer 5] {reason}')

    # Layer 6 — plausible size guard
    passed, reason = check_size_plausible(result)
    if not passed:
        reasons.append(f'[Layer 6] {reason}')

    return (len(reasons) == 0, reasons, trust_tier)


def filter_results(results, user_query=''):
    """Filter a list of results through all pre-download layers.

    Args:
        results: list of dicts from search engines (apibay/yts/eztv).
        user_query: optional original search query string.

    Returns:
        (safe: list[dict], blocked_count: int, block_reasons: dict)
          Each safe result gets '_trust_tier' added.
          block_reasons maps release name -> list of failure reasons.
    """
    safe = []
    block_reasons = {}

    for r in results:
        passed, reasons, tier = filter_result(r, user_query=user_query)
        if passed:
            r['_trust_tier'] = tier
            safe.append(r)
        else:
            block_reasons[r.get('name', '?')] = reasons

    return (safe, len(block_reasons), block_reasons)


def validate_downloaded_file(filepath, base_dir):
    """Post-download validation: path traversal + magic-byte check.

    Call this after aria2c finishes to verify the file is safe.

    Args:
        filepath: path to the downloaded file.
        base_dir: allowed download directory.

    Returns:
        (safe: bool, file_type: str, reason: str)
    """
    # Layer 5 — path traversal
    passed, resolved, reason = check_path_safe(filepath, base_dir)
    if not passed:
        return (False, 'path_escape', reason)

    # Layer 7 — magic-byte header
    return check_file_header(resolved, delete_if_dangerous=True)
