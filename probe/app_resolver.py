"""
APP-MODE resolver: a faithful Python mirror of the FIXED Kotlin resolver logic
(Resolvers.kt after the parity fixes), used to verify the app's algorithms
against live sites. Reference mode (monolith) stays in probe_harness.py.

Mirrors exactly:
- Registry recursion (result != input -> re-resolve, unless same resolver
  re-claims a clean media path)                      [Resolvers.kt resolve()]
- Loadedfiles: effective-host probe (301 shells), verbatim downloadUrl
  (host-anchored regex), pt#2+ Location unconditional,
  Content-Type video/* = file                        [LoadedfilesResolver]
- Vidbasic: decrypt both attr orders + .mkv, mirror-selector delegation
  to other resolvers, vidbasic-only iframe recursion [VidbasicResolver]
- Vidsrc: playlist returned untouched when it already has token=
  (non-destructive)                                  [VidsrcResolver]
- Wildshare: ?pt= fetched no-redirect, Location IS the answer
- Waffi: pure ?preview strip (identity ok)
- DramaGateway: location.href extraction
- PlutoMovies: dl.plutomovies.com / movie|series page link extraction
- NaijaPrey provider chain: vdl/sdm_downloads page -> a.sdm_download
  anchor -> wildshare                                [NaijaPreyProvider]
Unclaimed URLs return None like the app.
"""
import re, time
from urllib.parse import urljoin, urlparse

try:
    from src._aes import aes_cbc_decrypt
except Exception:
    aes_cbc_decrypt = None

MEDIA_EXTS = (".mkv", ".mp4", ".webm", ".avi", ".m3u8", ".ts", "-mp4", "-mkv")
VB_KEY = b"94588293375053432799222445521289"
VB_IV = b"5259228356829423"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

_last_working_host = None  # loadedfiles sticky host (effective, post-redirect)


def _path_is_media(u):
    p = u.split("?", 1)[0].split("#", 1)[0].lower()
    return any(p.endswith(e) for e in MEDIA_EXTS)


def _get(session, url, referer=None, no_redirect=False, max_bytes=300 * 1024):
    """GET with caps; returns (status, headers-dict, body-bytes-or-None, final_url)."""
    headers = {"User-Agent": _UA}
    if referer:
        headers["Referer"] = referer
    try:
        r = session.get(url, timeout=20, headers=headers, stream=True,
                        allow_redirects=not no_redirect)
        hdrs = dict(r.headers)
        data, got = [], 0
        for chunk in r.iter_content(16384):
            data.append(chunk)
            got += len(chunk)
            if got >= max_bytes:
                break
        body = b"".join(data)[:max_bytes]
        code = r.status_code
        final = r.url
        r.close()
        return code, hdrs, body, final
    except Exception:
        return 0, {}, None, url


def _decrypt_vb(html):
    """Kotlin decryptPayload parity: crypto-name order first, then bare value."""
    if aes_cbc_decrypt is None:
        return None
    for pat in (r'''data-name=["']crypto["'][^>]*?data-value=["']([^"']+)["']''',
                r'''data-value=["']([^"']+)["']'''):
        m = re.search(pat, html)
        if not m:
            continue
        try:
            import base64
            out = aes_cbc_decrypt(m.group(1), VB_KEY, VB_IV).decode("utf-8", "ignore").strip()
            if out.startswith("http") and any(x in out for x in (".m3u8", ".mp4", ".mkv")):
                return out
        except Exception:
            continue
    return None


def _app_vidbasic(url, session, depth):
    code, _, body, _ = _get(session, url, max_bytes=300 * 1024)
    if code != 200 or not body:
        return None
    text = body.decode("utf-8", "ignore")

    direct = _decrypt_vb(text)
    if direct:
        return direct

    # mirror-selector: data-* candidates + iframes delegate to OTHER resolvers.
    # Kotlin parity (Resolvers.kt:141-163): the app delegates into its own
    # ported registry in RESOLVERS order (Vidbasic skipped; WaffiCloud ->
    # Streamwish -> Vidhide -> ...), falling through to the next candidate when
    # a mirror is dead/expired. Ported hosts use the faithful ports below; the
    # remaining hosts (dood, mixdrop, streamtape, ...) fall back to the
    # reference registry.
    cands = re.findall(r'data-(?:video|src|embed|link)=["\']([^"\']+)["\']', text)
    cands += re.findall(r'<iframe[^>]+src=["\']([^\']+)["\']', text)
    seen = set()
    for cand in cands:
        cand = urljoin(url, cand.replace("&amp;", "&").strip())
        if not cand.startswith("http") or cand == url or cand in seen:
            continue
        seen.add(cand)
        low = cand.lower()
        resolved = None
        if "waffi.cloud" in low:
            resolved = _app_waffi(cand, session)
        elif any(h in low for h in _SW_HOSTS):
            resolved = _app_streamwish(cand, session)
        elif any(h in low for h in _VH_HOSTS):
            resolved = _app_vidhide(cand, session)
        else:
            from src.resolvers import ResolverRegistry
            for other in ResolverRegistry.RESOLVERS:
                if type(other).__name__ == "VidbasicResolver":
                    continue
                try:
                    if other.can_resolve(cand):
                        resolved = ResolverRegistry.resolve(cand, session, _depth=depth + 1)
                        break
                except Exception:
                    continue
        if resolved:
            return resolved

    mv = re.search(r'data-video=["\']([^"\']+)["\']', text)
    if mv:
        player = urljoin(url, mv.group(1))
        code2, _, body2, _ = _get(session, player, referer=url)
        if code2 == 200 and body2:
            direct = _decrypt_vb(body2.decode("utf-8", "ignore"))
            if direct:
                return direct

    mi = re.search(r'<iframe[^>]+src=["\']([^"\']*(?:vidbasic|vidb\.top)[^"\']*)["\']', text)
    if mi and depth < 3:
        inner = urljoin(url, mi.group(1))
        if inner != url:
            return _app_vidbasic(inner, session, depth + 1)

    # legacy plaintext fallback
    m = re.search(r'https?://[^\s"\'<>]+\.(?:mp4|mkv|m3u8)[^\s"\'<>]*', text)
    return m.group(0) if m else None


def _probe_effective(session, start_url):
    """Follow up to 3 manual redirects; return URL that actually served."""
    url = start_url
    for _ in range(3):
        code, hdrs, _, fin = _get(session, url, referer="https://my9jarocks.bz/", no_redirect=True)
        loc = hdrs.get("Location") or hdrs.get("location")
        if code in (301, 302, 303, 307, 308) and loc:
            url = loc
        elif 200 <= code < 300:
            return url
        else:
            return None
    return None


def _app_loadedfiles(url, session):
    global _last_working_host
    hosts = []
    if _last_working_host:
        hosts.append(_last_working_host)
    m = re.search(r'loadedfiles\.[a-z0-9-]+', url.lower())
    if m:
        hosts.append(m.group())
    for tld in ("st", "net", "org", "to", "com"):
        hosts.append(f"loadedfiles.{tld}")

    curr = None
    for h in hosts:
        cand = re.sub(r'loadedfiles\.[a-z0-9-]+', h, url, flags=re.I)
        eff = _probe_effective(session, cand)
        if eff:
            _last_working_host = eff.split("://")[1].split("/")[0].lower()
            curr = eff
            break
    if not curr:
        return None

    pt_hops = 0
    for _ in range(8):
        ref = None if (pt_hops >= 1 and "?pt=" in curr) else (
            f"https://{_last_working_host}/" if "?pt=" in curr else (
                "https://my9jarocks.bz/" if curr == url else curr))
        code, hdrs, body, fin = _get(session, curr, referer=ref, no_redirect=True)
        loc = hdrs.get("Location") or hdrs.get("location")
        if loc and pt_hops >= 1:
            return loc                       # hop-3 semantics: Location IS the answer
        if loc:
            curr = loc
            continue
        if code == 200 and body:
            ct = (hdrs.get("Content-Type") or "").lower()
            if ct.startswith("video/") or "octet-stream" in ct or "matroska" in ct:
                return curr                  # the file itself (new serve-direct chain)
            text = body.decode("utf-8", "ignore")
            dm = re.search(r'https?://[^\s"\'<>]+\.(?:mp4|mkv|m3u8)[^\s"\'<>]*', text)
            if dm and "loadedfiles." not in dm.group(0):
                return dm.group(0)
            m = re.search(r'var downloadUrl = \'(https://loadedfiles\.[a-z0-9-]+/[^\']+)\'', text)
            if m:
                curr = m.group(1)            # VERBATIM — tokens are TLD-bound
                pt_hops += 1
                continue
        break
    return None


def _app_wildshare(url, session):
    html_code, _, body, _ = _get(session, url)
    if body is None:
        return None
    html = body.decode("utf-8", "ignore")
    m = re.search(r'pt=([A-Za-z0-9%+=/]+)', html)
    if not m:
        return None
    parts = url.rstrip("/").split("/")
    file_id = next((p for p in reversed(parts) if not p.endswith(".mkv") and not p.endswith(".mp4")), parts[-1])
    pt_url = f"https://wildshare.net/{file_id}?{m.group(0)}"
    code, hdrs, _, _ = _get(session, pt_url, no_redirect=True)
    loc = hdrs.get("Location") or hdrs.get("location")
    return loc or None


def _app_waffi(url, session):
    return url.split("?preview", 1)[0]


# --- Kotlin-parity mirror ports (Resolvers.kt) -------------------------------
# The app's VidbasicResolver delegates mirror candidates into its own ported
# registry: WaffiCloudResolver, StreamwishResolver, VidhideResolver, ... in
# registry order. The reference monolith ports differ subtly (Streamwish returns
# None on a dead page where Kotlin `continue`s to the next mirror; monolith
# VidhideResolver crashes on urlunparse), so the app-mirror carries faithful
# ports here and only falls back to the reference registry for hosts the app
# ports don't cover.

_SW_HOSTS = ("hglink.to", "streamwish.", "strwsh.", "stwish.", "wishembed.",
             "mwish.", "awish.", "sfastwish.", "swishsrv.", "ajmidyad",
             "khadhnayad", "obeywish.com", "jodwish.com", "streamwish.to",
             "embedwish.", "filelions.")
_VH_HOSTS = ("vidhidefast.", "minochinos.com", "vidhide.", "vidhidepro.",
             "vidhidevip.", "filelions.", "vid-guard.", "nining.",
             "peytonepre.com", "techradar.ink", "ryderjet.com")
_VH_MIRRORS = ("vidhide.com", "minochinos.com", "vidhidefast.com",
               "vidhidevip.com", "vidhidepro.com", "filelions.to")
_DEAD_MARKERS = ("file is no longer available", "file was deleted",
                 "file deleted", "file not found", "video not found",
                 "this file was deleted", "has been removed", "no longer exists")
_last_working_vidhide = None


def _extract_m3u8(html):
    """Kotlin extractM3u8FromHtml parity: first bare .m3u8 URL."""
    m = re.search(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html)
    return m.group(0) if m else None


def _looks_dead_page(html):
    low = (html or "").lower()
    return any(mk in low for mk in _DEAD_MARKERS)


def _app_streamwish(url, session):
    """Kotlin StreamwishResolver parity: /e/ shell bypass via sfastwish.com /
    embedwish.com, referer https://asianc.id/, dead page = try next mirror."""
    try:
        from src.resolvers import _unpack_packed_js
    except Exception:
        def _unpack_packed_js(t):
            return ""
    vid = url.rstrip("/").split("/")[-1]
    if len(vid) >= 6:
        candidates = [f"https://sfastwish.com/e/{vid}",
                      f"https://embedwish.com/e/{vid}",
                      url]
    else:
        candidates = [url]
    for cand in candidates:
        code, _, body, _ = _get(session, cand, referer="https://asianc.id/",
                                max_bytes=300 * 1024)
        if code != 200 or not body:
            continue
        html = body.decode("utf-8", "ignore")
        if _looks_dead_page(html):
            continue
        direct = _extract_m3u8(html)
        if direct:
            return direct
        unpacked = _unpack_packed_js(html)
        if unpacked:
            direct = _extract_m3u8(unpacked)
            if direct:
                return direct
    return None


def _app_vidhide(url, session):
    """Kotlin VidhideResolver parity: frontend rotation with sticky last
    working host (filelions network)."""
    global _last_working_vidhide
    try:
        from src.resolvers import _unpack_packed_js
    except Exception:
        def _unpack_packed_js(t):
            return ""
    mm = re.match(r'(https?://)([^/:]+)(.*)', url)
    url_host = url.split("//", 1)[1].split("/", 1)[0].lower() if "//" in url else ""
    hosts = []
    if _last_working_vidhide:
        hosts.append(_last_working_vidhide)
    if url_host not in hosts:
        hosts.append(url_host)
    for h in _VH_MIRRORS:
        if h not in hosts:
            hosts.append(h)
    for host in hosts:
        cand = url if host == url_host else (mm.group(1) + host + mm.group(3) if mm else url)
        code, _, body, _ = _get(session, cand, referer=cand, max_bytes=300 * 1024)
        if code != 200 or not body:
            continue
        html = body.decode("utf-8", "ignore")
        if _looks_dead_page(html):
            continue
        direct = _extract_m3u8(html)
        if direct:
            _last_working_vidhide = host
            return direct
        unpacked = _unpack_packed_js(html)
        if unpacked:
            direct = _extract_m3u8(unpacked)
            if direct:
                _last_working_vidhide = host
                return direct
    return None


def _app_dramagateway(url, session):
    code, _, body, _ = _get(session, url, referer="https://dramarain.com/")
    if body is None:
        return None
    m = re.search(r'window\.location\.href\s*=\s*["\']([^"\']+)["\']',
                  body.decode("utf-8", "ignore"))
    return m.group(1) if m else None


# --- Kotlin-parity Vidsrc mirror --------------------------------------------
# The monolith VidsrcResolver now stamps the generate.php JWT itself (with a
# per-origin cache in src/resolvers.py). This mirror keeps a second cache for
# the case the monolith path missed (e.g. it returned a tokenless master when
# generate.php was 429): generate.php is aggressively rate-limited (~1 hit per
# IP per window), so reuse the cached token instead of hammering it per item.
_VSRC_TOKENS = {}   # origin -> (token, exp_epoch)


def _vidsrc_token(session, origin):
    import json as _json
    import time as _t
    now = _t.time()
    hit = _VSRC_TOKENS.get(origin)
    if hit and hit[1] > now + 60:
        return hit[0]
    code, _, body, _ = _get(session, origin + "/generate.php")
    tok = (body or b"").decode("utf-8", "ignore").strip() if code == 200 and body else ""
    if not tok:
        return None          # 429/empty: do not cache, retry next call
    exp = now + 4 * 3600
    try:
        import base64 as _b64
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = int(_json.loads(_b64.urlsafe_b64decode(payload).decode("utf-8", "ignore")).get("exp", 0))
    except Exception:
        pass
    _VSRC_TOKENS[origin] = (tok, exp)
    return tok


def _app_vidsrc(url, session):
    from src.resolvers import VidsrcResolver
    direct = VidsrcResolver.resolve(url, session)
    if not direct:
        return None
    # non-destructive token rule: existing token= wins; generate.php only
    # stamps token-less URLs; empty generate output returns unchanged.
    if "token=" in direct:
        return direct
    om = re.match(r'(https?://[^/]+)/', direct)
    if om:
        tok = _vidsrc_token(session, om.group(1))
        if tok:
            cleaned = re.sub(r'[?&]token=[^&]*', "", direct)
            sep = "&" if "?" in cleaned else "?"
            return cleaned + sep + "token=" + tok
    return direct


def _app_pluto(url, session):
    code, _, body, _ = _get(session, url, referer="https://plutomovies.com/")
    if body is None:
        return None
    text = body.decode("utf-8", "ignore")
    m = re.search(r'location\.href\s*=\s*[\'"](https?://[^\'"]+)[\'"]', text)
    if m:
        return m.group(1)
    # Kotlin parity (Resolvers.kt after 9b5eab9/689ffee): /series/ episode
    # pages embed a plain dl.plutomovies.com anchor (live-verified 2026-08-22).
    a = re.search(r'href="(https://[^"]*dl\.plutomovies\.com[^"]*)"', text)
    if a:
        return a.group(1)
    a = re.search(r'href="(https://[^"]*kissorgrab\.com[^"]*)"', text)
    if a:
        return a.group(1)
    dm = re.search(r'https?://[^\s"\'<>]+\.(?:mp4|mkv)[^\s"\'<>]*', text)
    if dm:
        return dm.group(0)
    # Directory pages (series hubs / season listings) expose no download link,
    # only child /series/ pages: descend into the most specific child so the
    # registry recursion walks hub -> season -> episode -> dl anchor.
    children = []
    seen = set()
    for cm in re.finditer(r'href="([^"]*/series/[^"]*)"', text):
        href = cm.group(1).split("#", 1)[0]
        href = urljoin(url, href)
        if href and href.lower() != url.lower() and href not in seen:
            seen.add(href)
            children.append(href)
    if children:
        ep = next((c for c in children if re.search(r's\d{1,2}[-_]?e\d{1,2}|episode-\d{1,3}', c, re.I)), None)
        se = next((c for c in children if re.search(r'season-\d{1,2}', c, re.I)), None)
        return ep or se or children[0]
    return None


def _app_anitaku(url, session, depth):
    """Mirror of AnitakuProvider.resolveEpisode (Kotlin AnitakuProvider.kt:
    140-218): episode page -> malId/ep -> gogoanime fetch_download_links
    admin-ajax -> anchor list -> registry resolve each; fallback: episode
    page data-video/iframe candidates -> registry resolve each."""
    code, _, body, _ = _get(session, url, referer="https://gogoanime.or.at/",
                            max_bytes=400 * 1024)
    if code != 200 or not body:
        return None
    html = body.decode("utf-8", "ignore")

    # 0. Gogoanime direct download links API (malId/ep from the page)
    mal = re.search(r"""malId\s*=\s*['"](\d+)['"]""", html)
    ep = re.search(r"""ep\s*=\s*['"](\d+)['"]""", html)
    if mal and ep:
        host = (urlparse(url).netloc or "gogoanime.or.at")
        ajax = f"https://{host}/wp-admin/admin-ajax.php"
        try:
            r = session.post(ajax, data={
                "action": "fetch_download_links",
                "mal_id": mal.group(1),
                "ep": ep.group(1),
            }, headers={"Referer": url, "X-Requested-With": "XMLHttpRequest"},
                timeout=20)
            if r.status_code == 200 and r.text.strip().startswith("{"):
                payload = r.json()
                dl_html = (payload.get("data") or {}).get("result") or ""
                if dl_html:
                    for a in re.finditer(r'<a[^>]+href="([^"]+)"', dl_html):
                        dl_link = a.group(1)
                        if not dl_link.startswith("http"):
                            continue
                        # Kotlin parity: unclaimed hosts resolve to None in the
                        # app registry; the worker URLs this API returns today
                        # are behind a Cloudflare JS challenge, so skip any
                        # result that is unchanged passthrough (RefPorts) too.
                        resolved = app_resolve(dl_link, session, depth + 1)
                        if resolved and resolved != dl_link:
                            return resolved
        except Exception:
            pass

    # 1. Fallback: episode page embed candidates (Kotlin selectors)
    cands = []
    for m in re.finditer(r'<a[^>]+data-video=["\']([^"\']+)["\']', html):
        cands.append(m.group(1))
    for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', html):
        cands.append(m.group(1))
    seen = set()
    for cand in cands:
        src = urljoin(url, cand.replace("&amp;", "&"))
        if not src.startswith("http") or src == url or src in seen:
            continue
        seen.add(src)
        resolved = app_resolve(src, session, depth + 1)
        if resolved:
            return resolved
    return None


def _naijaprey_chain(url, session, depth):
    if depth >= 2:
        return None
    code, _, body, _ = _get(session, url)
    if body is None:
        return None
    text = body.decode("utf-8", "ignore")
    dm = re.search(r'https?://[^\s"\'<>]+\.(?:mp4|mkv|avi|webm)[^\s"\'<>]*', text)
    if dm:
        return dm.group(0)
    # Kotlin parity: the sdm post's anchor carries class="sdm_download ..."
    # (plain href-regex matches the page's SELF link or wp-json oEmbed URLs
    # that appear earlier in the document and kill the chain).
    a = re.search(r'<a[^>]*class="[^"]*sdm_download[^"]*"[^>]*href="(https://[^"]+)"', text)
    nxt = a.group(1) if a else None
    if not nxt:
        m2 = re.search(r'https://(?:vdl\.np-downloader\.com/sdm_downloads/|wildshare\.net/)[^\s"\'<>]*', text)
        nxt = m2.group(0) if m2 else None
    if not nxt or nxt == url:
        return None
    # wildshare and /d/ hops are final download URLs (Kotlin parity: the
    # provider accepts direct.contains("/d/") as a resolved direct link)
    if "wildshare.net" in nxt or "/d/" in nxt:
        return nxt
    return _naijaprey_chain(nxt, session, depth + 1)


def app_resolve(url, session, depth=0):
    """Mirror of the FIXED Kotlin ResolverRegistry.resolve with recursion."""
    if depth > 6:
        return None
    trimmed = url.strip()
    low = trimmed.lower()
    direct = None
    owner = None

    if "loadedfiles." in low:
        direct, owner = _app_loadedfiles(trimmed, session), "AppLoadedfiles"
    elif "gogoanime.or.at" in low or "anitaku.com.ro" in low:
        direct, owner = _app_anitaku(trimmed, session, depth), "AppAnitaku"
    elif "wildshare.net" in low:
        direct, owner = _app_wildshare(trimmed, session), "AppWildshare"
    elif "dramarain.com/download?link=" in low:
        gw = _app_dramagateway(trimmed, session)
        direct, owner = (_app_waffi(gw, session) if gw else None), "AppDramaGateway+Waffi"
    elif "vidbasic." in low or "vidb.top" in low:
        direct, owner = _app_vidbasic(trimmed, session, depth), "AppVidbasic"
    elif any(h in low for h in ("nepu.gd", "vidsrc.mov", "vsembed.ru", "cloudorchestranova.com")):
        direct, owner = _app_vidsrc(trimmed, session), "AppVidsrc"
    elif "dl.plutomovies.com" in low or re.search(r'plutomovies\.com/(movie|series)/', low):
        direct, owner = _app_pluto(trimmed, session), "AppPlutoMovies"
    elif "np-downloader.com" in low:
        if "/d/" in trimmed:
            # a /d/ link IS the final direct URL (Kotlin acceptance); do not
            # re-fetch it through the chain
            direct, owner = trimmed, "AppNaijaPreyDirect"
        else:
            scraped = _naijaprey_chain(trimmed, session, 0)
            direct, owner = (app_resolve(scraped, session, depth + 1) if scraped else None), "AppNaijaPreyChain"
    elif "waffi.cloud" in low:
        direct, owner = _app_waffi(trimmed, session), "AppWaffi"
    elif any(h in low for h in _SW_HOSTS):
        direct, owner = _app_streamwish(trimmed, session), "AppStreamwish"
    elif any(h in low for h in _VH_HOSTS):
        direct, owner = _app_vidhide(trimmed, session), "AppVidhide"
    else:
        # faithful ports (downloadwella, kissorgrab, nkiserv-direct, ...) --
        # use the reference registry like the app does for its remaining ports
        from src.resolvers import ResolverRegistry
        direct, owner = ResolverRegistry.resolve(trimmed, session), "RefPorts"

    if not direct:
        return None
    log_line(f"HIT {owner} d{depth} -> {direct[:90]}")
    # recursion parity: intermediate results flow back through the registry.
    # The app skips the second pass only when the SAME resolver re-claims a
    # clean media path (its final answer). Known terminal CDNs are treated
    # the same way to save a pointless pass.
    terminal_cdn = any(h in low for h in (
        "downloadwella.com", "gfrdaseazzs.com", "nkiserv.com", "waffi.cloud",
        "wildshare.net", "kissorgrab.com", "dl.plutomovies.com"))
    if direct != trimmed and depth < 6 and not (terminal_cdn and _path_is_media(direct)):
        deeper = app_resolve(direct, session, depth + 1)
        if deeper:
            return deeper
    return direct


def log_line(msg):
    print("   " + msg, flush=True)
