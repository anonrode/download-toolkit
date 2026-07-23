from .base import *

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

    # Method 1: direct waffi.cloud links (CDN subdomain rotates — drip, japa, etc.)
    # Dedup by href: a page can expose the same episode under two anchors
    # (e.g. quality variants), which would double-count and skew episode indexing.
    waffi_links = list(dict.fromkeys(
        (a.text.strip(), a['href']) for a in soup.find_all('a', href=True)
        if WAFFI_CLOUD_RE.search(a['href'])))
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
    # as 9jaRocks). dramakey still links the dead loadedfiles.org host; the
    # resolver rewrites .org -> the live .st host, so these resolve fine.
    lf_links = list(dict.fromkeys(
        (a.text.strip(), a['href']) for a in soup.find_all('a', href=True)
        if 'loadedfiles.st' in a['href'] or 'loadedfiles.org' in a['href']))
    if lf_links:
        lf_links = _filter_by_episode_range(lf_links, ctx)
        if not lf_links:
            safe_print(render_message('no_episodes_in_range'))
            return
        safe_print(f"[*] Found {len(lf_links)} loadedfiles link(s) - saving to: {folder}")
        _notify_start(name, len(lf_links))
        for i, (label, ep_url) in enumerate(lf_links, 1):
            if _stopped(ctx): break
            _wait(ctx)
            fname = safe_filename(f"{name} {_episode_label(ep_url, label, i)}.mp4")
            safe_print(f"\n[{i}/{len(lf_links)}] {fname}")
            done, _ = already_downloaded(folder, fname, series_url=url)
            if done:
                safe_print(render_message('already_saved'))
                summary.add_skipped()
                continue
            direct = ResolverRegistry.resolve(ep_url, session)
            if direct:
                ext = 'mkv' if '.mkv' in direct else 'mp4'
                fname = safe_filename(f"{name} {_episode_label(ep_url, label, i)}.{ext}")
                download_file(direct, folder, fname, summary,
                              series_url=url, series_name=name,
                              bandwidth_limit=bw, quality=quality, current_process=cur_proc,
                              stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'),
                              source_url=ep_url)
            else:
                safe_print(f"  [X] Could not resolve link")
                summary.add_failed(fname)
            time.sleep(0.5)
        if summary.failed == 0 and not _stopped(ctx):
            mark_series_complete(url)
        summary.report()
        return

    # Method 2: downloadwella.com / wetafiles.com intermediate links
    dw_links = list(dict.fromkeys(
        (a.text.strip(), a['href']) for a in soup.find_all('a', href=True)
        if 'downloadwella.com' in a['href'] or 'wetafiles.com' in a['href']))
    if dw_links:
        dw_links = _filter_by_episode_range(dw_links, ctx)
        if not dw_links:
            safe_print(render_message('no_episodes_in_range'))
            return
        safe_print(f"[*] Found {len(dw_links)} downloadwella link(s) - saving to: {folder}")
        _notify_start(name, len(dw_links))
        for i, (label, ep_url) in enumerate(dw_links, 1):
            if _stopped(ctx): break
            _wait(ctx)
            fname = safe_filename(f"{name} {_episode_label(ep_url, label, i)}.mp4")
            safe_print(f"\n[{i}/{len(dw_links)}] {fname}")
            done, _ = already_downloaded(folder, fname, series_url=url)
            if done:
                safe_print(render_message('already_saved'))
                summary.add_skipped()
                continue
            direct = ResolverRegistry.resolve(ep_url, session)
            if direct:
                ext = 'mkv' if '.mkv' in direct else 'mp4'
                fname = safe_filename(f"{name} {_episode_label(ep_url, label, i)}.{ext}")
                download_file(direct, folder, fname, summary,
                              series_url=url, series_name=name,
                              bandwidth_limit=bw, quality=quality, current_process=cur_proc,
                              stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'),
                              source_url=ep_url)
            else:
                safe_print(f"  [X] Could not resolve link")
                summary.add_failed(fname)
            time.sleep(0.5)
        if summary.failed == 0 and not _stopped(ctx):
            mark_series_complete(url)
        summary.report()
        return

    # Method 3: /download intermediate pages (legacy layout fallback)
    dl_links = list(dict.fromkeys(
        (a.text.strip(), a['href']) for a in soup.find_all('a', href=True)
        if any(x in a['href'] for x in
               [f'{DRAMARAIN_DOMAIN}/download', f'{DRAMAKEY_CC}/download'])))
    if dl_links:
        dl_links = _filter_by_episode_range(dl_links, ctx)
        if not dl_links:
            safe_print(render_message('no_episodes_in_range'))
            return
        safe_print(f"[*] Found {len(dl_links)} episode(s) - saving to: {folder}")
        _notify_start(name, len(dl_links))
        for i, (label, dl_url) in enumerate(dl_links, 1):
            if _stopped(ctx): break
            _wait(ctx)
            fname = safe_filename(f"{name} {_episode_label(dl_url, label, i)}.mp4")
            safe_print(f"\n[{i}/{len(dl_links)}] {fname}")
            done, _ = already_downloaded(folder, fname, series_url=url)
            if done:
                safe_print(render_message('already_saved'))
                summary.add_skipped()
                continue
            direct = ResolverRegistry.resolve(dl_url, session)
            if direct:
                ext = 'mkv' if '.mkv' in direct else 'mp4'
                fname = safe_filename(f"{name} {_episode_label(dl_url, label, i)}.{ext}")
                download_file(direct, folder, fname, summary,
                              series_url=url, series_name=name,
                              bandwidth_limit=bw, quality=quality, current_process=cur_proc,
                              stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'),
                              source_url=dl_url)
            else:
                safe_print(f"  [X] Could not resolve link")
                summary.add_failed(fname)
            time.sleep(0.5)
        if summary.failed == 0 and not _stopped(ctx):
            mark_series_complete(url)
        summary.report()
        return

    safe_print(f"[!] No download links found")
    diagnose_page(soup, url, "loadedfiles, waffi.cloud or downloadwella.com links")
