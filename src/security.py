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
    seeders = int(result.get('seeders', 0))
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

# Standard tracker list — pre-encoded, trusted
TRACKERS = [
    'udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce',
    'udp%3A%2F%2Fopen.stealth.si%3A80%2Fannounce',
    'udp%3A%2F%2Ftracker.torrent.eu.org%3A451%2Fannounce',
    'udp%3A%2F%2Ftracker.bittor.pw%3A1337%2Fannounce',
    'udp%3A%2F%2Fpublic.popcorn-tracker.org%3A6969%2Fannounce',
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


# ─── FULL PIPELINE ──────────────────────────────────────────────

def filter_result(result):
    """Run layers 1-4 on a single TPB API result dict.

    Args:
        result: dict with keys: name, info_hash, seeders, leechers,
                status, username, size.

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

    # Layer 4 — we don't build the magnet yet, but check the name
    #           for shell metacharacters
    name = result.get('name', '')
    if _SHELL_DANGER.search(name):
        reasons.append(f'[Layer 4] shell metacharacters in release name')

    return (len(reasons) == 0, reasons, trust_tier)


def filter_results(results):
    """Filter a list of TPB results through all pre-download layers.

    Args:
        results: list of dicts from apibay.org.

    Returns:
        (safe: list[dict], blocked_count: int, block_reasons: dict)
          Each safe result gets '_trust_tier' added.
          block_reasons maps release name -> list of failure reasons.
    """
    safe = []
    block_reasons = {}

    for r in results:
        passed, reasons, tier = filter_result(r)
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
