from .base import *

def extract_myasiantv(url, session, ctx=None):
    ctx = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    safe_print(render_message('site_mode', site='MyAsianTV'))
    slug = url_slug(url)
    name = re.sub(r'-episode-\d+.*$', '', slug)
    name = re.sub(r'-\d{4}.*$', '', name)
    name = clean_name(name)
    safe_print(f"[*] Series: {name}")
    folder  = os.path.join(BASE_DIR, safe_filename(name))
    bd      = base_domain(url)
    summary = DownloadSummary()

    if 'episode-' in url:
        ep_links = [url]
        safe_print(f"[*] Saving to: {folder}")
    else:
        safe_print(render_message('fetching_episode_list'))
        r = safe_get(session, url, referer=bd + '/', timeout=30)
        if r is None:
            return
        soup      = BeautifulSoup(r.text, 'html.parser')
        show_slug = re.sub(r'-\d{4}.*$', '', slug)
        ep_links  = list(dict.fromkeys(
            urljoin(bd, a['href']) for a in soup.find_all('a', href=True)
            if ('episode-' in a['href'].lower() and show_slug.lower() in a['href'].lower() and (bd in a['href'] or a['href'].startswith('/')))
        ))
        if not ep_links:
            safe_print(render_message('no_episode_links'))
            return
        ep_links.sort(key=lambda u: int(m.group(1)) if (m := re.search(r'episode-(\d+)', u)) else 0)
        ep_links = _filter_by_episode_range(ep_links, ctx)
        if not ep_links:
            safe_print(render_message('no_episodes_in_range'))
            return
        safe_print(f"[*] Found {len(ep_links)} episode(s) - saving to: {folder}")
    _notify_start(name, len(ep_links))

    def _resolve_ep(ep_url):
        """Fetch the episode page, find the player iframe and resolve it to a
        direct video url. Runs in the prefetch thread so the (slow) embed
        extraction overlaps the previous episode's download. Returns the direct
        url or None.

        The referer swap must happen in whichever thread does the resolve, so
        it lives here. Only one prefetch thread runs at a time (get() joins the
        previous before the next is queued), and download_file never touches the
        session — so this mutation of session.headers is not racing anything."""
        r = safe_get(session, ep_url, referer=bd + '/', timeout=30)
        if r is None:
            return None
        soup   = BeautifulSoup(r.text, 'html.parser')
        # Prefer a known player host; the current .com.ro layout serves vidb.top
        # (vidbasic) and kissasian9.ro embeds. Fall back to the first iframe so a
        # new host still gets a resolve attempt rather than a hard failure.
        iframe = (soup.find('iframe', src=re.compile(r'vidbasic|vidb\.|kissasian|vidmoly|megaplay|kisskh'))
                  or soup.find('iframe', src=True))
        if not iframe:
            return None
        src = iframe.get('src', '')
        if src.startswith('//'):
            # protocol-relative: //host/path -> https://host/path
            src = 'https:' + src
        elif not src.startswith('http'):
            # root-relative (/embed/x) or relative: resolve against the episode URL.
            # 'https:' + '/embed/x' would produce the malformed 'https:/embed/x'.
            src = urljoin(ep_url, src)
        # Megaplay/iframe players require the episode URL as referer to avoid "Embed Only" block
        old_referer = session.headers.get('Referer')
        session.headers['Referer'] = ep_url
        try:
            return ResolverRegistry.resolve(src, session)
        finally:
            if old_referer is not None:
                session.headers['Referer'] = old_referer
            else:
                session.headers.pop('Referer', None)

    def _cdn_alive(cdn_url, referer):
        """Ranged GET to confirm the resolved link is still live before download.
        The embed CDN keys off the episode referer, so pass it through — without
        it a healthy link can 403 and trigger a needless re-resolve."""
        try:
            r = session.get(cdn_url, timeout=5, allow_redirects=True,
                            headers={'Range': 'bytes=0-0', 'Referer': referer})
            return r.status_code in (200, 206)
        except Exception:
            return False

    # Build the work-list first — skip checks are local (no network) so the
    # prefetcher never wastes an embed extraction on an episode we'd skip.
    work = []
    for i, ep_url in enumerate(ep_links, 1):
        ep_name = ep_url.rstrip('/').split('/')[-1]
        done, _ = already_downloaded(folder, safe_filename(f"{ep_name}.mp4"), series_url=url)
        if not done:
            done, _ = already_downloaded(folder, safe_filename(f"{ep_name}.mkv"), series_url=url)
        if done:
            safe_print(f"\n[{i}/{len(ep_links)}] {ep_name}")
            safe_print(render_message('already_saved'))
            summary.add_skipped()
            continue
        work.append((ep_name, ep_url))

    # Prefetch the next episode's resolve while the current one downloads. get()
    # runs for every item before any failure branch, keeping the queue aligned.
    prefetcher = Prefetcher(_resolve_ep)
    if work:
        prefetcher.prefetch(work[0][1])

    for i, (ep_name, ep_url) in enumerate(work, 1):
        if _stopped(ctx):
            break
        _wait(ctx)
        safe_print(f"\n[{i}/{len(work)}] {ep_name}")

        direct = prefetcher.get(timeout=30)
        if i < len(work):
            prefetcher.prefetch(work[i][1])

        # Prefetch is an optimization, not the source of truth. If it came back
        # empty (usually a transient network blip in the background thread) or
        # the token aged out during the previous download, resolve fresh here so
        # the resolver's network-aware retry can ride out a dropped connection
        # instead of failing every remaining episode at once.
        if not direct or (not ('.m3u8' in direct.lower() or 'manifest' in direct.lower()) and not _cdn_alive(direct, ep_url)):
            if direct:
                safe_print(f"  [*] Link expired - re-resolving...")
            direct = resolve_with_retry(_resolve_ep, ep_url, ctx)
            if not direct:
                if _stopped(ctx):
                    break
                safe_print(f"  [X] Could not extract video")
                record_episode_failure(url, name, safe_filename(f"{ep_name}.mp4"), summary, ep_name)
                continue

        ext = 'mkv' if '.mkv' in direct.lower() else 'mp4'
        download_file(direct, folder, safe_filename(f"{ep_name}.{ext}"), summary,
                      series_url=url, series_name=name,
                      bandwidth_limit=bw, quality=quality,
                      current_process=cur_proc, stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'))
    if summary.failed == 0 and not _stopped(ctx):
        mark_series_complete(url)
    summary.report()
