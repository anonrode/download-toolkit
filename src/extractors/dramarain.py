from .base import *

# ─── dramakey.cc .episode-item layout helpers ─────────────────
_DIRECT_CDN_HOSTS = ('nkiserv.com', 'waffi.cloud', 'fileditchfiles.')
_INTERMEDIATE_HOSTS = ('downloadwella.com', 'wetafiles.com', 'loadedfiles.',
                       'dramakey.cc', 'dramakey.com', 'dramarain.com')

def _is_direct_cdn_url(url):
    """True if the URL is a direct media file needing no resolver."""
    parsed = urlparse(url)
    path = parsed.path.lower()
    host = parsed.netloc.lower()
    if any(h in host for h in _DIRECT_CDN_HOSTS):
        return True
    if any(path.endswith(ext) for ext in ('.mp4', '.mkv', '.m3u8', '.webm')):
        if not any(g in host for g in _INTERMEDIATE_HOSTS):
            return True
    return False

def _parse_episode_items(soup):
    """Parse .episode-item containers into [(label, href), ...].

    Keys by episode number from the container, NOT by href.  Two episodes
    sharing the same href produce two distinct rows with a warning.
    """
    items = []
    seen_hrefs = {}
    for el in soup.select('.episode-item'):
        num_el = el.select_one('.episode-number')
        btn    = el.select_one('a.download-btn')
        if not btn or not btn.get('href'):
            continue
        href = btn['href'].strip()

        # Episode label from container metadata
        label = num_el.get_text(strip=True) if num_el else ''
        if not label:
            key_attr = el.get('key', '')
            km = re.match(r'ep-(\d+)-(\d+)', key_attr)
            if km:
                label = f"Episode {km.group(2)}"

        # Warn on duplicate href but keep both rows
        display = label or '?'
        if href in seen_hrefs:
            safe_print(f"  [!] Warning: {display} shares download link with "
                       f"{seen_hrefs[href]} (source-site mislabel)")
        else:
            seen_hrefs[href] = display

        items.append((label, href))
    return items

def extract_dramarain(url, session, ctx=None):
    ctx  = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)
    site = 'DramaKey.cc' if DRAMAKEY_CC in url else 'DramaRain'
    safe_print(f"[*] {site} mode")

    slug   = url_slug(url)
    name   = re.sub(r'-(chinese|korean|thai|japanese|drama|tvshows|movies?).*$', '', slug, flags=re.IGNORECASE)
    name   = clean_name(name)
    safe_print(f"[*] Title: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))

    site_referer = f'https://{DRAMAKEY_CC}/' if DRAMAKEY_CC in url else f'https://{DRAMARAIN_DOMAIN}/'
    r = safe_get(session, url, referer=site_referer)
    if r is None:
        return
    soup    = BeautifulSoup(r.text, 'html.parser')
    summary = DownloadSummary()

    def _resolve_ep(ep_url):
        """Resolve an intermediate link (loadedfiles / downloadwella / /download)
        to a direct CDN url. Runs in the prefetch thread while the previous
        episode downloads."""
        return ResolverRegistry.resolve(ep_url, session)

    def _cdn_alive(cdn_url):
        """Ranged GET to confirm the CDN link is still live before download —
        the link was resolved one episode ago, so its token may have aged out."""
        try:
            rr = session.get(cdn_url, timeout=5, allow_redirects=True,
                             headers={'Range': 'bytes=0-0'})
            return rr.status_code in (200, 206)
        except Exception:
            return False

    def _run_prefetch_loop(links, kind):
        """Shared resolve+download loop with background prefetch, used by every
        layout that resolves an intermediate link to a direct CDN url
        (loadedfiles, downloadwella, /download). The prefetch thread resolves the
        next episode while the current one downloads. The direct-CDN layouts
        (waffi, nkiserv) don't route here — they have nothing to resolve."""
        links = _filter_by_episode_range(links, ctx)
        if not links:
            safe_print(render_message('no_episodes_in_range'))
            return
        safe_print(f"[*] Found {len(links)} {kind} link(s) - saving to: {folder}")
        _notify_start(name, len(links))

        # Build work-list first — skip checks are local (disk + resume state, no
        # network) so the prefetcher never wastes a resolve on a skipped episode.
        work = []
        for i, (label, ep_url) in enumerate(links, 1):
            fbase = safe_filename(f"{name} {_episode_label(ep_url, label, i)}")
            done, _ = already_downloaded(folder, fbase + '.mp4', series_url=url)
            if not done:
                done, _ = already_downloaded(folder, fbase + '.mkv', series_url=url)
            if done:
                safe_print(f"\n[{i}/{len(links)}] {fbase}")
                safe_print(render_message('already_saved'))
                summary.add_skipped()
                continue
            work.append((fbase, ep_url))

        prefetcher = Prefetcher(_resolve_ep)
        if work:
            prefetcher.prefetch(work[0][1])
        for i, (fbase, ep_url) in enumerate(work, 1):
            if _stopped(ctx):
                break
            _wait(ctx)
            safe_print(f"\n[{i}/{len(work)}] {fbase}")
            direct = prefetcher.get(timeout=30)
            if i < len(work):
                prefetcher.prefetch(work[i][1])
            # Prefetch is an optimization, not the source of truth. If it came
            # back empty (usually a transient network blip in the background
            # thread) or the token aged out during the previous download,
            # resolve fresh here so the resolver's network-aware retry can ride
            # out a dropped connection instead of failing every remaining
            # episode at once.
            if not direct or not _cdn_alive(direct):
                if direct:
                    safe_print(f"  [*] CDN link expired - re-resolving...")
                direct = resolve_with_retry(lambda u: ResolverRegistry.resolve(u, session), ep_url, ctx)
                if not direct:
                    if _stopped(ctx):
                        break
                    safe_print(f"  [X] Could not resolve link")
                    record_episode_failure(url, name, safe_filename(f"{fbase}.mp4"), summary, fbase)
                    continue
            ext = 'mkv' if '.mkv' in direct else 'mp4'
            download_file(direct, folder, safe_filename(f"{fbase}.{ext}"), summary,
                          series_url=url, series_name=name,
                          bandwidth_limit=bw, quality=quality, current_process=cur_proc,
                          stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'),
                          source_url=ep_url)
        if summary.failed == 0 and not _stopped(ctx):
            mark_series_complete(url)
        summary.report()

    # ─── Method 0: dramakey.cc .episode-item layout ──────────────────
    # Parses each episode from its own container so mixed hosts and
    # mislabeled hrefs don't cause episodes to be dropped or merged.
    # Falls through to the host-bucket methods below for pages
    # (e.g. dramarain.com) that lack this layout.
    ep_items = _parse_episode_items(soup)
    if ep_items:
        ep_items = _filter_by_episode_range(ep_items, ctx)
        if not ep_items:
            safe_print(render_message('no_episodes_in_range'))
            return
        safe_print(f"[*] Found {len(ep_items)} episode(s) - saving to: {folder}")
        _notify_start(name, len(ep_items))

        for i, (label, ep_url) in enumerate(ep_items, 1):
            if _stopped(ctx):
                break
            _wait(ctx)
            # The .episode-number container text is authoritative for this
            # layout. The source site can point an episode's button at the
            # wrong file (e.g. E04's button -> the E05 href), so trusting the
            # URL's embedded SxxExx tag (as _episode_label does) would name
            # E04 "S01E05", collide with the real E05, and drop it. Key off
            # the container number instead; take the season from the URL if it
            # carries one, else default S01.
            lm = re.search(r'(\d+)', label or '')
            sm = EPISODE_TAG_RE.search(ep_url)
            if lm:
                season = int(sm.group(1)) if sm else 1
                ep_tag = f"S{season:02d}E{int(lm.group(1)):02d}"
            else:
                ep_tag = _episode_label(ep_url, label, i)
            fbase = safe_filename(f"{name} {ep_tag}")
            safe_print(f"\n[{i}/{len(ep_items)}] {fbase}")
            done, _ = already_downloaded(folder, fbase + '.mp4', series_url=url)
            if not done:
                done, _ = already_downloaded(folder, fbase + '.mkv', series_url=url)
            if done:
                safe_print(render_message('already_saved'))
                summary.add_skipped()
                continue

            if _is_direct_cdn_url(ep_url):
                direct = _strip_preview_param(ep_url)
            else:
                direct = resolve_with_retry(
                    lambda u: ResolverRegistry.resolve(u, session), ep_url, ctx)
                if not direct:
                    if _stopped(ctx):
                        break
                    safe_print(f"  [X] Could not resolve link")
                    record_episode_failure(url, name,
                                           safe_filename(f"{fbase}.mp4"),
                                           summary, fbase)
                    continue

            ext = 'mkv' if '.mkv' in direct else 'mp4'
            download_file(direct, folder, safe_filename(f"{fbase}.{ext}"),
                          summary, series_url=url, series_name=name,
                          bandwidth_limit=bw, quality=quality,
                          current_process=cur_proc,
                          stop_flag=stop, pause_flag=pause,
                          wait_fn=ctx.get('wait'), source_url=ep_url)

        if summary.failed == 0 and not _stopped(ctx):
            mark_series_complete(url)
        summary.report()
        return

    # Method 1: direct waffi.cloud links (CDN subdomain rotates — drip, japa, etc.)
    # Dedup by href: a page can expose the same episode under two anchors
    # (e.g. quality variants), which would double-count and skew episode indexing.
    waffi_links = _dedup_by_href(
        (a.text.strip(), a['href']) for a in soup.find_all('a', href=True)
        if WAFFI_CLOUD_RE.search(a['href']))
    if waffi_links:
        waffi_links = _filter_by_episode_range(waffi_links, ctx)
        if not waffi_links:
            safe_print(render_message('no_episodes_in_range'))
            return
        safe_print(f"[*] Found {len(waffi_links)} direct link(s) - saving to: {folder}")
        _notify_start(name, len(waffi_links))
        for i, (label, link) in enumerate(waffi_links, 1):
            if _stopped(ctx): break
            _wait(ctx)
            direct = _strip_preview_param(link)
            ext = 'mkv' if '.mkv' in direct else 'mp4'
            fname = safe_filename(f"{name} {_episode_label(link, label, i)}.{ext}")
            safe_print(f"\n[{i}/{len(waffi_links)}] {fname}")
            done, _ = already_downloaded(folder, fname, series_url=url)
            if done:
                safe_print(render_message('already_saved'))
                summary.add_skipped()
                continue
            download_file(direct, folder, fname, summary,
                          series_url=url, series_name=name,
                          bandwidth_limit=bw, quality=quality, current_process=cur_proc,
                          stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'),
                          source_url=link)
        if summary.failed == 0 and not _stopped(ctx):
            mark_series_complete(url)
        summary.report()
        return

    # Method 1b: loadedfiles links (current dramakey.cc layout — same files/host
    # as 9jaRocks). loadedfiles rotates TLDs; the resolver rewrites any TLD to
    # the live .st host, so we match generically here.
    lf_links = _dedup_by_href(
        (a.text.strip(), a['href']) for a in soup.find_all('a', href=True)
        if re.search(r'loadedfiles\.[a-z0-9-]+', a['href'], re.I))
    # Skip links that are already error pages on the host
    _dead = [(l, h) for l, h in lf_links if 'error?e=' in h or 'errore=' in h]
    lf_links = [(l, h) for l, h in lf_links if 'error?e=' not in h and 'errore=' not in h]
    for _, _h in _dead:
        _msg = _h.split('error?e=')[-1].replace('+', ' ').rstrip('.')
        safe_print(f"  [!] File removed by host: {_msg}")
    if lf_links:
        _run_prefetch_loop(lf_links, 'loadedfiles')
        return

    # Method 1c: nkiserv.com direct CDN links (current dramakey.cc layout —
    # same files as NKiri). These are direct download URLs, no resolver needed.
    nk_links = _dedup_by_href(
        (a.text.strip(), a['href']) for a in soup.find_all('a', href=True)
        if 'nkiserv.com' in a['href'])
    if nk_links:
        nk_links = _filter_by_episode_range(nk_links, ctx)
        if not nk_links:
            safe_print(render_message('no_episodes_in_range'))
            return
        safe_print(f"[*] Found {len(nk_links)} nkiserv link(s) - saving to: {folder}")
        _notify_start(name, len(nk_links))
        for i, (label, ep_url) in enumerate(nk_links, 1):
            if _stopped(ctx): break
            _wait(ctx)
            ext = 'mkv' if '.mkv' in ep_url else 'mp4'
            fname = safe_filename(f"{name} {_episode_label(ep_url, label, i)}.{ext}")
            safe_print(f"\n[{i}/{len(nk_links)}] {fname}")
            done, _ = already_downloaded(folder, fname, series_url=url)
            if done:
                safe_print(render_message('already_saved'))
                summary.add_skipped()
                continue
            download_file(ep_url, folder, fname, summary,
                          series_url=url, series_name=name,
                          bandwidth_limit=bw, quality=quality, current_process=cur_proc,
                          stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'),
                          source_url=ep_url)
        if summary.failed == 0 and not _stopped(ctx):
            mark_series_complete(url)
        summary.report()
        return

    # Method 2: downloadwella.com / wetafiles.com intermediate links
    dw_links = _dedup_by_href(
        (a.text.strip(), a['href']) for a in soup.find_all('a', href=True)
        if 'downloadwella.com' in a['href'] or 'wetafiles.com' in a['href'])
    if dw_links:
        _run_prefetch_loop(dw_links, 'downloadwella')
        return

    # Method 3: /download intermediate pages (legacy layout fallback)
    dl_links = _dedup_by_href(
        (a.text.strip(), a['href']) for a in soup.find_all('a', href=True)
        if any(x in a['href'] for x in
               [f'{DRAMARAIN_DOMAIN}/download', f'{DRAMAKEY_CC}/download']))
    if dl_links:
        _run_prefetch_loop(dl_links, 'episode')
        return

    safe_print(f"[!] No download links found")
    diagnose_page(soup, url, "loadedfiles, waffi.cloud, downloadwella.com or nkiserv.com links")
