from .base import *

def extract_naijaprey(url, session, ctx=None):
    ctx = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    safe_print("[*] NaijaPrey mode")
    slug   = url.rstrip('/').split('/')[-1]
    name   = clean_name(slug)
    safe_print(f"[*] Title: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))

    r = safe_get(session, url, referer=f'https://www.{NAIJAPREY_DOMAIN}/')
    if r is None:
        return
    soup = BeautifulSoup(r.text, 'html.parser')
    ep_links = list(dict.fromkeys(
        a['href'] for a in soup.find_all('a', href=True)
        if 'vdl.np-downloader.com' in a['href']
    ))
    safe_print(f"[*] Found {len(ep_links)} episode(s) — saving to: {folder}")
    ep_links = _filter_by_episode_range(ep_links, ctx)
    summary = DownloadSummary()

    for i, ep_url in enumerate(ep_links, 1):
        if _stopped(ctx):
            break
        _wait(ctx)
        ep_name = ep_url.rstrip('/').split('/')[-1]
        safe_print(f"\n[{i}/{len(ep_links)}] {ep_name}")

        # Early skip before hitting the intermediate page
        done, _ = already_downloaded(folder, f"{ep_name}.mp4", series_url=url)
        if not done:
            done, _ = already_downloaded(folder, f"{ep_name}.mkv", series_url=url)
        if done:
            safe_print(f"  [✓] Already downloaded — skipping")
            summary.add_skipped()
            continue

        try:
            r2 = safe_get(session, ep_url, referer=f'https://www.{NAIJAPREY_DOMAIN}/')
            if not r2:
                summary.add_failed(ep_name)
                continue
            soup2  = BeautifulSoup(r2.text, 'html.parser')
            ws_url = next((a['href'] for a in soup2.find_all('a', href=True)
                           if 'wildshare.net' in a['href']), None)
            if ws_url:
                direct = ResolverRegistry.resolve(ws_url, session)
                if direct:
                    ext = 'mkv' if '.mkv' in direct else 'mp4'
                    download_file(direct, folder, safe_filename(f"{ep_name}.{ext}"), summary,
                                  series_url=url, series_name=name,
                                  bandwidth_limit=bw, current_process=cur_proc,
                                  stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'))
                else:
                    safe_print(f"  [✗] Wildshare failed")
                    summary.add_failed(ep_name)
            else:
                safe_print(f"  [!] No wildshare link found")
                summary.add_failed(ep_name)
        except Exception as e:
            safe_print(f"  [!] Error: {e}")
            summary.add_failed(ep_name)
        time.sleep(1)
    if summary.failed == 0 and not _stopped(ctx):
        mark_series_complete(url)
    summary.report()
