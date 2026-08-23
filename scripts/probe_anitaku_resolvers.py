#!/usr/bin/env python3
"""One-off survey: crawl ~100 anitaku series, pull the first 2 episode pages of
each, extract the embed hosts they serve, and map each host to the resolver (if
any) that would handle it. Tells us which resolvers anitaku actually exercises,
which are dead weight, and how much of the catalog is stuck on unresolvable
players like nova.upn.one. Read-only against the live site."""
import os, re, sys, time, collections
from urllib.parse import urljoin, urlparse
from html import unescape

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from bs4 import BeautifulSoup
from src.resolvers import ResolverRegistry

BASE = "https://anitaku.com.ro"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
TARGET_SERIES = 100
EPS_PER_SERIES = 2

sess = requests.Session()
sess.headers.update({'User-Agent': UA, 'Referer': BASE + '/'})


def get(url, **kw):
    for _ in range(3):
        try:
            r = sess.get(url, timeout=20, **kw)
            if r.status_code == 200:
                return r
        except Exception:
            pass
        time.sleep(1)
    return None


def which_resolver(embed_url):
    """Name of the resolver that claims this URL, or None."""
    for r in ResolverRegistry.RESOLVERS:
        try:
            if r.can_resolve(embed_url):
                return r.__name__.replace('Resolver', '')
        except Exception:
            continue
    return None


def collect_series_urls(limit):
    """Gather series (category) URLs from the A-Z catalog. The full alphabetical
    list lives in <a> tags under div.leftseries on /anime-list.html -- ~140 per
    page, so one or two pages is plenty for a 100-series sample."""
    urls, seen = [], set()
    for page_url in (f"{BASE}/anime-list.html",
                     f"{BASE}/anime-list.html?page=2",
                     f"{BASE}/anime-list.html?page=3"):
        listing = get(page_url)
        if not listing:
            continue
        soup = BeautifulSoup(listing.text, 'html.parser')
        anchors = soup.select('div.leftseries a[href], .leftseries a[href]')
        for a in anchors:
            href = urljoin(BASE, a['href'])
            if 'anitaku.com.ro/' not in href:
                continue
            slug = href.rstrip('/').split('/')[-1]
            if re.search(r'episode-\d+', slug):
                continue
            if any(x in href for x in ('/genre', '/tag/', '/page/', '?', '#',
                                       '/anime-list', 'facebook', 'twitter',
                                       't.me', '/category', '/season/',
                                       '/status/', '/release/')):
                continue
            if slug in ('', 'anitaku.com.ro') or href in seen:
                continue
            seen.add(href)
            urls.append(href)
            if len(urls) >= limit:
                return urls
        time.sleep(0.3)
    return urls[:limit]


def episode_pages(series_url, n):
    """First n episode watch-page URLs for a series."""
    r = get(series_url)
    if not r:
        return []
    soup = BeautifulSoup(r.text, 'html.parser')
    container = (soup.select_one('div.bixbox.bxcl.epcheck')
                 or soup.select_one('div.eplister')
                 or soup.select_one('div.bxcl'))
    if not container:
        return []
    out, seen = [], set()
    slug = series_url.rstrip('/').split('/')[-1]
    for a in (container.select('li a[href]') or container.find_all('a', href=True)):
        href = urljoin(BASE, a['href'])
        if 'anitaku.com.ro/' not in href:
            continue
        child = href.rstrip('/').split('/')[-1]
        if not child or child == slug or href in seen:
            continue
        if any(x in href for x in ('pinterest', 't.me', 'facebook', 'twitter',
                                   'whatsapp', '/genre', '/tag/', '?')):
            continue
        seen.add(href)
        out.append(href)
    # container lists newest-first for long series; sort by ep number so "first
    # 2" is deterministic, then take head.
    def num(u):
        m = re.search(r'episode-(\d+)', u)
        return int(m.group(1)) if m else 0
    out.sort(key=num)
    return out[:n]


def embeds_on(ep_url):
    """Embed/player hosts served on one episode page (same logic as extractor)."""
    r = get(ep_url)
    if not r:
        return []
    soup = BeautifulSoup(r.text, 'html.parser')
    links = []
    multi = soup.find('div', class_=re.compile(r'anime_muti_link|servers', re.I))
    if multi:
        for a in multi.find_all('a'):
            link = a.get('data-video') or a.get('href')
            if link:
                links.append(urljoin(ep_url, unescape(link)))
    for iframe in soup.find_all('iframe', src=True):
        links.append(urljoin(ep_url, iframe['src']))
    # dedup, drop javascript:
    out, seen = [], set()
    for l in links:
        if l in seen or l.startswith('javascript:'):
            continue
        seen.add(l)
        out.append(l)
    return out


def main():
    print(f"[*] Collecting up to {TARGET_SERIES} series URLs...")
    series = collect_series_urls(TARGET_SERIES)
    print(f"[*] Got {len(series)} series. Probing {EPS_PER_SERIES} eps each...\n")

    host_counts = collections.Counter()          # netloc -> times seen
    host_resolver = {}                            # netloc -> resolver name / None
    resolver_hits = collections.Counter()         # resolver name -> embeds
    unresolved_hosts = collections.Counter()      # netloc -> times, no resolver
    eps_probed = 0
    eps_with_resolvable = 0
    eps_all_unresolved = 0
    series_probed = 0

    for i, s in enumerate(series, 1):
        eps = episode_pages(s, EPS_PER_SERIES)
        if not eps:
            print(f"[{i:>3}/{len(series)}] (no episodes)  {s.split('/')[-1][:50]}")
            continue
        series_probed += 1
        for ep in eps:
            embeds = embeds_on(ep)
            eps_probed += 1
            any_resolvable = False
            if not embeds:
                eps_all_unresolved += 1
            for e in embeds:
                host = urlparse(e).netloc.lower()
                if not host:
                    continue
                host_counts[host] += 1
                name = host_resolver.get(host)
                if host not in host_resolver:
                    name = which_resolver(e)
                    host_resolver[host] = name
                if name:
                    resolver_hits[name] += 1
                    any_resolvable = True
                else:
                    unresolved_hosts[host] += 1
            if any_resolvable:
                eps_with_resolvable += 1
            elif embeds:
                eps_all_unresolved += 1
        tag = "OK" if any_resolvable else "!!"
        print(f"[{i:>3}/{len(series)}] {tag}  {s.split('/')[-1][:50]}")
        time.sleep(0.2)

    print("\n" + "=" * 66)
    print(f"SUMMARY  ({series_probed} series with episodes, {eps_probed} ep pages probed)")
    print("=" * 66)
    print(f"  episodes with >=1 resolvable server : {eps_with_resolvable}")
    print(f"  episodes with NO resolvable server  : {eps_all_unresolved}")

    print("\n--- Resolvers actually used by anitaku (by embeds seen) ---")
    for name, c in resolver_hits.most_common():
        print(f"  {c:>4}  {name}")
    used = set(resolver_hits)
    all_res = {r.__name__.replace('Resolver', '') for r in ResolverRegistry.RESOLVERS}
    print("\n--- Resolvers NEVER hit on this anitaku sample ---")
    for name in sorted(all_res - used):
        print(f"        {name}")

    print("\n--- Unresolvable hosts anitaku serves (candidates / dead ends) ---")
    for host, c in unresolved_hosts.most_common(25):
        print(f"  {c:>4}  {host}")

    print("\n--- All distinct hosts -> resolver mapping ---")
    for host, c in host_counts.most_common():
        print(f"  {c:>4}  {host:<32} -> {host_resolver.get(host) or 'NONE'}")


if __name__ == '__main__':
    main()
