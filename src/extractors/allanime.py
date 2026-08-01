from .base import *
from ..downloader import mark_series_waiting_for_network
import hashlib
import base64
import json
import time
import re
from urllib.parse import quote

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

# ─── ALLANIME CONSTANTS ──────────────────────────────────────────────
ALLANIME_PRIMARY_API = 'https://api.allanime.day/api'
ALLANIME_FALLBACK_API = 'https://api.mkissa.net/api'
ALLANIME_HASH = "f4662f4b7510b26795dd53ef824a0bf1740fbbc5d1273fab18222ac831bca8d0"

HEADERS_ALLANIME = {
    'User-Agent': UA_DESKTOP,
    'Referer': 'https://youtu-chan.com',
    'Origin': 'https://youtu-chan.com',
    'Content-Type': 'application/json',
}

HEADERS_MKISSA = {
    'User-Agent': UA_DESKTOP,
    'Referer': 'https://mkissa.to',
    'Origin': 'https://mkissa.to',
    'Content-Type': 'application/json',
}

def decode_provider_url(src):
    """Decode AllAnime provider URL string if hex encoded ('--')."""
    if not src or not isinstance(src, str):
        return src
    if src.startswith('--'):
        try:
            raw_bytes = bytes.fromhex(src[2:])
            return raw_bytes.decode('utf-8', errors='ignore')
        except Exception:
            return src
    return src

def generate_aareq(key_bytes, epoch):
    """Generate AES-256-GCM encrypted aaReq token."""
    if not key_bytes or not HAVE_CRYPTO:
        return None
    try:
        ts = int(time.time() * 1000)
        payload = json.dumps({
            "v": 1,
            "ts": ts,
            "epoch": epoch,
            "qh": ALLANIME_HASH
        }, separators=(',', ':'))
        
        iv_base = f"01{epoch}:{ALLANIME_HASH}:{ts}"
        iv_md5 = hashlib.md5(iv_base.encode()).hexdigest()
        iv_bytes = bytes.fromhex(iv_md5)[:12]
        
        cipher = Cipher(algorithms.AES(key_bytes), modes.GCM(iv_bytes), backend=default_backend())
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(payload.encode()) + encryptor.finalize()
        tag = encryptor.tag
        
        return (iv_bytes + ciphertext + tag).hex()
    except Exception:
        return None

def _allanime_post(payload, timeout=15):
    """POST GraphQL query to AllAnime API with failover across primary/fallback endpoints."""
    try:
        r = requests.post(ALLANIME_PRIMARY_API, json=payload, headers=HEADERS_ALLANIME, timeout=timeout)
        if r.status_code == 200 and 'errors' not in r.json():
            return r.json()
    except Exception:
        pass

    try:
        r = requests.post(ALLANIME_FALLBACK_API, json=payload, headers=HEADERS_MKISSA, timeout=timeout)
        if r.status_code == 200 and 'errors' not in r.json():
            return r.json()
    except Exception:
        pass

    return None

def search_allanime(query, mode='sub'):
    """Search AllAnime titles. Returns list of dicts: {id, name, sub_eps, dub_eps}."""
    payload = {
        'variables': {
            'search': {
                'allowAdult': False,
                'allowUnknown': False,
                'query': query,
            },
            'limit': 20,
            'page': 1,
            'translationType': mode,
            'countryOrigin': 'ALL',
        },
        'query': (
            'query($search:SearchInput $limit:Int $page:Int'
            ' $translationType:VaildTranslationTypeEnumType'
            ' $countryOrigin:VaildCountryOriginEnumType){'
            'shows(search:$search limit:$limit page:$page'
            ' translationType:$translationType countryOrigin:$countryOrigin)'
            '{edges{_id name englishName availableEpisodes __typename}}}'
        ),
    }
    data = _allanime_post(payload)
    if not data:
        return []

    edges = (((data.get('data') or {}).get('shows') or {}).get('edges') or [])
    results = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        eps = edge.get('availableEpisodes') or {}
        name = edge.get('name') or edge.get('englishName') or 'Unknown'
        results.append({
            'id': edge.get('_id', ''),
            'name': name,
            'sub_eps': eps.get('sub', 0),
            'dub_eps': eps.get('dub', 0),
        })
    return results

def _get_episode_list(show_id, mode='sub'):
    """Get sorted list of episode strings for a show."""
    payload = {
        'variables': {'showId': show_id},
        'query': 'query($showId:String!){show(_id:$showId){_id availableEpisodesDetail}}',
    }
    data = _allanime_post(payload)
    if not data:
        return []
    detail = (((data.get('data') or {}).get('show') or {}).get('availableEpisodesDetail') or {})
    eps = detail.get(mode) or detail.get('sub') or detail.get('dub') or []

    def ep_sort_key(e):
        try:
            return (0, float(e))
        except ValueError:
            return (1, e)

    return sorted(eps, key=ep_sort_key)

def _get_provider_url(show_id, ep_str, mode='sub', quality='360p'):
    """Fetch episode source URLs and resolve to direct stream link."""
    payload = {
        'variables': {
            'showId': show_id,
            'translationType': mode,
            'episodeString': ep_str,
        },
        'query': (
            'query($showId:String!$translationType:VaildTranslationTypeEnumType!'
            '$episodeString:String!){'
            'episode(showId:$showId translationType:$translationType episodeString:$episodeString)'
            '{episodeString sourceUrls}}'
        ),
    }
    data = _allanime_post(payload)
    if not data:
        return None, False

    ep_data = (data.get('data') or {}).get('episode') or {}
    source_urls = ep_data.get('sourceUrls') or []

    for src_obj in source_urls:
        if not isinstance(src_obj, dict):
            continue
        raw_url = src_obj.get('sourceUrl', '')
        decoded = decode_provider_url(raw_url)
        if decoded and ('m3u8' in decoded or 'mp4' in decoded or 'wixmp' in decoded or 'mp4upload' in decoded):
            needs_ytdlp = 'youtube' in decoded or 'youtu.be' in decoded or '.m3u8' in decoded
            return decoded, needs_ytdlp

    return None, False

def extract_allanime(show_id, show_name, episodes, mode='sub', ctx=None):
    """Download selected episodes of an AllAnime show."""
    ctx = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    q_label = '360p'
    for lbl in ('1080p', '720p', '480p', '360p'):
        if lbl in (quality or ''):
            q_label = lbl
            break

    safe_name = safe_filename(show_name)
    folder = os.path.join(BASE_DIR, 'Anime', safe_name)
    os.makedirs(folder, exist_ok=True)
    safe_print(f'[*] Saving to: {folder}')

    total = len(episodes)
    if total == 0:
        safe_print(render_message('no_episodes_to_download'))
        return
    pad = 3 if total >= 100 else 2
    summary = DownloadSummary()

    _notify_start(show_name, total)
    mark_series_waiting_for_network(f'allanime:{show_id}')

    for i, ep_str in enumerate(episodes, 1):
        if _stopped(ctx):
            break
        _wait(ctx)

        try:
            ep_num = int(float(ep_str))
            ep_name = f'Episode {str(ep_num).zfill(pad)}'
        except ValueError:
            ep_name = f'Episode {ep_str}'

        fname = f'{ep_name}.mp4'
        safe_print(f'\n[{i}/{total}] {ep_name}')

        done, _ = already_downloaded(folder, fname, series_url=f'allanime:{show_id}')
        if done:
            safe_print(render_message('already_saved'))
            summary.add_skipped()
            continue

        safe_print(render_message('resolving_provider'))
        direct, needs_ytdlp = _get_provider_url(show_id, ep_str, mode=mode, quality=q_label)

        if not direct:
            safe_print(f'  [X] Could not resolve provider for {ep_name}')
            summary.add_failed(ep_name)
            continue

        if needs_ytdlp:
            safe_print(f'  [*] Stream provider link - using yt-dlp')
            download_with_ytdlp(
                direct, folder, safe_filename(fname), summary,
                quality=quality, current_process=cur_proc,
                stop_flag=stop, pause_flag=pause,
            )
        else:
            download_file(
                direct, folder, safe_filename(fname), summary,
                series_url=f'allanime:{show_id}',
                series_name=show_name,
                bandwidth_limit=bw,
                quality=quality,
                current_process=cur_proc,
                stop_flag=stop,
                pause_flag=pause,
                wait_fn=ctx.get('wait'),
            )
        time.sleep(1)

    if summary.failed == 0 and not _stopped(ctx):
        mark_series_complete(f'allanime:{show_id}')
    summary.report(show_name)
