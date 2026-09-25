import re
import sys
import time
import base64
import socket
import struct
import tempfile
import subprocess
import os
import threading
from html import unescape
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, quote, urlunparse

from ._aes import aes_cbc_decrypt

# Lazy `requests`/`urllib3`/`BeautifulSoup`: importing them (+ charset_normalizer)
# costs ~900ms and nothing needs them to draw the banner or run the REPL — only
# an actual resolve/scrape does. They load on first use, not at startup. The
# InsecureRequestWarning suppression (for expired SSL certs on hosts like
# wetafiles) runs once, the first time requests is loaded.
class _LazyRequests:
    _mod = None
    def _load(self):
        if _LazyRequests._mod is None:
            import requests as _r
            import urllib3 as _u3
            try:
                _u3.disable_warnings(_u3.exceptions.InsecureRequestWarning)
            except Exception:
                pass
            _LazyRequests._mod = _r
        return _LazyRequests._mod
    def __getattr__(self, name):
        return getattr(self._load(), name)

requests = _LazyRequests()

def BeautifulSoup(*args, **kwargs):
    from bs4 import BeautifulSoup as _BS
    return _BS(*args, **kwargs)

# Ensure console stdout is configured to handle UTF-8 symbols when supported.
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, OSError):
    pass

# Try importing thread-safe utilities from local modules
try:
    from .downloader import safe_print, UA_DESKTOP
except ImportError:
    safe_print = print
    UA_DESKTOP = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# Helper to find video files in HTML/scripts
_MEDIA_EXTS = ('.m3u8', '.mp4', '.mkv')
# Extensions that mean "this is a web page", never a media stream.
_PAGE_EXTS = ('.html', '.htm', '.php', '.asp', '.aspx', '.jsp')


def _is_media_path(candidate):
    """True if this URL plausibly points at media rather than an embed page.

    The naive "`.mp4` appears anywhere in the URL" test also matches embed PAGES
    that merely mention a media name in their HOSTNAME -- vidb.top's dead-mirror
    page yields `https://www.mp4upload.com/embed-bw0vvtzc7ywp.html`, whose host
    contains ".mp4". That URL serves 16 bytes of the literal text "File was
    deleted", which aria2c happily saves as `<episode>.html.mp4` and reports OK.

    So: a path ending in a page extension is never media, and the extension has
    to show up in the path or query -- a mention in the host alone doesn't count.
    Token-signed streams (`...master.m3u8?t=abc`) and redirector endpoints
    (`/dl?file=video.mp4`) both still pass, which the stricter
    "path must END in media ext" rule would have wrongly rejected."""
    try:
        p = urlparse(candidate)
    except Exception:
        return False
    path = (p.path or '').lower()
    query = (p.query or '').lower()
    if path.endswith(_PAGE_EXTS):
        return False
    if path.endswith(_MEDIA_EXTS):
        return True
    return any(e in path or e in query for e in _MEDIA_EXTS)


def find_direct_video(text):
    """Pull the first real direct-media URL out of player JS/HTML.

    Every candidate must pass `_is_media_path` -- a bare substring match happily
    returns embed pages (see that helper). Preference order keeps HLS first."""
    for ext in [r'\.m3u8', r'\.mp4', r'\.mkv']:
        found = re.findall(r'https?://[^\s"\'<>\\]+' + ext + r'[^\s"\'<>\\]*', text)
        for cand in found:
            cand = cand.rstrip('.,;)')
            if _is_media_path(cand):
                return cand
    return None

def _exc_chain(exc):
    """Walk an exception's __cause__/__context__ chain."""
    seen = []
    cur = exc
    while cur is not None and cur not in seen:
        seen.append(cur)
        cur = cur.__cause__ or cur.__context__
    return seen


_NET_EXC_CACHE = None
_TRANSIENT_EXC_CACHE = None


def _http_exc_bases():
    """Broad HTTP exception bases across BOTH stacks, for `except` clauses.

    Both stacks are live at once: make_session() (downloader.py) returns a
    `curl_cffi.requests.session.Session` whenever curl_cffi imports -- it is in
    requirements.txt and installed -- while a few call sites still use plain
    `requests`. curl_cffi's exception tree is DISJOINT from requests':
    `issubclass(curl_cffi...ConnectionError, requests.RequestException)` is
    False. So a bare `except requests.RequestException` clause never fires for
    anything a resolver's `session.get()` raises; every dropped connection fell
    through to the `except Exception: return None` below it and was reported as
    "link is gone" -- a permanent episode failure for one lost packet, which is
    exactly what the wait-for-network retry machinery exists to prevent.
    """
    global _NET_EXC_CACHE
    if _NET_EXC_CACHE is None:
        bases = [requests.RequestException]
        try:
            from curl_cffi.requests import exceptions as _cfx
            bases.append(_cfx.RequestException)
        except Exception:
            pass
        _NET_EXC_CACHE = tuple(bases)
    return _NET_EXC_CACHE


def _transient_exc_types():
    """Connectivity-failure classes from both stacks, for isinstance checks."""
    global _TRANSIENT_EXC_CACHE
    if _TRANSIENT_EXC_CACHE is None:
        types = []
        for mod in ('requests', 'curl_cffi'):
            try:
                if mod == 'requests':
                    _e = requests.exceptions
                else:
                    from curl_cffi.requests import exceptions as _e
                types += [_e.ConnectionError, _e.Timeout]
            except Exception:
                pass
        _TRANSIENT_EXC_CACHE = tuple(types)
    return _TRANSIENT_EXC_CACHE


def _is_network_error(exc):
    """True if the exception is a transient connectivity failure (DNS drop,
    connection refused/reset, timeout) rather than a real 'not found'.

    These deserve a wait-for-network retry — the target link almost certainly
    still exists; the device just lost signal for a moment.

    isinstance first, name-matching second. Name-matching alone missed
    curl_cffi's `DNSError`: it subclasses curl_cffi's ConnectionError but its
    name is in no marker set, so the single most common mobile failure -- the
    radio dropping mid-resolve -- was classified as "genuinely gone" and failed
    the episode permanently. The name set is still consulted because urllib3's
    inner causes (NameResolutionError, gaierror) are not in either tree.
    """
    chain = _exc_chain(exc)
    transient = _transient_exc_types()
    if transient and any(isinstance(e, transient) for e in chain):
        return True
    names = {type(e).__name__ for e in chain}
    net_markers = {
        'ConnectionError', 'ConnectTimeout', 'ReadTimeout', 'Timeout',
        'NewConnectionError', 'MaxRetryError', 'NameResolutionError',
        'ConnectTimeoutError', 'gaierror', 'DNSError', 'ConnectionResetError',
    }
    return bool(names & net_markers)


def _resolver_wait_for_network(stop_flag=None, max_wait=60):
    """Wait (bounded) for connectivity to return before a resolver retry.

    BOUNDED on purpose: the download-loop's wait_for_network() can block
    forever (correct there — a paused download should wait indefinitely), but
    a resolver retry must NOT hang the whole app if the network never comes
    back or check_connection() is blocked by a captive portal / firewall.
    Caps at max_wait seconds, polling every 3s, then gives up so the resolve
    fails normally instead of freezing the terminal.
    """
    from .downloader import check_connection
    waited = 0
    while waited < max_wait:
        if stop_flag is not None:
            try:
                from .downloader import _is_stopped
                if _is_stopped(stop_flag):
                    return False
            except Exception:
                pass
        try:
            if check_connection():
                return True
        except Exception:
            pass
        time.sleep(3)
        waited += 3
    return False


def safe_get(session, url, timeout=20, referer=None, retries=3, _seen=None):
    if _seen is None:
        _seen = set()
    if url in _seen:
        safe_print(f"      [!] JS redirect loop detected: {url[:60]}")
        return None
    _seen.add(url)
    for attempt in range(retries):
        try:
            headers = {'Referer': referer} if referer else {}
            r = session.get(url, timeout=timeout, headers=headers)

            if not r.ok:
                safe_print(f"      [!] HTTP {r.status_code}: {url[:60]}")
                if attempt < retries - 1:
                    time.sleep(2)
                    continue
                return None

            # Only follow a JS redirect out of a SUCCESSFUL page. This check used
            # to run before the r.ok test above, and dead intermediate links
            # routinely answer 404/410 with a "bounce to the homepage" script --
            # so safe_get followed it and handed the HOMEPAGE back as a success.
            # LoadedfilesResolver then returned None for what was really a dead
            # link, and StreamtapeResolver's find_direct_video(r.text) fallback
            # would return whatever unrelated video sat on that homepage: a
            # wrong-file download instead of a clean failure.
            m = re.search(r'window\.location\.href\s*=\s*["\']([^"\']+)["\']', r.text)
            if m:
                redirect_url = m.group(1)
                if not redirect_url.startswith('http'):
                    redirect_url = urljoin(url, redirect_url)
                safe_print(f"      [*] Following JS redirect: {redirect_url[:60]}...")
                # Forward the caller's timeout -- dropping it silently reverted to
                # the 20s default, so LoadedfilesResolver's deliberate timeout=10
                # per-candidate-host liveness probe cost 20s per TLD instead.
                return safe_get(session, redirect_url, timeout=timeout, referer=referer,
                                retries=max(1, retries - 1), _seen=_seen)

            return r
        except Exception as e:
            safe_print(f"      [!] Attempt {attempt+1}/{retries} failed: {e}")
            if attempt < retries - 1:
                time.sleep(2)
    return None

class BaseResolver:
    @staticmethod
    def can_resolve(url: str) -> bool:
        return False

    @staticmethod
    def resolve(url: str, session) -> str:
        return None

# --- INDIVIDUAL RESOLVERS ---

class WaffiCloudResolver(BaseResolver):
    @staticmethod
    def can_resolve(url: str) -> bool:
        return 'waffi.cloud' in urlparse(url).netloc.lower()

    @staticmethod
    def resolve(url: str, session) -> str:
        # Strip preview param to get direct file link
        return url.split('?preview')[0] if '?preview' in url else url

class DownloadwellaResolver(BaseResolver):
    @staticmethod
    def can_resolve(url: str) -> bool:
        netloc = urlparse(url).netloc.lower()
        return any(domain in netloc for domain in ['downloadwella.com', 'wetafiles.com'])

    @staticmethod
    def resolve(url: str, session) -> str:
        # Network failures (DNS drop, reset) re-raise so the registry's unified
        # wait-and-retry handles them — a dropped connection does NOT mean the
        # link is gone. Real "not found" (bad HTTP / no form) fails fast.
        try:
            # verify=False handles SSL issues on expired host certs
            try:
                r = session.get(url, timeout=20, verify=False)
            except TypeError:
                r = session.get(url, timeout=20)
            if not r or r.status_code != 200:
                safe_print(f"      [!] Downloadwella: Failed to load page (HTTP {r.status_code if r else 'No Response'})")
                return None

            soup = BeautifulSoup(r.text, 'html.parser')
            form = soup.find('form')
            if not form:
                for a in soup.find_all('a', href=True):
                    href = a['href'].strip()
                    if any(k in href for k in ('/d/', '/download/', 'token=', '?pt=')) or any(href.lower().endswith(ext) for ext in ('.mp4', '.mkv', '.webm')):
                        if not href.startswith('http'):
                            href = urljoin(url, href)
                        return href
                direct = find_direct_video(r.text)
                if direct:
                    return direct
                safe_print("      [!] Downloadwella: No form element found on page")
                return None

            data = {inp.get('name'): inp.get('value', '')
                    for inp in form.find_all('input') if inp.get('name')}
            # LIVE (2026-08): forcing method_free to 'Free Download' makes the
            # server reject the POST -- submit the form verbatim as rendered.

            try:
                r2 = session.post(url, data=data, timeout=20, verify=False)
            except TypeError:
                r2 = session.post(url, data=data, timeout=20)

            if not r2 or r2.status_code != 200:
                safe_print(f"      [!] Downloadwella: Post request failed (HTTP {r2.status_code if r2 else 'No Response'})")
                return None

            return find_direct_video(r2.text)
        except _http_exc_bases() as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] Downloadwella: Network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] Downloadwella: Resolution error: {e}")
            return None

class LoadedfilesResolver(BaseResolver):
    # loadedfiles keeps switching TLDs (.st / .org / .net / …) but every host
    # serves the same file hashes. A fixed rewrite to one TLD breaks whenever
    # that host goes offline (e.g. .st refusing connections while .net is live),
    # so try the link's own TLD first and fall back through the other known
    # hosts until one answers.
    _HOST = re.compile(r'loadedfiles\.[a-z0-9-]+', re.I)
    _FALLBACK_TLDS = ('st', 'net', 'org', 'to', 'com')
    _LAST_WORKING_HOST = None
    # Circuit breaker: when the .net origin hangs (TCP+TLS to the Cloudflare
    # edge succeed but the origin never sends a byte -- the Sept 2026 Death
    # Note outage), every TLD in the walk eventually 301s back to the same
    # dead origin, so each link pays 5 full timeouts. Skip a host that just
    # timed out for a few minutes instead of re-hanging per episode.
    _DEAD_UNTIL = {}
    _DEAD_COOLDOWN = 300.0

    @classmethod
    def _rewrite(cls, text: str, host: str) -> str:
        """Rewrite any loadedfiles.<tld> occurrence in text to the live host."""
        return cls._HOST.sub(host, text)

    @classmethod
    def _to_st(cls, text: str) -> str:  # back-compat alias
        return cls._rewrite(text, 'loadedfiles.st')

    @classmethod
    def _candidate_hosts(cls, url: str):
        """Live-host candidates: last known working host first, then the link's
        own TLD, then known fallbacks."""
        m = cls._HOST.search(url)
        url_host = m.group(0).lower() if m else None
        hosts = []
        if cls._LAST_WORKING_HOST:
            hosts.append(cls._LAST_WORKING_HOST)
        if url_host and url_host not in hosts:
            hosts.append(url_host)
        for tld in cls._FALLBACK_TLDS:
            h = f'loadedfiles.{tld}'
            if h not in hosts:
                hosts.append(h)
        return hosts

    @staticmethod
    def can_resolve(url: str) -> bool:
        netloc = urlparse(url).netloc.lower()
        return re.match(r'(www\.)?loadedfiles\.[a-z0-9-]+$', netloc) is not None

    @staticmethod
    def _unescape_js_url(u: str) -> str:
        if not u:
            return u
        u = u.replace(r'\/', '/').replace(r'\u0026', '&')
        try:
            u = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), u)
        except Exception:
            pass
        return u.strip('\'" ')

    @classmethod
    def _extract_link(cls, html: str, live_host: str) -> str:
        if not html:
            return None
        # 1. var downloadUrl = '...'
        m = re.search(r'''(?:var\s+downloadUrl|downloadUrl)\s*=\s*['"]([^'"]+)['"]''', html, re.I)
        if m:
            raw = cls._unescape_js_url(m.group(1))
            return cls._rewrite(raw, live_host)

        # 2. Alpine.js dlTimer({ ... link: '...' ... })
        m = re.search(r'''dlTimer\s*\(\s*\{.*?link\s*:\s*['"]([^'"]+)['"]''', html, re.DOTALL | re.I)
        if m:
            raw = cls._unescape_js_url(m.group(1))
            return cls._rewrite(raw, live_host)

        # 3. Anchor fallback with /d/, /token/download/, or ?pt=
        try:
            soup = BeautifulSoup(html, 'html.parser')
            for a in soup.find_all('a', href=True):
                href = a['href'].strip()
                if any(k in href for k in ('/d/', '/token/download/', '?pt=')):
                    raw = cls._unescape_js_url(href)
                    if not raw.startswith('http'):
                        raw = urljoin(f'https://{live_host}/', raw)
                    return cls._rewrite(raw, live_host)
        except Exception:
            pass

        m = re.search(r'''href=['"]([^'"]*(?:/d/|/token/download/|\?pt=)[^'"]*)['"]''', html, re.I)
        if m:
            raw = cls._unescape_js_url(m.group(1))
            if not raw.startswith('http'):
                raw = urljoin(f'https://{live_host}/', raw)
            return cls._rewrite(raw, live_host)

        return None

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            hosts = LoadedfilesResolver._candidate_hosts(url)
            r1 = None
            live_host = None
            for host in hosts:
                # Circuit breaker: skip a host that just hung (see _DEAD_UNTIL).
                dead_until = LoadedfilesResolver._DEAD_UNTIL.get(host, 0)
                if time.time() < dead_until:
                    continue
                candidate = LoadedfilesResolver._rewrite(url, host)
                t0 = time.time()
                r1 = safe_get(session, candidate, referer='https://my9jarocks.bz/', timeout=10, retries=1)
                if r1:
                    live_host = host
                    LoadedfilesResolver._LAST_WORKING_HOST = host
                    LoadedfilesResolver._DEAD_UNTIL.pop(host, None)
                    break
                if time.time() - t0 >= 9.0:
                    # A full near-timeout on a 10s GET means the edge answered
                    # (or swallowed the connect) but never served the page --
                    # park it so the remaining episodes skip this host fast.
                    LoadedfilesResolver._DEAD_UNTIL[host] = time.time() + LoadedfilesResolver._DEAD_COOLDOWN
                    safe_print(f"      [!] {host}: origin not responding -- "
                                f"pausing this host for {int(LoadedfilesResolver._DEAD_COOLDOWN/60)} min")
            if not r1:
                return None
            step1 = LoadedfilesResolver._extract_link(r1.text, live_host)
            if not step1:
                return None
            step1 = LoadedfilesResolver._rewrite(step1, live_host)
            r2 = safe_get(session, step1, referer=f'https://{live_host}/', timeout=10, retries=2)
            if not r2:
                return None
            step2 = LoadedfilesResolver._extract_link(r2.text, live_host)
            if not step2:
                if any(k in step1 for k in ('.mp4', '.mkv', '/d/', '?pt=')):
                    return step1
                return None
            try:
                step2 = LoadedfilesResolver._rewrite(step2, live_host)
                r3 = session.get(step2, timeout=10, allow_redirects=False)
                loc = r3.headers.get('location')
                return loc if loc else step2
            except Exception as e:
                safe_print(f"      [!] Loadedfiles redirect: {e}")
                return step2
        except Exception as e:
            safe_print(f"      [!] Loadedfiles: {e}")
            return None

class WildshareResolver(BaseResolver):
    @staticmethod
    def can_resolve(url: str) -> bool:
        netloc = urlparse(url).netloc.lower()
        return netloc in ['wildshare.net', 'www.wildshare.net']

    @staticmethod
    def resolve(url: str, session) -> str:
        # The 302 Location from the token handshake IS the final direct file
        # (same path, ?download_token= query). Resolve() gets called again on
        # it because the registry fast-path excludes wildshare .mkv URLs --
        # GETting it here would stream the whole episode into the 20s timeout
        # (measured: retries burned 7-20MB each and failed every episode).
        if 'download_token=' in url:
            return url
        try:
            try:
                from curl_cffi import requests as cf_requests
                s = cf_requests.Session(impersonate='chrome120')
            except ImportError:
                safe_print("      [!] Wildshare: curl_cffi unavailable — plain requests (TLS fingerprint may be blocked)")
                s = requests.Session()
            try:
                s.headers['User-Agent'] = UA_DESKTOP

                r = s.get(url, timeout=20)
                if not r or r.status_code != 200:
                    return None
                pt_val = None
                m = re.search(r'''(?:[?&]pt=|["']pt["']\s*:\s*["']|\bpt\s*[:=]\s*["'])([A-Za-z0-9%+=/_\-]+)''', r.text)
                if m:
                    pt_val = m.group(1)
                elif re.search(r'''pt=([A-Za-z0-9%+=/_\-]+)''', r.text):
                    pt_val = re.search(r'''pt=([A-Za-z0-9%+=/_\-]+)''', r.text).group(1)
                if not pt_val:
                    safe_print("      [!] Wildshare: could not extract ?pt= token")
                    return None
                parts = url.rstrip('/').split('/')
                file_id = next((p for p in reversed(parts) if not p.endswith(('.mkv', '.mp4', '.m3u8'))), parts[-1])
                pt_url = f'https://wildshare.net/{file_id}?pt={pt_val}'
                r2 = s.get(pt_url, timeout=20, allow_redirects=False)
                loc = r2.headers.get('location')
                if loc:
                    return loc
                return None
            finally:
                s.close()
        except Exception as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] Wildshare: {e}")
            return None

class NaijaPreyChainResolver(BaseResolver):
    """Kotlin parity (NaijaPreyProvider.extractFileLink): the naijaprey pages
    embed a vdl.np-downloader.com/sdm_downloads gateway whose post body holds a
    single a.sdm_download anchor pointing at wildshare.net (which serves the
    direct media file). Depth-capped at 2 => at most 2 page fetches."""

    @staticmethod
    def can_resolve(url: str) -> bool:
        netloc = urlparse(url).netloc.lower()
        return 'np-downloader.com' in netloc or 'sdm_downloads' in url

    @staticmethod
    def _extract_file_link(url: str, session, depth: int):
        if depth >= 2:
            return None
        # a /d/ link IS the final direct URL (Kotlin acceptance) -- never
        # re-fetch it through the chain
        if '/d/' in url:
            return url
        try:
            r = session.get(url, timeout=20)
            if not r or r.status_code != 200:
                return None
            text = r.text
        except _http_exc_bases() as e:
            if _is_network_error(e):
                raise
            return None
        except Exception as e:
            safe_print(f"      [!] NaijaPreyChain: {e}")
            return None
        # 1) A ready direct media URL anywhere in the markup wins immediately.
        m = re.search(r'https?://[^\s"\'<>]+\.(?:mp4|mkv|avi|webm)[^\s"\'<>]*', text)
        if m:
            return m.group(0)
        # 2) Otherwise hop to the next stage: the a.sdm_download anchor (class
        #    selector parity — a plain href regex hits the page's SELF link or
        #    wp-json oEmbed URLs that appear earlier in the document).
        a = re.search(r'<a[^>]*class="[^"]*sdm_download[^"]*"[^>]*href="(https://[^"]+)"', text)
        nxt = a.group(1) if a else None
        if not nxt:
            m2 = re.search(r'https://(?:vdl\.np-downloader\.com/sdm_downloads/|wildshare\.net/)[^\s"\'<>]*', text)
            nxt = m2.group(0) if m2 else None
        if not nxt or nxt == url:
            return None
        # wildshare and /d/ hops are final download URLs (Kotlin parity: the
        # provider accepts direct.contains("/d/") as a resolved direct link)
        if 'wildshare.net' in nxt or '/d/' in nxt:
            return nxt
        return NaijaPreyChainResolver._extract_file_link(nxt, session, depth + 1)

    @staticmethod
    def resolve(url: str, session) -> str:
        return NaijaPreyChainResolver._extract_file_link(url, session, 0)


class StreamtapeResolver(BaseResolver):
    _HOSTS = ('streamtape.com', 'watchadsontape.com', 'strtape.tech')

    @staticmethod
    def can_resolve(url: str) -> bool:
        netloc = urlparse(url).netloc.lower()
        return any(domain in netloc for domain in StreamtapeResolver._HOSTS)

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            r = safe_get(session, url, referer='https://watchadsontape.com/')
            if not r or r.status_code == 404:
                return None
            m = re.search(
                r'''getElementById\(['"]robotlink['"]\)[^;]*innerHTML\s*=\s*['"]([^'"]+)['"]\s*\+\s*(?:\(['"]|['"])([^'"\)]+)(?:['"]\)|['"])''',
                r.text, re.DOTALL
            )
            if m:
                base_s, raw = m.group(1), m.group(2)
                find_m = re.search(r'''getElementById\(['"]robotlink['"]\)''', r.text)
                subtext = r.text[find_m.start():] if find_m else r.text
                for n in re.findall(r'\.substring\((\d+)\)', subtext):
                    raw = raw[int(n):]
                get_url = 'https:' + base_s + raw
                r2 = session.get(get_url, timeout=20, allow_redirects=False)
                loc = r2.headers.get('location')
                if loc:
                    return loc
            else:
                safe_print(f"      [!] Streamtape JS pattern not matched — site may have changed")
            return find_direct_video(r.text)
        except Exception as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] Streamtape: {e}")
            return None

class VidmolyResolver(BaseResolver):
    _HOSTS = ('vidmoly.me', 'vidmoly.to', 'vidmoly.net', 'vidmoly.biz')

    @staticmethod
    def can_resolve(url: str) -> bool:
        netloc = urlparse(url).netloc.lower()
        return any(h in netloc for h in VidmolyResolver._HOSTS)

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            r = session.get(url, timeout=20, headers={'User-Agent': UA_DESKTOP, 'Referer': url})
            if not r or r.status_code != 200:
                return None
            if _looks_dead(r.text):
                safe_print("      [!] Vidmoly: file expired/deleted")
                return None

            unpacked = _unpack_packed_js(r.text) or r.text

            # Vidmoly hides stream link in file: "http...playlist.m3u8" or .mp4 inside javascript
            m = re.search(r'file\s*:\s*["\'](https?://[^"\']+\.(?:m3u8|mp4)[^"\']*)["\']', unpacked)
            if m:
                return m.group(1)

            direct = _find_hls_or_mp4(unpacked) or find_direct_video(unpacked)
            if direct:
                return direct
            return None
        except _http_exc_bases() as e:
            if _is_network_error(e):
                raise  # let the registry wait-and-retry
            safe_print(f"      [!] Vidmoly: Network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] Vidmoly: Resolution error: {e}")
            return None

# vidbasic's /3rdplayer.html scheme: the video URL is AES-256-CBC encrypted in
# the `data-value` attribute of the crypto <script> tag and decrypted in-browser
# by obfuscated JS. Key + IV are static UTF-8 constants baked into that JS —
# recovered by running the real player under a CryptoJS interceptor (see
# docs/vidbasic_crypto.md). If the site rotates them, decrypt yields non-URL
# bytes and resolve() fails cleanly; re-recover with the documented harness.
_VIDBASIC_KEY = b'94588293375053432799222445521289'
_VIDBASIC_IV  = b'5259228356829423'

class VidbasicResolver(BaseResolver):
    # vidbasic.top / vidbasic.to / vidb.top serve the CryptoJS player directly;
    # embedload.cfd is a thin wrapper that iframes one of them.
    _HOSTS = ('vidbasic.', 'vidb.top', 'embedload.cfd')

    @staticmethod
    def can_resolve(url: str) -> bool:
        p = urlparse(url)
        net = p.netloc.lower()
        if not any(h in net for h in VidbasicResolver._HOSTS):
            return False
        # The decrypted output lives on stream.vidbasic.top/…​.m3u8 — that's a
        # direct stream, not an embed. Don't let the registry's re-resolve loop
        # feed it back here (it would fetch the manifest and find no payload).
        if p.path.lower().endswith(('.m3u8', '.mp4', '.mkv', '.ts')):
            return False
        return True

    @staticmethod
    def _decrypt_payload(html_text: str):
        """Find the crypto <script data-value="..."> payload and AES-decrypt it
        to the direct stream URL. Returns None if the tag is absent or the key
        no longer fits (decrypt produced non-URL bytes)."""
        m = re.search(r'data-name=["\']crypto["\'][^>]*?data-value=["\']([^"\']+)["\']', html_text)
        if not m:  # attribute order can vary
            m = re.search(r'data-value=["\']([^"\']+)["\'][^>]*?data-name=["\']crypto["\']', html_text)
        if not m:
            return None
        try:
            ct = base64.b64decode(m.group(1))
            pt = aes_cbc_decrypt(ct, _VIDBASIC_KEY, _VIDBASIC_IV).decode('utf-8', 'ignore').strip()
        except Exception:
            return None
        if pt.startswith('http') and ('.m3u8' in pt or '.mp4' in pt or '.mkv' in pt):
            return pt
        return None

    @staticmethod
    def resolve(url: str, session, _depth: int = 0) -> str:
        try:
            r = session.get(url, timeout=20)
            if not r or r.status_code != 200:
                return None
            text = r.text

            # 1) this page already carries the encrypted payload (3rdplayer.html)
            direct = VidbasicResolver._decrypt_payload(text)
            if direct:
                return direct

            # 1a) fast-path for 3rdplayer: if page contains data-video or iframe src pointing to 3rdplayer,
            # fetch and decrypt directly
            m_player = re.search(r'(?:data-video|<iframe[^>]+src)=["\']([^"\']*3rdplayer[^"\']*)["\']', text)
            if m_player:
                player_url = urljoin(url, unescape(m_player.group(1)))
                pr = session.get(player_url, timeout=20, headers={'Referer': url})
                if pr is not None and pr.status_code == 200:
                    direct = VidbasicResolver._decrypt_payload(pr.text)
                    if direct:
                        return direct

            # 1b) server-selector layout: vidb.top now serves a multi-server page
            # whose data-video / data-src / iframe attrs point at EXTERNAL mirror
            # embeds (streamwish hglink.to, vidhide minochinos.com, doodstream,
            # streamtape) rather than a vidbasic crypto player. Try resolving candidates
            # via the registry, falling through to the next mirror if one is dead/expired.
            cands = re.findall(r'data-(?:video|src|embed|link)=["\']([^"\']+)["\']', text)
            cands += re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', text)
            seen = set()
            for cand in cands:
                cand = urljoin(url, unescape(cand.strip()))
                if not cand.startswith('http') or cand == url or cand in seen:
                    continue
                seen.add(cand)

                if '3rdplayer' in cand:
                    pr = session.get(cand, timeout=20, headers={'Referer': url})
                    if pr is not None and pr.status_code == 200:
                        direct = VidbasicResolver._decrypt_payload(pr.text)
                        if direct:
                            return direct
                    continue

                for other in ResolverRegistry.RESOLVERS:
                    if other is VidbasicResolver:
                        continue
                    try:
                        if other.can_resolve(cand):
                            resolved = ResolverRegistry.resolve(cand, session, _depth=1)
                            if resolved:
                                return resolved
                            safe_print(f"      [!] Vidbasic mirror failed/dead: {cand[:60]} -- trying next candidate...")
                            break
                    except Exception:
                        continue

            # 2) embedload.cfd wrapper iframes the real vidbasic host.
            # This recursion is our own, so the registry's _depth > 5 limit never
            # sees it: A can iframe B which iframes A again, and `inner != url`
            # only catches a page iframing itself. Carry our own counter.
            mi = re.search(r'<iframe[^>]+src=["\']([^"\']*(?:vidbasic|vidb\.top|3rdplayer|/embed/)[^"\']*)["\']', text)
            if mi and _depth < 3:
                inner = urljoin(url, mi.group(1))
                if inner != url:
                    return VidbasicResolver.resolve(inner, session, _depth=_depth + 1)

            # 4) legacy plaintext fallback (pre-CryptoJS scheme)
            return find_direct_video(text)
        except _http_exc_bases() as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] Vidbasic: Network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] Vidbasic: Resolution error: {e}")
            return None

class KissasianResolver(BaseResolver):
    # kissasian9.ro /kisskh/<id> player: inline JSON carries a /source API path
    # that returns {"status":"ok","source":"<m3u8 url>","tracks":[...]} directly.
    _HOSTS = ('kissasian9.ro',)

    @staticmethod
    def can_resolve(url: str) -> bool:
        p = urlparse(url)
        return (any(h in p.netloc.lower() for h in KissasianResolver._HOSTS)
                and '/kisskh/' in p.path
                and not p.path.lower().endswith(('.m3u8', '.mp4', '.mkv')))

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            r = session.get(url, timeout=20, headers={
                'Referer': url, 'Sec-Fetch-Dest': 'iframe',
                'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Site': 'cross-site',
            })
            if not r or r.status_code != 200:
                return None
            m = re.search(r'"sourceUrl"\s*:\s*"([^"]+)"', r.text)
            if not m:
                return None
            api = urljoin(url, m.group(1))
            ar = session.get(api, timeout=20, headers={
                'Referer': url, 'Accept': 'application/json',
                'Sec-Fetch-Dest': 'empty', 'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin',
            })
            if not ar or ar.status_code != 200:
                return None
            d = ar.json()
            src = d.get('source') if isinstance(d, dict) else None
            if src and src.startswith('http') and '.m3u8' in src:
                return src
            return None
        except _http_exc_bases() as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] Kissasian: Network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] Kissasian: Resolution error: {e}")
            return None

class KisskhMegaplayResolver(BaseResolver):
    _HOSTS = ('kisskh.megaplay.', 'megaplays.se', 'embtaku.', 'takuembed.',
              'anihdplay.', 'gogohd.', 'megaplay.', 'animesama.', 'tamilembed.',
              'gogoanime.me.uk', 'vidmoly.biz', 'vidmoly.me', 'vidmoly.to',
              'vidmoly.net', 'vkspeed.com', 'ansembed.net', 'sibnet.ru')

    @staticmethod
    def can_resolve(url: str) -> bool:
        if '/playlist.php' in url or '/api/' in url:
            return False
        netloc = urlparse(url).netloc.lower()
        return any(h in netloc for h in KisskhMegaplayResolver._HOSTS) or '/kisskh/' in url

    @staticmethod
    def resolve(url: str, session, quality=None) -> str:
        try:
            headers = {
                'User-Agent': UA_DESKTOP,
                'Sec-Fetch-Dest': 'iframe',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'cross-site',
            }
            # LIVE (2026-08): tamilembed 403s any request carrying a Referer.
            if 'tamilembed' not in url:
                headers['Referer'] = session.headers.get('Referer', '')
            r = session.get(url, timeout=20, headers=headers)
            # NB: `not r` is truthy for any 4xx/5xx (requests.Response.__bool__
            # returns .ok), so it would swallow the 404s we explicitly want to
            # allow here. tamilembed.lol serves its real player page (with the
            # Blogger iframe) under a 404 status, so gate on `is None` and check
            # the code separately -- otherwise every 404-served embed dies before
            # we ever read the body.
            if r is None or r.status_code not in (200, 404):
                safe_print(f"      [!] KisskhMegaplay: embed fetch status={getattr(r, 'status_code', 'None')}")
                return None

            # 0) tamilembed.lol / player wrapper layout: check for inner nested <iframe>
            # Player wrappers (e.g. tamilembed.lol, APICodes) embed Blogger (www.blogger.com/video.g?token=...)
            # or secondary player streams inside an inner iframe tag.
            if r.text:
                soup = BeautifulSoup(r.text, 'html.parser')
                iframe = soup.find('iframe', src=True)
                if iframe:
                    nested_src = urljoin(url, unescape(iframe['src']))
                    if nested_src != url and not nested_src.startswith('javascript:'):
                        if 'blogger.com' in nested_src:
                            return nested_src
                        if urlparse(nested_src).netloc != urlparse(url).netloc:
                            safe_print(f"      [>] Following inner iframe: {nested_src[:80]}...")
                            sub_res = ResolverRegistry.resolve(nested_src, session, quality=quality, _depth=1)
                            if sub_res:
                                return sub_res
                            safe_print(f"      [!] Inner iframe resolution returned None")

            # 1) animesama.se layout: const STREAM = "..."
            sm_m = re.search(r'''const\s+STREAM\s*=\s*["']([^"']+)["']''', r.text)
            if sm_m:
                return sm_m.group(1).replace('\\/', '/')

            # 2) megaplays.se / takuembed layout: proxyBase + defaultUrl / qualities
            pb_m = re.search(r'''var\s+proxyBase\s*=\s*["']([^"']+)["']''', r.text)
            def_m = re.search(r'''var\s+defaultUrl\s*=\s*["']([^"']+)["']''', r.text)
            if def_m:
                target_url = def_m.group(1).replace('\\/', '/')
                if pb_m:
                    proxy_base = pb_m.group(1)
                    return proxy_base + quote(target_url, safe='')
                return target_url

            # 3) qualities map in script: {"1080p":"...", "720p":"...", "360p":"..."}
            q_m = re.search(r'''var\s+qualities\s*=\s*(\{.*?\});''', r.text, re.DOTALL)
            if q_m and pb_m:
                try:
                    import json
                    q_dict = json.loads(q_m.group(1))
                    q_lbl = (quality or '').lower()
                    target = (q_dict.get(q_lbl) or q_dict.get(q_lbl + 'p')
                              or q_dict.get('480p') or q_dict.get('360p') or q_dict.get('720p')
                              or next(iter(q_dict.values()), None))
                    if target:
                        return pb_m.group(1) + quote(target.replace('\\/', '/'), safe='')
                except Exception:
                    pass

            # 4) megaplay.buzz layout: getSources JSON endpoint
            # e.g. https://megaplay.buzz/stream/s-2/31069/sub -> data-id="31069"
            # LIVE (2026-08): the API keys on data-id (the file id), NOT
            # data-realid / the /stream/ URL id.
            if 'megaplay.buzz' in url.lower() or 'megaplay' in url.lower():
                data_id_m = re.search(r'data-id=["\'](\d+)["\']', r.text)
                real_id_m = re.search(r'data-realid=["\'](\d+)["\']', r.text)
                ep_id_m = re.search(r'/stream/(?:s-\d+/)?(\d+)', url)
                stream_id = (data_id_m.group(1) if data_id_m
                             else (real_id_m.group(1) if real_id_m
                                   else (ep_id_m.group(1) if ep_id_m else None)))
                if stream_id:
                    api_url = f"https://megaplay.buzz/stream/getSources?id={stream_id}"
                    try:
                        api_h = {
                            'User-Agent': UA_DESKTOP,
                            'Referer': url,
                            'X-Requested-With': 'XMLHttpRequest'
                        }
                        r_api = session.get(api_url, headers=api_h, timeout=10)
                        if r_api and r_api.status_code == 200:
                            data = r_api.json()
                            file_url = data.get('sources', {}).get('file') if isinstance(data.get('sources'), dict) else None
                            if not file_url and data.get('enc'):
                                try:
                                    import json as _json
                                    b64 = data['enc'].replace('-', '+').replace('_', '/')
                                    pad = len(b64) % 4
                                    if pad:
                                        b64 += '=' * (4 - pad)
                                    raw = base64.b64decode(b64)
                                    key = b'i?LMTAx0Q6,:}50U'.ljust(32, b'\0')[:32]
                                    iv = b"W0;27ToaUpl_P%'c"[:16]
                                    _plain = aes_cbc_decrypt(raw, key, iv)
                                    _obj = _json.loads(_plain.decode('utf-8', errors='ignore'))
                                    file_url = _obj.get('file')
                                except Exception as _e:
                                    safe_print(f"      [!] megaplay enc decrypt error: {_e}")
                            if file_url:
                                return file_url
                            safe_print(f"      [!] getSources returned no file: {data}")
                        else:
                            safe_print(f"      [!] getSources status={getattr(r_api, 'status_code', 'None')}")
                    except Exception as e:
                        safe_print(f"      [!] getSources error: {e}")
                else:
                    safe_print(f"      [!] megaplay: no stream_id found in URL or page")

            # 4b) sibnet.ru shell: the source is a RELATIVE path in inline JS --
            # player.src([{src: "/v/<hash>/<id>.mp4", ...}]) -- which 302s to a
            # signed CDN URL. Absolute-URL regexes never see it.
            sib_m = re.search(r'''player\.src\(\[\{src:\s*["']([^"']+)["']''', r.text)
            if sib_m and sib_m.group(1).startswith('/'):
                return urljoin('https://video.sibnet.ru/', sib_m.group(1))

            # 5) Standard source tag
            m = re.search(r'"source"\s*:\s*"([^"]+\.m3u8[^"]*)"', r.text)
            if m:
                return m.group(1)

            # 6) Packed JS payload (e.g. vkspeed.com)
            unpacked = _unpack_packed_js(r.text)
            if unpacked:
                direct = find_direct_video(unpacked)
                if direct:
                    return direct

            # 7) Raw .m3u8 URL in page body (e.g. vidmoly.biz)
            m3u8_m = re.search(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', r.text)
            if m3u8_m:
                return m3u8_m.group(0)

            return find_direct_video(r.text)
        except _http_exc_bases() as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] KisskhMegaplay: Network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] KisskhMegaplay: Resolution error: {e}")
            return None

class LightDLResolver(BaseResolver):
    @staticmethod
    def can_resolve(url: str) -> bool:
        if '/api/download/' in url:
            return False
        return 'lightdl.cc' in urlparse(url).netloc.lower()

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            parsed = urlparse(url)
            parts = [p for p in parsed.path.strip('/').split('/') if p]
            code = parts[-1] if parts else None
            if not code:
                return None
            headers = {
                'User-Agent': UA_DESKTOP,
                'Referer': url,
                'Accept': 'application/json',
            }

            r1 = None
            for attempt in range(2):
                r1 = session.get(f'https://lightdl.cc/api/files/code/{code}', headers=headers, timeout=20)
                if r1 and r1.status_code == 200:
                    break
                time.sleep(1)

            if not r1 or r1.status_code != 200:
                return None
            data1 = r1.json() if isinstance(r1.json(), dict) else {}
            file_info = data1.get('file')
            if not file_info or not isinstance(file_info, dict):
                return None
            file_id = file_info.get('id')
            if not file_id:
                return None

            r2 = None
            for attempt in range(2):
                r2 = session.post(f'https://lightdl.cc/api/files/{file_id}/download-token', headers=headers, timeout=20)
                if r2 and r2.status_code == 200:
                    break
                time.sleep(1)

            if not r2 or r2.status_code != 200:
                return None
            data2 = r2.json() if isinstance(r2.json(), dict) else {}
            return data2.get('downloadUrl')
        except _http_exc_bases() as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] LightDL: Network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] LightDL: Resolution error: {e}")
            return None

class FivePlayResolver(BaseResolver):
    @staticmethod
    def can_resolve(url: str) -> bool:
        netloc = urlparse(url).netloc.lower()
        return netloc in ('5play.cc', 'www.5play.cc')

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            headers = {'User-Agent': UA_DESKTOP, 'Referer': 'https://dramakey.cc/'}
            r = session.get(url, timeout=20, headers=headers)
            if not r or r.status_code != 200:
                return None
            return find_direct_video(r.text)
        except _http_exc_bases() as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] 5play: Network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] 5play: Resolution error: {e}")
            return None

class EmbedResolver(BaseResolver):
    KNOWN_EMBED_DOMAINS = [
        'megaplay.buzz', 'megaplay.cc',
        'tamilembed.lol',
        'embedsito.com',
    ]

    @staticmethod
    def can_resolve(url: str) -> bool:
        netloc = urlparse(url).netloc.lower()
        return any(netloc == d or netloc.endswith('.' + d) for d in EmbedResolver.KNOWN_EMBED_DOMAINS)

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            # The impersonating session goes FIRST. A bare requests.get carries no
            # UA, no cookies and no curl_cffi TLS fingerprint, so these hosts serve
            # it a challenge page or a 403 — and a ConnectionError in that call
            # re-raises out of here before any fallback can run. Plain requests is
            # only useful as a second opinion when the session itself is refused.
            headers = {'Referer': session.headers.get('Referer', '')}
            r = None
            try:
                r = session.get(url, timeout=20)
            except Exception as e:
                if _is_network_error(e):
                    raise
                r = None
            if r is None or r.status_code != 200:
                r = requests.get(url, timeout=20, headers=headers)
            if not r or r.status_code != 200:
                return None
            return find_direct_video(r.text)
        except _http_exc_bases() as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] Embed: Network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] Embed: Resolution error: {e}")
            return None

class VikingFileResolver(BaseResolver):
    @staticmethod
    def can_resolve(url: str) -> bool:
        p = urlparse(url)
        if 'vikingfile.com' not in p.netloc.lower():
            return False
        # If /d/ is in the path, allow it even if it ends with .mkv or .mp4
        # (Vikingfile mints /d/<token>/<title>.mkv which redirects to Cloudflare R2)
        if '/d/' in p.path.lower():
            return True
        # The resolved CDN link is usually ANOTHER vikingfile.com host, and
        # 'vikingfile.com' is listed in the registry's resolver_domains — so the
        # registry's `.mp4` fast-path deliberately does NOT short-circuit it, and
        # cls.resolve() re-enters here with the direct file URL we just returned.
        # That second pass finds no download page, returns None, and throws away
        # a perfectly good resolve (after GETting the movie body to look for HTML
        # in it). Refuse media paths outright, same as VidbasicResolver.
        if p.path.lower().endswith(('.mp4', '.mkv', '.webm', '.ts', '.m3u8')):
            return False
        return True

    @staticmethod
    def _page_text(session, url, headers, allow_redirects=True, max_bytes=2_000_000):
        """GET a candidate URL, but only read the body if it IS a page.

        The URL handed to us can redirect straight to the video. Reading `.text`
        on that pulls the whole movie into memory (twice — the raw buffer plus
        the decoded str) just to regex it for HTML, which on a phone is an OOM.
        So stream it, look at Content-Type first, and read at most `max_bytes`
        of markup even when the type says page (some hosts lie).

        Returns (text_or_None, final_url, response). text is None when the body
        is media — in that case final_url is the thing worth downloading."""
        r = session.get(url, timeout=15, allow_redirects=allow_redirects,
                        headers=headers, stream=True)
        final = getattr(r, 'url', None) or url
        ctype = (r.headers.get('Content-Type') or '').lower()
        # An absent Content-Type is treated as a page: the read is bounded
        # anyway, so guessing wrong costs at most max_bytes, not a whole movie.
        if ctype and not any(t in ctype for t in ('html', 'text', 'json', 'javascript')):
            try:
                r.close()
            except Exception:
                pass
            return None, final, r
        buf = b''
        try:
            for chunk in r.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                buf += chunk
                if len(buf) >= max_bytes:
                    break
        finally:
            try:
                r.close()
            except Exception:
                pass
        enc = getattr(r, 'encoding', None) or 'utf-8'
        try:
            text = buf.decode(enc, errors='replace')
        except (LookupError, TypeError):
            text = buf.decode('utf-8', errors='replace')
        return text, final, r

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            headers = {'User-Agent': UA_DESKTOP, 'Referer': 'https://www.naijavault.com/'}

            r1 = None
            for attempt in range(3):
                try:
                    r1 = session.get(url, timeout=15, allow_redirects=False, headers=headers)
                    break
                except Exception:
                    if attempt < 2:
                        time.sleep(2)
                    else:
                        raise

            loc1 = r1.headers.get('location')
            if loc1:
                if any(h in loc1.lower() for h in ('r2.cloudflarestorage.com', '.r2.dev')):
                    return loc1
                r2 = None
                for attempt in range(3):
                    try:
                        r2 = session.get(loc1, timeout=15, allow_redirects=False, headers=headers)
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
                if any(h in loc1.lower() for h in ('r2.cloudflarestorage.com', '.r2.dev')):
                    return loc1
                if any(x in loc1 for x in ['.mp4', '.mkv', 'cdn', 'download']):
                    return loc1
                # r2 was fetched with allow_redirects=False and has no location,
                # so it is the body of loc1 itself — stream it rather than
                # touching .text, which would buffer a movie if loc1 was media.
                text2, _final2, _r = VikingFileResolver._page_text(
                    session, loc1, headers, allow_redirects=False)
                if text2 is None:
                    return loc1
                cdn = find_direct_video(text2)
                return cdn if cdn else loc1

            if r1.status_code == 200:
                text1, final_url, _r = VikingFileResolver._page_text(session, url, headers)
                if final_url != url and any(x in final_url for x in ['.mp4', '.mkv', 'cdn', 'download']):
                    return final_url
                if text1 is None:
                    # Followed the redirects into a media body: that final URL
                    # IS the answer even if it doesn't carry a tell-tale token.
                    return final_url if final_url != url else None
                cdn = find_direct_video(text1)
                if cdn:
                    return cdn
                for pattern in [
                    r'https?://[^\s"\'<>]*cdn[^\s"\'<>]*\.(?:mp4|mkv)',
                    r'https?://[^\s"\'<>]+\.(?:mp4|mkv)\b',
                    r'"(https?://[^\s"\'<>]+(?:download|file)[^\s"\'<>]*)"',
                ]:
                    m = re.search(pattern, text1, re.IGNORECASE)
                    if m:
                        return m.group(0).strip('"')
            safe_print(f"      [!] VikingFile: could not resolve {url[:60]}")
            return None
        except Exception as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] VikingFile: {e}")
            return None

class LulaCloudResolver(BaseResolver):
    @staticmethod
    def can_resolve(url: str) -> bool:
        return 'lulacloud.com' in urlparse(url).netloc.lower()

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            headers = {'User-Agent': UA_DESKTOP, 'Referer': 'https://www.naijavault.com/'}

            r1 = None
            for attempt in range(3):
                try:
                    r1 = session.get(url, timeout=15, allow_redirects=False, headers=headers)
                    break
                except Exception:
                    if attempt < 2:
                        time.sleep(2)
                    else:
                        raise
                        
            loc = r1.headers.get('location')
            if loc:
                if 'lulacloud' in loc:
                    r2 = session.get(loc, timeout=15, allow_redirects=False, headers=headers)
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
            safe_print(f"      [!] LulaCloud: could not resolve {url[:60]}")
            return None
        except Exception as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] LulaCloud: {e}")
            return None

class DramaGatewayResolver(BaseResolver):
    @staticmethod
    def can_resolve(url: str) -> bool:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        path = parsed.path.lower()
        return any(domain in netloc for domain in ['dramarain.com', 'dramakey.cc']) and '/download' in path

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            parsed = urlparse(url)
            referer = f"https://{parsed.netloc}/"
            
            try:
                r = session.get(url, timeout=20, headers={'Referer': referer}, verify=False)
            except TypeError:
                r = session.get(url, timeout=20, headers={'Referer': referer})
                
            if not r or r.status_code != 200:
                return None
                
            # Method 1: JS window.location / window.location.href redirection
            m = re.search(r'''window\.location(?:\.href)?\s*=\s*['"]([^'"]+)['"]''', r.text)
            if m:
                dest = m.group(1)
                if not dest.startswith('http'):
                    dest = urljoin(url, dest)
                return dest

            # Method 2: Download button elements
            soup = BeautifulSoup(r.text, 'html.parser')
            btn = soup.find('a', class_=re.compile(r'download', re.I)) or soup.find('a', id=re.compile(r'download', re.I))
            if btn and btn.get('href'):
                dest = btn['href'].strip()
                if not dest.startswith('http'):
                    dest = urljoin(url, dest)
                return dest

            # Method 3: Locker anchors
            for a in soup.find_all('a', href=True):
                href = a['href'].strip()
                if any(x in href.lower() for x in ['waffi', 'vikingfile', 'lulacloud', 'loadedfiles', 'downloadwella']):
                    if not href.startswith('http'):
                        href = urljoin(url, href)
                    return href
            return None
        except _http_exc_bases() as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] DramaGateway: Network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] DramaGateway: Resolution error: {e}")
            return None

class NaijaVaultGatewayResolver(BaseResolver):
    @staticmethod
    def can_resolve(url: str) -> bool:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        path = parsed.path.lower()
        return 'naijavault.com' in netloc and ('/dl-' in path or '/temp/' in path)

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            # Catch redirects manually. We only need the Location header, so
            # no stream=True (that left the body unread and the pooled
            # connection leaked). Close r1 explicitly once the header is read.
            try:
                r1 = session.get(url, timeout=15, allow_redirects=False, verify=False,
                                 headers={'Referer': 'https://www.naijavault.com/'})
            except TypeError:
                r1 = session.get(url, timeout=15, allow_redirects=False,
                                 headers={'Referer': 'https://www.naijavault.com/'})

            loc = r1.headers.get('location')
            temp_url = loc if loc else url
            try:
                r1.close()
            except Exception:
                pass

            try:
                r2 = session.get(temp_url, timeout=15, verify=False,
                                 headers={'Referer': 'https://www.naijavault.com/'})
            except TypeError:
                r2 = session.get(temp_url, timeout=15,
                                 headers={'Referer': 'https://www.naijavault.com/'})
                
            if not r2 or r2.status_code != 200:
                return None
                
            soup = BeautifulSoup(r2.text, 'html.parser')
            
            # Method A: Class download-btn / download button elements
            btn = soup.find('a', class_=re.compile(r'download', re.I)) or soup.find('a', id=re.compile(r'download', re.I))
            if btn and btn.get('href'):
                dest = btn['href'].strip()
                if not dest.startswith('http'):
                    dest = urljoin(temp_url, dest)
                return dest
                
            # Method B: Regex search downloadURL / window.location script variables
            m = re.search(r'''(?:(?:var\s+)?downloadURL|window\.location(?:\.href)?)\s*=\s*['"]([^'"]+)['"]''', r2.text)
            if m:
                dest = m.group(1)
                if not dest.startswith('http'):
                    dest = urljoin(temp_url, dest)
                return dest
                
            # Method C: Find all known locker anchors
            known_lockers = ['vikingfile.com', 'lulacloud.com', 'loadedfiles', 'downloadwella.com', 'wetafiles.com', 'pixeldrain.com', 'waffi.cloud', 'wildshare.net']
            for a in soup.find_all('a', href=True):
                href = a['href'].strip()
                if any(x in href.lower() for x in known_lockers):
                    if not href.startswith('http'):
                        href = urljoin(temp_url, href)
                    return href
            return None
        except _http_exc_bases() as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] NaijaVaultGateway: Network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] NaijaVaultGateway: Resolution error: {e}")
            return None

class PixelDrainResolver(BaseResolver):
    @staticmethod
    def can_resolve(url: str) -> bool:
        return 'pixeldrain.com' in urlparse(url).netloc.lower()

    @staticmethod
    def resolve(url: str, session) -> str:
        # pixeldrain exposes a stable direct-download API: the /u/{id} share
        # slug maps 1:1 to /api/file/{id}?download. Also accept an already-built
        # api URL so a re-resolve is a clean no-op (returns itself).
        try:
            m = re.search(r'pixeldrain\.com/(?:u|api/file)/([A-Za-z0-9]+)', url)
            if not m:
                return None
            return f'https://pixeldrain.com/api/file/{m.group(1)}?download'
        except Exception as e:
            safe_print(f"      [!] PixelDrain: {e}")
            return None

class PlutoMoviesResolver(BaseResolver):
    @staticmethod
    def can_resolve(url: str) -> bool:
        return 'plutomovies.com' in urlparse(url).netloc.lower()

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            r = session.get(url, timeout=20, headers={'Referer': 'https://plutomovies.com/'})
            if not r or r.status_code != 200:
                return None
                
            text = r.text
            # Extract PlutoMovies download scripts
            # Primary: downloadButton onclick handler
            m = re.search(
                r"getElementById\('downloadButton'\)\.onclick\s*=\s*function\(\)\s*\{"
                r"\s*location\.href\s*=\s*'(https://[^']+)'",
                text, re.DOTALL
            )
            if m:
                return m.group(1)
            # Fallback: generic window.location.href
            m = re.search(r"window\.location\.href\s*=\s*['\"]([^'\"]+)['\"]", text)
            if m:
                return m.group(1)
            # Kotlin parity (9b5eab9/689ffee): /series/ episode pages embed a
            # plain dl.plutomovies.com anchor (live-verified 2026-08-22).
            a = re.search(r'href="(https://[^"]*dl\.plutomovies\.com[^"]*)"', text)
            if a:
                return a.group(1)
            # Directory pages (series hubs / season listings) have no download
            # link, only child /series/ pages: descend into the most specific
            # child so the registry recursion walks hub -> season -> episode.
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
        except _http_exc_bases() as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] PlutoMovies: Network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] PlutoMovies: Resolution error: {e}")
            return None

# --- SHARED HELPERS FOR MIRROR-HOST RESOLVERS ---

def _unpack_packed_js(text):
    """Deobfuscate Dean Edwards' p.a.c.k.e.r payloads
    (`eval(function(p,a,c,k,e,d){...}('payload',radix,count,'a|b|c'.split('|')))`)
    used by mixdrop/streamwish/vidhide players to hide the video URL. Returns the
    unpacked source string, or '' if the text isn't packed / doesn't parse.

    The packer replaces each token with a base-`radix` index into the `k` word
    list; we rebuild that mapping and substitute every `\\b\\w+\\b` token back."""
    try:
        m = re.search(
            r"\}\s*\(\s*'(.*?)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'(.*?)'\.split\('\|'\)",
            text, re.DOTALL)
        if not m:
            return ''
        payload, radix, count, words = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split('|')
        # Payload uses \' and \\ escapes in the JS string literal - unescape them.
        payload = payload.replace("\\'", "'").replace('\\\\', '\\')

        def _base_n(n, base):
            digits = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
            if n == 0:
                return '0'
            out = ''
            while n > 0:
                out = digits[n % base] + out
                n //= base
            return out

        table = {}
        for i in range(count):
            key = _base_n(i, radix)
            table[key] = words[i] if i < len(words) and words[i] else key

        return re.sub(r'\b\w+\b', lambda mo: table.get(mo.group(0), mo.group(0)), payload)
    except Exception:
        return ''


_DEAD_FILE_MARKERS = (
    'file is no longer available', 'file was deleted', 'file deleted',
    'file not found', 'video not found', 'this file was deleted',
    'has been removed', 'no longer exists',
)

def _looks_dead(text):
    """True if an embed page is a tombstone for an expired/deleted upload."""
    low = (text or '').lower()
    return any(marker in low for marker in _DEAD_FILE_MARKERS)


def _find_hls_or_mp4(text):
    """Pull the first .m3u8 (preferred) or .mp4 URL out of player JS/HTML.
    Looks at `file:`/`src:`/`sources:` assignments first, then any bare URL."""
    for pat in (
        r'''["']?(?:file|src)["']?\s*:\s*["'](https?://[^"']+\.m3u8[^"']*)["']''',
        r'''["']?(?:file|src)["']?\s*:\s*["'](https?://[^"']+\.mp4[^"']*)["']''',
    ):
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return find_direct_video(text)


# --- MIRROR-HOST RESOLVERS (asianc.id / vidb.top server list) ---
class DoodstreamResolver(BaseResolver):
    """dood.wf & friends. The embed page hides the stream behind a `/pass_md5/`
    token endpoint: GET the pass_md5 path (with the embed as Referer) to receive
    a URL prefix, then append 10 random chars + `?token=<slug>&expiry=<ms>` to
    build a short-lived direct .mp4. Confirmed live: returns 206 video/mp4."""
    _HOSTS = ('dood.', 'doodstream.', 'ds2play.com', 'dooood.com', 'd0000d.com',
              'd000d.com', 'vidply.com', 'do0od.com', 'dood.re')

    @staticmethod
    def can_resolve(url: str) -> bool:
        net = urlparse(url).netloc.lower()
        return any(h in net for h in DoodstreamResolver._HOSTS)

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            parsed = urlparse(url)
            original_host = parsed.netloc.lower()
            candidate_hosts = []
            if original_host:
                candidate_hosts.append(original_host)
            for h in ('doodstream.com', 'dood.to', 'd000d.com'):
                if h not in candidate_hosts:
                    candidate_hosts.append(h)

            path = parsed.path.replace('/d/', '/e/')

            for host in candidate_hosts:
                base = f'https://{host}'
                embed = f'{base}{path}'
                try:
                    r = session.get(embed, timeout=15,
                                    headers={'Referer': base + '/', 'User-Agent': UA_DESKTOP})
                    if not r or r.status_code != 200:
                        continue
                    if _looks_dead(r.text):
                        safe_print("      [!] Doodstream: file expired/deleted")
                        return None
                    m = re.search(r"(/pass_md5/[^'\"\s]+)", r.text)
                    if not m:
                        continue
                    pass_url = base + m.group(1)
                    r2 = session.get(pass_url, timeout=15,
                                     headers={'Referer': embed, 'User-Agent': UA_DESKTOP})
                    if not r2 or r2.status_code != 200 or not r2.text.strip():
                        continue
                    prefix = r2.text.strip()
                    token = pass_url.rstrip('/').split('/')[-1]
                    import random as _rnd, string as _str
                    rand = ''.join(_rnd.choice(_str.ascii_letters + _str.digits) for _ in range(10))
                    expiry = int(time.time() * 1000)
                    return f'{prefix}{rand}?token={token}&expiry={expiry}'
                except Exception:
                    continue

            return None
        except _http_exc_bases() as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] Doodstream: network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] Doodstream: {e}")
            return None


class MixdropResolver(BaseResolver):
    """mixdrop.ps & mirrors. The embed serves a packed p.a.c.k.e.r script; after
    unpacking, `MDCore.wurl="//host/....mp4?..."` is the direct file. Confirmed
    live: returns 206 video/mp4."""
    _HOSTS = ('mixdrop.', 'mixdrp.', 'mdfx9dc8n.net', 'mixdroop.')

    @staticmethod
    def can_resolve(url: str) -> bool:
        net = urlparse(url).netloc.lower()
        return any(h in net for h in MixdropResolver._HOSTS)

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            parsed = urlparse(url)
            base = f'{parsed.scheme}://{parsed.netloc}'
            embed = url.replace('/f/', '/e/')
            headers = {'Referer': base + '/', 'User-Agent': UA_DESKTOP}
            r = None
            try:
                r = session.get(embed, headers=headers, timeout=20)
            except requests.exceptions.SSLError:
                r = session.get(embed, headers=headers, timeout=20, verify=False)
            except Exception as e:
                if 'ssl' in type(e).__name__.lower() or 'certificate' in str(e).lower():
                    try:
                        r = session.get(embed, headers=headers, timeout=20, verify=False)
                    except Exception:
                        r = safe_get(session, embed, referer=base + '/', timeout=20)
                else:
                    r = safe_get(session, embed, referer=base + '/', timeout=20)
            if not r or r.status_code != 200:
                return None
            if _looks_dead(r.text):
                safe_print("      [!] Mixdrop: file expired/deleted")
                return None
            unpacked = _unpack_packed_js(r.text) or r.text
            m = re.search(r'wurl\s*=\s*["\'](//[^"\']+|https?://[^"\']+)["\']', unpacked)
            if not m:
                return None
            wurl = m.group(1)
            if wurl.startswith('//'):
                wurl = 'https:' + wurl
            return wurl
        except _http_exc_bases() as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] Mixdrop: network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] Mixdrop: {e}")
            return None


class StreamwishResolver(BaseResolver):
    """streamwish (hglink.to & mirrors). The /e/ embed wraps the player in a JS
    loader shell that requires client-side execution. Transform to sfastwish.com
    /e/ (or embedwish.com) to bypass that shell and get the raw player HTML with
    a Dean Edwards packed script containing `jwplayer` `links.hls2` master.m3u8.
    Returns an .m3u8 (yt-dlp then selects quality via the height-capped format)."""
    _HOSTS = ('hglink.to', 'streamwish.', 'strwsh.', 'stwish.', 'wishembed.',
              'mwish.', 'awish.', 'sfastwish.', 'swishsrv.', 'ajmidyad', 'khadhnayad',
              'obeywish.com', 'jodwish.com', 'streamwish.to', 'embedwish.', 'filelions.')

    @staticmethod
    def can_resolve(url: str) -> bool:
        net = urlparse(url).netloc.lower()
        return any(h in net for h in StreamwishResolver._HOSTS)

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            parsed = urlparse(url)
            # Extract video ID from path. Streamwish embed URLs typically carry
            # the video ID as the last path segment: /e/<id> or /v/<id>.
            vid = parsed.path.rstrip('/').split('/')[-1]
            if not vid or len(vid) < 6:
                safe_print("      [!] Streamwish: no video ID in URL")
                return None

            # Transform to sfastwish.com /e/ path (or embedwish.com). These domains
            # bypass the obfuscated main.js loader and serve the raw player HTML
            # containing a Dean Edwards packed script with the jwplayer config.
            candidates = [
                url,  # active embed URL first
                f'https://sfastwish.com/e/{vid}',
                f'https://embedwish.com/e/{vid}',
            ]

            for cand in candidates:
                r = safe_get(session, cand, referer='https://asianc.id/', timeout=20)
                if not r:
                    continue
                if _looks_dead(r.text):
                    safe_print("      [!] Streamwish: file expired/deleted")
                    return None

                # The raw player HTML has a Dean Edwards packed script. Unpack it
                # to reveal jwplayer config containing `links: { "hls2": "...m3u8" }`.
                unpacked = _unpack_packed_js(r.text) or r.text
                if _looks_dead(unpacked):
                    safe_print("      [!] Streamwish: file expired/deleted")
                    return None

                # Look for jwplayer `links` object with `hls2` key first (preferred),
                # then fall back to generic HLS/mp4 extraction.
                hls2 = re.search(r'''["']?hls2["']?\s*:\s*["'](https?://[^"']+\.m3u8[^"']*)["']''', unpacked)
                if hls2:
                    return hls2.group(1)

                direct = _find_hls_or_mp4(unpacked)
                if direct:
                    return direct

            return None
        except _http_exc_bases() as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] Streamwish: network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] Streamwish: {e}")
            return None


class VidhideResolver(BaseResolver):
    """vidhide (minochinos.com & mirrors). Same player family as streamwish -
    the source is a `sources:[{file:"...m3u8"}]` assignment, sometimes inside a
    packed script. Many asianc uploads are expired; those are detected and fail
    cleanly (None) rather than returning a tombstone page.

    filelions network: frontend domains rotate (vidhidepro.com -> vidhidefast.com,
    2026-08) and every frontend serves the same file-id space under the same
    /e/ /f/ /d/ paths. A link on a dead frontend (522 / conn error) is retried
    on the live mirrors instead of failing outright."""
    _HOSTS = ('vidhidefast.', 'minochinos.com', 'vidhide.', 'vidhidepro.',
              'vidhidevip.', 'filelions.', 'vid-guard.', 'nining.',
              'peytonepre.com', 'techradar.ink', 'ryderjet.com')
    _MIRROR_HOSTS = ('vidhide.com', 'minochinos.com', 'vidhidefast.com',
                     'vidhidevip.com', 'vidhidepro.com', 'filelions.to', 'ryderjet.com')
    _LAST_WORKING_HOST = None

    @classmethod
    def _candidate_hosts(cls, url):
        """Last working host first, then the pasted host, then known mirrors."""
        netloc = urlparse(url).netloc.lower()
        hosts = []
        if cls._LAST_WORKING_HOST:
            hosts.append(cls._LAST_WORKING_HOST)
        if netloc not in hosts:
            hosts.append(netloc)
        for h in cls._MIRROR_HOSTS:
            if h not in hosts:
                hosts.append(h)
        return hosts

    @staticmethod
    def can_resolve(url: str) -> bool:
        net = urlparse(url).netloc.lower()
        return any(h in net for h in VidhideResolver._HOSTS)

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            parsed = urlparse(url)
            for host in VidhideResolver._candidate_hosts(url):
                cand = url if host == parsed.netloc.lower() else urlunparse(
                    (parsed.scheme, host, parsed.path, parsed.params,
                     parsed.query, parsed.fragment))
                r = safe_get(session, cand, referer=f'{parsed.scheme}://{host}/', timeout=20)
                if not r:
                    continue
                if _looks_dead(r.text):
                    continue
                unpacked = _unpack_packed_js(r.text) or r.text
                m = re.search(
                    r'sources\s*:\s*\[\s*\{[^}]*?file\s*:\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']',
                    unpacked)
                if m:
                    VidhideResolver._LAST_WORKING_HOST = host
                    return m.group(1)
                direct = _find_hls_or_mp4(unpacked)
                if direct:
                    VidhideResolver._LAST_WORKING_HOST = host
                    return direct
            return None
        except _http_exc_bases() as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] Vidhide: network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] Vidhide: {e}")
            return None

# --- BLOGGER (batchexecute RPC) ---

# itag → quality mapping. Ordered ascending so the data-saver loop can pick
# the FIRST match (lowest quality) that resolves.
_BLOGGER_ITAG_MAP = {
    '18': '360p',
    '22': '720p',
    '37': '1080p',
}
# Preferred quality order: ascending (data-saver first)
_BLOGGER_QUALITY_ORDER = ['18', '22', '37']


class BloggerResolver(BaseResolver):
    """Resolve blogger.com/video.g?token=… URLs via Blogger's internal
    batchexecute RPC endpoint.

    yt-dlp's built-in Blogger extractor is broken ('Unable to extract JSON
    data'), so this resolver bypasses it entirely. The approach:

      1. GET the video.g page → scrape FdrFJe (f.sid) and bl (build label)
      2. POST /_/BloggerVideoPlayerUi/data/batchexecute with WcwnYd RPC
      3. Parse googlevideo.com direct MP4 URLs from the response
      4. Return the lowest-quality URL (itag 18 = 360p preferred)

    The returned URL is a direct progressive MP4 on googlevideo.com, so
    download_file routes it through aria2c (fast, resumable, no yt-dlp).
    """

    @staticmethod
    def can_resolve(url: str) -> bool:
        low = url.lower()
        return 'blogger.com' in low and ('video.g' in low or 'token=' in low)

    @staticmethod
    def resolve(url: str, session) -> str:
        import json as _json
        try:
            # ── Step 1: Fetch the video page and extract bootstrap values ──
            headers = {
                'User-Agent': UA_DESKTOP,
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
            }
            r = session.get(url, timeout=20, headers=headers)
            if not r or r.status_code != 200:
                safe_print(f"      [!] Blogger: Failed to load page (HTTP {r.status_code if r else 'N/A'})")
                return None

            html = r.text

            # Extract f.sid (FdrFJe)
            fsid_m = re.search(r'"FdrFJe":"([^"]+)"', html)
            if not fsid_m:
                safe_print("      [!] Blogger: Could not extract f.sid (FdrFJe)")
                return None
            f_sid = fsid_m.group(1)

            # Extract bl (build label)
            bl_m = re.search(r'boq_bloggeruiserver_[^\'"", ]+', html)
            if not bl_m:
                safe_print("      [!] Blogger: Could not extract build label (bl)")
                return None
            bl = bl_m.group(0)

            # Extract the token from the URL
            token_m = re.search(r'[?&]token=([^&]+)', url)
            if not token_m:
                safe_print("      [!] Blogger: No token found in URL")
                return None
            token = token_m.group(1)

            # ── Step 2: POST to batchexecute RPC ──
            rpc_url = (
                f"https://www.blogger.com/_/BloggerVideoPlayerUi/data/batchexecute"
                f"?rpcids=WcwnYd"
                f"&source-path=%2Fvideo.g"
                f"&f.sid={quote(f_sid)}"
                f"&bl={quote(bl)}"
                f"&hl=en-US"
                f"&rt=c"
            )

            # Build the f.req payload exactly as the C# script does
            f_req = f'[[["WcwnYd","[\\"{token}\\"]",null,"generic"]]]'

            rpc_headers = {
                'User-Agent': UA_DESKTOP,
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': 'https://www.blogger.com/',
                'Origin': 'https://www.blogger.com',
                'X-Same-Domain': '1',
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Pragma': 'no-cache',
                'Cache-Control': 'no-cache',
            }

            r2 = session.post(rpc_url, data={'f.req': f_req},
                              timeout=20, headers=rpc_headers)
            if not r2 or r2.status_code != 200:
                safe_print(f"      [!] Blogger: batchexecute failed (HTTP {r2.status_code if r2 else 'N/A'})")
                return None

            body = r2.text

            # ── Step 3: Extract googlevideo.com URLs ──
            raw_urls = re.findall(
                r'"(https://[^"]+googlevideo\.com[^"]+)"', body)
            if not raw_urls:
                safe_print("      [!] Blogger: No googlevideo URLs found in RPC response")
                return None

            # Unescape unicode and backslash entities. The batchexecute
            # response double-escapes (\\\\u003d), so after replacing the
            # unicode escapes bare backslashes remain. Strip them all.
            def _unescape(u):
                u = u.replace('\\u003d', '=')
                u = u.replace('\\u0026', '&')
                u = u.replace('\\/', '/')
                u = u.replace('\\', '')
                return u

            urls = list(dict.fromkeys(_unescape(u) for u in raw_urls))

            # ── Step 4: Pick the lowest quality (data-saver) ──
            # Build itag → url map
            itag_map = {}
            for u in urls:
                itag_m = re.search(r'[?&]itag=(\d+)', u)
                if itag_m:
                    itag_map[itag_m.group(1)] = u

            # Pick in ascending quality order
            for itag in _BLOGGER_QUALITY_ORDER:
                if itag in itag_map:
                    quality = _BLOGGER_ITAG_MAP.get(itag, f'itag {itag}')
                    safe_print(f"      [*] Blogger: Resolved {quality} direct MP4 (bypassing yt-dlp)")
                    return itag_map[itag]

            # Fallback: return the first URL if no known itag matched
            if urls:
                safe_print("      [*] Blogger: Resolved direct MP4 (unknown quality)")
                return urls[0]

            return None

        except _http_exc_bases() as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] Blogger: Network error: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] Blogger: Resolution error: {e}")
            return None


class _WasmInterpreter:
    def __init__(self, wasm_bytes):
        self.bytes = wasm_bytes
        self.memory = bytearray(128 * 65536)
        self.globals = [0] * 16
        self.functions = []
        self._deadline = None
        self._parse()

    def _read_leb128_u(self, data, offset):
        val = 0; shift = 0
        while True:
            b = data[offset]; offset += 1
            val |= (b & 0x7f) << shift; shift += 7
            if not (b & 0x80): break
        return val, offset

    def _read_leb128_s(self, data, offset):
        val = 0; shift = 0
        while True:
            b = data[offset]; offset += 1
            val |= (b & 0x7f) << shift; shift += 7
            if not (b & 0x80): break
        if shift < 32 and (b & 0x40):
            val |= (~0 << shift)
        return val, offset

    def _parse(self):
        pos = 8
        sections = {}
        while pos < len(self.bytes):
            sec_id = self.bytes[pos]; pos += 1
            size, pos = self._read_leb128_u(self.bytes, pos)
            sections[sec_id] = self.bytes[pos:pos+size]
            pos += size

        if 11 in sections:
            raw = sections[11]
            idx = 0
            count, idx = self._read_leb128_u(raw, idx)
            for _ in range(count):
                flags = raw[idx]; idx += 1
                mem_offset = 0
                if flags == 0:
                    idx += 1
                    mem_offset, idx = self._read_leb128_s(raw, idx)
                    idx += 1
                data_len, idx = self._read_leb128_u(raw, idx)
                self.memory[mem_offset:mem_offset+data_len] = raw[idx:idx+data_len]
                idx += data_len

        if 10 in sections:
            raw = sections[10]
            idx = 0
            count, idx = self._read_leb128_u(raw, idx)
            for _ in range(count):
                fn_size, idx = self._read_leb128_u(raw, idx)
                fn_body = raw[idx:idx+fn_size]
                idx += fn_size
                self.functions.append(fn_body)

    def decrypt(self, ptr, length, deadline=None):
        return self.call_func(4, [ptr, length], deadline)

    def call_func(self, fn_idx, args, deadline=None):
        body = self.functions[fn_idx]
        idx = 0
        local_groups, idx = self._read_leb128_u(body, idx)
        num_locals = len(args)
        locals_list = list(args)
        for _ in range(local_groups):
            cnt, idx = self._read_leb128_u(body, idx)
            t = body[idx]; idx += 1
            num_locals += cnt
            locals_list.extend([0] * cnt)

        op_bytes = body[idx:]
        stack = []
        return self._exec_block(op_bytes, 0, locals_list, stack, deadline)

    def _exec_block(self, code, pc, locals_val, stack, deadline=None):
        def i32(val): return val & 0xffffffff
        def s32(val):
            val = val & 0xffffffff
            return val if val < 0x80000000 else val - 0x100000000
        def rotr32(val, count):
            count %= 32
            return ((val >> count) | (val << (32 - count))) & 0xffffffff
        def rotl32(val, count):
            count %= 32
            return ((val << count) | (val >> (32 - count))) & 0xffffffff

        blocks = []
        ops = 0
        while pc < len(code):
            ops += 1
            if deadline is not None and not ops % 65536 and time.time() > deadline:
                raise RuntimeError("wasm interpreter exceeded its time budget")
            op = code[pc]; pc += 1
            if op in (0x00, 0x01):
                pass
            elif op == 0x02:
                pc += 1
                blocks.append(('block', pc))
            elif op == 0x03:
                pc += 1
                blocks.append(('loop', pc))
            elif op == 0x04:
                pc += 1
                cond = stack.pop()
                if cond != 0:
                    blocks.append(('if_true', pc))
                else:
                    depth = 1
                    while pc < len(code) and depth > 0:
                        b = code[pc]; pc += 1
                        if b in (0x02, 0x03, 0x04): depth += 1
                        elif b == 0x05 and depth == 1: break
                        elif b == 0x0b: depth -= 1
                    blocks.append(('if_false', pc))
            elif op == 0x05:
                depth = 1
                while pc < len(code) and depth > 0:
                    b = code[pc]; pc += 1
                    if b in (0x02, 0x03, 0x04): depth += 1
                    elif b == 0x0b: depth -= 1
            elif op == 0x0b:
                if blocks:
                    blocks.pop()
                else:
                    break
            elif op == 0x0c:
                target_depth, pc = self._read_leb128_u(code, pc)
                for _ in range(target_depth + 1):
                    btype, bpc = blocks.pop()
                    if btype == 'loop':
                        pc = bpc
                        blocks.append(('loop', bpc))
                        break
            elif op == 0x0d:
                target_depth, pc = self._read_leb128_u(code, pc)
                cond = stack.pop()
                if cond != 0:
                    for _ in range(target_depth + 1):
                        btype, bpc = blocks.pop()
                        if btype == 'loop':
                            pc = bpc
                            blocks.append(('loop', bpc))
                            break
            elif op == 0x10:
                callee, pc = self._read_leb128_u(code, pc)
                param_counts = {0: 2, 1: 2, 2: 2, 3: 1, 4: 2}
                num_p = param_counts.get(callee, 0)
                call_args = [stack.pop() for _ in range(num_p)][::-1]
                ret = self.call_func(callee, call_args)
                if ret is not None:
                    stack.append(ret)
            elif op == 0x1a:
                if stack: stack.pop()
            elif op == 0x20:
                lidx, pc = self._read_leb128_u(code, pc)
                stack.append(locals_val[lidx])
            elif op == 0x21:
                lidx, pc = self._read_leb128_u(code, pc)
                locals_val[lidx] = stack.pop()
            elif op == 0x22:
                lidx, pc = self._read_leb128_u(code, pc)
                locals_val[lidx] = stack[-1]
            elif op == 0x23:
                gidx, pc = self._read_leb128_u(code, pc)
                stack.append(self.globals[gidx])
            elif op == 0x24:
                gidx, pc = self._read_leb128_u(code, pc)
                self.globals[gidx] = stack.pop()
            elif op == 0x28:
                align, pc = self._read_leb128_u(code, pc)
                offset, pc = self._read_leb128_u(code, pc)
                base_addr = stack.pop() + offset
                val = struct.unpack('<i', self.memory[base_addr:base_addr+4])[0]
                stack.append(val)
            elif op == 0x2d:
                align, pc = self._read_leb128_u(code, pc)
                offset, pc = self._read_leb128_u(code, pc)
                base_addr = stack.pop() + offset
                stack.append(self.memory[base_addr])
            elif op == 0x36:
                align, pc = self._read_leb128_u(code, pc)
                offset, pc = self._read_leb128_u(code, pc)
                val = stack.pop()
                base_addr = stack.pop() + offset
                self.memory[base_addr:base_addr+4] = struct.pack('<I', i32(val))
            elif op == 0x3a:
                align, pc = self._read_leb128_u(code, pc)
                offset, pc = self._read_leb128_u(code, pc)
                val = stack.pop()
                base_addr = stack.pop() + offset
                self.memory[base_addr] = val & 0xff
            elif op == 0x3f:
                pc += 1
                stack.append(len(self.memory) // 65536)
            elif op == 0x40:
                pc += 1
                if stack: stack.pop()
                stack.append(128)
            elif op == 0x41:
                val, pc = self._read_leb128_s(code, pc)
                stack.append(val)
            elif op == 0x45:
                a = stack.pop(); stack.append(1 if a == 0 else 0)
            elif op == 0x46:
                b = stack.pop(); a = stack.pop(); stack.append(1 if a == b else 0)
            elif op == 0x47:
                b = stack.pop(); a = stack.pop(); stack.append(1 if a != b else 0)
            elif op == 0x48:
                b = s32(stack.pop()); a = s32(stack.pop()); stack.append(1 if a < b else 0)
            elif op == 0x49:
                b = i32(stack.pop()); a = i32(stack.pop()); stack.append(1 if a < b else 0)
            elif op == 0x4a:
                b = s32(stack.pop()); a = s32(stack.pop()); stack.append(1 if a > b else 0)
            elif op == 0x4b:
                b = i32(stack.pop()); a = i32(stack.pop()); stack.append(1 if a > b else 0)
            elif op == 0x4c:
                b = s32(stack.pop()); a = s32(stack.pop()); stack.append(1 if a <= b else 0)
            elif op == 0x4d:
                b = i32(stack.pop()); a = i32(stack.pop()); stack.append(1 if a <= b else 0)
            elif op == 0x4e:
                b = s32(stack.pop()); a = s32(stack.pop()); stack.append(1 if a >= b else 0)
            elif op == 0x4f:
                b = i32(stack.pop()); a = i32(stack.pop()); stack.append(1 if a >= b else 0)
            elif op == 0x6a:
                b = stack.pop(); a = stack.pop(); stack.append(s32(a + b))
            elif op == 0x6b:
                b = stack.pop(); a = stack.pop(); stack.append(s32(a - b))
            elif op == 0x6c:
                b = stack.pop(); a = stack.pop(); stack.append(s32(a * b))
            elif op == 0x6e:
                b = i32(stack.pop()); a = i32(stack.pop()); stack.append(i32(a // b) if b != 0 else 0)
            elif op == 0x71:
                b = stack.pop(); a = stack.pop(); stack.append(a & b)
            elif op == 0x72:
                b = stack.pop(); a = stack.pop(); stack.append(a | b)
            elif op == 0x73:
                b = stack.pop(); a = stack.pop(); stack.append(a ^ b)
            elif op == 0x74:
                b = stack.pop() & 31; a = stack.pop(); stack.append(s32((a & 0xffffffff) << b))
            elif op == 0x75:
                b = stack.pop() & 31; a = s32(stack.pop()); stack.append(a >> b)
            elif op == 0x76:
                b = stack.pop() & 31; a = i32(stack.pop()); stack.append(a >> b)
            elif op == 0x77:
                b = stack.pop(); a = i32(stack.pop()); stack.append(rotl32(a, b))
            elif op == 0x78:
                b = stack.pop(); a = i32(stack.pop()); stack.append(rotr32(a, b))

        return stack[-1] if stack else None


# --- node binary lookup for the vidsrc wasm decryptor ---
_NODE_PATH = None
_NODE_PROBED = False


def _find_node():
    """Locate a usable node binary once per process. The vidsrc decryptor
    wasm runs in <1s under node but can take minutes under the pure-Python
    interpreter, so a PATH that hides node used to silently push every
    encrypted stream_urls resolve into the slow fallback."""
    global _NODE_PATH, _NODE_PROBED
    if _NODE_PROBED:
        return _NODE_PATH
    _NODE_PROBED = True
    import shutil
    cand = shutil.which('node')
    if not cand:
        for p in (r'C:\Tools\nodejs\node.exe',
                  r'C:\Program Files\nodejs\node.exe',
                  r'C:\Program Files (x86)\nodejs\node.exe',
                  '/usr/local/bin/node', '/usr/bin/node'):
            if os.path.isfile(p):
                cand = p
                break
    if cand:
        try:
            chk = subprocess.run([cand, '-e', 'process.exit(0)'],
                                 capture_output=True, timeout=5)
            if chk.returncode == 0:
                _NODE_PATH = cand
        except Exception:
            _NODE_PATH = None
    return _NODE_PATH


class VidsrcResolver(BaseResolver):
    """
    Resolves Vidsrc stream embeds (vidsrc.mov, vsembed.ru, cloudorchestranova.com,
    data.vidsrcme.ru, nepu.gd/watch/...) into raw .m3u8 HLS master playlist URLs.
    Supports Node.js fast-path with an embedded pure-Python WASM fallback.
    """
    @staticmethod
    def can_resolve(url: str) -> bool:
        low = (url or '').lower()
        return any(dom in low for dom in [
            'vidsrc.mov', 'vidsrc.me', 'vidsrc.cc', 'vsembed.ru',
            'cloudorchestranova.com', 'data.vidsrcme.ru', 'nepu.gd/watch', 'nepu.to/watch'
        ])

    @staticmethod
    def resolve(url: str, session) -> str:
        # Wall-clock budget for the whole chain (iframe -> api.php -> wasm ->
        # decrypt -> token). ResolverRegistry wraps us in a 3-attempt retry,
        # so unbounded internal timeouts used to stack into 3+ minute
        # "resolves" the caller had already abandoned and re-run.
        deadline = time.time() + 45.0

        def _tleft():
            return deadline - time.time()

        if 'nepu.' in url:
            try:
                url = url.replace('nepu.to', 'nepu.gd')
                headers = {'User-Agent': UA_DESKTOP, 'Referer': 'https://nepu.gd/', 'Cookie': 'hv=1'}
                r = session.get(url, headers=headers,
                                timeout=max(3, min(10, _tleft())))
                if r and r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'html.parser')
                    iframe = soup.find('iframe', id='playerFrame') or soup.find('iframe', src=re.compile(r'vidsrc'))
                    if iframe and iframe.get('src'):
                        url = iframe['src']
            except Exception:
                pass

        m_type = 'tv' if '/tv/' in url else 'movie'
        m = re.search(r'/(?:movie|tv)/(\d+)(?:/(\d+)/(\d+))?', url)
        if not m:
            safe_print("      [!] vidsrc: no tmdb id in URL")
            return None

        tmdb_id = m.group(1)
        season = m.group(2)
        episode = m.group(3)

        if m_type == 'tv' and season and episode:
            api_url = f"https://data.vidsrcme.ru/api.php?type=tv&tmdb={tmdb_id}&season={season}&episode={episode}&stream_urls"
        else:
            api_url = f"https://data.vidsrcme.ru/api.php?type=movie&tmdb={tmdb_id}&stream_urls"

        headers = {'User-Agent': UA_DESKTOP, 'Referer': 'https://cloudorchestranova.com/'}
        # data.vidsrcme.ru is Cloudflare-fronted and flaky (verified
        # 2026-08-22: api.php occasionally takes 6s+, wasm.php stalls 20s+).
        # Two tries, but both gated on the wall-clock budget so a stalled
        # host burns at most ~40s here instead of 20s+20s on every
        # registry retry attempt.
        data = None
        for attempt in (0, 1):
            if attempt and _tleft() < 8:
                break
            try:
                r = session.get(api_url, headers=headers,
                                timeout=max(3, min(20, _tleft())))
                data = r.json()
                break
            except Exception:
                if not attempt and _tleft() > 8:
                    time.sleep(0.5)

        if data is None:
            safe_print("      [!] vidsrc: api.php unreachable or non-JSON")
            return None

        stream_data = data.get('data', {}).get('stream_urls')
        if not stream_data:
            safe_print("      [!] vidsrc: api.php returned no stream_urls")
            return None

        if isinstance(stream_data, list):
            return stream_data[0] if stream_data else None

        enc_b64 = stream_data
        wasm_url = data.get('vs', {}).get('wasm_url')
        if not wasm_url:
            safe_print("      [!] vidsrc: api.php returned no wasm_url")
            return None

        wasm_bytes = None
        for attempt in (0, 1):
            if attempt and _tleft() < 8:
                break
            try:
                wasm_bytes = session.get(wasm_url, headers=headers,
                                         timeout=max(3, min(20, _tleft()))).content
                if wasm_bytes:
                    break
            except Exception:
                if not attempt and _tleft() > 8:
                    time.sleep(0.5)
        if not wasm_bytes:
            safe_print("      [!] vidsrc: wasm download failed")
            return None

        # Strategy 1: Node.js fast path (if node is available).
        # NOTE: with `node -e <script> <arg1> <arg2>` the args land at
        # process.argv[1] and process.argv[2] (the -e script is NOT argv[1]).
        # The historical argv[2]/argv[3] layout read the wasm path as the
        # ciphertext and undefined as the wasm path -> fs.readFileSync
        # TypeError, so every encrypted stream_urls RESOLVE-FAILED
        # (nepu.gd verified 2026-08-22). Worse, the cleanup call
        # `os.unlink(wasm_path)` sat AFTER subprocess.run but `os` was never
        # imported in this module -- the NameError was swallowed by the bare
        # except below, so the node result was discarded EVERY time and every
        # encrypted resolve fell through to the minutes-slow interpreter.
        master = None
        node_exe = _find_node()
        if node_exe and _tleft() > 12:
            wasm_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix='.wasm', delete=False) as f:
                    f.write(wasm_bytes)
                    wasm_path = f.name

                js_code = (
                    "const fs=require('fs');"
                    "const enc=Buffer.from(process.argv[1],'base64');"
                    "const wasm=fs.readFileSync(process.argv[2]);"
                    "WebAssembly.instantiate(wasm,{}).then(i=>{"
                    "const {alloc,decrypt,memory}=i.instance.exports;"
                    "const p=alloc(enc.length);"
                    "new Uint8Array(memory.buffer,p,enc.length).set(enc);"
                    "const l=decrypt(p,enc.length);"
                    "process.stdout.write(new TextDecoder().decode(new Uint8Array(memory.buffer,p+12,l)));"
                    "}).catch(()=>{process.exit(1);});"
                )

                res = subprocess.run(
                    [node_exe, '-e', js_code, enc_b64, wasm_path],
                    capture_output=True, text=True, timeout=10
                )
                if res.returncode == 0 and res.stdout.strip():
                    urls = [u.strip() for u in res.stdout.split('\n') if u.strip().startswith('http')]
                    if urls:
                        master = urls[0]
            except Exception:
                pass
            finally:
                if wasm_path:
                    try:
                        os.unlink(wasm_path)
                    except OSError:
                        pass

        # Strategy 2: Pure Python WASM interpreter fallback (no node required).
        # Very slow on the 7KB vidsrc decryptor wasm, so it only runs when
        # node is unavailable or failed, and under the same wall-clock budget
        # (it used to run unbounded -- minutes per call).
        if not master and _tleft() > 15:
            try:
                interp = _WasmInterpreter(wasm_bytes)
                enc = base64.b64decode(enc_b64)
                ptr = 16384
                interp.memory[ptr:ptr+len(enc)] = enc
                out_len = interp.decrypt(ptr, len(enc), deadline=deadline)
                decrypted = bytes(interp.memory[ptr+12:ptr+12+out_len]).decode('utf-8', errors='ignore')
                urls = [u.strip() for u in decrypted.split('\n') if u.strip().startswith('http')]
                if urls:
                    master = urls[0]
            except Exception:
                pass

        if not master:
            safe_print("      [!] vidsrc: decrypt produced no stream URL "
                       "(node + Python interpreter both failed)")
            return None

        # Playlist URLs are CDN-gated by an IP-bound JWT issued by the origin's
        # generate.php; without it the CDN answers 401 (tokenless master fetch
        # verified 2026-08-22). A URL that already carries a token is
        # authoritative (Kotlin VidsrcResolver parity: stripping and
        # re-stamping rotates a valid token into a dead one). generate.php is
        # aggressively rate-limited (429 after ~1 hit per IP per window), so
        # tokens are cached per origin for the JWT lifetime (exp claim, ~4h).
        if "token=" in master:
            return master
        tok = _vidsrc_token_for(session, master)
        if not tok:
            # A tokenless master is CDN-401 (verified 2026-08-22). Returning
            # it fakes a resolve and burns the download on a 401; failing
            # honestly lets the caller's retry hit the token cache or catch
            # the end of the 429 window.
            safe_print("      [!] vidsrc: no JWT from generate.php - "
                       "master would 401 without it")
            return None
        if "__TOKEN__" in master:
            return master.replace("__TOKEN__", tok)
        return master + (("&" if "?" in master else "?") + "token=" + tok)


_VSRC_TOKEN_CACHE = {}   # origin -> (token, exp_epoch)
_VSRC_TOKEN_LOCK = threading.Lock()


def _vidsrc_token_for(session, master_url):
    """IP-bound JWT from <origin>/generate.php, cached per origin for its
    ~4h lifetime. generate.php 429s after ~1 hit per IP per window, so the
    fetch is serialized per process (a prefetch thread and an inline
    re-resolve were double-hitting it) and an empty answer gets exactly one
    spaced retry. Empty answers are NOT cached so the next resolve retries.
    Mirrors Kotlin VidsrcResolver's generate.php stamping."""
    try:
        origin = re.match(r'(https?://[^/]+)', master_url).group(1)
    except Exception:
        return None
    with _VSRC_TOKEN_LOCK:
        now = time.time()
        hit = _VSRC_TOKEN_CACHE.get(origin)
        if hit and hit[1] > now + 60:
            return hit[0]
        tok = ""
        for attempt in (0, 1):
            try:
                headers = {'User-Agent': UA_DESKTOP}
                r = session.get(origin + "/generate.php", headers=headers, timeout=10)
                if r and r.status_code == 200 and r.content:
                    tok = r.content.decode("utf-8", "ignore").strip()
            except Exception:
                pass
            if tok or attempt:
                break
            time.sleep(3.0)   # short 429 window; one spaced retry
        if not tok:
            return None
        exp = now + 4 * 3600
        try:
            payload = tok.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            import json
            exp = int(json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", "ignore")).get("exp", 0))
        except Exception:
            pass
        _VSRC_TOKEN_CACHE[origin] = (tok, exp)
        return tok


# --- REGISTRY ---

class ResolverRegistry:
    # host -> (host, ip_or_None, probed_at); shared across resolver passes so
    # one DNS lookup per host serves every episode in a batch. See the park
    # gate at the bottom of [resolve].
    _park_cache = {}

    RESOLVERS = [
        StreamwishResolver,
        VidhideResolver,
        DoodstreamResolver,
        MixdropResolver,
        WaffiCloudResolver,
        DownloadwellaResolver,
        LoadedfilesResolver,
        WildshareResolver,
        StreamtapeResolver,
        VidmolyResolver,
        BloggerResolver,
        VidbasicResolver,
        KissasianResolver,
        KisskhMegaplayResolver,
        LightDLResolver,
        FivePlayResolver,
        EmbedResolver,
        VikingFileResolver,
        LulaCloudResolver,
        DramaGatewayResolver,
        NaijaVaultGatewayResolver,
        NaijaPreyChainResolver,
        PlutoMoviesResolver,
        PixelDrainResolver,
        VidsrcResolver,
    ]

    @classmethod
    def get(cls, name: str):
        """Lookup resolver by name safely."""
        name_lower = name.lower()
        for r in cls.RESOLVERS:
            r_name = getattr(r, '__name__', '').lower()
            if name_lower in r_name:
                return r.resolve
        return None

    @classmethod
    def resolve(cls, url: str, session, quality=None, _depth=0) -> str:
        if _depth > 5:
            safe_print(f"      [!] Resolver depth limit reached — returning: {url[:60]}")
            return url

        # Check if already a direct download link (excluding resolver domains that append filenames)
        # Match on the path only, so links with query strings (…/file.mp4?token=…) still hit the fast path.
        _path = urlparse(url).path.lower()
        if any(_path.endswith(ext) for ext in ['.mp4', '.mkv', '.m3u8', '.webm']):
            parsed = urlparse(url).netloc.lower()
            resolver_domains = ['waffi.cloud', 'loadedfiles.', 'wildshare.net', 'vikingfile.com', 'lulacloud.com', 'pixeldrain.com', 'streamtape.com', 'watchadsontape.com', 'strtape.tech', 'vidmoly.']
            if not any(dom in parsed for dom in resolver_domains):
                return url

        for resolver in cls.RESOLVERS:
            if resolver.can_resolve(url):
                # Wrap each resolver in a network-aware retry: a dropped
                # connection (DNS/reset/timeout) does NOT mean the host is
                # gone — wait for the network and try again, up to 3 times,
                # instead of failing the episode on a transient blip. Real
                # "not found" (a clean None with no exception) fails fast.
                res = None
                for attempt in range(3):
                    try:
                        # Try calling resolve with quality parameter, falling back to 2-arg call if resolver signature doesn't take quality
                        try:
                            res = resolver.resolve(url, session, quality=quality)
                        except TypeError:
                            res = resolver.resolve(url, session)
                        break
                    except Exception as e:
                        if _is_network_error(e) and attempt < 2:
                            name = getattr(resolver, '__name__', 'Resolver').replace('Resolver', '')
                            safe_print(f"      [!] {name}: network dropped - "
                                       f"waiting for connection (retry {attempt+1}/3)...")
                            _resolver_wait_for_network()
                            continue
                        # Not a network error, or out of retries — let the
                        # resolver's own handler have logged it; give up.
                        res = None
                        break
                if res and res != url:
                    return cls.resolve(res, session, quality=quality, _depth=_depth + 1)
                return res

        # Direct passthrough fallback
        if 'nkiserv.com' in url or 'cdn' in url:
            return url

        # Nothing claimed this host. Before handing it back as if it were a
        # direct stream, catch the specific case that silently corrupts a
        # download: an embed PAGE or dead-upload tombstone. This is how a
        # 16-byte "File was deleted" page from mp4upload became a 141 B/s
        # "successful" episode -- no resolver claims mp4upload, so it fell
        # straight through and aria2c saved the error body as `<episode>.mp4`.
        #
        # Deliberately narrow: only URLs whose path looks like a web page
        # (.html/.php/...) are probed. Real CDN streams are frequently
        # extensionless (googlevideo `/videoplayback?...` is the common one) and
        # can answer a probe with an HTML challenge/redirect while still being
        # perfectly downloadable -- probing those cost us working episodes, so
        # they are passed through untouched and the downloader's Ghost-file
        # check remains the backstop.
        if urlparse(url).path.lower().endswith(_PAGE_EXTS):
            try:
                probe = session.get(url, timeout=10,
                                    headers={'Range': 'bytes=0-2047'})
                body = (probe.text or '')[:2048]
                if _looks_dead(body):
                    safe_print("      [!] Dead upload (tombstone page): "
                               f"{urlparse(url).netloc}")
                    return None
                # A page that isn't a tombstone may still embed the real media.
                inner = find_direct_video(body)
                if inner and inner != url:
                    return cls.resolve(inner, session, _depth=_depth + 1)
                safe_print("      [!] Unresolved embed page, not a media URL: "
                           f"{urlparse(url).netloc}")
                return None
            except Exception:
                # Probe failure is not proof of death -- fall through and let
                # the downloader (and its Ghost-file check) have the final say.
                pass

        # DNS-park gate. A host whose public A record is a known resolver IP
        # (8.8.8.8 / 1.0.0.1 / 9.9.9.9) is domain-parked: it 302s every
        # request to the DNS provider's homepage. Feeding such a URL to
        # aria2c downloads a 216-byte redirect HTML body as "<movie>.mkv" and
        # then burns every retry cycle on HTTP 500s (kissorgrab.com during
        # the Insurgent attempt did exactly this -- its record flipped
        # 300s-TTL between a real CDN server and 8.8.8.8). Failing fast here
        # lets the caller's re-resolve/retry window catch the TTL flip and
        # keeps the error honest. Results are cached ~5 min (matching the
        # TTLs involved) so a 20-episode batch does one lookup per host, and
        # a host that unparks itself is re-probed when the cache expires.
        _PARKED_IPS = {'8.8.8.8', '8.8.4.4', '1.0.0.1', '1.1.1.1', '9.9.9.9'}
        _PARK_CACHE_TTL = 300.0
        try:
            url_host = urlparse(url).hostname or ''
        except Exception:
            url_host = ''
        if url_host and url_host != cls._park_cache.get(url_host, (None,))[0]:
            try:
                cls._park_cache[url_host] = (url_host, socket.gethostbyname(url_host))
            except socket.gaierror:
                cls._park_cache[url_host] = (url_host, None)
            except Exception:
                pass  # probe failure must never break a working URL
            cls._park_cache[url_host] = (url_host, cls._park_cache[url_host][1], time.time())
        entry = cls._park_cache.get(url_host)
        if entry and len(entry) == 3 and time.time() - entry[2] < _PARK_CACHE_TTL:
            ip = entry[1]
            if ip is None:
                safe_print(f"      [!] {url_host}: hostname does not resolve "
                           "(NXDOMAIN) -- link is dead upstream")
                return None
            if ip in _PARKED_IPS:
                safe_print(f"      [!] {url_host}: domain parked (A record is "
                           "a public DNS resolver IP) -- link is dead upstream")
                return None

        return url
