import os
import re
import time
import subprocess
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import requests

from ..resolvers import ResolverRegistry
from ..downloader import (
    DownloadSummary, download_file, download_batch, download_with_ytdlp,
    download_social_ytdlp, Prefetcher, safe_print, safe_filename,
    find_direct_video, base_domain, ProcessContainer,
    mark_series_complete, already_downloaded, BASE_DIR, DIAG_LOG, UA_DESKTOP,
    _notify_start, register_process, unregister_process, finish_process,
    update_status, _drain_futures_interruptible,
)

# ─── SITE DOMAIN CONSTANTS ────────────────────────────────────
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

WAFFI_CLOUD_RE = re.compile(r'https?://[a-z0-9-]+\.waffi\.cloud/\S+', re.IGNORECASE)
EPISODE_TAG_RE = re.compile(r'[Ss](\d{1,2})[Ee](\d{1,3})')

def _strip_preview_param(url):
    return url.split('?preview')[0] if '?preview' in url else url

def _episode_label(url, link_text, fallback_index):
    m = EPISODE_TAG_RE.search(url)
    if m:
        return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
    text = (link_text or '').strip()
    if text and text.lower() not in ('download', 'click here', 'link', 'watch'):
        return text
    return f"episode-{fallback_index}"

EP_KEYWORDS = ['-e', 'episode', 's0', 's1', 's2', 's3', 's4', 's5', 's6', 's7', 's8', 's9']

SOCIAL_DOMAINS = [
    'facebook.com', 'fb.watch', 'instagram.com', 'twitter.com', 'x.com',
    'tiktok.com', 'youtube.com', 'youtu.be', 'vimeo.com', 'dailymotion.com',
    'twitch.tv', 'reddit.com', 'pinterest.com', 'pin.it', 'snapchat.com'
]

# ─── HELPERS ──────────────────────────────────────────────────
def safe_get(session, url, timeout=20, referer=None, retries=3, _seen=None):
    if _seen is None:
        _seen = set()
    if url in _seen:
        safe_print(f"  [!] JS redirect loop detected: {url[:60]}")
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
                safe_print(f"  [*] Following JS redirect: {redirect_url[:60]}...")
                return safe_get(session, redirect_url, referer=referer, retries=max(1, retries - 1), _seen=_seen)

            if not r.ok:
                safe_print(f"  [!] HTTP {r.status_code}: {url[:60]}")
                if attempt < retries - 1:
                    time.sleep(2)
                    continue
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
    import threading
    proc_container = ctx.get('current_process')
    if proc_container is None:
        proc_container = ProcessContainer()

    stop_event = ctx.get('stop')
    if stop_event is None:
        stop_event = threading.Event()

    pause_event = ctx.get('pause')
    if pause_event is None:
        pause_event = threading.Event()

    return (
        stop_event,
        ctx.get('wait',            lambda: None),
        ctx.get('bandwidth',       0),
        ctx.get('quality',         None),
        ctx.get('parallel',        1),
        proc_container,
        pause_event,
    )

def _stopped(ctx):
    stop_event = ctx.get('stop')
    if stop_event is not None:
        return stop_event.is_set()
    return False

def _wait(ctx):
    fn = ctx.get('wait')
    if fn:
        fn()

def _filter_by_episode_range(items, ctx):
    selected = ctx.get('episode_filter') if ctx else None
    if not selected:
        return items
    filtered = [item for idx, item in enumerate(items, 1) if idx in selected]
    safe_print(f"[*] Episode range selected: {len(filtered)} of {len(items)}")
    return filtered

def diagnose_page(soup, url, expected_pattern=None):
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
        os.makedirs(os.path.dirname(DIAG_LOG), exist_ok=True)
        try:
            if os.path.exists(DIAG_LOG) and os.path.getsize(DIAG_LOG) > 5 * 1024 * 1024:
                backup = DIAG_LOG + '.old'
                if os.path.exists(backup):
                    os.remove(backup)
                os.rename(DIAG_LOG, backup)
        except Exception:
            pass
        with open(DIAG_LOG, 'a', encoding='utf-8') as f:
            f.write(output + '\n')
    except Exception:
        pass

def _is_valid_cdn_url(url):
    if not url or not isinstance(url, str):
        return False
    VALID_HOSTS = [
        'vikingfile.com', 'cdn.filevault',  'cdn.filevault.com.ng',
        'lulacloud.com', 'kwik.cx', 'animepahe',
    ]
    GATE_HOSTS = [
        'naijavault.com', 'thenkiri.com', 'nkiri.com',
        'dramakey.com', 'dramakey.cc', 'dramarain.com'
    ]
    GATE_PATTERNS = ['dl-', '.php?', 'downloadwella']
    
    if any(gate in url.lower() for gate in GATE_HOSTS):
        return False
    if any(pat in url.lower() for pat in GATE_PATTERNS):
        return False
    if any(cdnhost in url.lower() for cdnhost in VALID_HOSTS):
        return True
    if any(url.endswith(ext) for ext in ['.mp4', '.mkv', '.m3u8', '.webm']):
        return True
    return False

def safe_resolve(resolver_fn, url, session, resolver_name='', max_attempts=3):
    for attempt in range(max_attempts):
        try:
            result = resolver_fn(url, session)
            if result and _is_valid_cdn_url(result):
                safe_print(f"  [✓] {resolver_name} resolved: {result[:60]}...")
                return result
            elif result:
                safe_print(f"  [!] {resolver_name} returned invalid URL: {result[:60]}...")
                if attempt < max_attempts - 1:
                    time.sleep(2)
                continue
            elif attempt < max_attempts - 1:
                time.sleep(2)
        except requests.Timeout:
            safe_print(f"  [!] {resolver_name} timed out (attempt {attempt+1}/{max_attempts})")
            if attempt < max_attempts - 1:
                time.sleep(2)
        except Exception as e:
            safe_print(f"  [!] {resolver_name} failed: {e} (attempt {attempt+1}/{max_attempts})")
            if attempt < max_attempts - 1:
                time.sleep(2)
    safe_print(f"  [✗] {resolver_name} failed after {max_attempts} attempts")
    return None

def try_resolver_chain(resolvers, url, session):
    for resolver_fn, resolver_name in resolvers:
        result = safe_resolve(resolver_fn, url, session, resolver_name)
        if result:
            return result
    return None

def resolve_downloadwella(url, session):
    try:
        r = safe_get(session, url, timeout=20)
        if r is None:
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

def _ensure_package(package_name):
    try:
        return __import__(package_name)
    except ImportError:
        safe_print(f"[*] Installing {package_name}...")
        try:
            subprocess.run(
                ['pip', 'install', package_name, '--break-system-packages', '-q'],
                check=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return __import__(package_name)
        except Exception as e:
            safe_print(f"[!] Could not install {package_name}: {e}")
            return None

def _extract_downloadwella_site(url, session, ctx, site_label, name_cleaner):
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    safe_print(f"[*] {site_label} mode")
    slug   = url.rstrip('/').split('/')[-1]
    name   = clean_name(name_cleaner(slug))
    safe_print(f"[*] Series: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))

    r = safe_get(session, url)
    if r is None:
        safe_print(f"[!] Could not fetch page: {url[:70]}")
        return
    soup = BeautifulSoup(r.text, 'html.parser')
    links = list(dict.fromkeys(
        a['href'] for a in soup.find_all('a', href=True)
        if 'downloadwella.com' in a['href'] or 'wetafiles.com' in a['href']
    ))
    if not links:
        safe_print(f"[!] No downloadwella/wetafiles links found on page")
        diagnose_page(soup, url, "downloadwella.com links")
        return
    links = _filter_by_episode_range(links, ctx)
    if not links:
        safe_print("[!] No episodes matched that range")
        return

    safe_print(f"[*] Found {len(links)} episode(s) — saving to: {folder}")
    _notify_start(name, len(links))
    summary = DownloadSummary()

    batch_size = max(1, parallel)
    batches    = [links[i:i+batch_size] for i in range(0, len(links), batch_size)]
    ep_index   = 0

    for batch in batches:
        if _stopped(ctx):
            break
        _wait(ctx)

        to_process = []
        for ep_url in batch:
            ep_index += 1
            ep_name = ep_url.split('/')[-1].replace('.html', '')
            ep_name = re.sub(r'\.(mkv|mp4)$', '', ep_name, flags=re.IGNORECASE)
            safe_print(f"\n[{ep_index}/{len(links)}] {ep_name}")
            done, _ = already_downloaded(folder, safe_filename(f"{ep_name}.mp4"), series_url=url)
            if not done:
                done, _ = already_downloaded(folder, safe_filename(f"{ep_name}.mkv"), series_url=url)
            if done:
                safe_print(f"  [✓] Already downloaded — skipping")
                summary.add_skipped()
            else:
                to_process.append((ep_url, ep_name))

        if not to_process:
            continue

        if len(to_process) == 1 or batch_size == 1:
            ep_url, ep_name = to_process[0]
            direct = ResolverRegistry.resolve(ep_url, session)
            if direct:
                ext   = 'mkv' if '.mkv' in direct else 'mp4'
                fname = safe_filename(f"{ep_name}.{ext}")
                download_file(direct, folder, fname, summary,
                              series_url=url, series_name=name,
                              bandwidth_limit=bw, quality=quality,
                              current_process=cur_proc,
                              stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'),
                              source_url=ep_url)
            else:
                safe_print(f"  [✗] Could not extract link")
                summary.add_failed(ep_name)
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            safe_print(f"\n  [*] Resolving {len(to_process)} link(s)...")
            resolved = {}
            with ThreadPoolExecutor(max_workers=min(len(to_process), 8)) as ex:
                futures = {
                    ex.submit(ResolverRegistry.resolve, ep_url, session): (ep_url, ep_name)
                    for ep_url, ep_name in to_process
                }
                for f in as_completed(futures):
                    ep_url, ep_name = futures[f]
                    try:
                        direct = f.result()
                        resolved[(ep_url, ep_name)] = direct
                    except Exception:
                        resolved[(ep_url, ep_name)] = None

            items = []
            for (ep_url, ep_name), direct in resolved.items():
                if direct:
                    ext = 'mkv' if '.mkv' in direct else 'mp4'
                    items.append((direct, safe_filename(f"{ep_name}.{ext}"), ep_name, ep_url))
                else:
                    safe_print(f"  [✗] Could not extract link: {ep_name}")
                    summary.add_failed(ep_name)

            if items:
                per_thread_bw = (bw // len(items)) if bw else 0
                ex = ThreadPoolExecutor(max_workers=min(len(items), 8))
                thread_futures = {}
                for direct, fname, ep_name, src_url in items:
                    thread_proc = ProcessContainer()
                    f = ex.submit(
                        download_file,
                        direct, folder, fname, summary,
                        series_url=url, series_name=name,
                        bandwidth_limit=per_thread_bw, quality=quality,
                        current_process=thread_proc,
                        stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'),
                        parallel_mode=True, source_url=src_url,
                    )
                    thread_futures[f] = ep_name
                for f, ep_name in _drain_futures_interruptible(thread_futures, stop, executor=ex):
                    try:
                        f.result()
                    except Exception as e:
                        if not _stopped(ctx):
                            safe_print(f"  [✗] Thread error for {ep_name}: {e}")
                            summary.add_failed(ep_name)
                ex.shutdown(wait=False, cancel_futures=True)

    if summary.failed == 0 and not _stopped(ctx):
        mark_series_complete(url)
    summary.report(name)

    if summary.failed > 0 and not _stopped(ctx) and summary.prompt_retry():
        retry_summary = DownloadSummary()
        for failed_fname in summary.failed_list:
            if _stopped(ctx):
                break
            safe_print(f"\n[*] Retrying: {failed_fname}")
            stem = re.sub(r'\.(mkv|mp4)$', '', failed_fname, flags=re.IGNORECASE).lower()
            ep_url = next((l for l in links if l.lower().replace('.html', '').rstrip('/').endswith(stem)), None)
            if not ep_url:
                safe_print(f"  [!] Could not find episode URL for retry")
                retry_summary.add_failed(failed_fname)
                continue
            direct = ResolverRegistry.resolve(ep_url, session)
            if direct:
                ext = 'mkv' if '.mkv' in direct else 'mp4'
                download_file(direct, folder, safe_filename(f"{stem}.{ext}"),
                              retry_summary, series_url=url, series_name=name,
                              bandwidth_limit=bw, quality=quality,
                              current_process=cur_proc, stop_flag=stop,
                              pause_flag=pause,
                              wait_fn=ctx.get('wait'),
                              source_url=ep_url)
            else:
                safe_print(f"  [✗] Could not extract link on retry")
                retry_summary.add_failed(failed_fname)
        retry_summary.report(f"{name} (retry)")

# Expose all symbols including underscore helpers to wildcard imports
__all__ = [name for name in globals() if not name.startswith('__')]
