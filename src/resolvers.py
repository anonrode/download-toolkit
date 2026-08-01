import re
import sys
import time
import base64
from html import unescape
from urllib.parse import urljoin, urlparse, quote

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
def find_direct_video(text):
    for ext in [r'\.m3u8', r'\.mp4', r'\.mkv']:
        found = re.findall(r'https?://[^\s"\'<>,\\]+' + ext + r'[^\s"\'<>,\\]*', text)
        if found:
            return found[0].rstrip('.,;)')
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
                safe_print("      [!] Downloadwella: No form element found on page")
                return None

            data = {inp.get('name'): inp.get('value', '')
                    for inp in form.find_all('input') if inp.get('name')}
            data['method_free'] = 'Free Download'

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
    def resolve(url: str, session) -> str:
        try:
            hosts = LoadedfilesResolver._candidate_hosts(url)
            r1 = None
            live_host = None
            for host in hosts:
                candidate = LoadedfilesResolver._rewrite(url, host)
                r1 = safe_get(session, candidate, referer='https://my9jarocks.bz/', timeout=10, retries=1)
                if r1:
                    live_host = host
                    LoadedfilesResolver._LAST_WORKING_HOST = host
                    break
            if not r1:
                return None
            m1 = re.search(r"var downloadUrl = '(https://loadedfiles\.[a-z0-9-]+/[^']+)'", r1.text, re.I)
            if not m1:
                return None
            step1 = LoadedfilesResolver._rewrite(m1.group(1), live_host)
            r2 = safe_get(session, step1, referer=f'https://{live_host}/', timeout=10, retries=2)
            if not r2:
                return None
            m2 = re.search(r"var downloadUrl = '(https://loadedfiles\.[a-z0-9-]+/[^']+)'", r2.text, re.I)
            if not m2:
                return None
            try:
                step2 = LoadedfilesResolver._rewrite(m2.group(1), live_host)
                r3 = session.get(step2, timeout=10, allow_redirects=False)
                return r3.headers.get('location')
            except Exception as e:
                safe_print(f"      [!] Loadedfiles redirect: {e}")
                return None
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
        try:
            try:
                from curl_cffi import requests as cf_requests
                s = cf_requests.Session(impersonate='chrome120')
            except ImportError:
                s = requests.Session()
            try:
                s.headers['User-Agent'] = UA_DESKTOP

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
            finally:
                s.close()
        except Exception as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] Wildshare: {e}")
            return None

class StreamtapeResolver(BaseResolver):
    @staticmethod
    def can_resolve(url: str) -> bool:
        netloc = urlparse(url).netloc.lower()
        return any(domain in netloc for domain in ['streamtape.com', 'watchadsontape.com'])

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            r = safe_get(session, url, referer='https://watchadsontape.com/')
            if not r or r.status_code == 404:
                return None
            m = re.search(
                r"getElementById\('robotlink'\)[^;]*innerHTML\s*=\s*'([^']+)'\s*\+\s*\('([^']+)'\)",
                r.text, re.DOTALL
            )
            if m:
                base_s, raw = m.group(1), m.group(2)
                find_idx = r.text.find("getElementById('robotlink')")
                subtext = r.text[find_idx:] if find_idx != -1 else r.text
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
    @staticmethod
    def can_resolve(url: str) -> bool:
        return 'vidmoly.me' in urlparse(url).netloc.lower()

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            r = session.get(url, timeout=20)
            if not r or r.status_code != 200:
                return None
                
            # Vidmoly hides stream link in file: "http...playlist.m3u8" inside javascript
            m = re.search(r'file\s*:\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', r.text)
            if m:
                return m.group(1)
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

            # 2) embed page points at /3rdplayer.html?...&key=... — fetch and decrypt
            mv = re.search(r'data-video=["\']([^"\']+)["\']', text)
            if mv:
                player_url = urljoin(url, unescape(mv.group(1)))
                pr = session.get(player_url, timeout=20, headers={'Referer': url})
                if pr is not None and pr.status_code == 200:
                    direct = VidbasicResolver._decrypt_payload(pr.text)
                    if direct:
                        return direct

            # 3) embedload.cfd wrapper iframes the real vidbasic host.
            # This recursion is our own, so the registry's _depth > 5 limit never
            # sees it: A can iframe B which iframes A again, and `inner != url`
            # only catches a page iframing itself. Carry our own counter.
            mi = re.search(r'<iframe[^>]+src=["\']([^"\']*(?:vidbasic|vidb\.top)[^"\']*)["\']', text)
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
              'anihdplay.', 'gogohd.', 'megaplay.', 'animesama.', 'tamilembed.')

    @staticmethod
    def can_resolve(url: str) -> bool:
        if '/playlist.php' in url or '/api/' in url:
            return False
        netloc = urlparse(url).netloc.lower()
        return any(h in netloc for h in KisskhMegaplayResolver._HOSTS) or '/kisskh/' in url

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            # 0) tamilembed.lol layout: embed page with data-code -> loader.php
            if 'tamilembed.' in url:
                parsed = urlparse(url)
                parts = [p for p in parsed.path.strip('/').split('/') if p]
                code = parts[-1] if parts else None
                if code:
                    return f'https://tamilembed.lol/loader.php?id={code}'

            headers = {
                'User-Agent': UA_DESKTOP,
                'Referer': session.headers.get('Referer', ''),
                'Sec-Fetch-Dest': 'iframe',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'cross-site',
            }
            r = session.get(url, timeout=20, headers=headers)
            if not r or r.status_code != 200:
                return None

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
                    target = q_dict.get('720p') or q_dict.get('360p') or next(iter(q_dict.values()), None)
                    if target:
                        return pb_m.group(1) + quote(target.replace('\\/', '/'), safe='')
                except Exception:
                    pass

            # 4) Standard source tag
            m = re.search(r'"source"\s*:\s*"([^"]+\.m3u8[^"]*)"', r.text)
            if m:
                return m.group(1)
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
                
            m = re.search(r'window\.location\.href\s*=\s*"([^"]+)"', r.text)
            if m:
                return m.group(1)
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
            
            # Method A: Class download-btn
            btn = soup.find('a', class_='download-btn')
            if btn and btn.get('href'):
                return btn['href']
                
            # Method B: Regex search downloadURL script variables
            m = re.search(r'var\s+downloadURL\s*=\s*"([^"]+)"', r2.text)
            if m:
                return m.group(1)
                
            # Method C: Find vikingfile / lulacloud anchors
            for a in soup.find_all('a', href=True):
                href = a['href']
                if any(x in href.lower() for x in ['vikingfile.com', 'lulacloud.com']):
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
                
            # Extract PlutoMovies download scripts
            # Primary: downloadButton onclick handler
            m = re.search(
                r"getElementById\('downloadButton'\)\.onclick\s*=\s*function\(\)\s*\{"
                r"\s*location\.href\s*=\s*'(https://[^']+)'",
                r.text, re.DOTALL
            )
            if m:
                return m.group(1)
            # Fallback: generic window.location.href
            m = re.search(r"window\.location\.href\s*=\s*['\"]([^'\"]+)['\"]", r.text)
            if m:
                return m.group(1)
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
            base = f'{parsed.scheme}://{parsed.netloc}'
            # Normalise /d/ share links to the /e/ embed the player script lives on.
            embed = url.replace('/d/', '/e/')
            r = safe_get(session, embed, referer=base + '/', timeout=20)
            if not r:
                return None
            if _looks_dead(r.text):
                safe_print("      [!] Doodstream: file expired/deleted")
                return None
            m = re.search(r"(/pass_md5/[^'\"\s]+)", r.text)
            if not m:
                return None
            pass_url = base + m.group(1)
            r2 = session.get(pass_url, timeout=20,
                             headers={'Referer': embed, 'User-Agent': UA_DESKTOP})
            if not r2 or r2.status_code != 200 or not r2.text.strip():
                return None
            prefix = r2.text.strip()
            token = pass_url.rstrip('/').split('/')[-1]
            import random as _rnd, string as _str
            rand = ''.join(_rnd.choice(_str.ascii_letters + _str.digits) for _ in range(10))
            expiry = int(time.time() * 1000)
            return f'{prefix}{rand}?token={token}&expiry={expiry}'
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
            r = safe_get(session, embed, referer=base + '/', timeout=20)
            if not r:
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
              'obeywish.com', 'jodwish.com', 'streamwish.to', 'embedwish.')

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
                f'https://sfastwish.com/e/{vid}',
                f'https://embedwish.com/e/{vid}',
                url,  # fallback to original if transform fails
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
    cleanly (None) rather than returning a tombstone page."""
    _HOSTS = ('minochinos.com', 'vidhide.', 'vidhidepro.', 'vidhidevip.',
              'filelions.', 'vid-guard.', 'nining.', 'peytonepre.com',
              'techradar.ink', 'ryderjet.com')

    @staticmethod
    def can_resolve(url: str) -> bool:
        net = urlparse(url).netloc.lower()
        return any(h in net for h in VidhideResolver._HOSTS)

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            parsed = urlparse(url)
            base = f'{parsed.scheme}://{parsed.netloc}'
            r = safe_get(session, url, referer=base + '/', timeout=20)
            if not r:
                return None
            if _looks_dead(r.text):
                safe_print("      [!] Vidhide: file expired/deleted")
                return None
            unpacked = _unpack_packed_js(r.text) or r.text
            m = re.search(
                r'sources\s*:\s*\[\s*\{[^}]*?file\s*:\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']',
                unpacked)
            if m:
                return m.group(1)
            return _find_hls_or_mp4(unpacked)
        except _http_exc_bases() as e:
            if _is_network_error(e):
                raise
            safe_print(f"      [!] Vidhide: network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] Vidhide: {e}")
            return None

# --- REGISTRY ---

class ResolverRegistry:
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
        PlutoMoviesResolver,
        PixelDrainResolver,
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
    def resolve(cls, url: str, session, _depth=0) -> str:
        if _depth > 5:
            safe_print(f"      [!] Resolver depth limit reached — returning: {url[:60]}")
            return url

        # Check if already a direct download link (excluding resolver domains that append filenames)
        # Match on the path only, so links with query strings (…/file.mp4?token=…) still hit the fast path.
        _path = urlparse(url).path.lower()
        if any(_path.endswith(ext) for ext in ['.mp4', '.mkv', '.m3u8', '.webm']):
            parsed = urlparse(url).netloc.lower()
            resolver_domains = ['waffi.cloud', 'loadedfiles.', 'wildshare.net', 'vikingfile.com', 'lulacloud.com', 'pixeldrain.com', 'streamtape.com', 'watchadsontape.com', 'vidmoly.me']
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
                    return cls.resolve(res, session, _depth=_depth + 1)
                return res

        # Direct passthrough fallback
        if 'nkiserv.com' in url or 'cdn' in url:
            return url

        return url
