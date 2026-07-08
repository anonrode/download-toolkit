from .base import *
import hashlib
import base64

# ─── ALLANIME CONSTANTS ──────────────────────────────────────
ALLANIME_API     = 'https://api.allanime.day/api'
ALLANIME_REFERER = 'https://youtu-chan.com'
ALLANIME_RAW_KEY = b'Xot36i3lK3:v1'
ALLANIME_KEY     = hashlib.sha256(ALLANIME_RAW_KEY).digest()

ALLANIME_HEADERS = {
    'Content-Type':  'application/json',
    'User-Agent':    UA_DESKTOP,
    'Referer':       ALLANIME_REFERER,
    'Origin':        ALLANIME_REFERER,
}

def _allanime_post(payload, timeout=20):
    """POST a GraphQL payload to AllAnime. Returns parsed JSON or None."""
    try:
        r = requests.post(ALLANIME_API, json=payload, headers=ALLANIME_HEADERS, timeout=timeout)
        if r.status_code != 200:
            safe_print(f'  [!] AllAnime API returned {r.status_code}')
            return None
        return r.json()
    except Exception as e:
        safe_print(f'  [!] AllAnime API error: {e}')
        return None

def _decrypt_allanime(tobeparsed):
    """Decrypt AllAnime's AES-256-CTR encrypted provider blob."""
    try:
        crypto = _ensure_package('cryptography')
        if not crypto:
            return ''
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        data    = base64.b64decode(tobeparsed + '=' * (-len(tobeparsed) % 4))
        iv_int  = int.from_bytes(data[1:13], 'big') << 32 | 2
        ct      = data[13:len(data) - 16]
        cipher  = Cipher(algorithms.AES(ALLANIME_KEY), modes.CTR(iv_int.to_bytes(16, 'big')))
        dec     = cipher.decryptor()
        return (dec.update(ct) + dec.finalize()).decode('utf-8')
    except Exception as e:
        safe_print(f'  [!] Decrypt failed: {e}')
        return ''

def _resolve_wixmp(path, quality):
    """Resolve wixmp CDN path to best matching mp4/m3u8 URL for given quality."""
    try:
        url = 'https://allanime.day' + path
        r   = requests.get(url, headers=ALLANIME_HEADERS, timeout=15)
        if not r.ok:
            return None
        data  = r.json()
        links = data.get('links', [])
        if not links:
            return None
        # Quality preference: exact match → next-best → first available
        q_num = int(re.sub(r'\D', '', quality) or '480')
        def q_score(link):
            res = link.get('resolutionStr', '')
            n   = int(re.sub(r'\D', '', res) or '0')
            return abs(n - q_num)
        links.sort(key=q_score)
        return links[0].get('link')
    except Exception as e:
        safe_print(f'  [!] wixmp resolve error: {e}')
        return None

def _resolve_mp4upload(embed_url):
    """Scrape an mp4upload embed page to get the direct mp4 URL."""
    try:
        headers = dict(ALLANIME_HEADERS)
        headers['Referer'] = 'https://www.mp4upload.com/'
        r = requests.get(embed_url, headers=headers, timeout=15)
        if not r.ok:
            return None
        m = re.search(r'src:\s*["\']([^"\']+\.mp4[^"\']*)["\']', r.text)
        return m.group(1) if m else None
    except Exception as e:
        safe_print(f'  [!] Mp4Upload resolve error: {e}')
        return None

def _get_provider_url(show_id, ep_str, mode='sub', quality='480p'):
    """
    Steps 3+4: get embed URLs for an episode → resolve to direct URL.
    Tries providers in order: wixmp → Mp4Upload → SharePoint → yt-dlp fallback.
    Returns (direct_url, needs_ytdlp) where needs_ytdlp signals a YouTube URL.
    """
    payload = {
        'variables': {
            'showId':          show_id,
            'translationType': mode,
            'episodeString':   ep_str,
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

    ep_data    = (data.get('data') or {}).get('episode') or {}
    source_urls = ep_data.get('sourceUrls', [])

    providers = {}
    for src in source_urls:
        name = src.get('sourceName', '')
        url  = src.get('sourceUrl', '')
        if not url:
            continue
        # Encrypted blob
        if src.get('type') == 'iframe' and 'tobeparsed' in url:
            decrypted = _decrypt_allanime(url.replace('tobeparsed://', ''))
            # parse "ProviderName :url" lines
            for line in decrypted.splitlines():
                if ' :' in line:
                    pname, purl = line.split(' :', 1)
                    providers[pname.strip()] = purl.strip()
        else:
            providers[name] = url

    # Provider priority: wixmp → Mp4Upload → SharePoint → YouTube
    if 'Default' in providers:
        path = providers['Default']
        if path.startswith('/'):
            result = _resolve_wixmp(path, quality)
            if result:
                return result, ('m3u8' in result)

    if 'Mp4' in providers:
        result = _resolve_mp4upload(providers['Mp4'])
        if result:
            return result, False

    # SharePoint — direct mp4
    for key in ('S-mp4', 'Sharepoint'):
        if key in providers:
            url = providers[key]
            if url.startswith('http'):
                return url, False

    # YouTube fallback
    if 'Yt-mp4' in providers:
        return providers['Yt-mp4'], True

    safe_print('  [!] No usable provider found for this episode')
    return None, False

def search_allanime(query, mode='sub'):
    """
    Step 1: Search AllAnime. Returns list of dicts:
    {id, name, sub_eps, dub_eps}
    """
    payload = {
        'variables': {
            'search': {
                'allowAdult':   False,
                'allowUnknown': False,
                'query':        query,
            },
            'limit':           40,
            'page':            1,
            'translationType': mode,
            'countryOrigin':   'ALL',
        },
        'query': (
            'query($search:SearchInput $limit:Int $page:Int'
            ' $translationType:VaildTranslationTypeEnumType'
            ' $countryOrigin:VaildCountryOriginEnumType){'
            'shows(search:$search limit:$limit page:$page'
            ' translationType:$translationType countryOrigin:$countryOrigin)'
            '{edges{_id name availableEpisodes __typename}}}'
        ),
    }
    data = _allanime_post(payload)
    if not data:
        return []
    edges = (data.get('data') or {}).get('shows', {}).get('edges', [])
    results = []
    for edge in edges:
        eps = edge.get('availableEpisodes', {})
        results.append({
            'id':      edge.get('_id', ''),
            'name':    edge.get('name', 'Unknown'),
            'sub_eps': eps.get('sub', 0),
            'dub_eps': eps.get('dub', 0),
        })
    return results

def _get_episode_list(show_id, mode='sub'):
    """Step 2: Get sorted list of episode strings for a show."""
    payload = {
        'variables': {'showId': show_id},
        'query': 'query($showId:String!){show(_id:$showId){_id availableEpisodesDetail}}',
    }
    data = _allanime_post(payload)
    if not data:
        return []
    detail = ((data.get('data') or {}).get('show') or {}).get('availableEpisodesDetail', {})
    eps    = detail.get('sub', detail.get('dub', []))
    # Sort numerically where possible, keep specials at end
    def ep_sort_key(e):
        try:
            return (0, float(e))
        except ValueError:
            return (1, e)
    return sorted(eps, key=ep_sort_key)

def extract_allanime(show_id, show_name, episodes, mode='sub', ctx=None):
    """
    Download selected episodes of an AllAnime show.
    episodes: list of episode strings from _get_episode_list()
    """
    ctx  = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    # Strip yt-dlp format string — we want plain quality label for wixmp
    q_label = '480p'
    for lbl in ('1080p', '720p', '480p', '360p'):
        if lbl in (quality or ''):
            q_label = lbl
            break

    safe_name = safe_filename(show_name)
    folder    = os.path.join(BASE_DIR, 'Anime', safe_name)
    os.makedirs(folder, exist_ok=True)
    safe_print(f'[*] Saving to: {folder}')

    total   = len(episodes)
    pad     = 3 if total >= 100 else 2
    summary = DownloadSummary()

    for i, ep_str in enumerate(episodes, 1):
        if _stopped(ctx):
            break
        _wait(ctx)

        try:
            ep_num  = int(float(ep_str))
            ep_name = f'Episode {str(ep_num).zfill(pad)}'
        except ValueError:
            ep_name = f'Episode {ep_str}'

        fname = f'{ep_name}.mp4'
        safe_print(f'\n[{i}/{total}] {ep_name}')

        done, _ = already_downloaded(folder, fname, series_url=f'allanime:{show_id}')
        if done:
            safe_print('  [✓] Already downloaded — skipping')
            summary.add_skipped()
            continue

        safe_print('  [*] Resolving provider...')
        direct, needs_ytdlp = _get_provider_url(show_id, ep_str, mode=mode, quality=q_label)

        if not direct:
            safe_print(f'  [✗] Could not resolve provider for {ep_name}')
            summary.add_failed(ep_name)
            continue

        if needs_ytdlp:
            safe_print(f'  [*] YouTube provider — using yt-dlp')
            download_with_ytdlp(
                direct, folder, safe_filename(fname), summary,
                quality=quality, current_process=cur_proc,
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
