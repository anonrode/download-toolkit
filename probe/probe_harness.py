"""
Parity probe harness: for each site, search real titles, enumerate episodes,
resolve through the MONOLITH's battle-tested resolver registry, download only
the first 100KB of the resolved file, and verify media magic bytes.

Usage:  python probe_harness.py --site nkiri [--type series|movie] [--max 15]
Output: one JSON line per tested item -> probe/results-<site>.jsonl
"""
import sys, os, json, re, time, argparse, traceback

sys.path.insert(0, r'C:\Users\Anon\download-toolkit')
os.chdir(r'C:\Users\Anon\download-toolkit')

from src.downloader import make_session
from src.resolvers import ResolverRegistry

MODE = "reference"  # or "app" -- set by --mode
def _resolve(url, session):
    if MODE == "app":
        import app_resolver
        return app_resolver.app_resolve(url, session)
    return ResolverRegistry.resolve(url, session)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
HEAD_CAP = 100 * 1024          # never pull more than 100KB of any resolved file
PAGE_CAP = 300 * 1024          # page fetches capped too

SERIES_Q = ["cobra kai", "vincenzo", "squid game", "money heist", "breaking bad",
            "stranger things", "the heirs", "h2o just add water", "abbott elementary",
            "the witcher", "peaky blinders", "lupin", "narcos", "dark", "mr robot"]
MOVIES_Q = ["uncharted", "divergent", "the impossible", "godzilla", "titanic",
            "venom", "joker", "interstellar", "gladiator", "the batman",
            "top gun maverick", "black panther", "fast furious", "morbius", "tenet"]


def fetch(session, url, referer=None, cap=PAGE_CAP):
    """GET with a hard byte cap; returns (text_or_None, final_url, status)."""
    try:
        headers = {"Referer": referer} if referer else {}
        r = session.get(url, timeout=20, headers=headers, stream=True)
        chunks, got = [], 0
        for chunk in r.iter_content(chunk_size=16384):
            chunks.append(chunk)
            got += len(chunk)
            if got >= cap:
                break
        r.close()
        return b"".join(chunks)[:cap].decode("utf-8", "ignore"), r.url, r.status_code
    except Exception as e:
        return None, url, str(e)


def head_100kb(session, url, referer=None):
    """Download AT MOST 100KB of the resolved file; classify by magic bytes."""
    try:
        headers = {"Range": "bytes=0-102399"}
        if referer:
            headers["Referer"] = referer
        r = session.get(url, timeout=25, headers=headers, stream=True)
        ct = (r.headers.get("Content-Type") or "").lower()
        data = b""
        for chunk in r.iter_content(chunk_size=16384):
            data += chunk
            if len(data) >= HEAD_CAP:
                break
        r.close()
        data = data[:HEAD_CAP]
        if not data:
            return {"ok": False, "kind": "EMPTY", "ct": ct, "bytes": 0}
        low = data[:1200].lower().lstrip()
        if low.startswith((b"<!doctype", b"<html", b"<head", b"<body")) or b"<html" in low:
            return {"ok": False, "kind": "HTML-NOT-MEDIA", "ct": ct, "bytes": len(data)}
        if len(data) >= 12 and data[4:8] == b"ftyp":
            return {"ok": True, "kind": "MP4", "ct": ct, "bytes": len(data)}
        if data.startswith(b"\x1a\x45\xdf\xa3"):
            return {"ok": True, "kind": "MKV/EBML", "ct": ct, "bytes": len(data)}
        if data.startswith(b"ID3") or (len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
            return {"ok": True, "kind": "MP3/AAC", "ct": ct, "bytes": len(data)}
        if data[0:1] == b"\x47" and len(data) > 188 and data[188:189] == b"\x47":
            return {"ok": True, "kind": "MPEG-TS", "ct": ct, "bytes": len(data)}
        if data[:4] in (b"styp", b"sidx", b"moov"):
            return {"ok": True, "kind": "fMP4", "ct": ct, "bytes": len(data)}
        if ct.startswith(("video/", "audio/")) or "octet-stream" in ct:
            return {"ok": True, "kind": f"MEDIA-CT", "ct": ct, "bytes": len(data)}
        return {"ok": False, "kind": f"UNKNOWN ct={ct[:40]} head={data[:20]!r}", "ct": ct, "bytes": len(data)}
    except Exception as e:
        return {"ok": False, "kind": f"FETCH-ERR {e}", "ct": "", "bytes": 0}


def hls_probe(session, master_url, referer=None):
    """For resolved .m3u8: master -> first variant -> first segment -> 100KB."""
    try:
        text, _, status = fetch(session, master_url, referer=referer)
        if not text or status != 200:
            return {"ok": False, "kind": f"HLS-MASTER-{status}"}
        lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
        if not lines:
            return {"ok": False, "kind": "HLS-EMPTY-MASTER"}
        variant = lines[0]
        if not variant.startswith("http"):
            base = master_url.rsplit("/", 1)[0]
            variant = f"{base}/{variant}"
        if ".m3u8" in variant:
            text2, _, st2 = fetch(session, variant, referer=master_url)
            if not text2 or st2 != 200:
                return {"ok": False, "kind": f"HLS-VARIANT-{st2}"}
            segs = [l.strip() for l in text2.splitlines() if l.strip() and not l.startswith("#")]
            if not segs:
                return {"ok": False, "kind": "HLS-EMPTY-VARIANT"}
            seg = segs[0]
            if not seg.startswith("http"):
                seg = f"{variant.rsplit('/', 1)[0]}/{seg}"
        else:
            seg = variant
        v = head_100kb(session, seg, referer=master_url)
        v["kind"] = "HLS-" + v["kind"]
        return v
    except Exception as e:
        return {"ok": False, "kind": f"HLS-ERR {e}"}


# ---------------- per-site search + candidate extraction ----------------

def _hrefs(html, base_pat):
    out = []
    for m in re.finditer(r'href="(' + base_pat + r')"', html):
        u = m.group(1)
        if u not in out:
            out.append(u)
    return out


def search_nkiri(s, q):
    html, _, _ = fetch(s, f"https://nkiri.top/?s={q.replace(' ', '+')}")
    if not html:
        return []
    urls = _hrefs(html, r"https://nkiri\.top/[a-z0-9\-]+/?")
    skip = ("/page/", "/category/", "/tag/", "/how-to", "/contact", "/about", "/dmca", "/wp-", "/sitemap", "/feed", "/search/", "/movies", "/tv-series", "/xmlrpc")
    return [u for u in urls if not any(k in u for k in skip)]


def candidates_nkiri(s, show_url):
    html, _, _ = fetch(s, show_url)
    if not html:
        return []
    return _hrefs(html, r"https://downloadwella\.com/[^\"']+\.html")[:1]


def search_9jarocks(s, q):
    html, _, _ = fetch(s, f"https://9jarocks.net/search/{q.replace(' ', '+')}/feed/rss2/")
    if not html:
        return []
    return _hrefs(html, r"https://9jarocks\.net/videodownload/[^\"']+\.html")


def candidates_9jarocks(s, show_url):
    html, _, _ = fetch(s, show_url)
    if not html:
        return []
    links = _hrefs(html, r"https://loadedfiles\.(?:net|org|st|to)/[a-z0-9]+")
    return links[:1]


def search_naijavault(s, q):
    html, _, _ = fetch(s, f"https://www.naijavault.com/wp-json/wp/v2/posts?search={q.replace(' ', '+')}&_embed=1")
    if not html:
        return []
    try:
        data = json.loads(html)
    except Exception:
        return []
    return [p.get("link") for p in data if isinstance(p, dict) and p.get("link")]


LOCKER_RE = re.compile(r'https://(?:loadedfiles\.(?:net|org|st|to)|downloadwella\.com|filevault[^/"\' ]*|lulacloud\.com|pixeldrain\.com)/[^"\'< )]+', re.I)


def candidates_naijavault(s, show_url):
    html, _, _ = fetch(s, show_url)
    if not html:
        return []
    seen, out = set(), []
    for m in LOCKER_RE.finditer(html):
        u = m.group(0)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:1]


def search_naijaprey(s, q):
    html, _, _ = fetch(s, f"https://www.naijaprey.tv/search/{q.replace(' ', '+')}/feed/rss2/")
    if not html:
        return []
    return _hrefs(html, r"https://www\.naijaprey\.tv/[a-z0-9\-]+/")


def candidates_naijaprey(s, show_url):
    html, _, _ = fetch(s, show_url)
    if not html:
        return []
    # real download post or the vdl file-host link
    posts = _hrefs(html, r"https://www\.naijaprey\.tv/download-[a-z0-9\-]+/")
    vdl = re.findall(r'https://vdl\.np-downloader\.com/sdm_downloads/[^"\'< )]+', html)
    return (vdl[:1] or posts[:1])


def search_nepu(s, q):
    html, _, _ = fetch(s, f"https://nepu.gd/api/search?q={q.replace(' ', '+')}")
    if not html:
        return []
    try:
        data = json.loads(html)
    except Exception:
        return []
    out = []
    for r in (data.get("results") or []):
        u = r.get("url") or ""
        if "/watch/" in u:
            out.append(u)
    return out


def candidates_nepu(s, show_url):
    # series page: episode links; movie page: itself
    html, _, _ = fetch(s, show_url)
    eps = []
    if html:
        eps = _hrefs(html, r"https://nepu\.gd/watch/tv/\d+/\d+/\d+")[:1]
    return eps[:1] if eps else [show_url]


def search_asianc(s, q):
    html, _, _ = fetch(s, f"https://asianc.id/api?a=search&keyword={q.replace(' ', '+')}")
    if not html:
        return []
    try:
        data = json.loads(html)
    except Exception:
        return []
    out = []
    for r in data:
        u = r.get("url") or ""
        if u.startswith("/"):
            u = "https://asianc.id" + u
        if "drama-detail" in u:
            out.append(u)
    return out


def candidates_asianc(s, show_url):
    html, _, _ = fetch(s, show_url)
    if not html:
        return []
    eps = _hrefs(html, r"https://asianc\.id/[a-z0-9\-]*episode[^\"']*\.html")
    if not eps:
        eps = _hrefs(html, r"https://asianc\.id/[a-z0-9\-]+\.html")
    return eps[:1]


def search_pluto(s, q):
    html, _, _ = fetch(s, f"https://plutomovies.com/search/{q.replace(' ', '+')}/page/1")
    if not html:
        return []
    # hrefs may be absolute (https://plutomovies.com/movie/<id>/<slug>)
    # or site-relative (/movie/<id>/<slug>) -- accept both, absolutize.
    pat = re.compile(r'href="((?:https://plutomovies\.com)?/(movie|series)/(\d+)/[^"]+)"')
    out, seen = [], set()
    for m in pat.finditer(html):
        u = m.group(1)
        if not u.startswith("http"):
            u = "https://plutomovies.com" + u
        u = u.split("#", 1)[0]          # drop #disqus_thread-style fragments
        if u not in seen:
            seen.add(u)
            out.append((m.group(2), u))
    return out


def candidates_pluto(s, show_url):
    html, _, _ = fetch(s, show_url)
    if not html:
        return []
    dl = _hrefs(html, r"https://dl\.plutomovies\.com/[^\"']+")
    return dl[:1]


# Category LISTING pages have the exact shape /<single-word>-drama/ (e.g.
# /chinese-drama/, /thai-drama/). Show pages are /<title>-<category>-drama/
# (e.g. /dating-in-the-kitchen-chinese-drama/) and MUST be kept, so the prefix
# before "-drama" must be hyphen-free to count as a category listing.
CATEGORY_DRAMA_RE = re.compile(r"^https://dramarain\.com/[a-z]+-drama/$")


def search_dramarain(s, q):
    html, _, _ = fetch(s, f"https://dramarain.com/?s={q.replace(' ', '+')}")
    if not html:
        return []
    urls = _hrefs(html, r"https://dramarain\.com/[a-z0-9\-]+/")
    return [u for u in urls if not CATEGORY_DRAMA_RE.match(u)]


def candidates_dramarain(s, show_url):
    html, _, _ = fetch(s, show_url)
    if not html:
        return []
    # current page shapes: (a) per-episode gateway links, (b) direct wetafiles
    # anchors, (c) legacy file-host patterns
    for pat in (r"https://dramarain\.com/download\?link=[^\"']+",
                r"https://wetafiles\.com/[^\"']+\.html",
                r"https://downloadwella\.com/[^\"']+\.html",
                r"https://loadedfiles\.(?:net|org|st|to)/[a-z0-9]+"):
        links = _hrefs(html, pat)
        if links:
            return links[:1]
    return []


SITES = {
    "nkiri":      {"search": search_nkiri,      "cands": candidates_nkiri},
    "9jarocks":   {"search": search_9jarocks,   "cands": candidates_9jarocks},
    "naijavault": {"search": search_naijavault, "cands": candidates_naijavault},
    "naijaprey":  {"search": search_naijaprey,  "cands": candidates_naijaprey},
    "nepu":       {"search": search_nepu,       "cands": candidates_nepu},
    "asianc":     {"search": search_asianc,     "cands": candidates_asianc},
    "pluto":      {"search": search_pluto,      "cands": candidates_pluto},
    "dramarain":  {"search": search_dramarain,  "cands": candidates_dramarain},
}


def classify(url):
    u = url.lower()
    if any(k in u for k in ("season", "series", "episode", "-s01", "-s02", "korean-drama",
                            "chinese-drama", "thai-drama", "tv-series", "/tv/", "kdrama")):
        return "series"
    if any(k in u for k in ("movie", "film", "/movie/")):
        return "movie"
    return "unknown"


def run_site(site, want_type, max_items, out_path):
    cfg = SITES[site]
    s = make_session()
    queries = SERIES_Q if want_type == "series" else MOVIES_Q

    # 1) collect candidate show URLs
    shows, used = [], set()
    for q in queries:
        if len(shows) >= max_items:
            break
        try:
            found = cfg["search"](s, q)
        except Exception:
            found = []
        for f in found:
            if isinstance(f, tuple):
                kind, u = f
                if want_type == "series" and kind != "series":
                    continue
                if want_type == "movie" and kind != "movie":
                    continue
            else:
                u = f
                if want_type == "series" and classify(u) != "series":
                    continue
                if want_type == "movie" and classify(u) != "movie":
                    continue
            if u not in used:
                used.add(u)
                shows.append(u)

    print(f"[{site}/{want_type}] {len(shows)} candidate shows", flush=True)

    # 2) per show: first candidate -> resolve -> verify 100KB
    ok = fail = 0
    with open(out_path, "a", encoding="utf-8") as out:
        for i, show in enumerate(shows[:max_items]):
            rec = {"site": site, "type": want_type, "show": show}
            try:
                cands = cfg["cands"](s, show)
                rec["candidates"] = cands[:1]
                if not cands:
                    rec.update({"resolved": None, "result": "NO-CANDIDATE-LINK"})
                    fail += 1
                else:
                    target = cands[0]
                    direct = _resolve(target, s)
                    rec["resolved"] = (direct or "")[:200] or None
                    if not direct:
                        rec["result"] = "RESOLVE-FAILED"
                        fail += 1
                    elif direct.startswith("magnet:"):
                        rec["result"] = "MAGNET-SKIP"
                    elif ".m3u8" in direct.lower():
                        v = hls_probe(s, direct)
                        rec["verify"] = v
                        rec["result"] = "OK" if v["ok"] else "FAIL"
                        ok += v["ok"]; fail += (not v["ok"])
                    else:
                        v = head_100kb(s, direct)
                        rec["verify"] = v
                        rec["result"] = "OK" if v["ok"] else "FAIL"
                        ok += v["ok"]; fail += (not v["ok"])
            except Exception as e:
                rec["result"] = f"HARNESS-ERR {e}"
                fail += 1
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            print(f"  [{i+1}/{min(len(shows), max_items)}] {rec['result']:22s} {show[:70]}", flush=True)
            time.sleep(0.4)

    print(f"[{site}/{want_type}] DONE ok={ok} fail={fail}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--type", default="both", choices=["series", "movie", "both"])
    ap.add_argument("--max", type=int, default=15)
    ap.add_argument("--mode", default="reference", choices=["reference", "app"])
    args = ap.parse_args()

    os.makedirs(r"C:\Users\Anon\download-toolkit\probe", exist_ok=True)
    site = args.site
    globals()["MODE"] = args.mode
    types = ["series", "movie"] if args.type == "both" else [args.type]
    for t in types:
        out_path = rf"C:\Users\Anon\download-toolkit\probe\results-{site}-{t}.jsonl"
        try:
            run_site(site, t, args.max, out_path)
        except Exception:
            traceback.print_exc()
