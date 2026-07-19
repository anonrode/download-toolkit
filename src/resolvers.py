import re
import sys
import time
import urllib3
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

# Suppress certificate warnings (useful for expired SSL certs on hosts like wetafiles)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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

            m = re.search(r'window\.location\.href\s*=\s*["\']([^"\']+)["\']', r.text)
            if m:
                redirect_url = m.group(1)
                if not redirect_url.startswith('http'):
                    redirect_url = urljoin(url, redirect_url)
                safe_print(f"      [*] Following JS redirect: {redirect_url[:60]}...")
                return safe_get(session, redirect_url, referer=referer, retries=max(1, retries - 1), _seen=_seen)

            if not r.ok:
                safe_print(f"      [!] HTTP {r.status_code}: {url[:60]}")
                if attempt < retries - 1:
                    time.sleep(2)
                    continue
                return None
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
        except requests.RequestException as e:
            safe_print(f"      [!] Downloadwella: Network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] Downloadwella: Resolution error: {e}")
            return None

class LoadedfilesResolver(BaseResolver):
    @staticmethod
    def can_resolve(url: str) -> bool:
        netloc = urlparse(url).netloc.lower()
        return netloc in ['loadedfiles.org', 'www.loadedfiles.org']

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            r1 = safe_get(session, url, referer='https://my9jarocks.bz/')
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
        except requests.RequestException as e:
            safe_print(f"      [!] Vidmoly: Network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] Vidmoly: Resolution error: {e}")
            return None

class VidbasicResolver(BaseResolver):
    @staticmethod
    def can_resolve(url: str) -> bool:
        return 'vidbasic.to' in urlparse(url).netloc.lower()

    @staticmethod
    def resolve(url: str, session) -> str:
        try:
            r = session.get(url, timeout=20)
            if not r or r.status_code != 200:
                return None
            return find_direct_video(r.text)
        except requests.RequestException as e:
            safe_print(f"      [!] Vidbasic: Network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] Vidbasic: Resolution error: {e}")
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
            headers = {'Referer': session.headers.get('Referer', '')}
            r = requests.get(url, timeout=20, headers=headers)
            if not r or r.status_code != 200:
                r = session.get(url, timeout=20)
            if not r or r.status_code != 200:
                return None
            return find_direct_video(r.text)
        except requests.RequestException as e:
            safe_print(f"      [!] Embed: Network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] Embed: Resolution error: {e}")
            return None

class VikingFileResolver(BaseResolver):
    @staticmethod
    def can_resolve(url: str) -> bool:
        return 'vikingfile.com' in urlparse(url).netloc.lower()

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
                cdn = find_direct_video(r2.text)
                return cdn if cdn else loc1

            if r1.status_code == 200:
                r1b = session.get(url, timeout=15, allow_redirects=True, headers=headers)
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
            safe_print(f"      [!] VikingFile: could not resolve {url[:60]}")
            return None
        except Exception as e:
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
        except requests.RequestException as e:
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
        except requests.RequestException as e:
            safe_print(f"      [!] NaijaVaultGateway: Network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] NaijaVaultGateway: Resolution error: {e}")
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
        except requests.RequestException as e:
            safe_print(f"      [!] PlutoMovies: Network request failed: {e}")
            return None
        except Exception as e:
            safe_print(f"      [!] PlutoMovies: Resolution error: {e}")
            return None

# --- REGISTRY ---

class ResolverRegistry:
    RESOLVERS = [
        WaffiCloudResolver,
        DownloadwellaResolver,
        LoadedfilesResolver,
        WildshareResolver,
        StreamtapeResolver,
        VidmolyResolver,
        VidbasicResolver,
        EmbedResolver,
        VikingFileResolver,
        LulaCloudResolver,
        DramaGatewayResolver,
        NaijaVaultGatewayResolver,
        PlutoMoviesResolver,
    ]

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
            resolver_domains = ['waffi.cloud', 'loadedfiles.org', 'wildshare.net', 'vikingfile.com', 'lulacloud.com', 'streamtape.com', 'watchadsontape.com', 'vidmoly.me', 'vidbasic.to']
            if not any(dom in parsed for dom in resolver_domains):
                return url

        for resolver in cls.RESOLVERS:
            if resolver.can_resolve(url):
                res = resolver.resolve(url, session)
                if res and res != url:
                    return cls.resolve(res, session, _depth=_depth + 1)
                return res
                
        # Direct passthrough fallback
        if 'nkiserv.com' in url or 'cdn' in url:
            return url
            
        return url
