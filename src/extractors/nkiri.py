from .base import *

def extract_nkiri(url, session, ctx=None):
    ctx  = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    safe_print("[*] NKiri/TheNkiri mode")
    slug = url.rstrip('/').split('/')[-1]
    name = re.sub(r'-(korean|complete|drama|series|nollywood|hollywood|tv|movie).*$', '', slug, flags=re.IGNORECASE)
    name = clean_name(name)
    safe_print(f"[*] Title: {name}")
    folder  = os.path.join(BASE_DIR, safe_filename(name))
    summary = DownloadSummary()

    r = safe_get(session, url, timeout=20, referer='https://thenkiri.com/')
    if r is None:
        safe_print("[!] Could not fetch page")
        return
    soup = BeautifulSoup(r.text, 'html.parser')

    # Priority 1: downloadwella/wetafiles links (most common across full catalogue)
    dw_links = list(dict.fromkeys(
        a['href'] for a in soup.find_all('a', href=True)
        if 'downloadwella.com' in a['href'] or 'wetafiles.com' in a['href']
    ))
    if dw_links:
        dw_links = _filter_by_episode_range(dw_links, ctx)
        if not dw_links:
            safe_print("[!] No episodes matched that range")
            return
        safe_print(f"[*] Found {len(dw_links)} downloadwella link(s) — saving to: {folder}")
        _notify_start(name, len(dw_links))

        batch_size = max(1, parallel)
        batches    = [dw_links[i:i+batch_size] for i in range(0, len(dw_links), batch_size)]
        ep_index   = 0

        for batch in batches:
            if _stopped(ctx): break
            _wait(ctx)

            to_process = []
            for ep_url in batch:
                ep_index += 1
                ep_name = ep_url.split('/')[-1].replace('.html', '')
                ep_name = re.sub(r'\.(mkv|mp4)$', '', ep_name, flags=re.IGNORECASE)
                safe_print(f"\n[{ep_index}/{len(dw_links)}] {ep_name}")
                done, _ = already_downloaded(folder, safe_filename(f"{ep_name}.mp4"), series_url=url)
                if not done:
                    done, _ = already_downloaded(folder, safe_filename(f"{ep_name}.mkv"), series_url=url)
                if done:
                    safe_print(f"  [✓] Already downloaded — skipping")
                    summary.add_skipped()
                else:
                    to_process.append((ep_url, ep_name))

            if not to_process:
                continue

            if len(to_process) == 1 or batch_size == 1:
                ep_url, ep_name = to_process[0]
                direct = ResolverRegistry.resolve(ep_url, session)
                if direct:
                    ext = 'mkv' if '.mkv' in direct else 'mp4'
                    download_file(direct, folder, safe_filename(f"{ep_name}.{ext}"), summary,
                                  series_url=url, series_name=name,
                                  bandwidth_limit=bw, quality=quality,
                                  current_process=cur_proc,
                                  stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'),
                                  source_url=ep_url)
                else:
                    safe_print(f"  [✗] Could not extract link")
                    summary.add_failed(ep_name)
            else:
                from concurrent.futures import ThreadPoolExecutor, as_completed
                safe_print(f"\n  [*] Resolving {len(to_process)} link(s)...")
                resolved = {}
                with ThreadPoolExecutor(max_workers=min(len(to_process), 8)) as ex:
                    futures = {
                        ex.submit(ResolverRegistry.resolve, ep_url, session): (ep_url, ep_name)
                        for ep_url, ep_name in to_process
                    }
                    for f in as_completed(futures):
                        ep_url, ep_name = futures[f]
                        try:
                            resolved[(ep_url, ep_name)] = f.result()
                        except Exception:
                            resolved[(ep_url, ep_name)] = None

                items = []
                for (ep_url, ep_name), direct in resolved.items():
                    if direct:
                        ext = 'mkv' if '.mkv' in direct else 'mp4'
                        items.append((direct, safe_filename(f"{ep_name}.{ext}"), ep_url))
                    else:
                        safe_print(f"  [✗] Could not extract link: {ep_name}")
                        summary.add_failed(ep_name)

                if items:
                    per_thread_bw = (bw // len(items)) if bw else 0
                    ex = ThreadPoolExecutor(max_workers=min(len(items), 8))
                    tfutures = {}
                    for direct, fname, src_url in items:
                        thread_proc = ProcessContainer()
                        tfutures[ex.submit(
                            download_file,
                            direct, folder, fname, summary,
                            series_url=url, series_name=name,
                            bandwidth_limit=per_thread_bw, quality=quality,
                            current_process=thread_proc,
                            stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'),
                            parallel_mode=True, source_url=src_url,
                        )] = fname
                    for f, fname in _drain_futures_interruptible(tfutures, stop, executor=ex):
                        try:
                            f.result()
                        except Exception as e:
                            safe_print(f"  [!] Thread error: {e}")
                            summary.add_failed(fname)
                    ex.shutdown(wait=False)

        if summary.failed == 0 and not _stopped(ctx):
            mark_series_complete(url)
        summary.report(name)
        if summary.failed > 0 and not _stopped(ctx) and summary.prompt_retry():
            retry_summary = DownloadSummary()
            for failed_fname in summary.failed_list:
                if _stopped(ctx): break
                safe_print(f"\n[*] Retrying: {failed_fname}")
                stem = re.sub(r'\.(mkv|mp4)$', '', failed_fname, flags=re.IGNORECASE).lower()
                ep_url = next((l for l in dw_links
                               if l.lower().replace('.html', '').rstrip('/').endswith(stem)), None)
                if not ep_url:
                    retry_summary.add_failed(failed_fname)
                    continue
                direct = ResolverRegistry.resolve(ep_url, session)
                if direct:
                    ext = 'mkv' if '.mkv' in direct else 'mp4'
                    download_file(direct, folder, safe_filename(f"{stem}.{ext}"),
                                  retry_summary, series_url=url, series_name=name,
                                  bandwidth_limit=bw, current_process=cur_proc,
                                  source_url=ep_url,
                                  stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'))
                else:
                    retry_summary.add_failed(failed_fname)
            retry_summary.report(f"{name} (retry)")
        return

    # Priority 2: direct CDN links (newer posts)
    cdn_links = list(dict.fromkeys(
        a['href'] for a in soup.find_all('a', href=True)
        if 'nkiserv.com' in a['href'] and a['href'].endswith('.mkv')
    ))
    if cdn_links:
        cdn_links = _filter_by_episode_range(cdn_links, ctx)
        if not cdn_links:
            safe_print("[!] No episodes matched that range")
            return
        safe_print(f"[*] Found {len(cdn_links)} CDN link(s) — saving to: {folder}")
        _notify_start(name, len(cdn_links))
        items = []
        for cdn_url in cdn_links:
            fname = cdn_url.split('/')[-1]
            fname = re.sub(r'\.\([^)]+\)\.[a-z0-9]+\.mkv$', '.mkv', fname, flags=re.IGNORECASE)
            fname = safe_filename(fname)
            done, _ = already_downloaded(folder, fname, series_url=url)
            if done:
                summary.add_skipped()
            else:
                items.append((cdn_url, fname))
        if items:
            download_batch(items, folder, summary, parallel=parallel,
                           series_url=url, series_name=name,
                           bandwidth_limit=bw, quality=quality,
                           current_process=cur_proc, stop_flag=stop,
                           pause_flag=pause,
                           wait_fn=ctx.get('wait'))
        if summary.failed == 0 and not _stopped(ctx):
            mark_series_complete(url)
        summary.report(name)
        return

    safe_print("[!] No download links found")
    diagnose_page(soup, url, "downloadwella.com or nkiserv.com links")

def extract_dramakey_com(url, session, ctx=None):
    ctx = ctx or {}
    def cleaner(s):
        s = re.sub(r'-s\d+.*$', '', s, flags=re.IGNORECASE)
        s = re.sub(r'-(season|episode|complete).*$', '', s, flags=re.IGNORECASE)
        return s
    _extract_downloadwella_site(url, session, ctx, site_label='DramaKey.com', name_cleaner=cleaner)
