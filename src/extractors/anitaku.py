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
            if 'blogger.com' in resolved_stream:
                download_file(
                    resolved_stream, folder, safe_filename(f"{ep_name}.mp4"), summary,
                    series_url=url, series_name=name, bandwidth_limit=bw,
                    quality=quality, current_process=cur_proc, stop_flag=stop, pause_flag=pause
                )
            else:
                download_with_ytdlp(
                    resolved_stream, folder, safe_filename(f"{ep_name}.mp4"), summary,
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
        seen = set()
        ep_links = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            text = a.get_text(strip=True)
            if 'episode-' in href and href not in seen:
                ep_slug = href.rstrip('/').split('/')[-1]
                anime_base = slug.rstrip('/')
                if ep_slug.startswith(anime_base) or anime_base in ep_slug:
                    seen.add(href)
                    ep_links.append((urljoin(ANITAKU_BASE, href), text or ep_slug))
        if not ep_links:
            for a in soup.find_all('a', href=True):
                href = a['href']
                if 'episode-' in href and href not in seen:
                    seen.add(href)
                    ep_links.append((urljoin(ANITAKU_BASE, href), a.get_text(strip=True) or href.split('/')[-1]))
        if not ep_links:
            safe_print(render_message('no_episode_links'))
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
    summary.report(name)
