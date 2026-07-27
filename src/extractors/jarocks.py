from .base import *

def extract_9jarocks(url, session, ctx=None):
    ctx = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    safe_print(render_message('site_mode', site='9jaRocks'))
    slug   = url_slug(url)
    name   = clean_name(re.sub(r'-id\d+.*$', '', slug))
    safe_print(f"[*] Title: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))

    r = safe_get(session, url, referer=f'https://{JAROCKS_DOMAIN}/')
    if r is None:
        return
    soup = BeautifulSoup(r.text, 'html.parser')
    lf_links = list(dict.fromkeys(
        (a.get_text(strip=True), a['href'])
        for a in soup.find_all('a', href=True)
        if re.search(r'loadedfiles\.[a-z0-9-]+', a['href'], re.I)
    ))
    # Skip links that are already error pages on the host
    dead = [(label, href) for label, href in lf_links if 'error?e=' in href or 'errore=' in href]
    lf_links = [(label, href) for label, href in lf_links if 'error?e=' not in href and 'errore=' not in href]
    for _, href in dead:
        msg = href.split('error?e=')[-1].replace('+', ' ').rstrip('.')
        safe_print(f"  [!] File removed by host: {msg}")
    if not lf_links:
        safe_print(render_message('no_episode_links'))
        diagnose_page(soup, url, "loadedfiles links")
        return
    lf_links = _filter_by_episode_range(lf_links, ctx)
    if not lf_links:
        safe_print(render_message('no_episodes_in_range'))
        return
    safe_print(f"[*] Found {len(lf_links)} file(s) - saving to: {folder}")
    _notify_start(name, len(lf_links))
    summary = DownloadSummary()

    def _resolve_ep(lf_url):
        """Resolve a loadedfiles link to a direct CDN url. Runs in the prefetch
        thread while the previous episode downloads, so the network round-trip
        overlaps the download instead of stalling between episodes."""
        return ResolverRegistry.resolve(lf_url, session)

    def _cdn_alive(cdn_url):
        """Ranged GET to confirm the CDN link is still live before handing it to
        the downloader. The link was resolved one episode ago (while the previous
        file downloaded), so its token may have aged out — catch that here and
        re-resolve rather than failing the download."""
        try:
            r = session.get(cdn_url, timeout=5, allow_redirects=True,
                            headers={'Range': 'bytes=0-0'})
            return r.status_code in (200, 206)
        except Exception:
            return False

    # Build the work-list first. Skip checks are local (disk + resume state, no
    # network), so filtering here means the prefetcher never wastes a resolve on
    # an episode we'd only skip — matters on resume runs where most are done.
    work = []
    for i, (label, lf_url) in enumerate(lf_links, 1):
        # Anchor text is the episode code (e.g. S01E01) — use it when it's not generic
        label_clean = label.strip() if label else ''
        if label_clean and not re.fullmatch(r'download', label_clean, re.I):
            base_fname = safe_filename(f"{name} - {label_clean}")
        else:
            slug_part = lf_url.rstrip('/').split('/')[-1]
            base_fname = re.sub(r'\.(mkv|mp4|webm)$', '', safe_filename(slug_part))
            if re.fullmatch(r'[0-9a-f]{8,}', base_fname, re.I):
                base_fname = safe_filename(f"{name} - {i:02d}")
        done, _ = already_downloaded(folder, base_fname + '.mp4', series_url=url)
        if not done:
            done, _ = already_downloaded(folder, base_fname + '.mkv', series_url=url)
        if done:
            safe_print(f"\n[{i}/{len(lf_links)}] {base_fname}")
            safe_print(render_message('already_saved'))
            summary.add_skipped()
            continue
        work.append((base_fname, lf_url))

    # Prefetch each episode's resolve in the background while the current one
    # downloads. get() is called for every item before any failure/skip branch,
    # so the prefetch queue never drifts out of alignment with the loop.
    prefetcher = Prefetcher(_resolve_ep)
    if work:
        prefetcher.prefetch(work[0][1])

    for i, (base_fname, lf_url) in enumerate(work, 1):
        if _stopped(ctx):
            break
        _wait(ctx)
        safe_print(f"\n[{i}/{len(work)}] {base_fname}")

        # Consume this episode's prefetched resolve, then kick off the next one.
        direct = prefetcher.get(timeout=30)
        if i < len(work):
            prefetcher.prefetch(work[i][1])

        if not direct:
            safe_print(f"  [X] Could not extract: {base_fname}")
            summary.add_failed(base_fname)
            continue

        # Token may have expired while the previous episode downloaded.
        if not _cdn_alive(direct):
            safe_print(f"  [*] CDN link expired - re-resolving...")
            direct = ResolverRegistry.resolve(lf_url, session)
            if not direct:
                safe_print(f"  [X] Could not extract: {base_fname}")
                summary.add_failed(base_fname)
                continue

        ext = 'mkv' if '.mkv' in direct else 'mp4'
        download_file(direct, folder, safe_filename(f"{base_fname}.{ext}"), summary,
                      series_url=url, series_name=name,
                      bandwidth_limit=bw, quality=quality, current_process=cur_proc,
                      stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'),
                      source_url=lf_url)
    if summary.failed == 0 and not _stopped(ctx):
        mark_series_complete(url)
    summary.report()
