"""
sites/resolvers.py — File host resolvers.

Each function takes a URL + session and returns a direct CDN URL or None.
These are shared across multiple site extractors.
"""

import re
import time
import requests

from config import UA_DESKTOP
from config import safe_get, make_plain_session
from core import find_direct_video, base_domain
from config import log


def resolve_downloadwella(url: str, session) -> str | None:
    try:
        r = safe_get(session, url, timeout=20)
        if not r:
            return None
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, 'html.parser')
        form = soup.find('form')
        if not form:
            return None
        data = {
            inp.get('name'): inp.get('value', '')
            for inp in form.find_all('input') if inp.get('name')
        }
        data['method_free'] = 'Free Download'
        r2 = session.post(url, data=data, timeout=20)
        return find_direct_video(r2.text)
    except Exception as e:
        log.warning('Downloadwella: %s', e)
        return None


def resolve_loadedfiles(url: str, session) -> str | None:
    try:
        r1 = safe_get(session, url, referer='https://9jarocks.net/')
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
        r3 = session.get(m2.group(1), timeout=20, allow_redirects=False)
        return r3.headers.get('location')
    except Exception as e:
        log.warning('Loadedfiles: %s', e)
        return None


def resolve_wildshare(url: str) -> str | None:
    try:
        from curl_cffi import requests as cf
        s = cf.Session(impersonate='chrome120')
    except ImportError:
        print('  [!] Wildshare requires curl_cffi — pip install curl_cffi --break-system-packages')
        return None
    try:
        r = s.get(url, timeout=20)
        if not r or r.status_code != 200:
            return None
        pt = re.search(r'pt=([A-Za-z0-9%+=/]+)', r.text)
        if not pt:
            return None
        parts   = url.rstrip('/').split('/')
        file_id = next(
            (p for p in reversed(parts) if not p.endswith(('.mkv', '.mp4', '.m3u8'))),
            parts[-1]
        )
        pt_url = f'https://wildshare.net/{file_id}?{pt.group(0)}'
        r2 = s.get(pt_url, timeout=20, allow_redirects=False)
        return r2.headers.get('location')
    except Exception as e:
        log.warning('Wildshare: %s', e)
        return None


def resolve_streamtape(url: str, session) -> str | None:
    try:
        r = safe_get(session, url, referer='https://watchadsontape.com/')
        if not r or r.status_code == 404:
            return None
        for line in r.text.split('\n'):
            if "getElementById('robotlink')" in line and 'substring' in line:
                m = re.search(r"innerHTML\s*=\s*'([^']+)'\s*\+\s*\('([^']+)'\)", line.strip())
                if m:
                    base_str, raw = m.group(1), m.group(2)
                    for n in re.findall(r'\.substring\((\d+)\)', line):
                        raw = raw[int(n):]
                    get_url = 'https:' + base_str + raw
                    r2 = session.get(get_url, timeout=20, allow_redirects=False)
                    loc = r2.headers.get('location')
                    if loc:
                        return loc
        return find_direct_video(r.text)
    except Exception as e:
        log.warning('Streamtape: %s', e)
        return None


def resolve_vidmoly(embed_url: str, session) -> str | None:
    try:
        r = safe_get(session, embed_url, referer='https://myasiantv9.com.ro/')
        if not r:
            return None
        m3u8 = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', r.text)
        if m3u8:
            return m3u8[0]
        mp4 = re.findall(r'https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*', r.text)
        return mp4[0] if mp4 else None
    except Exception as e:
        log.warning('Vidmoly: %s', e)
        return None


_VIDBASIC_BLOCKED   = ['asianload', 'dood', 'streamvid']
_VIDBASIC_PREFERRED = ['watchadsontape.com', 'streamtape']


def resolve_vidbasic(embed_url: str, session) -> str | None:
    for attempt in range(2):
        try:
            r = safe_get(session, embed_url, referer='https://myasiantv9.com.ro/')
            if not r:
                continue
            raw_servers = re.findall(r'data-video="(https?://[^"]+)"', r.text)
            servers     = [u for u in raw_servers if not any(h in u for h in _VIDBASIC_BLOCKED)]
            if not servers:
                log.debug('Vidbasic: no usable servers (attempt %d)', attempt + 1)
                time.sleep(3)
                continue
            ordered = sorted(servers, key=lambda u: 0 if any(h in u for h in _VIDBASIC_PREFERRED) else 1)
            for sv_url in ordered:
                print(f'    [>] Trying: {sv_url[:60]}...')
                if 'watchadsontape.com' in sv_url or 'streamtape' in sv_url:
                    result = resolve_streamtape(sv_url, session)
                    if result:
                        return result
                else:
                    r2 = safe_get(session, sv_url, referer=embed_url, timeout=15)
                    if r2:
                        v = find_direct_video(r2.text)
                        if v:
                            return v
            return find_direct_video(r.text)
        except Exception as e:
            log.warning('Vidbasic attempt %d: %s', attempt + 1, e)
            time.sleep(3)
    return None


def resolve_embed(src: str, session) -> str | None:
    if 'vidmoly' in src:
        return resolve_vidmoly(src, session)
    if 'vidbasic' in src:
        return resolve_vidbasic(src, session)
    r = safe_get(session, src)
    return find_direct_video(r.text) if r else None


def resolve_drip_waffi(url: str, session) -> str | None:
    try:
        referer = 'https://dramakey.cc/' if 'dramakey.cc' in url else 'https://dramarain.com/'
        r = safe_get(session, url, referer=referer)
        if not r:
            return None
        m = re.search(r'window\.location\.href\s*=\s*"([^"]+)"', r.text)
        if m:
            return m.group(1)
        if 'drip.waffi.cloud' in url:
            return url
        m2 = re.search(r'https://drip[.]waffi[.]cloud/\S+', r.text)
        return m2.group(0) if m2 else None
    except Exception as e:
        log.warning('Drip: %s', e)
        return None


def resolve_vikingfile(url: str, session=None) -> str | None:
    """
    Resolve vikingfile.com URL to actual CDN download URL.
    Simple 2-hop redirect chain — confirmed working June 2026.
    Plain requests works fine, no curl_cffi needed.

    Hop 1: vikingfile.com/f/{id} → 302 → vikingfile.com/d/{id2}/{filename}.mkv
    Hop 2: vikingfile.com/d/{id2}/{filename}.mkv → 302 → lp.vikingfile.com/download/...
    """
    try:
        s = session if session else make_plain_session()
        s.headers['Referer'] = 'https://www.naijavault.com/'
        r1 = s.get(url, timeout=15, allow_redirects=False)
        loc1 = r1.headers.get('location')
        if not loc1:
            log.warning('VikingFile: no redirect on hop 1 for %s', url[:60])
            return None
        r2 = s.get(loc1, timeout=15, allow_redirects=False)
        loc2 = r2.headers.get('location')
        return loc2 if loc2 else loc1
    except Exception as e:
        log.warning('VikingFile: %s', e)
        return None


def resolve_plutomovies_dl(dl_url: str, session) -> str | None:
    try:
        from config import PLUTO_BASE
        session.headers['Referer'] = PLUTO_BASE + '/'
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
        log.debug('PlutoMovies DL pattern not found — page size %d bytes', len(r.text))
        return None
    except Exception as e:
        log.warning('PlutoMovies DL: %s', e)
        return None
