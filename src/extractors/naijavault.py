from .base import *

def extract_naijavault(url, session, ctx=None):
    ctx  = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    safe_print(render_message('site_mode', site='NaijaVault'))
    slug = url.rstrip('/').split('/')[-1]
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

    # Single dl- page pasted directly
    if not format_a and not format_b:
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

    if not format_a and not format_b:
        safe_print(render_message('no_episode_links'))
        diagnose_page(soup, url, "/dl- or lulacloud.com/d/ links")
        return

    total = len(format_a) + len(format_b)
    if ctx.get('episode_filter'):
        combined = [(kind, item) for kind, seq in (('a', format_a), ('b', format_b)) for item in seq]
        combined = _filter_by_episode_range(combined, ctx)
        format_a = [item for kind, item in combined if kind == 'a']
        format_b = [item for kind, item in combined if kind == 'b']
        if not format_a and not format_b:
            safe_print(render_message('no_episodes_in_range'))
            return
        total = len(format_a) + len(format_b)
    safe_print(f"[*] Found {total} episode(s) - Format A: {len(format_a)}, Format B: {len(format_b)}")
    safe_print(f"[*] Saving to: {folder}")
    _notify_start(name, total)

    zip_hit = False

    def _resolve_and_download(ep_label, ep_name, direct):
        """Download immediately after resolving — prevents token expiry."""
        if not direct:
            safe_print(f"  [\u2717] All resolvers failed")
            summary.add_failed(ep_label)
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

        r2 = safe_get(session, dl_url, timeout=20, referer=url)
        if not r2:
            safe_print(f"  [\u2717] Could not fetch dl page")
            summary.add_failed(ep_label)
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

        direct = None
        du_m   = re.search(r'var downloadURL\s*=\s*"([^"]+)"', r2.text)
        if du_m:
            cdn_url = du_m.group(1)
            if 'vikingfile.com' in cdn_url:
                direct = ResolverRegistry.resolve(cdn_url, session)
                if not direct:
                    lc = re.search(r'https?://(?:www\.)?lulacloud\.com/d/\S+', r2.text)
                    if lc:
                        direct = ResolverRegistry.resolve(lc.group(0).rstrip('.,;)\"\''), session)
            elif 'lulacloud.com' in cdn_url:
                direct = ResolverRegistry.resolve(cdn_url, session)
                if not direct:
                    vf = re.search(r'https?://(?:www\.)?vikingfile\.com/\S+', r2.text)
                    if vf:
                        direct = ResolverRegistry.resolve(vf.group(0).rstrip('.,;)\"\''), session)
            else:
                direct = cdn_url

        if not direct:
            vf = re.search(r'https?://(?:www\.)?vikingfile\.com/\S+', r2.text)
            if vf:
                direct = ResolverRegistry.resolve(vf.group(0).rstrip('.,;)\"\''), session)

        if not direct:
            lc = re.search(r'https?://(?:www\.)?lulacloud\.com/d/\S+', r2.text)
            if lc:
                direct = ResolverRegistry.resolve(lc.group(0).rstrip('.,;)\"\''), session)

        if not direct:
            nj_m = re.search(r"https?://[^ \t]+nj_download=[^ \t<>]+", r2.text)
            if nj_m and 'naijavault.com' in r2.text:
                try:
                    rr  = session.get(nj_m.group(0).rstrip('.,;)'), timeout=15, allow_redirects=False)
                    cdn = rr.headers.get('location')
                    if cdn and cdn.startswith('http'):
                        direct = cdn
                except Exception as e:
                    safe_print(f"  [!] nj_download failed: {e}")

        _resolve_and_download(ep_label, ep_name, direct)
        time.sleep(0.5)

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

            direct = ResolverRegistry.resolve(lc_url, session)
            if not direct:
                r2 = safe_get(session, lc_url, timeout=20)
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

            _resolve_and_download(ep_label, ep_name, direct)
            time.sleep(0.5)


    if summary.failed == 0 and not _stopped(ctx):
        mark_series_complete(url)
    summary.report(name=name)
