from .base import *
from ..downloader import mark_series_waiting_for_network, mark_series_complete
from ..resolvers import ResolverRegistry

ANITAKU_BASE = "https://anitaku.com.ro"

def extract_anitaku(url, session, ctx=None):
    """Download single episode or full series from Anitaku (anitaku.com.ro)."""
    ctx = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    safe_print(render_message('site_mode', site='Anitaku'))
    slug = url.rstrip('/').split('/')[-1]
    is_episode = 'episode-' in slug
    name = re.sub(r'-episode-\d+.*$', '', slug) if is_episode else slug
    name = clean_name(name)
    safe_print(f"[*] Title: {name}")
    folder = os.path.join(BASE_DIR, 'Anime', safe_filename(name))
    os.makedirs(folder, exist_ok=True)
    summary = DownloadSummary()

    def download_episode(ep_url, ep_name):
        s = requests.Session()
        s.headers['User-Agent'] = session.headers.get('User-Agent', UA_DESKTOP)
        s.headers['Referer'] = ep_url
        
        r = safe_get(s, ep_url, referer=ANITAKU_BASE + '/', timeout=15)
        if r is None:
            safe_print(f"  [X] Could not fetch episode page: {ep_name}")
            summary.add_failed(ep_name)
            return

        soup = BeautifulSoup(r.text, 'html.parser')
        embed_links = []

        # 1. Check server buttons in div.anime_muti_link
        multi_links = soup.find('div', class_=re.compile(r'anime_muti_link|servers', re.I))
        if multi_links:
            for a in multi_links.find_all('a'):
                link = a.get('data-video') or a.get('href')
                if link:
                    embed_links.append(urljoin(ep_url, unescape(link)))

        # 2. Check main iframe
        for iframe in soup.find_all('iframe', src=True):
            embed_links.append(urljoin(ep_url, iframe['src']))

        resolved_stream = None
        seen = set()
        for embed_url in embed_links:
            if embed_url in seen or embed_url.startswith('javascript:'):
                continue
            seen.add(embed_url)

            safe_print(f"  [*] Resolving embed: {embed_url[:60]}...")
            direct_url = ResolverRegistry.resolve(embed_url, s)
            if direct_url and direct_url != embed_url:
                resolved_stream = direct_url
                break

        if resolved_stream:
            safe_print(f"  [*] Downloading stream: {resolved_stream[:70]}...")
            download_file(
                resolved_stream, folder, safe_filename(f"{ep_name}.mp4"), summary,
                series_url=url, series_name=name, bandwidth_limit=bw,
                quality=quality, current_process=cur_proc, stop_flag=stop, pause_flag=pause
            )
        else:
            safe_print(f"  [X] Could not resolve video stream for {ep_name}")
            summary.add_failed(ep_name)

    if is_episode:
        safe_print(f"[*] Single episode - saving to: {folder}")
        download_episode(url, safe_filename(slug))
    else:
        safe_print(render_message('fetching_episode_list'))
        r = safe_get(session, url, referer=ANITAKU_BASE + '/', timeout=30)
        if r is None:
            safe_print(render_message('page_fetch_failed'))
            return
        soup = BeautifulSoup(r.text, 'html.parser')
        # Soft-404 guard. anitaku.com.ro serves its generic homepage/landing
        # (HTTP 200) for unknown slugs, so a status check isn't enough. The
        # episode list lives in div.bixbox.bxcl.epcheck (episodes under
        # div.inepcx) on the current layout -- its absence means this slug has
        # no series page. (The old #episode_page/#episode_related ids were the
        # legacy Gogoanime layout and no longer exist here.)
        container = (soup.select_one('div.bixbox.bxcl.epcheck')
                     or soup.select_one('div.eplister')
                     or soup.select_one('div.bxcl'))
        if not container:
            safe_print("  [!] Category page soft-404: Invalid category page.")
            return

        seen = set()
        ep_links = []
        anime_base = slug.rstrip('/')

        # Scrape episode links specifically inside the episode container
        search_root = container
        for a in search_root.find_all('a', href=True):
            href = a['href']
            text = a.get_text(strip=True)
            if 'episode-' in href and href not in seen:
                ep_slug = href.rstrip('/').split('/')[-1]
                # Strict matching: must start with anime_base + '-'
                if ep_slug.startswith(f"{anime_base}-") or ep_slug.startswith(f"{anime_base}_"):
                    seen.add(href)
                    ep_links.append((urljoin(ANITAKU_BASE, href), text or ep_slug))

        # Movie/Special fallback. A movie or special is listed as a single watch
        # page whose slug does NOT contain "-episode-N" (e.g.
        # one-piece-heroines-special-episode, ...-infinity-castle-movie-1-eng),
        # so the numbered-episode scrape above finds nothing. Rather than bail
        # with "no episode links", pick up that lone child watch page (any anchor
        # in the container whose slug is a child of anime_base but isn't the
        # series page itself) and download it as a one-off.
        if not ep_links:
            for a in search_root.find_all('a', href=True):
                href = a['href']
                if href in seen or href.startswith(('javascript:', '#')):
                    continue
                child = href.rstrip('/').split('/')[-1]
                if not child or child == anime_base:
                    continue
                if not (child.startswith(f"{anime_base}-")
                        or child.startswith(f"{anime_base}_")):
                    continue
                # Skip share/nav junk that occasionally leaks into the container.
                if any(x in href for x in ('pinterest', 't.me', 'facebook',
                                           'twitter', 'whatsapp', '/genre',
                                           '/tag/', '?')):
                    continue
                seen.add(href)
                ep_links.append((urljoin(ANITAKU_BASE, href),
                                 a.get_text(strip=True) or child))
                break  # a movie/special is a single entry; one is enough
            if ep_links:
                safe_print("[*] Movie/Special - single video, no episode list.")

        if not ep_links:
            safe_print(render_message('no_episode_links'))
            return

        def ep_num(item):
            m = re.search(r'episode-(\d+)', item[0])
            return int(m.group(1)) if m else 0

        ep_links.sort(key=ep_num)
        # Interactive preview: show the episode range and let the user pick a
        # slice before we start (skipped when a CLI --episodes range was given,
        # or when not on a TTY). Then apply any explicit CLI range on top.
        ep_links = _interactive_episode_preview(ep_links, ctx, title=name)
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
    summary.report(name)
