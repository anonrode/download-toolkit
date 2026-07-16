from .base import *

def extract_plutomovies(url, session, ctx=None):
    ctx  = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    safe_print("[*] PlutoMovies mode")
    is_movie = '/movie/' in url
    slug     = url.rstrip('/').split('/')[-1]
    name     = re.sub(r'-\d{4}.*$', '', slug).replace('-', ' ').title()
    safe_print(f"[*] Title: {name}")
    folder   = os.path.join(BASE_DIR, safe_filename(name))
    summary  = DownloadSummary()

    r = safe_get(session, url, timeout=30, referer=PLUTO_BASE + '/')
    if r is None:
        safe_print(f"[!] Could not fetch page: {url[:70]}")
        return
    soup    = BeautifulSoup(r.text, 'html.parser')
    dl_link = next((a['href'] for a in soup.find_all('a', href=True)
                    if f'dl.{PLUTO_DOMAIN}' in a['href']), None)

    if is_movie or dl_link:
        if dl_link:
            safe_print(f"[*] Direct link found — saving to: {folder}")
            direct = ResolverRegistry.resolve(dl_link, session)
            if direct:
                ext = 'mkv' if 'mkv' in direct.lower() else 'mp4'
                download_file(direct, folder, safe_filename(f"{name}.{ext}"), summary,
                              series_url=url, series_name=name,
                              bandwidth_limit=bw, current_process=cur_proc,
                              stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'))
            else:
                safe_print("[✗] Could not resolve download link")
                summary.add_failed(name)
        else:
            safe_print("[✗] No download link found on page")
            summary.add_failed(name)
        if summary.failed == 0 and not _stopped(ctx):
            mark_series_complete(url)
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
    # Track whether any season yielded episodes. If every season scrapes/filters
    # to zero, we must NOT mark the series complete (that wipes resume state and
    # reports false success on a markup change or non-matching filter).
    episodes_seen = False

    def _resolve_ep(ep_url):
        """Fetch ep page and resolve CDN url. Returns (dl_link, direct) or (None, None)."""
        r3 = safe_get(session, ep_url, timeout=30)
        if not r3:
            return None, None
        soup3   = BeautifulSoup(r3.text, 'html.parser')
        dl_link = next((a['href'] for a in soup3.find_all('a', href=True)
                        if f'dl.{PLUTO_DOMAIN}' in a['href']), None)
        if not dl_link:
            return None, None
        direct = ResolverRegistry.resolve(dl_link, session)
        return dl_link, direct

    def _cdn_alive(cdn_url):
        """Quick HEAD check to verify CDN token hasn't expired."""
        try:
            r = session.head(cdn_url, timeout=5, allow_redirects=True)
            return r.status_code in (200, 206)
        except Exception:
            return False

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
            has_next = any('next' in a.get_text(strip=True).lower() for a in soup2.find_all('a', href=True))
            if not unique:
                break
            for eu, _ in unique:
                seen_eps.add(eu)
            safe_print(f"  [*] Page {page}: {len(unique)} episode(s)")
            all_eps.extend(unique)
            if not has_next:
                break
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
        all_eps = _filter_by_episode_range(all_eps, ctx)
        if not all_eps:
            safe_print(f"  [*] No episodes matched your selection this season")
            continue
        episodes_seen = True
        safe_print(f"  [*] Total: {len(all_eps)} episode(s)")
        _notify_start(name, len(all_eps))


        # Resolve and download each episode immediately.
        # Prefetcher resolves next ep's CDN url in background while current ep downloads.
        # HEAD check guards against expired tokens if current ep took too long.
        # Prefetch ep[0] immediately, then prefetch ep[N+1] as soon as ep[N] starts downloading.
        # Always prefetch every ep in order — alignment is guaranteed because
        # we call get() for every ep before deciding to skip, so the queue never drifts.
        prefetcher = Prefetcher(_resolve_ep)
        if all_eps:
            prefetcher.prefetch(all_eps[0][0])

        for i, (ep_url, ep_name) in enumerate(all_eps, 1):
            if _stopped(ctx):
                break
            _wait(ctx)
            safe_print(f"\n  [{i}/{len(all_eps)}] {ep_name}")

            # Get prefetched result first — keeps queue aligned regardless of skip/fail
            dl_link, direct = prefetcher.get(timeout=30)

            # Kick off prefetch for next ep immediately while we process current
            if i < len(all_eps):
                prefetcher.prefetch(all_eps[i][0])

            # Skip check AFTER get() so queue alignment is never broken
            done, _ = already_downloaded(folder, f"{ep_name}.mp4", series_url=url)
            if not done:
                done, _ = already_downloaded(folder, f"{ep_name}.mkv", series_url=url)
            if done:
                safe_print(f"  [✓] Already downloaded — skipping")
                summary.add_skipped()
                continue

            if not dl_link:
                safe_print(f"  [✗] Could not fetch episode page")
                summary.add_failed(ep_name)
                continue
            if not direct:
                safe_print(f"  [✗] Could not resolve download link")
                summary.add_failed(ep_name)
                continue

            # HEAD check — re-resolve if token expired
            if not _cdn_alive(direct):
                safe_print(f"  [*] CDN token expired — re-resolving...")
                direct = ResolverRegistry.resolve(dl_link, session)
                if not direct:
                    safe_print(f"  [✗] Re-resolve failed")
                    summary.add_failed(ep_name)
                    continue

            ext = 'mkv' if 'mkv' in direct.lower() else 'mp4'
            download_file(direct, folder, safe_filename(f"{ep_name}.{ext}"), summary,
                          series_url=url, series_name=name,
                          bandwidth_limit=bw, quality=quality,
                          current_process=cur_proc,
                          stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'))
            time.sleep(0.5)

    if not episodes_seen and not _stopped(ctx):
        # Every season scraped/filtered to zero — do NOT report success or
        # wipe resume state. Surface the breakage instead.
        safe_print("[!] No episodes found across any season")
        diagnose_page(soup, url, "episode links")
        summary.report()
        return

    if summary.failed == 0 and not _stopped(ctx):
        mark_series_complete(url)
    summary.report()

def _yt_quality_prompt(default_quality):
    """Ask user to pick a quality. Returns a yt-dlp format string."""
    QUALITY_MAP = {
        '1': ('360p',  'bestvideo[height<=360]+bestaudio/best[height<=360]'),
        '2': ('480p',  'bestvideo[height<=480]+bestaudio/best[height<=480]'),
        '3': ('720p',  'bestvideo[height<=720]+bestaudio/best[height<=720]'),
        '4': ('1080p', 'bestvideo[height<=1080]+bestaudio/best[height<=1080]'),
        '5': ('2160p', 'bestvideo[height<=2160]+bestaudio/best[height<=2160]'),
    }
    # Work out which number matches the current default
    label_to_num = {'360p': '1', '480p': '2', '720p': '3', '1080p': '4', '2160': '5', '2160p': '5', '4k': '5'}
    default_quality = default_quality or '480p'
    default_label = '480p'
    for label in label_to_num:
        if label in default_quality:
            default_label = '2160p' if label in ('2160', '4k') else label
            break
    default_num = label_to_num.get(default_label, '2')

    safe_print(f"\n  Quality: [1] 360p  [2] 480p  [3] 720p  [4] 1080p  [5] 4K  (default: {default_label})")
    try:
        choice = input("  Pick [1-5] or Enter for default: ").strip()
    except EOFError:
        choice = ''
    if not choice:
        choice = default_num
    _, fmt = QUALITY_MAP.get(choice, QUALITY_MAP[default_num])
    return fmt


def _yt_get_playlist_count(url):
    """Return (count, title) for a YouTube playlist, or (None, None) on failure."""
    import shutil
    if not shutil.which('yt-dlp'):
        return None, None
    try:
        # Single call — print both id and playlist_title for every item,
        # then use line count for total and first title value for name
        result = subprocess.run(
            ['yt-dlp', '--flat-playlist',
             '--print', 'id',
             '--print', 'playlist_title',
             '--no-warnings', '--quiet',
             '--no-check-certificates',
             url],
            capture_output=True, text=True, timeout=25,
            stdin=subprocess.DEVNULL
        )
        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        # Output alternates: id, playlist_title, id, playlist_title ...
        ids    = lines[0::2]
        titles = [t for t in lines[1::2] if t and t != 'NA']
        count  = len(ids) if ids else None
        title  = titles[0] if titles else None
        return count, title
    except Exception:
        return None, None


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
            if len(parts) != 2:
                raise ValueError("need exactly two numbers")
            start, end = int(parts[0]), int(parts[1])
            if start > end:
                raise ValueError("start must be less than end")
            return r
        except Exception:
            safe_print("  [!] Invalid range — use format: 5-10")
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
