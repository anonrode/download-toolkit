"""
extractors.py — Site-specific extractors and file host resolvers.

Every extractor follows the same pattern:
  extract_<site>(url, session, ctx)
  ctx: dict with keys — stop, paused, wait, bandwidth, quality, parallel, current_process

Returns nothing — drives download_file/download_batch directly.
"""

import os
import re
import time
import requests
from urllib.parse import urljoin
from bs4 import BeautifulSoup

from downloader import (
    DownloadSummary, download_file, download_batch, download_with_ytdlp,
    download_social_ytdlp, Prefetcher, safe_print, safe_filename,
    find_direct_video, base_domain, is_streaming_link,
    mark_series_complete, BASE_DIR, DIAG_LOG, UA_DESKTOP
)

# ─── SITE DOMAIN CONSTANTS ────────────────────────────────────
# Change here if a site moves — one place, everything updates.
NKIRI_DOMAIN      = 'nkiri.com'
THENKIRI_DOMAIN   = 'thenkiri.com'
DRAMAKEY_COM      = 'dramakey.com'
DRAMARAIN_DOMAIN  = 'dramarain.com'
DRAMAKEY_CC       = 'dramakey.cc'
JAROCKS_DOMAIN    = '9jarocks.net'
NAIJAPREY_DOMAIN  = 'naijaprey.tv'
MYASIANTV_DOMAIN  = 'myasiantv9.com'
NAIJAVAULT_DOMAIN = 'naijavault.com'
ANITAKU_DOMAIN    = 'anitaku.com.ro'
PLUTO_DOMAIN      = 'plutomovies.com'
PLUTO_BASE        = f'https://{PLUTO_DOMAIN}'
ANITAKU_BASE      = f'https://{ANITAKU_DOMAIN}'

EP_KEYWORDS = ['-e', 'episode', 's0', 's1', 's2', 's3', 's4', 's5', 's6', 's7', 's8', 's9']

SOCIAL_DOMAINS = [
    'facebook.com', 'fb.watch', 'instagram.com', 'twitter.com', 'x.com',
    'tiktok.com', 'youtube.com', 'youtu.be', 'vimeo.com', 'dailymotion.com',
    'twitch.tv', 'reddit.com', 'pinterest.com', 'snapchat.com'
]

# ─── HELPERS ──────────────────────────────────────────────────
def safe_get(session, url, timeout=20, referer=None, retries=3):
    for attempt in range(retries):
        try:
            if referer:
                session.headers['Referer'] = referer
            r = session.get(url, timeout=timeout)
            return r
        except Exception as e:
            safe_print(f"  [!] Attempt {attempt+1}/{retries} failed: {e}")
            if attempt < retries - 1:
                time.sleep(2)
    return None

def clean_name(slug):
    name = re.sub(r'[-_]+', ' ', slug)
    name = re.sub(r'\s+', ' ', name).strip()
    return name.title()

def clean_ep_name(raw):
    name = re.sub(r'\([\w\s]+p\)', '', raw)
    name = re.sub(r'\[[\w\s]+\]', '', name)
    name = re.sub(r'download', '', name, flags=re.IGNORECASE)
    name = re.sub(r'[-–|]+', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name or raw

def _ctx(ctx):
    """Unpack context dict with safe defaults."""
    return (
        ctx.get('stop',            [False]),
        ctx.get('wait',            lambda: None),
        ctx.get('bandwidth',       0),
        ctx.get('quality',         None),
        ctx.get('parallel',        1),
        ctx.get('current_process', [None]),
    )

def _stopped(ctx):
    return ctx.get('stop', [False])[0]

def _wait(ctx):
    fn = ctx.get('wait')
    if fn:
        fn()

def diagnose_page(soup, url, expected_pattern=None):
    """Write diagnostic info to log file when extraction fails."""
    lines = [
        f"\n[DIAG] {time.strftime('%Y-%m-%d %H:%M')}",
        f"[DIAG] URL: {url}",
        f"[DIAG] Expected: {expected_pattern or 'unknown'}",
    ]
    domain_links = {}
    for a in soup.find_all('a', href=True):
        href = a['href']
        if href.startswith('http'):
            dom = base_domain(href)
        elif href.startswith('/'):
            dom = '[relative]'
        else:
            continue
        domain_links.setdefault(dom, []).append(href)

    lines.append("[DIAG] Links by domain:")
    for dom, links in sorted(domain_links.items(), key=lambda x: -len(x[1])):
        lines.append(f"  {dom}: {len(links)} links")
        for lnk in links[:3]:
            lines.append(f"    • {lnk[:80]}")

    output = '\n'.join(lines)
    safe_print(f"\n[!] No matching content found — details written to {DIAG_LOG}")
    safe_print(f"[!] Expected: {expected_pattern or 'unknown'}")
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(DIAG_LOG, 'a', encoding='utf-8') as f:
            f.write(output + '\n')
    except Exception:
        pass

# ─── FILE HOST RESOLVERS ──────────────────────────────────────

def resolve_downloadwella(url, session):
    try:
        r = safe_get(session, url, timeout=20)
        if not r:
            return None
        soup = BeautifulSoup(r.text, 'html.parser')
        form = soup.find('form')
        if not form:
            return None
        data = {inp.get('name'): inp.get('value', '')
                for inp in form.find_all('input') if inp.get('name')}
        data['method_free'] = 'Free Download'
        r2 = session.post(url, data=data, timeout=20)
        return find_direct_video(r2.text)
    except Exception as e:
        safe_print(f"  [!] Downloadwella: {e}")
        return None

def resolve_loadedfiles(url, session):
    try:
        r1 = safe_get(session, url, referer=f'https://{JAROCKS_DOMAIN}/')
        if not r1:
            return None
        m1 = re.search(r"var downloadUrl = '(https://loadedfiles\.org/[^']+)'", r1.text)
        if not m1:
            return None
        r2 = safe_get(session, m1.group(1), referer='https://loadedfiles.org/')
        if not r2:
            return None
        m2 = re.search(r"var downloadUrl = '(https://loadedfiles\.org/[^']+)'", r2.text)
        if not m2:
            return None
        try:
            r3 = session.get(m2.group(1), timeout=20, allow_redirects=False)
            return r3.headers.get('location')
        except Exception as e:
            safe_print(f"  [!] Loadedfiles redirect: {e}")
            return None
    except Exception as e:
        safe_print(f"  [!] Loadedfiles: {e}")
        return None

def resolve_wildshare(url):
    try:
        from curl_cffi import requests as cf_requests
        s = cf_requests.Session(impersonate='chrome120')
    except ImportError:
        safe_print("  [!] Wildshare requires curl_cffi — pip install curl_cffi --break-system-packages")
        return None
    try:
        r = s.get(url, timeout=20)
        if not r or r.status_code != 200:
            return None
        pt = re.search(r'pt=([A-Za-z0-9%+=/]+)', r.text)
        if not pt:
            return None
        parts = url.rstrip('/').split('/')
        file_id = next((p for p in reversed(parts) if not p.endswith(('.mkv', '.mp4', '.m3u8'))), parts[-1])
        pt_url = f'https://wildshare.net/{file_id}?{pt.group(0)}'
        r2 = s.get(pt_url, timeout=20, allow_redirects=False)
        return r2.headers.get('location')
    except Exception as e:
        safe_print(f"  [!] Wildshare: {e}")
        return None

def resolve_streamtape(url, session):
    try:
        r = safe_get(session, url, referer='https://watchadsontape.com/')
        if not r or r.status_code == 404:
            return None
        for line in r.text.split('\n'):
            if "getElementById('robotlink')" in line and 'substring' in line:
                m = re.search(r"innerHTML\s*=\s*'([^']+)'\s*\+\s*\('([^']+)'\)", line.strip())
                if m:
                    base_s, raw = m.group(1), m.group(2)
                    for n in re.findall(r'\.substring\((\d+)\)', line):
                        raw = raw[int(n):]
                    get_url = 'https:' + base_s + raw
                    r2 = session.get(get_url, timeout=20, allow_redirects=False)
                    loc = r2.headers.get('location')
                    if loc:
                        return loc
        return find_direct_video(r.text)
    except Exception as e:
        safe_print(f"  [!] Streamtape: {e}")
        return None

def resolve_vidmoly(embed_url, session):
    try:
        r = safe_get(session, embed_url, referer=f'https://{MYASIANTV_DOMAIN}.ro/')
        if not r:
            return None
        m3u8 = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', r.text)
        if m3u8:
            return m3u8[0]
        mp4 = re.findall(r'https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*', r.text)
        if mp4:
            return mp4[0]
        return None
    except Exception as e:
        safe_print(f"  [!] Vidmoly: {e}")
        return None

_VIDBASIC_BLOCKED   = ['asianload', 'dood', 'streamvid']
_VIDBASIC_PREFERRED = ['watchadsontape.com', 'streamtape']

def resolve_vidbasic(embed_url, session):
    for attempt in range(2):
        try:
            r = safe_get(session, embed_url, referer=f'https://{MYASIANTV_DOMAIN}.ro/')
            if not r:
                continue
            raw_servers = re.findall(r'data-video="(https?://[^"]+)"', r.text)
            servers = [u for u in raw_servers if not any(h in u for h in _VIDBASIC_BLOCKED)]
            if not servers:
                safe_print(f"  [!] No usable servers (attempt {attempt+1})")
                time.sleep(3)
                continue
            ordered = sorted(servers, key=lambda u: 0 if any(h in u for h in _VIDBASIC_PREFERRED) else 1)
            for sv_url in ordered:
                safe_print(f"    [>] Trying: {sv_url[:60]}...")
                if 'watchadsontape.com' in sv_url or 'streamtape' in sv_url:
                    result = resolve_streamtape(sv_url, session)
                    if result:
                        return result
                else:
                    try:
                        r2 = safe_get(session, sv_url, referer=embed_url, timeout=15)
                        if r2:
                            v = find_direct_video(r2.text)
                            if v:
                                return v
                    except Exception as e:
                        safe_print(f"    [!] Server error: {e}")
                        continue
            v = find_direct_video(r.text)
            if v:
                return v
        except Exception as e:
            safe_print(f"  [!] Vidbasic attempt {attempt+1}: {e}")
            time.sleep(3)
    return None

def resolve_embed(src, session):
    if 'vidmoly' in src:
        return resolve_vidmoly(src, session)
    elif 'vidbasic' in src:
        return resolve_vidbasic(src, session)
    else:
        safe_print(f"    [>] Unknown embed, trying generic: {src[:60]}...")
        r = safe_get(session, src)
        return find_direct_video(r.text) if r else None

def resolve_drip_waffi(url, session):
    try:
        referer = f'https://{DRAMAKEY_CC}/' if DRAMAKEY_CC in url else f'https://{DRAMARAIN_DOMAIN}/'
        r = safe_get(session, url, referer=referer)
        if not r:
            return None
        m = re.search(r'window\.location\.href\s*=\s*"([^"]+)"', r.text)
        if m:
            return m.group(1)
        if 'drip.waffi.cloud' in url:
            return url
        m2 = re.search(r'https://drip[.]waffi[.]cloud/\S+', r.text)
        if m2:
            return m2.group(0)
        return None
    except Exception as e:
        safe_print(f"  [!] Drip: {e}")
        return None

def resolve_vikingfile(url, session):
    """
    Resolve a vikingfile.com URL to the actual CDN download URL.
    Handles both old format (redirect-based) and new /f/{id} format
    (landing page). Always uses plain requests for reliable redirect control.
    """
    try:
        s = requests.Session()
        s.headers.update({'User-Agent': UA_DESKTOP, 'Referer': f'https://www.{NAIJAVAULT_DOMAIN}/'})

        r1 = None
        for attempt in range(3):
            try:
                r1 = s.get(url, timeout=15, allow_redirects=False)
                break
            except Exception:
                if attempt < 2:
                    time.sleep(2)
                else:
                    raise
        if not r1:
            return None

        loc1 = r1.headers.get('location')

        if loc1:
            # Classic redirect path — follow hop 2
            r2 = None
            for attempt in range(3):
                try:
                    r2 = s.get(loc1, timeout=15, allow_redirects=False)
                    break
                except Exception:
                    if attempt < 2:
                        time.sleep(2)
                    else:
                        raise
            if not r2:
                return loc1
            loc2 = r2.headers.get('location')
            if loc2:
                return loc2
            if any(x in loc1 for x in ['.mp4', '.mkv', 'cdn', 'download']):
                return loc1
            cdn = find_direct_video(r2.text)
            return cdn if cdn else loc1

        # No redirect — new /f/{id} landing page
        if r1.status_code == 200:
            r1b = s.get(url, timeout=15, allow_redirects=True)
            final_url = r1b.url
            if final_url != url and any(x in final_url for x in ['.mp4', '.mkv', 'cdn', 'download']):
                return final_url
            cdn = find_direct_video(r1b.text)
            if cdn:
                return cdn
            for pattern in [
                r'https?://[^\s"\'<>]*cdn[^\s"\'<>]*\.(?:mp4|mkv)',
                r'https?://[^\s"\'<>]+\.(?:mp4|mkv)\b',
                r'"(https?://[^\s"\'<>]+(?:download|file)[^\s"\'<>]*)"',
            ]:
                m = re.search(pattern, r1b.text, re.IGNORECASE)
                if m:
                    return m.group(0).strip('"')

        safe_print(f"  [!] VikingFile: could not resolve {url[:60]}")
        return None
    except Exception as e:
        safe_print(f"  [!] VikingFile: {e}")
        return None

def resolve_plutomovies_dl(dl_url, session):
    try:
        session.headers.update({'Referer': PLUTO_BASE + '/'})
        r = safe_get(session, dl_url, timeout=15)
        if not r:
            return None
        m = re.search(
            r"getElementById\('downloadButton'\)\.onclick\s*=\s*function\(\)\s*\{"
            r"\s*location\.href\s*=\s*'(https://[^']+)'",
            r.text, re.DOTALL
        )
        if m:
            return m.group(1)
        safe_print(f"  [!] PlutoMovies DL pattern not found")
        return None
    except Exception as e:
        safe_print(f"  [!] PlutoMovies DL: {e}")
        return None

# ─── SHARED DOWNLOADWELLA EXTRACTOR ───────────────────────────
def _extract_downloadwella_site(url, session, ctx, site_label, name_cleaner):
    stop, wait, bw, quality, parallel, cur_proc = _ctx(ctx)

    safe_print(f"[*] {site_label} mode")
    slug   = url.rstrip('/').split('/')[-1]
    name   = clean_name(name_cleaner(slug))
    safe_print(f"[*] Series: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))

    r = safe_get(session, url)
    if not r:
        return
    soup = BeautifulSoup(r.text, 'html.parser')
    links = list(dict.fromkeys(
        a['href'] for a in soup.find_all('a', href=True)
        if 'downloadwella.com' in a['href']
    ))
    if not links:
        safe_print(f"[!] No downloadwella links found on page")
        diagnose_page(soup, url, "downloadwella.com links")
        return

    safe_print(f"[*] Found {len(links)} episode(s) — saving to: {folder}")
    summary = DownloadSummary()
    pf      = Prefetcher(resolve_downloadwella)
    next_direct = [None]

    for i, ep_url in enumerate(links, 1):
        if _stopped(ctx):
            break
        _wait(ctx)

        ep_name = ep_url.split('/')[-1].replace('.html', '')
        safe_print(f"\n[{i}/{len(links)}] {ep_name}")

        if next_direct[0] is not None:
            direct = next_direct[0]
            next_direct[0] = None
        else:
            direct = resolve_downloadwella(ep_url, session)

        if i < len(links):
            pf.prefetch(links[i], session)

        if direct:
            ext   = 'mkv' if '.mkv' in direct else 'mp4'
            fname = safe_filename(f"{ep_name}.{ext}")
            download_file(direct, folder, fname, summary,
                          series_url=url, series_name=name,
                          bandwidth_limit=bw, quality=quality,
                          current_process=cur_proc,
                          stop_flag=stop, wait_fn=ctx.get('wait'))
            if i < len(links):
                next_direct[0] = pf.get(timeout=60)
        else:
            safe_print(f"  [✗] Could not extract link")
            summary.add_failed(ep_name)
            if i < len(links):
                next_direct[0] = pf.get(timeout=60)

    if summary.failed == 0 and not _stopped(ctx):
        mark_series_complete(url)
    summary.report()

# ─── SITE EXTRACTORS ──────────────────────────────────────────

def extract_nkiri(url, session, ctx=None):
    ctx = ctx or {}
    _extract_downloadwella_site(
        url, session, ctx,
        site_label='NKIRI/Thenkiri',
        name_cleaner=lambda s: re.sub(r'-s\d+.*$', '', s, flags=re.IGNORECASE)
    )

def extract_dramakey_com(url, session, ctx=None):
    ctx = ctx or {}
    def cleaner(s):
        s = re.sub(r'-s\d+.*$', '', s, flags=re.IGNORECASE)
        s = re.sub(r'-(season|episode|complete).*$', '', s, flags=re.IGNORECASE)
        return s
    _extract_downloadwella_site(url, session, ctx, site_label='DramaKey.com', name_cleaner=cleaner)

def extract_9jarocks(url, session, ctx=None):
    ctx = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc = _ctx(ctx)

    safe_print("[*] 9jaRocks mode")
    slug   = url.rstrip('/').split('/')[-1]
    name   = clean_name(re.sub(r'-id\d+.*$', '', slug))
    safe_print(f"[*] Title: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))

    r = safe_get(session, url, referer=f'https://{JAROCKS_DOMAIN}/')
    if not r:
        return
    soup = BeautifulSoup(r.text, 'html.parser')
    lf_links = list(dict.fromkeys(
        a['href'] for a in soup.find_all('a', href=True)
        if 'loadedfiles.org' in a['href']
    ))
    safe_print(f"[*] Found {len(lf_links)} file(s) — saving to: {folder}")
    summary = DownloadSummary()

    for i, lf_url in enumerate(lf_links, 1):
        if _stopped(ctx):
            break
        _wait(ctx)
        fname = lf_url.split('/')[-1][:60]
        safe_print(f"\n[{i}/{len(lf_links)}] {fname}")
        direct = resolve_loadedfiles(lf_url, session)
        if direct:
            ext = 'mkv' if '.mkv' in direct else 'mp4'
            download_file(direct, folder, safe_filename(f"{fname}.{ext}"), summary,
                          bandwidth_limit=bw, current_process=cur_proc,
                          stop_flag=stop, wait_fn=ctx.get('wait'))
        else:
            safe_print(f"  [✗] Could not extract: {fname}")
            summary.add_failed(fname)
        time.sleep(0.5)
    summary.report()

def extract_naijaprey(url, session, ctx=None):
    ctx = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc = _ctx(ctx)

    safe_print("[*] NaijaPrey mode")
    slug   = url.rstrip('/').split('/')[-1]
    name   = clean_name(slug)
    safe_print(f"[*] Title: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))

    r = safe_get(session, url, referer=f'https://www.{NAIJAPREY_DOMAIN}/')
    if not r:
        return
    soup = BeautifulSoup(r.text, 'html.parser')
    ep_links = list(dict.fromkeys(
        a['href'] for a in soup.find_all('a', href=True)
        if 'vdl.np-downloader.com' in a['href']
    ))
    safe_print(f"[*] Found {len(ep_links)} episode(s) — saving to: {folder}")
    summary = DownloadSummary()

    for i, ep_url in enumerate(ep_links, 1):
        if _stopped(ctx):
            break
        _wait(ctx)
        ep_name = ep_url.rstrip('/').split('/')[-1]
        safe_print(f"\n[{i}/{len(ep_links)}] {ep_name}")
        try:
            r2 = safe_get(session, ep_url, referer=f'https://www.{NAIJAPREY_DOMAIN}/')
            if not r2:
                summary.add_failed(ep_name)
                continue
            soup2  = BeautifulSoup(r2.text, 'html.parser')
            ws_url = next((a['href'] for a in soup2.find_all('a', href=True)
                           if 'wildshare.net' in a['href']), None)
            if ws_url:
                direct = resolve_wildshare(ws_url)
                if direct:
                    ext = 'mkv' if '.mkv' in direct else 'mp4'
                    download_file(direct, folder, safe_filename(f"{ep_name}.{ext}"), summary,
                                  bandwidth_limit=bw, current_process=cur_proc,
                                  stop_flag=stop, wait_fn=ctx.get('wait'))
                else:
                    safe_print(f"  [✗] Wildshare failed")
                    summary.add_failed(ep_name)
            else:
                safe_print(f"  [!] No wildshare link found")
                summary.add_failed(ep_name)
        except Exception as e:
            safe_print(f"  [!] Error: {e}")
            summary.add_failed(ep_name)
        time.sleep(1)
    summary.report()

def extract_myasiantv(url, session, ctx=None):
    ctx = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc = _ctx(ctx)

    safe_print("[*] MyAsianTV mode")
    slug = url.rstrip('/').split('/')[-1]
    name = re.sub(r'-episode-\d+.*$', '', slug)
    name = re.sub(r'-\d{4}.*$', '', name)
    name = clean_name(name)
    safe_print(f"[*] Series: {name}")
    folder  = os.path.join(BASE_DIR, safe_filename(name))
    bd      = base_domain(url)
    summary = DownloadSummary()

    if 'episode-' in url:
        ep_links = [url]
        safe_print(f"[*] Saving to: {folder}")
    else:
        safe_print("[*] Fetching episode list...")
        r = safe_get(session, url, referer=bd + '/', timeout=30)
        if not r:
            return
        soup      = BeautifulSoup(r.text, 'html.parser')
        show_slug = re.sub(r'-\d{4}.*$', '', slug)
        ep_links  = list(dict.fromkeys(
            a['href'] for a in soup.find_all('a', href=True)
            if ('episode-' in a['href'] and bd in a['href'] and show_slug in a['href'])
        ))
        if not ep_links:
            safe_print("[!] No episode links found")
            return
        ep_links.sort(key=lambda u: int(m.group(1)) if (m := re.search(r'episode-(\d+)', u)) else 0)
        safe_print(f"[*] Found {len(ep_links)} episode(s) — saving to: {folder}")

    for i, ep_url in enumerate(ep_links, 1):
        if _stopped(ctx):
            break
        _wait(ctx)
        ep_name = ep_url.rstrip('/').split('/')[-1]
        safe_print(f"\n[{i}/{len(ep_links)}] {ep_name}")
        r = safe_get(session, ep_url, referer=bd + '/', timeout=30)
        if not r:
            safe_print(f"  [✗] Could not fetch episode page")
            summary.add_failed(ep_name)
            continue
        soup   = BeautifulSoup(r.text, 'html.parser')
        iframe = soup.find('iframe', src=re.compile(r'vidbasic|vidmoly')) or soup.find('iframe', src=True)
        if not iframe:
            safe_print(f"  [!] No iframe found")
            summary.add_failed(ep_name)
            continue
        src = iframe.get('src', '')
        if not src.startswith('http'):
            src = 'https:' + src
        direct = resolve_embed(src, session)
        if direct:
            download_file(direct, folder, safe_filename(f"{ep_name}.mp4"), summary,
                          bandwidth_limit=bw, quality=quality,
                          current_process=cur_proc, stop_flag=stop, wait_fn=ctx.get('wait'))
        else:
            safe_print(f"  [✗] Could not extract video")
            summary.add_failed(ep_name)
        time.sleep(1)
    summary.report()

def extract_dramarain(url, session, ctx=None):
    ctx  = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc = _ctx(ctx)
    site = 'DramaKey.cc' if DRAMAKEY_CC in url else 'DramaRain'
    safe_print(f"[*] {site} mode")

    slug   = url.rstrip('/').split('/')[-1]
    name   = re.sub(r'-(chinese|korean|thai|japanese|drama|tvshows|movies?).*$', '', slug, flags=re.IGNORECASE)
    name   = clean_name(name)
    safe_print(f"[*] Title: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))

    site_referer = f'https://{DRAMAKEY_CC}/' if DRAMAKEY_CC in url else f'https://{DRAMARAIN_DOMAIN}/'
    session.headers['Referer'] = site_referer
    r = safe_get(session, url, referer=site_referer)
    if not r:
        return
    soup    = BeautifulSoup(r.text, 'html.parser')
    summary = DownloadSummary()

    # Method 1: direct drip links
    drip_links = [(a.text.strip(), a['href']) for a in soup.find_all('a', href=True)
                  if 'drip.waffi.cloud' in a['href']]
    if drip_links:
        safe_print(f"[*] Found {len(drip_links)} direct link(s) — saving to: {folder}")
        for i, (label, link) in enumerate(drip_links, 1):
            if _stopped(ctx): break
            _wait(ctx)
            fname = safe_filename(f"{label or f'episode-{i}'}.mp4")
            safe_print(f"\n[{i}/{len(drip_links)}] {fname}")
            download_file(link, folder, fname, summary,
                          bandwidth_limit=bw, current_process=cur_proc,
                          stop_flag=stop, wait_fn=ctx.get('wait'))
        summary.report()
        return

    # Method 2: download page links
    dl_links = [(a.text.strip(), a['href']) for a in soup.find_all('a', href=True)
                if any(x in a['href'] for x in
                       [f'{DRAMARAIN_DOMAIN}/download', f'{DRAMAKEY_CC}/download', 'drip.waffi.cloud'])]
    if dl_links:
        safe_print(f"[*] Found {len(dl_links)} episode(s) — saving to: {folder}")
        for i, (label, dl_url) in enumerate(dl_links, 1):
            if _stopped(ctx): break
            _wait(ctx)
            fname = safe_filename(f"{label or f'episode-{i}'}.mp4")
            safe_print(f"\n[{i}/{len(dl_links)}] {fname}")
            if 'drip.waffi.cloud' in dl_url:
                direct = dl_url
            else:
                session.headers['Referer'] = site_referer
                direct = resolve_drip_waffi(dl_url, session)
            if direct:
                download_file(direct, folder, fname, summary,
                              bandwidth_limit=bw, current_process=cur_proc,
                              stop_flag=stop, wait_fn=ctx.get('wait'))
            else:
                safe_print(f"  [✗] Could not resolve link")
                summary.add_failed(fname)
            time.sleep(0.5)
        summary.report()
        return

    safe_print(f"[!] No download links found")
    diagnose_page(soup, url, "drip.waffi.cloud links")

def extract_naijavault(url, session, ctx=None):
    ctx  = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc = _ctx(ctx)

    safe_print("[*] NaijaVault mode")
    slug = url.rstrip('/').split('/')[-1]
    name = re.sub(r'-\d{4}.*$', '', slug)
    name = re.sub(r'-season-\d+.*$', '', name, flags=re.IGNORECASE)
    name = clean_name(name)
    safe_print(f"[*] Title: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))

    session.headers['Referer'] = f'https://www.{NAIJAVAULT_DOMAIN}/'
    r = safe_get(session, url, timeout=30)
    if not r:
        return
    soup    = BeautifulSoup(r.text, 'html.parser')
    summary = DownloadSummary()

    # ZIP check
    zip_links = [a['href'] for a in soup.find_all('a', href=True)
                 if a['href'].endswith('.zip') or 'zip' in a.get_text(strip=True).lower()]
    if zip_links:
        safe_print(f"[*] ZIP file found — downloading ZIP")
        zip_url  = zip_links[0]
        zip_name = zip_url.split('/')[-1] or f"{name}.zip"
        download_file(zip_url, folder, safe_filename(zip_name), summary,
                      bandwidth_limit=bw, current_process=cur_proc,
                      stop_flag=stop, wait_fn=ctx.get('wait'))
        summary.report()
        return

    # Find /dl- episode links
    seen_dl  = set()
    dl_links = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if f'/dl-' in href and NAIJAVAULT_DOMAIN in href and href not in seen_dl:
            seen_dl.add(href)
            dl_links.append((a.text.strip(), href))

    # Single /dl- page pasted directly — detect by page content
    if not dl_links:
        is_dl_page = (
            re.search(r'var downloadURL = "([^"]+)"', r.text) or
            re.search(r'https?://vikingfile\.com/[^\s"\'<>]+', r.text) or
            re.search(r'[?&]nj_download=', r.text)
        )
        if is_dl_page:
            page_title = soup.find('title')
            label      = page_title.get_text(strip=True) if page_title else slug
            dl_links   = [(label, url)]

    if not dl_links:
        safe_print("[!] No episode links found")
        diagnose_page(soup, url, "/dl- links or vikingfile.com anchor")
        return

    safe_print(f"[*] Found {len(dl_links)} episode(s) — saving to: {folder}")

    items = []
    for i, (label, dl_url) in enumerate(dl_links, 1):
        if _stopped(ctx):
            break
        ep_name = safe_filename(clean_ep_name(label) or f"episode-{i}")
        safe_print(f"\n[{i}/{len(dl_links)}] Extracting: {ep_name}")

        session.headers.update({'Referer': url})
        r2 = safe_get(session, dl_url, timeout=20)
        if not r2:
            safe_print(f"  [✗] Could not fetch download page")
            summary.add_failed(ep_name)
            continue

        # Pattern 1 (PRIMARY): vikingfile.com anchor — new page format
        vf_anchor = re.search(r'https?://vikingfile\.com/[^\s"\'<>]+', r2.text)
        if vf_anchor:
            vf_url = vf_anchor.group(0).rstrip('.,;)')
            safe_print(f"  [*] VikingFile anchor found")
            direct = resolve_vikingfile(vf_url, session)
            if direct:
                ext = 'mkv' if '.mkv' in direct else 'mp4'
                items.append((direct, safe_filename(f"{ep_name}.{ext}")))
            else:
                safe_print(f"  [✗] VikingFile resolution failed")
                summary.add_failed(ep_name)
            time.sleep(0.5)
            continue

        # Pattern 2 (FALLBACK): old var downloadURL JS format
        vf_match = re.search(r'var downloadURL = "([^"]+)"', r2.text)
        if vf_match:
            vf_url = vf_match.group(1)
            direct = resolve_vikingfile(vf_url, session) if 'vikingfile.com' in vf_url else vf_url
            if direct:
                ext = 'mkv' if '.mkv' in direct else 'mp4'
                items.append((direct, safe_filename(f"{ep_name}.{ext}")))
            else:
                safe_print(f"  [✗] VikingFile resolution failed")
                summary.add_failed(ep_name)
            time.sleep(0.5)
            continue

        # Pattern 3: cdn.filevault.com.ng
        fv = re.findall(r'https?://cdn\.filevault\.com\.ng/[^\s"\'<>]+', r2.text)
        if fv:
            ext = 'mkv' if '.mkv' in fv[0] else 'mp4'
            items.append((fv[0], safe_filename(f"{ep_name}.{ext}")))
            time.sleep(0.5)
            continue

        # Pattern 4: nj_download redirect
        nj_dl = re.search(
            r'https?://[^\s"\'<>]*naijavault\.com[^\s"\'<>]*[?&]nj_download=[^\s"\'<>]+',
            r2.text
        )
        if nj_dl:
            nj_url = nj_dl.group(0).rstrip('.,;)')
            safe_print(f"  [*] nj_download link found — following redirect")
            try:
                rr  = session.get(nj_url, timeout=15, allow_redirects=False)
                cdn = rr.headers.get('location')
                if cdn and cdn.startswith('http'):
                    ext = 'mkv' if '.mkv' in cdn else 'mp4'
                    items.append((cdn, safe_filename(f"{ep_name}.{ext}")))
                    time.sleep(0.5)
                    continue
            except Exception as e:
                safe_print(f"  [!] nj_download redirect failed: {e}")

        safe_print(f"  [✗] No download URL found on page")
        summary.add_failed(ep_name)
        time.sleep(0.5)

    safe_print(f"\n[*] Starting {len(items)} download(s)...")
    for dl_url, dl_fname in items:
        if _stopped(ctx):
            break
        _wait(ctx)
        download_file(dl_url, folder, dl_fname, summary,
                      series_url=url, series_name=name,
                      bandwidth_limit=bw, current_process=cur_proc,
                      stop_flag=stop, wait_fn=ctx.get('wait'))

    if summary.failed == 0 and not _stopped(ctx):
        mark_series_complete(url)
    summary.report()

def extract_anitaku(url, session, ctx=None):
    ctx  = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc = _ctx(ctx)

    safe_print("[*] Anitaku mode")
    slug       = url.rstrip('/').split('/')[-1]
    is_episode = 'episode-' in slug
    name       = re.sub(r'-episode-\d+.*$', '', slug) if is_episode else slug
    name       = clean_name(name)
    safe_print(f"[*] Title: {name}")
    folder  = os.path.join(BASE_DIR, safe_filename(name))
    summary = DownloadSummary()

    def download_episode(ep_url, ep_name):
        r = safe_get(session, ep_url, referer=ANITAKU_BASE + '/', timeout=30)
        if not r:
            safe_print(f"  [✗] Could not fetch: {ep_name}")
            summary.add_failed(ep_name)
            return
        tamil_match = re.search(r"""(https://tamilembed\.lol/embed/[^\s"'<>]+)""", r.text)
        if tamil_match:
            embed_url = tamil_match.group(1)
            safe_print(f"  [*] Found tamilembed stream")
            download_with_ytdlp(embed_url, folder, safe_filename(f"{ep_name}.mp4"), summary,
                                quality=quality, current_process=cur_proc)
            return
        soup2  = BeautifulSoup(r.text, 'html.parser')
        iframe = soup2.find('iframe', src=re.compile(r'tamilembed|embed'))
        if iframe:
            src = iframe.get('src', '')
            if not src.startswith('http'):
                src = 'https:' + src
            safe_print(f"  [*] Found embed via iframe")
            download_with_ytdlp(src, folder, safe_filename(f"{ep_name}.mp4"), summary,
                                quality=quality, current_process=cur_proc)
            return
        safe_print(f"  [*] Trying yt-dlp on episode page directly")
        result = download_with_ytdlp(ep_url, folder, safe_filename(f"{ep_name}.mp4"), summary,
                                     quality=quality, current_process=cur_proc)
        if not result:
            safe_print(f"  [✗] All methods failed: {ep_name}")
            diagnose_page(soup2, ep_url, "tamilembed.lol embed URL")

    if is_episode:
        safe_print(f"[*] Single episode — saving to: {folder}")
        download_episode(url, safe_filename(slug))
    else:
        safe_print("[*] Fetching episode list...")
        r = safe_get(session, url, referer=ANITAKU_BASE + '/', timeout=30)
        if not r:
            safe_print("[!] Could not fetch series page")
            return
        soup  = BeautifulSoup(r.text, 'html.parser')
        seen  = set()
        ep_links = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            text = a.get_text(strip=True)
            if 'episode-' in href and ANITAKU_BASE in href and href not in seen:
                ep_slug    = href.rstrip('/').split('/')[-1]
                anime_base = slug.rstrip('/')
                if ep_slug.startswith(anime_base) or anime_base in ep_slug:
                    seen.add(href)
                    ep_links.append((href, text or ep_slug))
        if not ep_links:
            for a in soup.find_all('a', href=True):
                href = a['href']
                if 'episode-' in href and href not in seen:
                    seen.add(href)
                    ep_links.append((href, a.get_text(strip=True) or href.split('/')[-1]))
        if not ep_links:
            safe_print("[!] No episode links found")
            return

        def ep_num(item):
            m = re.search(r'episode-(\d+)', item[0])
            return int(m.group(1)) if m else 0
        ep_links.sort(key=ep_num)
        safe_print(f"[*] Found {len(ep_links)} episode(s) — saving to: {folder}")

        for i, (ep_url, ep_text) in enumerate(ep_links, 1):
            if _stopped(ctx):
                break
            _wait(ctx)
            ep_name = safe_filename(ep_url.rstrip('/').split('/')[-1])
            safe_print(f"\n[{i}/{len(ep_links)}] {ep_name}")
            from downloader import already_downloaded
            done, _ = already_downloaded(folder, f"{ep_name}.mp4")
            if done:
                safe_print(f"  [✓] Already downloaded — skipping")
                summary.add_skipped()
                continue
            download_episode(ep_url, ep_name)
            time.sleep(1)

    summary.report()

def extract_plutomovies(url, session, ctx=None):
    ctx  = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc = _ctx(ctx)

    safe_print("[*] PlutoMovies mode")
    is_movie = '/movie/' in url
    slug     = url.rstrip('/').split('/')[-1]
    name     = re.sub(r'-\d{4}.*$', '', slug).replace('-', ' ').title()
    safe_print(f"[*] Title: {name}")
    folder   = os.path.join(BASE_DIR, safe_filename(name))
    summary  = DownloadSummary()

    session.headers.update({'Referer': PLUTO_BASE + '/'})
    r = safe_get(session, url, timeout=30)
    if not r:
        return
    soup    = BeautifulSoup(r.text, 'html.parser')
    dl_link = next((a['href'] for a in soup.find_all('a', href=True)
                    if f'dl.{PLUTO_DOMAIN}' in a['href']), None)

    if is_movie or dl_link:
        if dl_link:
            safe_print(f"[*] Direct link found — saving to: {folder}")
            direct = resolve_plutomovies_dl(dl_link, session)
            if direct:
                ext = 'mkv' if 'mkv' in direct.lower() else 'mp4'
                download_file(direct, folder, safe_filename(f"{name}.{ext}"), summary,
                              bandwidth_limit=bw, current_process=cur_proc,
                              stop_flag=stop, wait_fn=ctx.get('wait'))
            else:
                safe_print("[✗] Could not resolve download link")
                summary.add_failed(name)
        else:
            safe_print("[✗] No download link found on page")
            summary.add_failed(name)
        summary.report()
        return

    season_links = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if '/series/' in href and 'season' in href.lower() and '#' not in href:
            full = urljoin(PLUTO_BASE, href)
            if full != url and full not in season_links:
                season_links.append(full)
    if not season_links:
        season_links = [url]

    safe_print(f"[*] Found {len(season_links)} season(s)")

    for season_url in season_links:
        if _stopped(ctx):
            break
        season_name = season_url.rstrip('/').split('/')[-1]
        for a in soup.find_all('a', href=True):
            href = a['href'].split('#')[0]
            if urljoin(PLUTO_BASE, href) == season_url:
                txt = a.get_text(strip=True)
                if txt:
                    season_name = txt
                break
        safe_print("\n[*] Season: " + season_name)
        page      = 1
        seen_eps  = set()
        all_eps   = []

        while True:
            if _stopped(ctx):
                break
            page_url = season_url if page == 1 else f"{season_url}/page/{page}"
            r2 = safe_get(session, page_url, timeout=30)
            if not r2 or r2.status_code == 404:
                break
            soup2    = BeautifulSoup(r2.text, 'html.parser')
            ep_items = []
            for a in soup2.find_all('a', href=True):
                href     = a['href'].split('#')[0]
                full_url = urljoin(PLUTO_BASE, href)
                if '/series/' not in href or full_url == season_url or full_url in seen_eps:
                    continue
                if not any(x in href.lower() for x in EP_KEYWORDS):
                    continue
                ep_name = a.get_text(strip=True) or safe_filename(href.rstrip('/').split('/')[-1])
                ep_items.append((full_url, safe_filename(ep_name)))
            # Deduplicate
            seen_u = set()
            unique = []
            for eu, en in ep_items:
                if eu not in seen_u:
                    seen_u.add(eu)
                    unique.append((eu, en))
            if not unique:
                break
            for eu, _ in unique:
                seen_eps.add(eu)
            safe_print(f"  [*] Page {page}: {len(unique)} episode(s)")
            all_eps.extend(unique)
            page += 1
            time.sleep(0.5)

        if not all_eps:
            safe_print(f"  [!] No episodes found for this season")
            continue

        # Sort EP1 → last
        def ep_sort(item):
            ep_url, ep_name = item
            m = re.search(r'[Ee](?:pisode\s*)?(\d+)', ep_name)
            if m: return int(m.group(1))
            m = re.search(r'-e(\d+)', ep_url.lower())
            if m: return int(m.group(1))
            return 0
        all_eps.sort(key=ep_sort)
        safe_print(f"  [*] Total: {len(all_eps)} episode(s)")

        # Extract download links
        items = []
        for i, (ep_url, ep_name) in enumerate(all_eps, 1):
            if _stopped(ctx):
                break
            _wait(ctx)
            safe_print(f"\n  [{i}/{len(all_eps)}] Extracting: {ep_name}")
            r3 = safe_get(session, ep_url, timeout=30)
            if not r3:
                safe_print(f"  [✗] Could not fetch episode page")
                summary.add_failed(ep_name)
                continue
            soup3   = BeautifulSoup(r3.text, 'html.parser')
            dl_link = next((a['href'] for a in soup3.find_all('a', href=True)
                            if f'dl.{PLUTO_DOMAIN}' in a['href']), None)
            if not dl_link:
                safe_print(f"  [✗] No download link on episode page")
                summary.add_failed(ep_name)
                continue
            direct = resolve_plutomovies_dl(dl_link, session)
            if direct:
                ext = 'mkv' if 'mkv' in direct.lower() else 'mp4'
                items.append((direct, safe_filename(f"{ep_name}.{ext}")))
            else:
                safe_print(f"  [✗] Could not resolve download link")
                summary.add_failed(ep_name)
            time.sleep(0.5)

        if items:
            safe_print(f"\n  [*] Starting download of {len(items)} episode(s)...")
            download_batch(items, folder, summary, parallel=parallel,
                           series_url=url, series_name=name,
                           bandwidth_limit=bw, quality=quality,
                           current_process=cur_proc, stop_flag=stop,
                           wait_fn=ctx.get('wait'))

    summary.report()

def extract_social(url, session, ctx=None):
    ctx  = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc = _ctx(ctx)

    bd   = base_domain(url).replace('https://', '').replace('www.', '')
    safe_print(f"[*] Social/Generic mode: {bd}")
    name   = bd.split('.')[0].title()
    folder = os.path.join(BASE_DIR, 'Social', safe_filename(name))

    slug     = url.rstrip('/').split('/')[-1] or 'video'
    slug     = re.sub(r'[^\w-]', '_', slug)[:50]
    filename = safe_filename(f"{slug}.mp4")

    safe_print(f"[*] Downloading: {filename}")
    safe_print(f"[*] Saving to: {folder}")
    summary = DownloadSummary()
    download_social_ytdlp(url, folder, filename, summary, current_process=cur_proc)
    summary.report()

# ─── SITE MAP & DETECTION ─────────────────────────────────────
SITE_MAP = {
    THENKIRI_DOMAIN:   extract_nkiri,
    NKIRI_DOMAIN:      extract_nkiri,
    DRAMAKEY_COM:      extract_dramakey_com,
    DRAMAKEY_CC:       extract_dramarain,
    DRAMARAIN_DOMAIN:  extract_dramarain,
    JAROCKS_DOMAIN:    extract_9jarocks,
    NAIJAPREY_DOMAIN:  extract_naijaprey,
    MYASIANTV_DOMAIN:  extract_myasiantv,
    'myasiantv9.com.ro': extract_myasiantv,
    NAIJAVAULT_DOMAIN: extract_naijavault,
    ANITAKU_DOMAIN:    extract_anitaku,
    PLUTO_DOMAIN:      extract_plutomovies,
}

def detect_site(url):
    for domain, extractor in SITE_MAP.items():
        if domain in url:
            return extractor
    for domain in SOCIAL_DOMAINS:
        if domain in url:
            return extract_social
    return None

def process_link_queue(links, session, ctx=None):
    ctx = ctx or {}
    for i, url in enumerate(links, 1):
        if _stopped(ctx):
            safe_print("[*] Stopped by user")
            break
        _wait(ctx)
        if len(links) > 1:
            safe_print(f"\n{'─'*50}")
            safe_print(f"  Queue [{i}/{len(links)}]: {url[:60]}")
            safe_print(f"{'─'*50}")
        extractor = detect_site(url)
        if not extractor:
            safe_print(f"[!] Unsupported site: {url}")
            safe_print(f"[!] Supported: {', '.join(SITE_MAP.keys())}")
            continue
        try:
            extractor(url, session, ctx)
        except Exception as e:
            safe_print(f"\n[!] Unexpected error: {e}")
            safe_print("[!] Please check the URL and try again")
