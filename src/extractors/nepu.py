from .base import *

def extract_nepu(url, session, ctx=None):
    """
    Extractor for Nepu (nepu.gd, formerly nepu.to).
    Handles movies and TV series, preserving quality preferences (360p/480p/720p/1080p).
    """
    ctx = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    safe_print(render_message('site_mode', site='Nepu'))

    # Normalize nepu.to -> nepu.gd
    url = url.replace('nepu.to', 'nepu.gd')

    # Detect if movie or TV show
    is_movie = '/movie/' in url or '/watch/movie/' in url

    # Fetch page
    r = safe_get(session, url, timeout=30, referer=NEPU_BASE + '/')
    if r is None:
        safe_print(f"[!] Could not fetch page: {url[:70]}")
        return

    soup = BeautifulSoup(r.text, 'html.parser')

    # Extract title
    title_text = soup.title.text if soup.title else ''
    title_text = re.sub(r'(?i)^\s*(nepu\.gd|nepu\.to)\s*[-|–]?\s*', '', title_text)
    title_text = re.sub(r'(?i)\s*[-|–]?\s*(nepu\.gd|nepu\.to|watch movies).*$', '', title_text).strip()

    if not title_text:
        slug = url.rstrip('/').split('/')[-1]
        title_text = slug.replace('-', ' ').title()

    safe_print(f"[*] Title: {title_text}")
    folder = os.path.join(BASE_DIR, safe_filename(title_text))
    summary = DownloadSummary()

    # ─── MOVIE PATH ───────────────────────────────────────────────
    if is_movie:
        safe_print(f"[*] Movie detected - resolving stream...")
        direct = ResolverRegistry.resolve(url, session)
        if direct:
            ext = 'mp4'
            filename = safe_filename(f"{title_text}.{ext}")
            download_file(direct, folder, filename, summary,
                          series_url=url, series_name=title_text,
                          bandwidth_limit=bw, quality=quality,
                          current_process=cur_proc,
                          stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'))
        else:
            safe_print(render_message('no_download_link'))
            summary.add_failed(title_text)

        if summary.failed == 0 and not _stopped(ctx):
            mark_series_complete(url)
        summary.report()
        return

    # ─── TV SHOW PATH ─────────────────────────────────────────────
    safe_print(f"[*] TV Series detected")

    # Extract TMDB ID
    m = re.search(r'/(?:movie|tv)/(\d+)', url)
    tmdb_id = m.group(1) if m else None

    if not tmdb_id:
        safe_print(f"[!] Could not extract TMDB ID from URL: {url[:70]}")
        summary.add_failed(title_text)
        summary.report()
        return

    # Discover available seasons & episodes
    ep_links = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if '/watch/tv/' in href or '/tv/' in href:
            m_ep = re.search(r'/(?:watch/)?tv/(\d+)(?:/(\d+)/(\d+))?', href)
            if m_ep and m_ep.group(2) and m_ep.group(3):
                s_num, ep_num = int(m_ep.group(2)), int(m_ep.group(3))
                full_ep_url = urljoin(NEPU_BASE, href)
                ep_links.append((s_num, ep_num, full_ep_url))

    if not ep_links:
        ep_links.append((1, 1, f"https://vidsrc.mov/embed/tv/{tmdb_id}/1/1"))

    seen_eps = set()
    all_eps = []
    for s_num, ep_num, ep_url in ep_links:
        key = (s_num, ep_num)
        if key not in seen_eps:
            seen_eps.add(key)
            ep_label = f"S{s_num:02d}E{ep_num:02d}"
            ep_filename = safe_filename(f"{title_text} {ep_label}.mp4")
            all_eps.append((s_num, ep_num, ep_url, ep_label, ep_filename))

    all_eps.sort(key=lambda x: (x[0], x[1]))
    all_eps_filtered = _filter_by_episode_range([(u, fn) for _, _, u, _, fn in all_eps], ctx)

    safe_print(f"[*] Found {len(all_eps)} episode(s)")
    _notify_start(title_text, len(all_eps))

    def _resolve_ep(ep_url):
        return ResolverRegistry.resolve(ep_url, session)

    prefetcher = Prefetcher(_resolve_ep)
    if all_eps:
        prefetcher.prefetch(all_eps[0][2])

    for i, (s_num, ep_num, ep_url, ep_label, ep_filename) in enumerate(all_eps, 1):
        if _stopped(ctx):
            break
        _wait(ctx)
        safe_print(f"\n  [{i}/{len(all_eps)}] {ep_label} - {title_text}")

        direct = prefetcher.get(timeout=30)

        if i < len(all_eps):
            prefetcher.prefetch(all_eps[i][2])

        done, _ = already_downloaded(folder, ep_filename, series_url=url)
        if done:
            safe_print(render_message('already_saved'))
            summary.add_skipped()
            continue

        if not direct:
            direct = ResolverRegistry.resolve(ep_url, session)

        if not direct:
            safe_print(f"  [X] Could not resolve stream link")
            record_episode_failure(url, title_text, ep_filename, summary, ep_label)
            continue

        download_file(direct, folder, ep_filename, summary,
                      series_url=url, series_name=title_text,
                      bandwidth_limit=bw, quality=quality,
                      current_process=cur_proc,
                      stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'))

    if summary.failed == 0 and not _stopped(ctx):
        mark_series_complete(url)
    summary.report()
