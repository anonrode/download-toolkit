from concurrent.futures import ThreadPoolExecutor, as_completed

from .base import *

def extract_nkiri(url, session, ctx=None):
    ctx  = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    safe_print(render_message('site_mode', site='NKiri/TheNkiri'))
    slug = url_slug(url)
    name = re.sub(r'-(korean|complete|drama|series|nollywood|hollywood|tv|movie).*$', '', slug, flags=re.IGNORECASE)
    name = clean_name(name)
    safe_print(f"[*] Title: {name}")
    folder  = os.path.join(BASE_DIR, safe_filename(name))
    summary = DownloadSummary()

    r = safe_get(session, url, timeout=20, referer='https://thenkiri.com/')
    if r is None:
        safe_print(render_message('page_fetch_failed'))
        return
    soup = BeautifulSoup(r.text, 'html.parser')

    # Collect BOTH link classes up front. Posts mix them: Alchemy of Souls S01
    # lists E01-E16+E19+E20 as downloadwella pages but E17/E18 ONLY as direct
    # nkiserv CDN files. The old priority gate (downloadwella first, nkiserv
    # only if NO downloadwella link exists) silently dropped every nkiserv
    # episode from a mixed post, which is why E17/E18 "went missing".
    dw_links = list(dict.fromkeys(
        a['href'] for a in soup.find_all('a', href=True)
        if 'downloadwella.com' in a['href'] or 'wetafiles.com' in a['href']
    ))
    cdn_links = list(dict.fromkeys(
        a['href'] for a in soup.find_all('a', href=True)
        if 'nkiserv.com' in a['href'] and (a['href'].endswith('.mkv') or a['href'].endswith('.mp4'))
    ))

    def _ep_no(link):
        # Matches E17, EP05, S01E19, _E20 — but not the year in
        # "(NKIRI.COM).2017.480p" (4 digits can't fit \d{1,3} + lookahead).
        m = re.search(r'(?:^|[^a-z0-9])(?:[Ss]\d{1,2})?[Ee][Pp]?[-_.]?(\d{1,3})(?![0-9])',
                      link.rsplit('/', 1)[-1])
        return int(m.group(1)) if m else None

    # When an episode exists both as a downloadwella page and as a direct
    # nkiserv file, keep the direct file: it skips resolution entirely, and
    # downloadwella page links rot (404) while the CDN copy stays up.
    if dw_links and cdn_links:
        dw_eps = {_ep_no(l) for l in dw_links} - {None}
        cdn_links = [l for l in cdn_links if _ep_no(l) is None or _ep_no(l) not in dw_eps]

    if dw_links or cdn_links:
        dw_links = _filter_by_episode_range(dw_links, ctx) if dw_links else []
        cdn_links = _filter_by_episode_range(cdn_links, ctx) if cdn_links else []
        if not dw_links and not cdn_links:
            safe_print(render_message('no_episodes_in_range'))
            return
        safe_print(f"[*] Found {len(dw_links)} downloadwella + {len(cdn_links)} direct CDN link(s) - saving to: {folder}")
        _notify_start(name, len(dw_links) + len(cdn_links))

        # Direct CDN files ride the same batch machinery as downloadwella
        # links but need no resolver — their "resolve" is the URL itself.
        all_items = [(l, True) for l in dw_links] + [(l, False) for l in cdn_links]

        batch_size = max(1, parallel)
        batches    = [all_items[i:i+batch_size] for i in range(0, len(all_items), batch_size)]
        ep_index   = 0

        for batch in batches:
            if _stopped(ctx): break
            _wait(ctx)

            to_process = []  # (url, name, needs_resolve)
            for ep_url, needs_resolve in batch:
                ep_index += 1
                ep_name = ep_url.split('/')[-1].replace('.html', '')
                ep_name = re.sub(r'\.(mkv|mp4)$', '', ep_name, flags=re.IGNORECASE)
                ep_name = _hash_safe_name(ep_name, ep_index)
                safe_print(f"\n[{ep_index}/{len(all_items)}] {ep_name}")
                done, _ = already_downloaded(folder, safe_filename(f"{ep_name}.mp4"), series_url=url)
                if not done:
                    done, _ = already_downloaded(folder, safe_filename(f"{ep_name}.mkv"), series_url=url)
                if done:
                    safe_print(render_message('already_saved'))
                    summary.add_skipped()
                else:
                    to_process.append((ep_url, ep_name, needs_resolve))

            if not to_process:
                continue

            # Split the batch: downloadwella pages resolve (network walk), CDN
            # files are already direct. Every resolved/direct item funnels into
            # one download pool.
            resolved_items = []
            to_resolve = [(u, n) for (u, n, r) in to_process if r]
            direct_only = [(u, n) for (u, n, r) in to_process if not r]

            if to_resolve:
                if len(to_resolve) == 1 or batch_size == 1:
                    ep_url, ep_name = to_resolve[0]
                    # Network-aware resolve: a dropped connection waits (up to the
                    # 2-min ceiling) and retries the SAME link instead of failing it.
                    direct = resolve_with_retry(lambda u: ResolverRegistry.resolve(u, session, quality=quality), ep_url, ctx)
                    if _stopped(ctx):
                        break
                    if direct:
                        resolved_items.append((direct, ep_name, ep_url))
                    else:
                        safe_print(f"  [X] Could not extract link: {ep_name}")
                        record_episode_failure(url, name, safe_filename(f"{ep_name}.mp4"), summary, ep_name)
                else:
                    safe_print(f"\n  [*] Resolving {len(to_resolve)} link(s)...")
                    resolved = {}
                    def _resolve_with_own_session(ep_url):
                        return ResolverRegistry.resolve(ep_url, make_session(), quality=quality)
                    with ThreadPoolExecutor(max_workers=min(len(to_resolve), 8)) as ex:
                        futures = {
                            ex.submit(_resolve_with_own_session, ep_url): (ep_url, ep_name)
                            for ep_url, ep_name in to_resolve
                        }
                        for f in as_completed(futures):
                            ep_url, ep_name = futures[f]
                            try:
                                resolved[(ep_url, ep_name)] = f.result()
                            except Exception:
                                resolved[(ep_url, ep_name)] = None

                    for (ep_url, ep_name), direct in resolved.items():
                        if not direct:
                            # A None here may be a genuine dead link OR the whole
                            # batch hit a network drop at once. Re-resolve with the
                            # network-aware path: it waits for the connection to come
                            # back and retries, so a transient outage never fails a
                            # link that would resolve fine when online.
                            direct = resolve_with_retry(lambda u: ResolverRegistry.resolve(u, session), ep_url, ctx)
                        if _stopped(ctx):
                            break
                        if direct:
                            resolved_items.append((direct, ep_name, ep_url))
                        else:
                            safe_print(f"  [X] Could not extract link: {ep_name}")
                            record_episode_failure(url, name, safe_filename(f"{ep_name}.mp4"), summary, ep_name)
                    if _stopped(ctx):
                        break

            for direct_url, ep_name in [(u, n) for (u, n) in direct_only]:
                resolved_items.append((direct_url, ep_name, direct_url))

            if resolved_items:
                per_thread_bw = (bw // len(resolved_items)) if bw else 0
                ex = ThreadPoolExecutor(max_workers=min(len(resolved_items), 8))
                tfutures = {}
                for direct, fname, src_url in [
                    (d, safe_filename(f"{n}.{('mkv' if '.mkv' in d.lower() else 'mp4')}"), s)
                    for (d, n, s) in resolved_items
                ]:
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
                direct = resolve_with_retry(lambda u: ResolverRegistry.resolve(u, session, quality=quality), ep_url, ctx)
                if _stopped(ctx):
                    break
                if direct:
                    ext = 'mkv' if '.mkv' in direct else 'mp4'
                    download_file(direct, folder, safe_filename(f"{stem}.{ext}"),
                                  retry_summary, series_url=url, series_name=name,
                                  bandwidth_limit=bw, quality=quality, current_process=cur_proc,
                                  source_url=ep_url,
                                  stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'))
                else:
                    retry_summary.add_failed(failed_fname)
            retry_summary.report(f"{name} (retry)")
        return

    safe_print(render_message('no_download_link'))
    diagnose_page(soup, url, "downloadwella.com or nkiserv.com links")

def extract_dramakey_com(url, session, ctx=None):
    ctx = ctx or {}
    def cleaner(s):
        s = re.sub(r'-s\d+.*$', '', s, flags=re.IGNORECASE)
        s = re.sub(r'-(season|episode|complete).*$', '', s, flags=re.IGNORECASE)
        return s
    _extract_downloadwella_site(url, session, ctx, site_label='DramaKey.com', name_cleaner=cleaner)
