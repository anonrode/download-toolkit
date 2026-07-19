from .base import *

def extract_9jarocks(url, session, ctx=None):
    ctx = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    safe_print(render_message('site_mode', site='9jaRocks'))
    slug   = url.rstrip('/').split('/')[-1]
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
        if 'loadedfiles.st' in a['href'] or 'loadedfiles.org' in a['href']
    ))
    if not lf_links:
        safe_print(render_message('no_episode_links'))
        diagnose_page(soup, url, "loadedfiles.st links")
        return
    lf_links = _filter_by_episode_range(lf_links, ctx)
    if not lf_links:
        safe_print(render_message('no_episodes_in_range'))
        return
    safe_print(f"[*] Found {len(lf_links)} file(s) - saving to: {folder}")
    _notify_start(name, len(lf_links))
    summary = DownloadSummary()

    for i, (label, lf_url) in enumerate(lf_links, 1):
        if _stopped(ctx):
            break
        _wait(ctx)
        # Extract from URL slug first (has real episode name), anchor text is always "DOWNLOAD"
        slug_part = lf_url.rstrip('/').split('/')[-1]
        # Strip extension from slug — will re-add with correct ext to avoid .mkv.mkv
        base_fname = re.sub(r'\.(mkv|mp4|webm)$', '', safe_filename(slug_part))
        safe_print(f"\n[{i}/{len(lf_links)}] {base_fname}")
        done, _ = already_downloaded(folder, base_fname + '.mp4', series_url=url)
        if not done:
            done, _ = already_downloaded(folder, base_fname + '.mkv', series_url=url)
        if done:
            safe_print(render_message('already_saved'))
            summary.add_skipped()
            continue
        direct = ResolverRegistry.resolve(lf_url, session)
        if direct:
            ext = 'mkv' if '.mkv' in direct else 'mp4'
            download_file(direct, folder, safe_filename(f"{base_fname}.{ext}"), summary,
                          series_url=url, series_name=name,
                          bandwidth_limit=bw, quality=quality, current_process=cur_proc,
                          stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'),
                          source_url=lf_url)
        else:
            safe_print(f"  [X] Could not extract: {base_fname}")
            summary.add_failed(base_fname)
        time.sleep(0.5)
    if summary.failed == 0 and not _stopped(ctx):
        mark_series_complete(url)
    summary.report()
