from .base import *

def extract_naijavault(url, session, ctx=None):
    ctx  = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    safe_print(render_message('site_mode', site='NaijaVault'))
    slug = url_slug(url)
    name = re.sub(r'-\d{4}.*$', '', slug)
    name = re.sub(r'-season-\d+.*$', '', name, flags=re.IGNORECASE)
    name = clean_name(name)
    safe_print(f"[*] Title: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))

    r = safe_get(session, url, timeout=30, referer=f'https://www.{NAIJAVAULT_DOMAIN}/')
    if r is None:
        return
    soup    = BeautifulSoup(r.text, 'html.parser')
    summary = DownloadSummary()

    if '/category/' in url:
        # Exclude nav/footer/taxonomy links — only real movie posts should recurse.
        # (Previously grabbed /contact/, /about/, etc. as if they were posts.)
        _NON_POST = ('/category/', '/page/', '/tag/', '/author/', '/feed',
                     '/contact', '/about', '/privacy', '/dmca', '/disclaimer',
                     '/terms', '/wp-login', '/wp-admin', '/request', '#')
        post_links = list(dict.fromkeys(
            a['href'] for a in soup.find_all('a', href=True)
            if NAIJAVAULT_DOMAIN in a['href']
            and not any(bad in a['href'].lower() for bad in _NON_POST)
            and a['href'].rstrip('/') != f'https://www.{NAIJAVAULT_DOMAIN}'
            and a['href'].rstrip('/') != f'https://{NAIJAVAULT_DOMAIN}'
        ))
        if post_links:
            safe_print(f"[*] Category Hub Page detected. Processing {len(post_links)} post(s)...")
            for post_url in post_links:
                if _stopped(ctx):
                    break
                extract_naijavault(post_url, session, ctx)
            return

    # ── Scan series page for both link formats ─────────────────
    # Format A: /dl-{hash}/ intermediate pages
    seen   = set()
    format_a = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if '/dl-' in href and NAIJAVAULT_DOMAIN in href and href not in seen:
            seen.add(href)
            format_a.append((a.get_text(strip=True), href))

    # Format B: lulacloud.com/d/ direct links
    format_b = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if 'lulacloud.com/d/' in href and href not in seen:
            seen.add(href)
            format_b.append((a.get_text(strip=True), href))

    # Format C: pixeldrain.com/u/ direct links (current NaijaVault layout)
    format_c = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if 'pixeldrain.com/u/' in href and href not in seen:
            seen.add(href)
            format_c.append((a.get_text(strip=True), href))

    # Single dl- page pasted directly
    if not format_a and not format_b and not format_c:
        is_dl = (
            'var downloadURL' in r.text or
            re.search(r'vikingfile\.com', r.text) or
            re.search(r'lulacloud\.com/d/', r.text) or
            re.search(r'nj_download=', r.text)
        )
        if is_dl:
            page_title = soup.find('title')
            label      = page_title.get_text(strip=True) if page_title else slug
            format_a   = [(label, url)]

    if not format_a and not format_b and not format_c:
        hints = []
        if soup.find('iframe') or soup.find('embed'):
            hints.append("page has video embeds (trailer/preview page?)")
        body_text = soup.get_text(' ', strip=True).lower()
        if 'coming soon' in body_text:
            hints.append("page says 'coming soon'")
        if hints:
            safe_print(f"[!] No download links found — {'; '.join(hints)}")
        else:
            safe_print(render_message('no_episode_links'))
        diagnose_page(soup, url, "/dl-, lulacloud.com/d/ or pixeldrain.com/u/ links")
        return

    total = len(format_a) + len(format_b) + len(format_c)
    if ctx.get('episode_filter'):
        combined = [(kind, item) for kind, seq in (('a', format_a), ('b', format_b), ('c', format_c)) for item in seq]
        combined = _filter_by_episode_range(combined, ctx)
        format_a = [item for kind, item in combined if kind == 'a']
        format_b = [item for kind, item in combined if kind == 'b']
        format_c = [item for kind, item in combined if kind == 'c']
        if not format_a and not format_b and not format_c:
            safe_print(render_message('no_episodes_in_range'))
            return
        total = len(format_a) + len(format_b) + len(format_c)
    safe_print(f"[*] Found {total} episode(s) - Format A: {len(format_a)}, Format B: {len(format_b)}, Format C: {len(format_c)}")
    safe_print(f"[*] Saving to: {folder}")
    _notify_start(name, total)

    zip_hit = False

    # NOTE: deliberately NOT using the Prefetcher/resolve-ahead pattern here
    # (unlike jarocks/naijaprey/myasiantv/dramarain). NaijaVault's CDN links
    # (vikingfile / lulacloud / filevault) are short-lived signed URLs — see the
    # "prevents token expiry" note below. Resolving one episode ahead would mint
    # a link that's almost always dead by download time, forcing a synchronous
    # re-resolve every episode: wasted background data for zero speed gain. The
    # immediate resolve-then-download below is the correct model for this host.
    def _resolve_and_download(ep_label, ep_name, resolve_fn):
        """Resolve (network-aware) then download immediately — prevents token expiry.

        resolve_fn() re-runs the full resolution and returns (ep_name, direct).
        A network drop returns (_, None); we wait for the connection to come
        back (up to the 2-min ceiling) and retry the SAME episode instead of
        failing it. Only a genuine miss while ONLINE is marked failed.
        """
        direct = None
        while True:
            new_name, direct = resolve_fn()
            ep_name = new_name or ep_name
            if direct:
                break
            if check_connection():
                break                     # online but nothing resolved -> genuine miss
            if _stopped(ctx):
                return
            if not wait_or_abort(ctx):    # offline: wait (may raise NetworkAbort); False if stopped
                return
        if not direct:
            safe_print(f"  [✗] All resolvers failed")
            record_episode_failure(url, name, safe_filename(f"{ep_label}.mp4"), summary, ep_label)
            return
        ext   = 'mkv' if '.mkv' in (direct + ep_name).lower() else 'mp4'
        fname = ep_name if '.' in ep_name else f"{ep_name}.{ext}"
        _wait(ctx)
        download_file(direct, folder, safe_filename(fname), summary,
                      series_url=url, series_name=name,
                      bandwidth_limit=bw, quality=quality, current_process=cur_proc,
                      stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'))

    # ── Process Format A (/dl- pages) — resolve & download immediately ──
    for i, (label, dl_url) in enumerate(format_a, 1):
        if _stopped(ctx) or zip_hit:
            break
        ep_label = clean_ep_name(label) or f"episode-{i}"
        safe_print(f"\n[A {i}/{len(format_a)}] {ep_label}")

        # ── Early skip: check before hitting the dl page ──
        done, _ = already_downloaded(folder, safe_filename(f"{ep_label}.mkv"), series_url=url)
        if not done:
            done, _ = already_downloaded(folder, safe_filename(f"{ep_label}.mp4"), series_url=url)
        if done:
            safe_print(render_message('already_saved'))
            summary.add_skipped()
            continue

        # Fetch the dl page — a dropped connection waits for the network to come
        # back and retries the SAME page (up to the 2-min ceiling) instead of
        # failing the episode. Only a failure while ONLINE (server down / dead
        # page) is marked failed.
        r2 = None
        while True:
            r2 = safe_get(session, dl_url, timeout=20, referer=url)
            if r2:
                break
            if check_connection():
                safe_print(f"  [✗] Could not fetch dl page")
                record_episode_failure(url, name, safe_filename(f"{ep_label}.mp4"), summary, ep_label)
                break
            if _stopped(ctx) or not wait_or_abort(ctx):
                break
        if not r2:
            continue

        ft_m    = re.search(r'var fileTitle\s*=\s*"([^"]+)"', r2.text)
        ep_name = safe_filename(ft_m.group(1)) if ft_m else safe_filename(f"{ep_label}.mkv")

        if ep_name.lower().endswith('.zip'):
            safe_print(f"  [*] ZIP - downloading season archive")
            du_m = re.search(r'var downloadURL\s*=\s*"([^"]+)"', r2.text)
            if du_m:
                zip_url = du_m.group(1)
                if 'vikingfile.com' in zip_url:
                    zip_url = ResolverRegistry.resolve(zip_url, session) or zip_url
                elif 'lulacloud.com' in zip_url:
                    zip_url = ResolverRegistry.resolve(zip_url, session) or zip_url
                if zip_url:
                    _wait(ctx)
                    download_file(zip_url, folder, ep_name, summary,
                                  series_url=url, series_name=name,
                                  bandwidth_limit=bw, current_process=cur_proc,
                                  stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'))
                    zip_hit = True
                    break
            continue

        # Resolver chain wrapped in a closure so _resolve_and_download can re-run
        # it after waiting out a network drop. The page HTML (r2.text) is already
        # cached; only the CDN token-minting step needs the network.
        def _resolve_A(_text=r2.text, _ep_name=ep_name):
            direct = None
            du_m   = re.search(r'var downloadURL\s*=\s*"([^"]+)"', _text)
            if du_m:
                cdn_url = du_m.group(1)
                if 'vikingfile.com' in cdn_url:
                    direct = ResolverRegistry.resolve(cdn_url, session)
                    if not direct:
                        lc = re.search(r'https?://(?:www\.)?lulacloud\.com/d/\S+', _text)
                        if lc:
                            direct = ResolverRegistry.resolve(lc.group(0).rstrip('.,;)\"\''), session)
                elif 'lulacloud.com' in cdn_url:
                    direct = ResolverRegistry.resolve(cdn_url, session)
                    if not direct:
                        vf = re.search(r'https?://(?:www\.)?vikingfile\.com/\S+', _text)
                        if vf:
                            direct = ResolverRegistry.resolve(vf.group(0).rstrip('.,;)\"\''), session)
                else:
                    direct = cdn_url
            if not direct:
                vf = re.search(r'https?://(?:www\.)?vikingfile\.com/\S+', _text)
                if vf:
                    direct = ResolverRegistry.resolve(vf.group(0).rstrip('.,;)\"\''), session)
            if not direct:
                lc = re.search(r'https?://(?:www\.)?lulacloud\.com/d/\S+', _text)
                if lc:
                    direct = ResolverRegistry.resolve(lc.group(0).rstrip('.,;)\"\''), session)
            if not direct:
                nj_m = re.search(r"https?://[^ \t]+nj_download=[^ \t<>]+", _text)
                if nj_m and 'naijavault.com' in _text:
                    try:
                        rr  = session.get(nj_m.group(0).rstrip('.,;)'), timeout=15, allow_redirects=False)
                        cdn = rr.headers.get('location')
                        if cdn and cdn.startswith('http'):
                            direct = cdn
                    except Exception as e:
                        safe_print(f"  [!] nj_download failed: {e}")
            return _ep_name, direct

        _resolve_and_download(ep_label, ep_name, _resolve_A)

    # ── Process Format B (lulacloud direct) — resolve & download immediately ──
    if not zip_hit:
        for i, (label, lc_url) in enumerate(format_b, 1):
            if _stopped(ctx):
                break
            ep_label  = clean_ep_name(label) or f"episode-{i}"
            safe_print(f"\n[B {i}/{len(format_b)}] {ep_label}")

            slug_part  = lc_url.rstrip('/').split('/')[-1]
            fname_slug = re.sub(r'^[a-f0-9]{8,}-', '', slug_part, flags=re.IGNORECASE)
            fname_slug = re.sub(r'-mkv$', '.mkv', fname_slug)
            fname_slug = re.sub(r'-mp4$', '.mp4', fname_slug)
            ep_name    = safe_filename(fname_slug or f"{ep_label}.mkv")

            # ── Early skip ──
            done, _ = already_downloaded(folder, ep_name, series_url=url)
            if not done:
                done, _ = already_downloaded(folder, safe_filename(f"{ep_label}.mkv"), series_url=url)
            if not done:
                done, _ = already_downloaded(folder, safe_filename(f"{ep_label}.mp4"), series_url=url)
            if done:
                safe_print(render_message('already_saved'))
                summary.add_skipped()
                continue

            def _resolve_B(_lc_url=lc_url, _ep_name=ep_name):
                direct = ResolverRegistry.resolve(_lc_url, session)
                if not direct:
                    r2 = safe_get(session, _lc_url, timeout=20)
                    if r2:
                        du_m = re.search(r'var downloadURL\s*=\s*"([^"]+)"', r2.text)
                        if du_m:
                            cdn = du_m.group(1)
                            if 'vikingfile.com' in cdn:
                                direct = ResolverRegistry.resolve(cdn, session)
                            elif 'lulacloud.com' in cdn:
                                direct = ResolverRegistry.resolve(cdn, session)
                            else:
                                direct = cdn
                        if not direct:
                            vf = re.search(r'https?://(?:www\.)?vikingfile\.com/\S+', r2.text)
                            if vf:
                                direct = ResolverRegistry.resolve(vf.group(0).rstrip('.,;)\"\''), session)
                        if not direct:
                            fv = re.search(r'https?://cdn\.filevault\.com\.ng/[^\s"\'<>]+', r2.text)
                            if fv:
                                direct = fv.group(0)
                return _ep_name, direct

            _resolve_and_download(ep_label, ep_name, _resolve_B)

    # ── Process Format C (pixeldrain.com/u/ direct) ──
    if not zip_hit:
        for i, (label, pd_url) in enumerate(format_c, 1):
            if _stopped(ctx):
                break
            ep_label = clean_ep_name(label) or f"episode-{i}"
            safe_print(f"\n[C {i}/{len(format_c)}] {ep_label}")

            # Pull the real filename from the pixeldrain info API so the file
            # extension is correct (the api download URL carries no extension).
            ep_name = safe_filename(f"{ep_label}.mkv")
            fid_m = re.search(r'pixeldrain\.com/u/([A-Za-z0-9]+)', pd_url)
            if fid_m:
                try:
                    info = session.get(f'https://pixeldrain.com/api/file/{fid_m.group(1)}/info',
                                       timeout=15).json()
                    if info.get('name'):
                        ep_name = safe_filename(info['name'])
                except Exception:
                    pass

            done, _ = already_downloaded(folder, ep_name, series_url=url)
            if done:
                safe_print(render_message('already_saved'))
                summary.add_skipped()
                continue

            def _resolve_C(_pd_url=pd_url, _ep_name=ep_name):
                return _ep_name, ResolverRegistry.resolve(_pd_url, session)

            _resolve_and_download(ep_label, ep_name, _resolve_C)


    if summary.failed == 0 and not _stopped(ctx):
        mark_series_complete(url)
    summary.report(name=name)
