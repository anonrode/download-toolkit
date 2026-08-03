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

    def _resolve_ep(ep_url):
        """Fetch the episode page, walk its server list and resolve the first
        embed that cracks to a direct stream. Returns (direct_url, tried_hosts)
        -- tried_hosts feeds the failure message so we can name the culprit.

        Split out of download_episode so it can run in the Prefetcher's
        background thread while the PREVIOUS episode is still downloading. Every
        other multi-episode extractor (myasiantv, dramarain, plutomovies,
        naijaprey, jarocks, naijavault) already does this; anitaku resolving
        serially is why there was a long stall between episodes.

        Uses its own requests.Session -- the shared `session` is touched by the
        main thread, and this runs concurrently with it."""
        s = requests.Session()
        s.headers['User-Agent'] = session.headers.get('User-Agent', UA_DESKTOP)
        s.headers['Referer'] = ep_url

        r = safe_get(s, ep_url, referer=ANITAKU_BASE + '/', timeout=15)
        if r is None:
            return None, set()

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

        seen = set()
        for embed_url in embed_links:
            if embed_url in seen or embed_url.startswith('javascript:'):
                continue
            seen.add(embed_url)

            safe_print(f"  [*] Resolving embed: {embed_url[:60]}...")
            direct_url = ResolverRegistry.resolve(embed_url, s)
            if direct_url and direct_url != embed_url:
                return direct_url, seen
        return None, seen

    def download_episode(ep_url, ep_name, resolved=None):
        """Download one episode. `resolved` is the (url, hosts) tuple the
        prefetcher already produced; when absent we resolve inline."""
        if resolved is None:
            resolved = _resolve_ep(ep_url)
        resolved_stream, seen = resolved

        if resolved_stream:
            safe_print(f"  [*] Downloading stream: {resolved_stream[:70]}...")
            download_file(
                resolved_stream, folder, safe_filename(f"{ep_name}.mp4"), summary,
                series_url=url, series_name=name, bandwidth_limit=bw,
                quality=quality, current_process=cur_proc, stop_flag=stop, pause_flag=pause
            )
        else:
            # nova.upn.one (upn.one) is a hardened player: the stream URLs come
            # back AES-256-CBC encrypted with a key derived from a live browser
            # fingerprint, behind an obfuscated string-table bundle. There's no
            # scrapable direct URL, so we surface a clear reason rather than a
            # generic "could not resolve". Anitaku only serves this as the sole
            # server on some movies/specials -- series use resolvable hosts.
            if any(re.search(r'\bupn\.one\b', e) for e in seen):
                safe_print(f"  [X] {ep_name}: only server is nova.upn.one, a "
                           "hardened/encrypted player we can't resolve. Skipping.")
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

        # The episode container IS the source of truth. Whatever watch links
        # live inside div.bixbox.bxcl.epcheck ARE the episodes -- 1171 for a
        # long-running series, or a single entry for a movie/special. So we take
        # every real watch link in the container, in document order, and DON'T
        # gate on the slug: anitaku rewords slugs between the series landing page
        # and the watch page (series "...-the-movie-infinity-castle" -> watch
        # "...-infinity-castle-dub-movie-1") and names specials without any
        # "-episode-N" at all, so any slug/regex gate here is whack-a-mole. The
        # "-episode-N" pattern is used ONLY to number/sort below, never to
        # decide what counts as an episode.
        seen = set()
        ep_links = []
        search_root = container

        # Prefer the list-item anchors (the site renders each episode as one
        # <li><a>), falling back to any anchor in the container if the markup
        # ever drops the <li> wrapper.
        anchors = search_root.select('li a[href]') or search_root.find_all('a', href=True)
        for a in anchors:
            href = a['href']
            if href in seen or not href or href.startswith(('javascript:', '#')):
                continue
            full = urljoin(ANITAKU_BASE, href)
            if 'anitaku.com.ro/' not in full:
                continue
            child = full.rstrip('/').split('/')[-1]
            # Not the series page itself, and not share/nav junk that sometimes
            # leaks into the container.
            if not child or child == slug.rstrip('/'):
                continue
            if any(x in full for x in ('pinterest', 't.me', 'facebook',
                                       'twitter', 'whatsapp', '/genre',
                                       '/genres/', '/tag/', '?')):
                continue
            seen.add(href)
            ep_links.append((full, a.get_text(strip=True) or child))

        if not ep_links:
            safe_print(render_message('no_episode_links'))
            return

        # A single entry with no "-episode-N" number is a movie/special.
        if len(ep_links) == 1 and not re.search(r'episode-(\d+)', ep_links[0][0]):
            safe_print("[*] Movie/Special - single video, no episode list.")

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

        # Build the work-list first — the skip checks are local (disk + resume
        # state, no network), so the prefetcher never burns an embed resolve on
        # an episode we were going to skip anyway.
        work = []
        for i, (ep_url, ep_text) in enumerate(ep_links, 1):
            ep_name = safe_filename(ep_url.rstrip('/').split('/')[-1])
            done, _ = already_downloaded(folder, safe_filename(f"{ep_name}.mp4"), series_url=url)
            if done:
                safe_print(f"\n[{i}/{len(ep_links)}] {ep_name}")
                safe_print(render_message('already_saved'))
                summary.add_skipped()
                continue
            work.append((ep_name, ep_url))

        # Resolve episode N+1's embed while episode N downloads. The resolve is
        # the slow part here (episode page fetch + embed crack, often several
        # seconds), so overlapping it with the download removes the stall the
        # user sees between episodes.
        prefetcher = Prefetcher(_resolve_ep)
        if work:
            prefetcher.prefetch(work[0][1])

        for i, (ep_name, ep_url) in enumerate(work, 1):
            if _stopped(ctx):
                break
            _wait(ctx)
            safe_print(f"\n[{i}/{len(work)}] {ep_name}")

            resolved = prefetcher.get(timeout=45)
            if i < len(work):
                prefetcher.prefetch(work[i][1])

            # Prefetch is an optimization, not the source of truth: a background
            # network blip yields None, and these embed links are short-lived, so
            # fall back to a fresh inline resolve rather than failing the episode.
            if not resolved or not resolved[0]:
                resolved = None
            download_episode(ep_url, ep_name, resolved=resolved)
            time.sleep(1)

    if summary.failed == 0 and not _stopped(ctx):
        mark_series_complete(url)
    summary.report(name)
