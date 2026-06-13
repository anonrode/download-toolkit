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
    mark_series_complete, already_downloaded, BASE_DIR, DIAG_LOG, UA_DESKTOP
)
from ui import LiveProgress, downloading, error, warn

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
    'twitch.tv', 'reddit.com', 'pinterest.com', 'pin.it', 'snapchat.com'
]

# ─── HELPERS ──────────────────────────────────────────────────
def safe_get(session, url, timeout=20, referer=None, retries=3):
    for attempt in range(retries):
        try:
            if referer:
                session.headers['Referer'] = referer
            r = session.get(url, timeout=timeout)
            if not r.ok:
                safe_print(f"  [!] HTTP {r.status_code}: {url[:60]}")
                return None
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
                else:
                    safe_print(f"  [!] Streamtape JS pattern not matched — site may have changed")
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

def resolve_lulacloud(url, session):
    """
    Resolve a lulacloud.com/d/ URL to the actual CDN download URL.
    Follows redirect chain — 1 or 2 hops.
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

        loc = r1.headers.get('location')
        if loc:
            if 'lulacloud' in loc:
                # Second hop
                r2 = s.get(loc, timeout=15, allow_redirects=False)
                loc2 = r2.headers.get('location')
                return loc2 if loc2 else loc
            return loc

        if r1.status_code == 200:
            ct = r1.headers.get('content-type', '')
            if ct.startswith('video/'):
                return url
            soup = BeautifulSoup(r1.text, 'html.parser')
            for a in soup.find_all('a', href=True):
                if any(ext in a['href'] for ext in ['.mkv', '.mp4', '.m3u8']):
                    return a['href']
            m = re.search(r'(?:window\.location|location\.href)\s*=\s*["\']([^"\']+)["\']', r1.text)
            if m:
                return m.group(1)
            cdn = find_direct_video(r1.text)
            if cdn:
                return cdn

        safe_print(f"  [!] LulaCloud: could not resolve {url[:60]}")
        return None
    except Exception as e:
        safe_print(f"  [!] LulaCloud: {e}")
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
    ctx  = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc = _ctx(ctx)

    safe_print("[*] NKiri/TheNkiri mode")
    slug = url.rstrip('/').split('/')[-1]
    name = re.sub(r'-(korean|complete|drama|series|nollywood|hollywood|tv|movie).*$', '', slug, flags=re.IGNORECASE)
    name = clean_name(name)
    safe_print(f"[*] Title: {name}")
    folder  = os.path.join(BASE_DIR, safe_filename(name))
    summary = DownloadSummary()

    session.headers['Referer'] = 'https://thenkiri.com/'
    r = safe_get(session, url, timeout=20)
    if not r:
        safe_print("[!] Could not fetch page")
        return
    soup = BeautifulSoup(r.text, 'html.parser')

    # Priority 1: downloadwella links (most common across full catalogue)
    dw_links = list(dict.fromkeys(
        a['href'] for a in soup.find_all('a', href=True)
        if 'downloadwella.com' in a['href']
    ))
    if dw_links:
        safe_print(f"[*] Found {len(dw_links)} downloadwella link(s) — saving to: {folder}")
        pf = Prefetcher(resolve_downloadwella)
        next_direct = [None]
        for i, ep_url in enumerate(dw_links, 1):
            if _stopped(ctx): break
            _wait(ctx)
            ep_name = ep_url.split('/')[-1].replace('.html', '')
            safe_print(f"\n[{i}/{len(dw_links)}] {ep_name}")
            direct = next_direct[0] if next_direct[0] else resolve_downloadwella(ep_url, session)
            next_direct[0] = None
            if i < len(dw_links):
                pf.prefetch(dw_links[i], session)
            if direct:
                ext = 'mkv' if '.mkv' in direct else 'mp4'
                download_file(direct, folder, safe_filename(f"{ep_name}.{ext}"), summary,
                              series_url=url, series_name=name,
                              bandwidth_limit=bw, current_process=cur_proc,
                              stop_flag=stop, wait_fn=ctx.get('wait'))
                if i < len(dw_links):
                    next_direct[0] = pf.get(timeout=60)
            else:
                safe_print(f"  [x] Could not extract link")
                summary.add_failed(ep_name)
                if i < len(dw_links):
                    next_direct[0] = pf.get(timeout=60)
        if summary.failed == 0 and not _stopped(ctx):
            mark_series_complete(url)
        summary.report()
        return

    # Priority 2: direct CDN links (newer posts)
    cdn_links = list(dict.fromkeys(
        a['href'] for a in soup.find_all('a', href=True)
        if 'nkiserv.com' in a['href'] and a['href'].endswith('.mkv')
    ))
    if cdn_links:
        safe_print(f"[*] Found {len(cdn_links)} CDN link(s) — saving to: {folder}")
        for i, cdn_url in enumerate(cdn_links, 1):
            if _stopped(ctx): break
            _wait(ctx)
            fname = cdn_url.split('/')[-1]
            fname = re.sub(r'\.\([^)]+\)\.[a-z0-9]+\.mkv$', '.mkv', fname, flags=re.IGNORECASE)
            fname = safe_filename(fname)
            safe_print(f"\n[{i}/{len(cdn_links)}] {fname}")
            download_file(cdn_url, folder, fname, summary,
                          series_url=url, series_name=name,
                          bandwidth_limit=bw, current_process=cur_proc,
                          stop_flag=stop, wait_fn=ctx.get('wait'))
            time.sleep(0.5)
        if summary.failed == 0 and not _stopped(ctx):
            mark_series_complete(url)
        summary.report()
        return

    safe_print("[!] No download links found")
    diagnose_page(soup, url, "downloadwella.com or nkiserv.com links")

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
            if ('episode-' in a['href'] and show_slug in a['href']
                and (bd in a['href'] or a['href'].startswith('/')))
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

    # ── Scan series page for both link formats ─────────────────
    # Format A: /dl-{hash}/ intermediate pages
    seen   = set()
    format_a = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if '/dl-' in href and NAIJAVAULT_DOMAIN in href and href not in seen:
            seen.add(href)
            format_a.append((a.get_text(strip=True), href))

    # Format B: lulacloud.com/d/ direct links
    format_b = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if 'lulacloud.com/d/' in href and href not in seen:
            seen.add(href)
            format_b.append((a.get_text(strip=True), href))

    # Single dl- page pasted directly
    if not format_a and not format_b:
        is_dl = (
            re.search('var downloadURL', r.text) or
            re.search('vikingfile.com', r.text) or
            re.search('lulacloud.com/d/', r.text) or
            re.search('nj_download=', r.text)
        )
        if is_dl:
            page_title = soup.find('title')
            label      = page_title.get_text(strip=True) if page_title else slug
            format_a   = [(label, url)]

    if not format_a and not format_b:
        safe_print("[!] No episode links found")
        diagnose_page(soup, url, "/dl- or lulacloud.com/d/ links")
        return

    total = len(format_a) + len(format_b)
    safe_print(f"[*] Found {total} episode(s) — Format A: {len(format_a)}, Format B: {len(format_b)}")
    safe_print(f"[*] Saving to: {folder}")

    items   = []
    zip_hit = False

    # ── Process Format A (/dl- pages) ─────────────────────────
    for i, (label, dl_url) in enumerate(format_a, 1):
        if _stopped(ctx) or zip_hit:
            break
        ep_label = clean_ep_name(label) or f"episode-{i}"
        safe_print(f"\n[A {i}/{len(format_a)}] Extracting: {ep_label}")

        session.headers['Referer'] = url
        r2 = safe_get(session, dl_url, timeout=20)
        if not r2:
            safe_print(f"  [x] Could not fetch dl page")
            summary.add_failed(ep_label)
            continue

        # Read var fileTitle for filename — most reliable
        ft_m    = re.search(r'var fileTitle\s*=\s*"([^"]+)"', r2.text)
        ep_name = safe_filename(ft_m.group(1)) if ft_m else safe_filename(f"{ep_label}.mkv")

        # ZIP detection — download ZIP and stop
        if ep_name.lower().endswith('.zip'):
            safe_print(f"  [*] ZIP found — downloading season archive")
            du_m = re.search(r'var downloadURL\s*=\s*"([^"]+)"', r2.text)
            if du_m:
                zip_url = du_m.group(1)
                if 'vikingfile.com' in zip_url:
                    zip_url = resolve_vikingfile(zip_url, session) or zip_url
                elif 'lulacloud.com' in zip_url:
                    zip_url = resolve_lulacloud(zip_url, session) or zip_url
                if zip_url:
                    items = [(zip_url, ep_name)]
                    zip_hit = True
                    break
            continue

        # Primary: var downloadURL (JS)
        du_m = re.search(r'var downloadURL\s*=\s*"([^"]+)"', r2.text)
        if du_m:
            cdn_url = du_m.group(1)
            direct  = None
            if 'vikingfile.com' in cdn_url:
                direct = resolve_vikingfile(cdn_url, session)
                if not direct:
                    # Fallback: check page for lulacloud
                    lc = re.search(r'https?://(?:www\.)?lulacloud\.com/d/\S+', r2.text)
                    if lc:
                        direct = resolve_lulacloud(lc.group(0).rstrip('.,;)"\''), session)
            elif 'lulacloud.com' in cdn_url:
                direct = resolve_lulacloud(cdn_url, session)
                if not direct:
                    # Fallback: check page for vikingfile
                    vf = re.search(r'https?://(?:www\.)?vikingfile\.com/\S+', r2.text)
                    if vf:
                        direct = resolve_vikingfile(vf.group(0).rstrip('.,;)"\''), session)
            elif 'cdn.filevault.com.ng' in cdn_url:
                direct = cdn_url
            else:
                direct = cdn_url
            if direct:
                ext = 'mkv' if '.mkv' in (direct + ep_name).lower() else 'mp4'
                fname = ep_name if '.' in ep_name else f"{ep_name}.{ext}"
                items.append((direct, safe_filename(fname)))
            else:
                safe_print(f"  [x] All resolvers failed")
                summary.add_failed(ep_label)
            time.sleep(0.5)
            continue

        # Fallback: vikingfile anchor directly in page
        vf = re.search(r'https?://(?:www\.)?vikingfile\.com/\S+', r2.text)
        if vf:
            direct = resolve_vikingfile(vf.group(0).rstrip('.,;)"\''), session)
            if direct:
                ext = 'mkv' if '.mkv' in (direct + ep_name).lower() else 'mp4'
                fname = ep_name if '.' in ep_name else f"{ep_name}.{ext}"
                items.append((direct, safe_filename(fname)))
                time.sleep(0.5)
                continue
            # Try lulacloud on same page
            lc = re.search(r'https?://(?:www\.)?lulacloud\.com/d/\S+', r2.text)
            if lc:
                direct = resolve_lulacloud(lc.group(0).rstrip('.,;)"\''), session)
                if direct:
                    ext = 'mkv' if '.mkv' in (direct + ep_name).lower() else 'mp4'
                    fname = ep_name if '.' in ep_name else f"{ep_name}.{ext}"
                    items.append((direct, safe_filename(fname)))
                    time.sleep(0.5)
                    continue

        # nj_download redirect
        nj_match = 'naijavault.com' in r2.text and 'nj_download=' in r2.text
        if nj_match:
            try:
                rr  = session.get(re.search(r"https?://[^ 	]+nj_download=[^ 	<>]+", r2.text).group(0).rstrip('.,;)'), timeout=15, allow_redirects=False)
                cdn = rr.headers.get('location')
                if cdn and cdn.startswith('http'):
                    ext = 'mkv' if '.mkv' in cdn else 'mp4'
                    fname = ep_name if '.' in ep_name else f"{ep_name}.{ext}"
                    items.append((cdn, safe_filename(fname)))
                    time.sleep(0.5)
                    continue
            except Exception as e:
                safe_print(f"  [!] nj_download failed: {e}")

        safe_print(f"  [x] No download URL found")
        summary.add_failed(ep_label)
        time.sleep(0.5)

    # ── Process Format B (lulacloud direct) ───────────────────
    if not zip_hit:
        for i, (label, lc_url) in enumerate(format_b, 1):
            if _stopped(ctx):
                break
            ep_label = clean_ep_name(label) or f"episode-{i}"
            safe_print(f"\n[B {i}/{len(format_b)}] Extracting: {ep_label}")

            # Parse filename from URL slug
            slug_part = lc_url.rstrip('/').split('/')[-1]
            # Strip leading hash token (alphanumeric, 8+ chars before first hyphen)
            fname_slug = re.sub(r'^[a-zA-Z0-9]{8,}-', '', slug_part)
            # Fix extension suffix: -mkv → .mkv
            fname_slug = re.sub(r'-mkv$', '.mkv', fname_slug)
            fname_slug = re.sub(r'-mp4$', '.mp4', fname_slug)
            ep_name    = safe_filename(fname_slug or f"{ep_label}.mkv")

            # Primary: lulacloud resolver
            direct = resolve_lulacloud(lc_url, session)
            if not direct:
                # Fallback: fetch page, look for vikingfile or var downloadURL
                r2 = safe_get(session, lc_url, timeout=20)
                if r2:
                    du_m = re.search(r'var downloadURL\s*=\s*"([^"]+)"', r2.text)
                    if du_m:
                        cdn = du_m.group(1)
                        if 'vikingfile.com' in cdn:
                            direct = resolve_vikingfile(cdn, session)
                        elif 'lulacloud.com' in cdn:
                            direct = resolve_lulacloud(cdn, session)
                        else:
                            direct = cdn   # bare CDN URL — use directly
                    if not direct:
                        vf = re.search(r'https?://(?:www\.)?vikingfile\.com/\S+', r2.text)
                        if vf:
                            direct = resolve_vikingfile(vf.group(0).rstrip('.,;)"\''), session)
                    if not direct:
                        fv = re.search(r'https?://cdn\.filevault\.com\.ng/[^\s"\'<>]+', r2.text)
                        if fv:
                            direct = fv.group(0)

            if direct:
                ext   = 'mkv' if '.mkv' in (direct + ep_name).lower() else 'mp4'
                fname = ep_name if '.' in ep_name else f"{ep_name}.{ext}"
                items.append((direct, safe_filename(fname)))
            else:
                safe_print(f"  [x] All resolvers failed")
                summary.add_failed(ep_label)
            time.sleep(0.5)

    # ── Download all resolved items ────────────────────────────
    safe_print(f"\n[*] Downloading {len(items)} file(s)...")
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

def _yt_quality_prompt(default_quality):
    """Ask user to pick a quality. Returns a yt-dlp format string."""
    QUALITY_MAP = {
        '1': ('360p',  'bestvideo[height<=360]+bestaudio/best[height<=360]'),
        '2': ('480p',  'bestvideo[height<=480]+bestaudio/best[height<=480]'),
        '3': ('720p',  'bestvideo[height<=720]+bestaudio/best[height<=720]'),
        '4': ('1080p', 'bestvideo[height<=1080]+bestaudio/best[height<=1080]'),
    }
    # Work out which number matches the current default
    label_to_num = {'360p': '1', '480p': '2', '720p': '3', '1080p': '4'}
    default_label = '480p'
    for label in label_to_num:
        if label in default_quality:
            default_label = label
            break
    default_num = label_to_num.get(default_label, '2')

    safe_print(f"\n  Quality: [1] 360p  [2] 480p  [3] 720p  [4] 1080p  (default: {default_label})")
    try:
        choice = input("  Pick [1-4] or Enter for default: ").strip()
    except EOFError:
        choice = ''
    if not choice:
        choice = default_num
    _, fmt = QUALITY_MAP.get(choice, QUALITY_MAP[default_num])
    return fmt


def _yt_get_playlist_count(url):
    """Return number of items in a YouTube playlist, or None on failure."""
    import shutil
    if not shutil.which('yt-dlp'):
        return None
    try:
        result = subprocess.run(
            ['yt-dlp', '--flat-playlist', '--print', 'id',
             '--no-warnings', '--quiet', url],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL
        )
        ids = [l for l in result.stdout.strip().splitlines() if l.strip()]
        return len(ids) if ids else None
    except Exception:
        return None


def _yt_playlist_items_prompt(count):
    """
    Ask what to download from a playlist.
    Returns a --playlist-items string, or None to cancel.
    'all' means download everything.
    """
    count_str = str(count) if count else '?'
    safe_print(f"\n  Playlist detected — {count_str} videos")
    safe_print(f"  [1] Download all")
    safe_print(f"  [2] Range      (e.g. 5-10)")
    safe_print(f"  [3] Specific   (e.g. 1,3,7)")
    safe_print(f"  [0] Cancel")
    try:
        choice = input("\n  Pick: ").strip()
    except EOFError:
        return None
    if choice == '0' or not choice:
        return None
    if choice == '1':
        return 'all'
    if choice == '2':
        try:
            r = input("  Range (e.g. 5-10): ").strip()
            parts = r.split('-')
            int(parts[0]); int(parts[1])  # validate
            return r
        except Exception:
            safe_print("  [!] Invalid range")
            return None
    if choice == '3':
        try:
            items = input("  Items (e.g. 1,3,7): ").strip()
            [int(x) for x in items.split(',')]  # validate
            return items
        except Exception:
            safe_print("  [!] Invalid selection")
            return None
    return None


def extract_social(url, session, ctx=None):
    ctx  = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc = _ctx(ctx)

    bd     = base_domain(url).replace('https://', '').replace('www.', '')
    is_yt  = 'youtube.com' in url or 'youtu.be' in url
    is_pin = 'pinterest.com' in url or 'pin.it' in url

    # ── Pinterest ──────────────────────────────────────────────
    if is_pin:
        safe_print(f"[*] Pinterest")
        # Boards: pinterest.com/user/boardname/  — multiple pins
        # Single pin: pinterest.com/pin/12345/
        is_board = bool(re.search(r'pinterest\.com/[^/]+/[^/]+/?$', url)) and '/pin/' not in url
        folder = os.path.join(BASE_DIR, 'Pinterest')
        summary = DownloadSummary()
        if is_board:
            board_slug = safe_filename(url.rstrip('/').split('/')[-1] or 'board')
            folder = os.path.join(folder, board_slug)
            safe_print(f"[*] Board: {board_slug}")
            safe_print(f"[*] Saving to: {folder}")
            fmt = 'bestvideo[height<=720]+bestaudio/best[height<=720]'
            os.makedirs(folder, exist_ok=True)
            out_template = os.path.join(folder, '%(playlist_index)s - %(title)s.%(ext)s')
            cmd = [
                'yt-dlp', '-f', fmt,
                '--merge-output-format', 'mp4',
                '-o', out_template,
                '--yes-playlist',
                '--retries', '3', '--fragment-retries', '3',
                '--quiet', '--no-warnings', '--progress', '--newline',
                url
            ]
        else:
            pin_id  = re.search(r'/pin/(\d+)', url)
            slug    = pin_id.group(1) if pin_id else 'pin'
            filename = safe_filename(f"{slug}.mp4")
            safe_print(f"[*] Pin: {slug}")
            safe_print(f"[*] Saving to: {folder}")
            fmt = 'bestvideo[height<=720]+bestaudio/best[height<=720]'
            os.makedirs(folder, exist_ok=True)
            out_template = os.path.join(folder, safe_filename(slug) + '.%(ext)s')
            cmd = [
                'yt-dlp', '-f', fmt,
                '--merge-output-format', 'mp4',
                '-o', out_template,
                '--no-playlist',
                '--retries', '3', '--fragment-retries', '3',
                '--quiet', '--no-warnings', '--progress', '--newline',
                url
            ]
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1
            )
            cur_proc[0] = proc
            progress = LiveProgress('pinterest')
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                if '[download]' in line:
                    pct_m = re.search(r'(\d+\.?\d*)%', line)
                    spd_m = re.search(r'at\s+([0-9.]+)([KMG])iB/s', line)
                    eta_m = re.search(r'ETA\s+(\d+:\d+)', line)
                    if pct_m:
                        pct = float(pct_m.group(1))
                        spd = None
                        if spd_m:
                            spd = float(spd_m.group(1))
                            if spd_m.group(2) == 'K': spd /= 1024
                            elif spd_m.group(2) == 'G': spd *= 1024
                        eta = eta_m.group(1) if eta_m else None
                        progress.update(pct, spd, eta)
            proc.wait()
            if proc.returncode == 0:
                progress.done()
                summary.add_success()
            else:
                progress.fail()
                summary.add_failed('pinterest')
        except Exception as e:
            error(f'pinterest error: {e}')
            summary.add_failed('pinterest')
        summary.report()
        return

    # ── YouTube ────────────────────────────────────────────────
    if is_yt:
        has_list    = 'list=' in url
        has_watch   = 'watch?v=' in url or 'youtu.be/' in url

        # Single video that is part of a playlist
        if has_watch and has_list:
            safe_print(f"\n  This video is part of a playlist")
            safe_print(f"  [1] This video only")
            safe_print(f"  [2] Full playlist")
            safe_print(f"  [0] Cancel")
            try:
                choice = input("\n  Pick: ").strip()
            except EOFError:
                choice = '1'
            if choice == '0':
                return
            if choice == '2':
                # strip to just the list URL
                list_id = re.search(r'list=([^&]+)', url)
                if list_id:
                    url = f'https://www.youtube.com/playlist?list={list_id.group(1)}'
                has_watch = False  # fall through to playlist flow below
            else:
                has_list = False   # fall through to single video flow below

        # Pure playlist
        if has_list and not has_watch:
            count       = _yt_get_playlist_count(url)
            items_sel   = _yt_playlist_items_prompt(count)
            if items_sel is None:
                return
            fmt         = _yt_quality_prompt(quality)
            list_id     = re.search(r'list=([^&]+)', url)
            folder_name = safe_filename(list_id.group(1) if list_id else 'playlist')
            folder      = os.path.join(BASE_DIR, 'YouTube', folder_name)
            os.makedirs(folder, exist_ok=True)
            safe_print(f"\n[*] Saving to: {folder}")
            out_template = os.path.join(folder, '%(playlist_index)s - %(title)s.%(ext)s')
            cmd = [
                'yt-dlp', '-f', fmt,
                '--merge-output-format', 'mp4',
                '-o', out_template,
                '--yes-playlist',
                '--retries', '3', '--fragment-retries', '3',
                '--quiet', '--no-warnings', '--progress', '--newline',
            ]
            if items_sel != 'all':
                cmd += ['--playlist-items', items_sel]
            cmd.append(url)
            summary = DownloadSummary()
            try:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1
                )
                cur_proc[0] = proc
                current_title = 'playlist'
                progress = LiveProgress(current_title)
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    # yt-dlp announces each video: [download] Downloading item N of M
                    title_m = re.search(r'Downloading item (\d+) of (\d+)', line)
                    if title_m:
                        n, total = title_m.group(1), title_m.group(2)
                        current_title = f'video {n}/{total}'
                        progress.fail()
                        progress = LiveProgress(current_title)
                        downloading(current_title)
                        continue
                    if '[download]' in line:
                        pct_m = re.search(r'(\d+\.?\d*)%', line)
                        spd_m = re.search(r'at\s+([0-9.]+)([KMG])iB/s', line)
                        eta_m = re.search(r'ETA\s+(\d+:\d+)', line)
                        if pct_m:
                            pct = float(pct_m.group(1))
                            spd = None
                            if spd_m:
                                spd = float(spd_m.group(1))
                                if spd_m.group(2) == 'K': spd /= 1024
                                elif spd_m.group(2) == 'G': spd *= 1024
                            eta = eta_m.group(1) if eta_m else None
                            progress.update(pct, spd, eta)
                            if pct >= 100:
                                progress.done()
                                progress = LiveProgress(current_title)
                proc.wait()
                if proc.returncode == 0:
                    progress.done()
                    summary.add_success()
                else:
                    progress.fail()
                    summary.add_failed('playlist')
            except Exception as e:
                error(f'playlist error: {e}')
                summary.add_failed('playlist')
            summary.report()
            return

        # Single YouTube video
        fmt      = _yt_quality_prompt(quality)
        folder   = os.path.join(BASE_DIR, 'YouTube')
        os.makedirs(folder, exist_ok=True)
        out_template = os.path.join(folder, '%(title)s.%(ext)s')
        # Fetch actual title for display
        filename = 'video.mp4'
        try:
            import subprocess as _sp
            r = _sp.run(['yt-dlp', '--get-title', '--no-warnings', url],
                        capture_output=True, text=True, timeout=10,
                        stdin=_sp.DEVNULL)
            t = r.stdout.strip()
            if t:
                filename = safe_filename(t) + '.mp4'
        except Exception:
            pass
        safe_print(f"\n[*] Saving to: {folder}")
        summary = DownloadSummary()
        download_social_ytdlp(url, folder, filename, summary,
                              current_process=cur_proc,
                              quality_override=fmt,
                              out_template=out_template)
        summary.report()
        return

    # ── Everything else (Instagram, TikTok, Facebook, etc.) ───
    safe_print(f"[*] Social/Generic mode: {bd}")
    name     = bd.split('.')[0].title()
    folder   = os.path.join(BASE_DIR, 'Social', safe_filename(name))
    slug     = url.rstrip('/').split('/')[-1] or 'video'
    slug     = re.sub(r'[^\w-]', '_', slug)[:50]
    filename = safe_filename(f"{slug}.mp4")
    safe_print(f"[*] Downloading: {filename}")
    safe_print(f"[*] Saving to: {folder}")
    summary  = DownloadSummary()
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
            from ui import after_unknown_url
            after_unknown_url(url)
            continue
        try:
            extractor(url, session, ctx)
        except Exception as e:
            safe_print(f"\n[!] Unexpected error: {e}")
            safe_print("[!] Please check the URL and try again")
