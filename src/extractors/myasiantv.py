from .base import *

def extract_myasiantv(url, session, ctx=None):
    ctx = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    safe_print(render_message('site_mode', site='MyAsianTV'))
    slug = url.rstrip('/').split('/')[-1]
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

    for i, ep_url in enumerate(ep_links, 1):
        if _stopped(ctx):
            break
        _wait(ctx)
        ep_name = ep_url.rstrip('/').split('/')[-1]
        safe_print(f"\n[{i}/{len(ep_links)}] {ep_name}")
        done, _ = already_downloaded(folder, safe_filename(f"{ep_name}.mp4"), series_url=url)
        if not done:
            done, _ = already_downloaded(folder, safe_filename(f"{ep_name}.mkv"), series_url=url)
        if done:
            safe_print(render_message('already_saved'))
            summary.add_skipped()
            continue
        r = safe_get(session, ep_url, referer=bd + '/', timeout=30)
        if r is None:
            safe_print(f"  [X] Could not fetch episode page")
            summary.add_failed(ep_name)
            continue
        soup   = BeautifulSoup(r.text, 'html.parser')
        iframe = soup.find('iframe', src=re.compile(r'vidbasic|vidmoly')) or soup.find('iframe', src=True)
        if not iframe:
            safe_print(f"  [!] No iframe found")
            summary.add_failed(ep_name)
            continue
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
            direct = ResolverRegistry.resolve(src, session)
        finally:
            if old_referer is not None:
                session.headers['Referer'] = old_referer
            else:
                session.headers.pop('Referer', None)
        
        if direct:
            ext = 'mkv' if '.mkv' in direct.lower() else 'mp4'
            download_file(direct, folder, safe_filename(f"{ep_name}.{ext}"), summary,
                          series_url=url, series_name=name,
                          bandwidth_limit=bw, quality=quality,
                          current_process=cur_proc, stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'))
        else:
            safe_print(f"  [X] Could not extract video")
            summary.add_failed(ep_name)
        time.sleep(1)
    if summary.failed == 0 and not _stopped(ctx):
        mark_series_complete(url)
    summary.report()
