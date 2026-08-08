from .base import *

def extract_9jarocks(url, session, ctx=None):
    ctx = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    safe_print(render_message('site_mode', site='9jaRocks'))
    slug   = url_slug(url)
    name   = clean_name(re.sub(r'-id\d+.*$', '', slug))
    safe_print(f"[*] Title: {name}")
    folder = os.path.join(BASE_DIR, safe_filename(name))

    r = safe_get(session, url, referer=f'https://{JAROCKS_DOMAIN}/')
    if r is None:
        return
    soup = BeautifulSoup(r.text, 'html.parser')
    entry = soup.find(class_=re.compile(r'\bentry-content\b', re.I)) or soup

    # Infer initial season state
    is_multi_pack = re.search(r'season-\d+-\d+', slug, re.I) or re.search(r'season\s*\d+\s*-\s*\d+', name, re.I)
    if not is_multi_pack:
        sm_url = re.search(r'season-(\d{1,2})', slug, re.I) or re.search(r'season\s*(\d{1,2})', name, re.I)
        current_season = int(sm_url.group(1)) if sm_url else 1
    else:
        current_season = 1

    extracted_items = []
    seen_hrefs = set()

    for elem in entry.find_all(True):
        if not isinstance(elem, Tag):
            continue

        if any(c in elem.get('class', []) for c in ['widget', 'related-posts', 'check-also-right', 'wpd-thread-list', 'check-also']):
            continue
        if elem.parent and any(c in elem.parent.get('class', []) for c in ['widget', 'related-posts', 'check-also-right', 'check-also']):
            continue

        # Track active Season headings in page content
        if elem.name in ('h1', 'h2', 'h3', 'h4', 'strong', 'b', 'p', 'div'):
            t = elem.get_text(strip=True)
            if len(t) < 60 and not re.search(r'\b(synopsis|storyline|about|comment|download|how to|click|added)\b', t, re.I):
                sm = re.search(r'\b(?:SEASON|S)\s*(\d{1,2})\b(?!\s*-\s*\d+)', t, re.I)
                if sm:
                    current_season = int(sm.group(1))

        if elem.name == 'a' and elem.has_attr('href'):
            href = elem['href']
            if re.search(r'loadedfiles\.[a-z0-9-]+', href, re.I) and href not in seen_hrefs:
                seen_hrefs.add(href)
                if 'error?e=' in href or 'errore=' in href:
                    continue

                text = elem.get_text(strip=True)
                p_text = elem.parent.get_text(strip=True) if elem.parent else ''

                prev_text = ''
                prev_node = elem.previous_sibling
                while prev_node:
                    if isinstance(prev_node, str):
                        prev_text = prev_node.strip() + ' ' + prev_text
                    elif isinstance(prev_node, Tag):
                        prev_text = prev_node.get_text(strip=True) + ' ' + prev_text
                    if len(prev_text.strip()) > 5:
                        break
                    prev_node = prev_node.previous_sibling

                # Season ZIP detection
                zip_match = re.search(r'\b(?:SEASON|S)\s*(\d{1,2})\b.*\bZIP\b', p_text, re.I) or re.search(r'\bZIP\b', href, re.I)
                if zip_match:
                    z_s = zip_match.group(1) if zip_match.groups() and zip_match.group(1) else (current_season or '')
                    label = f"S{int(z_s):02d} Complete Season ZIP" if z_s else "Full Season ZIP"
                    extracted_items.append((label, href))
                    continue

                qm = re.search(r'\b(\d{3,4}p)\b', text, re.I) or re.search(r'\b(\d{3,4}p)\b', prev_text, re.I) or re.search(r'\b(\d{3,4}p)\b', p_text, re.I)
                quality_str = qm.group(1).lower() if qm else None

                file_slug = href.rstrip('/').split('/')[-1]
                ep_match = (re.search(r'\b(?:EPISODE|EP|E)\s*(\d{1,3})\b', text, re.I) or
                            re.search(r'\b(?:EPISODE|EP|E)\s*(\d{1,3})\b', prev_text, re.I) or
                            re.search(r'\bS\d{1,2}E(\d{1,3})\b', file_slug, re.I) or
                            re.search(r'\b(?:EPISODE|EP|E)\s*(\d{1,3})\b', p_text, re.I))

                if ep_match:
                    ep_num = int(ep_match.group(1))
                    if current_season:
                        ep_code = f"S{current_season:02d}E{ep_num:02d}"
                    else:
                        ep_code = f"E{ep_num:02d}"
                    label = f"{ep_code}{f' [{quality_str}]' if quality_str else ''}"
                elif quality_str:
                    label = quality_str
                else:
                    clean_t = re.sub(r'\[?\s*server\s*\d*\s*\]?', '', text, flags=re.I)
                    clean_t = re.sub(r'\bdownload\b', '', clean_t, flags=re.I).strip()
                    if clean_t and clean_t.lower() not in ('click here', 'link', 'download'):
                        label = clean_t
                    elif current_season:
                        label = f"S{current_season:02d}"
                    else:
                        label = "Movie"

                extracted_items.append((label, href))

    lf_links = extracted_items
    if not lf_links:
        safe_print(render_message('no_episode_links'))
        diagnose_page(soup, url, "loadedfiles links")
        return
    lf_links = _filter_by_episode_range(lf_links, ctx)
    if not lf_links:
        safe_print(render_message('no_episodes_in_range'))
        return
    safe_print(f"[*] Found {len(lf_links)} file(s) - saving to: {folder}")
    _notify_start(name, len(lf_links))
    summary = DownloadSummary()

    def _resolve_ep(lf_url):
        return ResolverRegistry.resolve(lf_url, session)

    def _cdn_alive(cdn_url):
        try:
            r = session.get(cdn_url, timeout=5, allow_redirects=True,
                            headers={'Range': 'bytes=0-0'})
            return r.status_code in (200, 206)
        except Exception:
            return False

    work = []
    seen_fnames = set()
    for i, (label, lf_url) in enumerate(lf_links, 1):
        label_clean = label.strip() if label else ''
        if label_clean and label_clean not in ('Movie', 'Download'):
            base_fname = safe_filename(f"{name} - {label_clean}")
        else:
            base_fname = safe_filename(name)

        if base_fname in seen_fnames:
            base_fname = safe_filename(f"{base_fname} ({i:02d})")
        seen_fnames.add(base_fname)
        
        ext_target = '.zip' if 'zip' in base_fname.lower() else '.mp4'
        done, _ = already_downloaded(folder, base_fname + ext_target, series_url=url)
        if not done and ext_target == '.mp4':
            done, _ = already_downloaded(folder, base_fname + '.mkv', series_url=url)
        if done:
            safe_print(f"\n[{i}/{len(lf_links)}] {base_fname}")
            safe_print(render_message('already_saved'))
            summary.add_skipped()
            continue
        work.append((base_fname, lf_url))

    prefetcher = Prefetcher(_resolve_ep)
    if work:
        prefetcher.prefetch(work[0][1])

    for i, (base_fname, lf_url) in enumerate(work, 1):
        if _stopped(ctx):
            break
        _wait(ctx)
        safe_print(f"\n[{i}/{len(work)}] {base_fname}")

        direct = prefetcher.get(timeout=30)
        if i < len(work):
            prefetcher.prefetch(work[i][1])

        if not direct or not _cdn_alive(direct):
            if direct:
                safe_print(f"  [*] CDN link expired - re-resolving...")
            direct = resolve_with_retry(lambda u: ResolverRegistry.resolve(u, session), lf_url, ctx)
            if not direct:
                if _stopped(ctx):
                    break
                safe_print(f"  [X] Could not extract: {base_fname}")
                record_episode_failure(url, name, base_fname + '.mp4', summary, base_fname)
                continue

        ext = 'zip' if '.zip' in direct or 'zip' in base_fname.lower() else ('mkv' if '.mkv' in direct else 'mp4')
        download_file(direct, folder, safe_filename(f"{base_fname}.{ext}"), summary,
                      series_url=url, series_name=name,
                      bandwidth_limit=bw, quality=quality, current_process=cur_proc,
                      stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'),
                      source_url=lf_url)
    if summary.failed == 0 and not _stopped(ctx):
        mark_series_complete(url)
    summary.report()

