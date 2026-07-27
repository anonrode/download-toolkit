from .base import *

def extract_naijaprey(url, session, ctx=None):
    ctx = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    safe_print(render_message('site_mode', site='NaijaPrey'))
    slug   = url_slug(url)
    name   = clean_name(slug)
    safe_print(f"[*] Title: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))

    r = safe_get(session, url, referer=f'https://www.{NAIJAPREY_DOMAIN}/')
    if r is None:
        return
    soup = BeautifulSoup(r.text, 'html.parser')
    if '/category/' in url:
        _NON_POST = ('/category/', '/page/', '/tag/', '/author/', '/feed',
                     '/contact', '/about', '/privacy', '/dmca', '/disclaimer',
                     '/terms', '/wp-login', '/wp-admin', '/request', '#')
        post_links = list(dict.fromkeys(
            a['href'] for a in soup.find_all('a', href=True)
            if NAIJAPREY_DOMAIN in a['href']
            and not any(bad in a['href'].lower() for bad in _NON_POST)
            and a['href'].rstrip('/') != f'https://www.{NAIJAPREY_DOMAIN}'
            and a['href'].rstrip('/') != f'https://{NAIJAPREY_DOMAIN}'
        ))
        if post_links:
            safe_print(f"[*] Category Hub Page detected. Processing {len(post_links)} post(s)...")
            for post_url in post_links:
                if _stopped(ctx):
                    break
                extract_naijaprey(post_url, session, ctx)
            return

    ep_links = list(dict.fromkeys(
        a['href'] for a in soup.find_all('a', href=True)
        if 'vdl.np-downloader.com' in a['href']
    ))
    if not ep_links:
        safe_print(render_message('no_episode_links'))
        diagnose_page(soup, url, "vdl.np-downloader.com links")
        return
    ep_links = _filter_by_episode_range(ep_links, ctx)
    if not ep_links:
        safe_print(render_message('no_episodes_in_range'))
        return
    safe_print(f"[*] Found {len(ep_links)} episode(s) - saving to: {folder}")
    _notify_start(name, len(ep_links))
    summary = DownloadSummary()

    def _resolve_ep(ep_url):
        """Full resolve chain for one episode (runs in the prefetch thread):
        fetch the intermediate page, follow the optional 2-hop 'Proceed to
        Download' link, find the wildshare link and resolve it to a direct CDN
        url. Returns (ws_url, direct) or (None, None)."""
        try:
            r2 = safe_get(session, ep_url, referer=f'https://www.{NAIJAPREY_DOMAIN}/')
            if not r2:
                return None, None
            soup2  = BeautifulSoup(r2.text, 'html.parser')
            ws_url = next((a['href'] for a in soup2.find_all('a', href=True)
                           if 'wildshare.net' in a['href']), None)
            # Two-hop: some pages show a "Proceed to Download Page" link
            # to a /d/ path instead of a direct wildshare link.
            if not ws_url:
                hop2 = next((a['href'] for a in soup2.find_all('a', href=True)
                             if 'np-downloader.com/d/' in a['href']), None)
                if hop2:
                    r3 = safe_get(session, hop2, referer=ep_url)
                    if r3:
                        soup3 = BeautifulSoup(r3.text, 'html.parser')
                        ws_url = next((a['href'] for a in soup3.find_all('a', href=True)
                                       if 'wildshare.net' in a['href']), None)
            if not ws_url:
                return None, None
            return ws_url, ResolverRegistry.resolve(ws_url, session)
        except Exception:
            return None, None

    def _cdn_alive(cdn_url):
        """Ranged GET to confirm the resolved CDN link is still live before
        download — the link was resolved one episode ago while the previous
        file downloaded, so its token may have aged out."""
        try:
            r = session.get(cdn_url, timeout=5, allow_redirects=True,
                            headers={'Range': 'bytes=0-0'})
            return r.status_code in (200, 206)
        except Exception:
            return False

    # Build the work-list first — skip checks are local (disk + resume state, no
    # network), so the prefetcher never wastes a full 2-hop resolve chain on an
    # episode we'd only skip.
    work = []
    for i, ep_url in enumerate(ep_links, 1):
        ep_name = _hash_safe_name(ep_url.rstrip('/').split('/')[-1], i)
        done, _ = already_downloaded(folder, safe_filename(f"{ep_name}.mp4"), series_url=url)
        if not done:
            done, _ = already_downloaded(folder, safe_filename(f"{ep_name}.mkv"), series_url=url)
        if done:
            safe_print(f"\n[{i}/{len(ep_links)}] {ep_name}")
            safe_print(render_message('already_saved'))
            summary.add_skipped()
            continue
        work.append((ep_name, ep_url))

    # Prefetch each episode's resolve chain in the background while the current
    # one downloads. get() runs for every item before any failure branch, so
    # the prefetch queue stays aligned with the loop.
    prefetcher = Prefetcher(_resolve_ep)
    if work:
        prefetcher.prefetch(work[0][1])

    for i, (ep_name, ep_url) in enumerate(work, 1):
        if _stopped(ctx):
            break
        _wait(ctx)
        safe_print(f"\n[{i}/{len(work)}] {ep_name}")

        ws_url, direct = prefetcher.get(timeout=30)
        if i < len(work):
            prefetcher.prefetch(work[i][1])

        if not direct:
            safe_print(f"  [X] Could not resolve download link")
            summary.add_failed(ep_name)
            continue

        # Token may have expired while the previous episode downloaded.
        if not _cdn_alive(direct):
            safe_print(f"  [*] CDN link expired - re-resolving...")
            ws2, direct = _resolve_ep(ep_url)
            ws_url = ws2 or ws_url
            if not direct:
                safe_print(f"  [X] Could not resolve download link")
                summary.add_failed(ep_name)
                continue

        ext = 'mkv' if '.mkv' in direct else 'mp4'
        download_file(direct, folder, safe_filename(f"{ep_name}.{ext}"), summary,
                      series_url=url, series_name=name,
                      bandwidth_limit=bw, quality=quality, current_process=cur_proc,
                      stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'),
                      source_url=ws_url)

    if summary.failed == 0 and not _stopped(ctx):
        mark_series_complete(url)
    summary.report()
