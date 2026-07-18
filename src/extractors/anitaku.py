from .base import *

def extract_anitaku(url, session, ctx=None):
    ctx  = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    safe_print(render_message('site_mode', site='Anitaku'))
    slug       = url.rstrip('/').split('/')[-1]
    is_episode = 'episode-' in slug
    name       = re.sub(r'-episode-\d+.*$', '', slug) if is_episode else slug
    name       = clean_name(name)
    safe_print(f"[*] Title: {name}")
    folder  = os.path.join(BASE_DIR, safe_filename(name))
    summary = DownloadSummary()

    def download_episode(ep_url, ep_name):
        r = safe_get(session, ep_url, referer=ANITAKU_BASE + '/', timeout=30)
        if r is None:
            safe_print(f"  [X] Could not fetch: {ep_name}")
            summary.add_failed(ep_name)
            return
        tamil_match = re.search(r"""(https://tamilembed\.lol/embed/[^\s"'<>]+)""", r.text)
        if tamil_match:
            embed_url = tamil_match.group(1)
            safe_print(f"  [*] Found tamilembed stream")
            download_with_ytdlp(embed_url, folder, safe_filename(f"{ep_name}.mp4"), summary,
                                quality=quality, current_process=cur_proc,
                                stop_flag=stop, pause_flag=pause)
            return
        soup2  = BeautifulSoup(r.text, 'html.parser')
        iframe = soup2.find('iframe', src=re.compile(r'tamilembed|embed'))
        if iframe:
            src = iframe.get('src', '')
            if not src.startswith('http'):
                # urljoin handles both //host/path (protocol-relative) and
                # /embed/x (root-relative); 'https:' + src breaks the latter.
                src = urljoin(ANITAKU_BASE, src)
            safe_print(f"  [*] Found embed via iframe")
            download_with_ytdlp(src, folder, safe_filename(f"{ep_name}.mp4"), summary,
                                quality=quality, current_process=cur_proc,
                                stop_flag=stop, pause_flag=pause)
            return
        safe_print(f"  [*] Trying yt-dlp on episode page directly")
        result = download_with_ytdlp(ep_url, folder, safe_filename(f"{ep_name}.mp4"), summary,
                                     quality=quality, current_process=cur_proc,
                                     stop_flag=stop, pause_flag=pause)
        if not result:
            safe_print(f"  [X] All methods failed: {ep_name}")
            diagnose_page(soup2, ep_url, "tamilembed.lol embed URL")

    if is_episode:
        safe_print(f"[*] Single episode - saving to: {folder}")
        download_episode(url, safe_filename(slug))
    else:
        safe_print(render_message('fetching_episode_list'))
        r = safe_get(session, url, referer=ANITAKU_BASE + '/', timeout=30)
        if r is None:
            safe_print(render_message('page_fetch_failed'))
            return
        soup  = BeautifulSoup(r.text, 'html.parser')
        seen  = set()
        ep_links = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            text = a.get_text(strip=True)
            if 'episode-' in href and ANITAKU_BASE in href and href not in seen:
                ep_slug    = href.rstrip('/').split('/')[-1]
                anime_base = slug.rstrip('/')
                if ep_slug.startswith(anime_base) or anime_base in ep_slug:
                    seen.add(href)
                    ep_links.append((href, text or ep_slug))
        if not ep_links:
            for a in soup.find_all('a', href=True):
                href = a['href']
                if 'episode-' in href and href not in seen:
                    seen.add(href)
                    ep_links.append((href, a.get_text(strip=True) or href.split('/')[-1]))
        if not ep_links:
            safe_print(render_message('no_episode_links'))
            diagnose_page(soup, url, "episode-* links")
            return

        def ep_num(item):
            m = re.search(r'episode-(\d+)', item[0])
            return int(m.group(1)) if m else 0
        ep_links.sort(key=ep_num)
        ep_links = _filter_by_episode_range(ep_links, ctx)
        if not ep_links:
            safe_print(render_message('no_episodes_in_range'))
            return
        safe_print(f"[*] Found {len(ep_links)} episode(s) - saving to: {folder}")
        _notify_start(name, len(ep_links))

        for i, (ep_url, ep_text) in enumerate(ep_links, 1):
            if _stopped(ctx):
                break
            _wait(ctx)
            ep_name = safe_filename(ep_url.rstrip('/').split('/')[-1])
            safe_print(f"\n[{i}/{len(ep_links)}] {ep_name}")
            done, _ = already_downloaded(folder, safe_filename(f"{ep_name}.mp4"), series_url=url)
            if done:
                safe_print(render_message('already_saved'))
                summary.add_skipped()
                continue
            download_episode(ep_url, ep_name)
            time.sleep(1)

    if summary.failed == 0 and not _stopped(ctx):
        mark_series_complete(url)
    summary.report()
