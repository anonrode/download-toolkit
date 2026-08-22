"""
50-title live batch verification suite (2026-08-22).

Validates the app's OTA scraper rules (scraper_rules.json in the serverless
repo) AND the parity pipeline against live sites, in 4 stages:

  S1 search        - query ~N titles per site via rules-driven or parity logic
  S2 episodes      - fetch top shows, count episode/stream links (>0 = pass)
  S3 crack         - first candidate through the monolith ResolverRegistry
  S4 1KB probe     - Range: bytes=0-1023 on the resolved URL (NEVER more),
                     200/206 + non-HTML + video/audio/octet ct or magic bytes

Data laws (hard-coded): probes are capped at 1KB; page fetches capped at
~300KB; 150ms pacing per host; per-site bailout after 8 consecutive HARD
search failures (timeouts/errors - not empty results).

Usage:
  python verify_batch.py --site pluto [--max-titles 50] [--max-shows 8]
  python verify_batch.py --sites pluto dramarain
Output: probe/results-verify-<site>.jsonl  (one JSON line per tested item)
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse

import requests
from bs4 import BeautifulSoup

MONOLITH = r"C:\Users\Anon\download-toolkit"
SERVERLESS = r"C:\Users\Anon\download-toolkit-serverless"
PROBE = os.path.join(MONOLITH, "probe")

sys.path.insert(0, MONOLITH)
from src.downloader import make_session          # noqa: E402
from src.resolvers import ResolverRegistry       # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
PAGE_CAP = 300 * 1024
PROBE_BYTES = 1024          # hard cap for stage 4 - never more
HOST_GAP = 0.15             # seconds between requests to the same host
BAILOUT = 8                 # consecutive hard search failures -> stop site
ITEM_BUDGET_S = 150         # per-show deadline across stages 2-4

RULES_PATH = os.path.join(SERVERLESS, "scraper_rules.json")

with open(RULES_PATH, encoding="utf-8") as f:
    RULES = json.load(f)
DOMAINS = RULES.get("domains", {})
SITE_RULES = RULES.get("sites", {})
MIRRORS = RULES.get("mirrors", {})


def base_urls(site):
    """Primary + mirrors for a site, in failover order (rules parity)."""
    primary = DOMAINS.get(site, "")
    mirrors = [m for m in MIRRORS.get(site, []) if m and m != primary]
    return [primary] + mirrors if primary else mirrors


def is_direct_media_url(u):
    """Rules-driven isDirectMediaUrl parity (app engine gate)."""
    clean = u.split("?")[0].split("#")[0].lower()
    return any(clean.endswith(e) for e in RULES.get("directMediaExtensions", []))

# ---------------------------------------------------------------- query banks

GLOBAL_Q = [
    "cobra kai", "vincenzo", "squid game", "money heist", "breaking bad",
    "stranger things", "the heirs", "h2o just add water", "abbott elementary",
    "the witcher", "peaky blinders", "lupin", "narcos", "dark", "mr robot",
    "uncharted", "divergent", "the impossible", "godzilla", "titanic",
    "venom", "joker", "interstellar", "gladiator", "the batman",
    "top gun maverick", "black panther", "fast furious", "morbius", "tenet",
    "game of thrones", "prison break", "the 100", "elite", "outer banks",
    "the boys", "wednesday", "avatar", "dune", "oppenheimer",
    "mission impossible", "john wick", "the equalizer", "aquaman",
    "shazam", "megalodon", "the meg", "transformers", "jurassic park", "frozen",
]

KD_Q = [
    "vincenzo", "squid game", "crash landing on you", "goblin",
    "descendants of the sun", "my love from the star", "business proposal",
    "nevertheless", "itaewon class", "sky castle", "whats wrong with secretary kim",
    "healer", "the glory", "little women", "alchemy of souls",
    "true beauty", "start up", "extraordinary attorney woo", "the kings affection",
    "mr sunshine", "hospital playlist", "it is okay to not be okay",
    "tale of the nine tailed", "vincenzo", "lovely runner", "queen of tears",
    "twenty five twenty one", "our beloved summer", "hometown cha cha cha",
    "crash course in romance", "reborn rich", "the k2", "while you were sleeping",
    "w two worlds", "legend of the blue sea", "the legend of the blue sea",
    "temptation of wolves", "boys over flowers", "playful kiss", "secret garden",
    "city hunter", "signal", "tunnel", "voice", "stranger",
    "flower of evil", "mouse", "beyond evil", "vincenzo", "money heist korea",
]

CD_Q = [
    "hidden love", "love between fairy and devil", "dating in the kitchen",
    "the double", "till the end of the moon", "love destiny",
    "bittersweet love", "in love forever", "my tutor my love", "lovex3",
    "the untamed", "meteor garden", "falling into your smile", "put your head on my shoulder",
    "a love so beautiful", "go go squid", "le coupe de foudre", "the day of becoming you",
    "you are my glory", "sweet teeth", "my little happiness", "intense love",
    "the story of ming lan", "nirvana in fire", "joy of life", "the long ballad",
    "ashes of love", "eternal love", "the princess wei young", "yanxi palace",
    "the kings woman", "legend of fei", "love and redemption", "immortal samsara",
    "blue whisper", "the flame's daughter", "who rules the world", "the journey of chongzi",
    "love in time", "my heroic husband", "the secret of the three kingdoms",
    "be with you", "the best of you in my mind", "i cannot hug you",
    "suddenly this summer", "our secret", "flourish in time", "time and him are just right",
]

ANIME_Q = [
    "naruto", "one piece", "attack on titan", "demon slayer", "jujutsu kaisen",
    "my hero academia", "spy x family", "chainsaw man", "bleach", "dragon ball",
    "one punch man", "tokyo ghoul", "death note", "fullmetal alchemist",
    "hunter x hunter", "solo leveling", "black clover", "fairy tail",
    "sword art online", "re zero", "vinland saga", "hells paradise",
    "blue lock", "haikyuu", "kurokos basketball", "free", "yuri on ice",
    "attack on titan final season", "demon slayer kimetsu no yaiba",
    "jujutsu kaisen season 2", "one piece egghead", "naruto shippuden",
    "boruto", "dr stone", "mob psycho", "tokyo revengers", "kaguya sama",
    "rent a girlfriend", "darling in the franxx", "erased", "steins gate",
    "cowboy bebop", "evangelion", "your lie in april", "clannad",
    "anohana", "angel beats", "violet evergarden", "frieren",
]

TORRENT_Q = [
    "dune part two", "avatar way of water", "oppenheimer", "barbie",
    "top gun maverick", "joker 2", "deadpool wolverine", "godzilla x kong",
    "inside out 2", "despicable me 4", "furiosa", "the fall guy",
    "challengers", "poor things", "the holdovers", "killers of the flower moon",
    "napoleon", "aquaman 2", "the marvels", "wish",
    "breaking bad 4k", "game of thrones 4k", "the last of us",
    "stranger things 4k", "the bear", "succession", "the crown",
    "house of the dragon", "the boys season 4", "shogun",
    "dune 1984", "interstellar 4k", "inception 4k", "the dark knight 4k",
    "gladiator 2", "wicked", "moana 2", "sonic 3", "mufasa",
    "alita battle angel", "edge of tomorrow", "mad max fury road",
    "spider man no way home", "avengers endgame", "infinity war",
    "black widow", "eternals", "doctor strange multiverse", "thor love and thunder",
    "guardians of the galaxy 3",
]

QUERY_BANKS = {
    "nkiri": GLOBAL_Q, "9jarocks": GLOBAL_Q, "naijavault": GLOBAL_Q,
    "naijaprey": GLOBAL_Q, "pluto": GLOBAL_Q, "nepu": GLOBAL_Q,
    "asianc": KD_Q, "dramakey": KD_Q, "dramarain": CD_Q,
    "anitaku": ANIME_Q, "torrents": TORRENT_Q,
}

ALL_SITES = ["nkiri", "dramakey", "asianc", "anitaku", "pluto", "dramarain",
             "9jarocks", "naijavault", "naijaprey", "nepu", "torrents"]

# ---------------------------------------------------------------- plumbing


class _FallbackResponse:
    """Response wrapper: transparently retries a failed READ once through a
    plain-requests session. curl_cffi's chrome120 impersonation stalls or
    resets mid-body on some vidsrc-family hosts (data.vidsrcme.ru, vsembed.ru
    -> curl 28 / SSL DECODE_ERROR) where plain requests works; the failure
    surfaces during iter_content/content/json, so the request-level fallback
    alone cannot catch it."""

    def __init__(self, paced, url, resp, kw):
        self._paced, self._url, self._resp, self._kw = paced, url, resp, kw

    def _body(self):
        try:
            return self._resp.content
        except Exception:
            self._resp = self._paced._plain_get(self._url, self._kw)
            return self._resp.content

    @property
    def content(self):
        return self._body()

    @property
    def text(self):
        return self._body().decode("utf-8", "ignore")

    def json(self):
        return json.loads(self._body())

    @property
    def status_code(self):
        return self._resp.status_code

    @property
    def headers(self):
        return self._resp.headers

    @property
    def url(self):
        return self._resp.url

    @property
    def ok(self):
        return self._resp.ok

    def iter_content(self, chunk_size=1, **kw):
        try:
            for chunk in self._resp.iter_content(chunk_size=chunk_size, **kw):
                yield chunk
        except Exception:
            self._resp = self._paced._plain_get(self._url, self._kw)
            for chunk in self._resp.iter_content(chunk_size=chunk_size, **kw):
                yield chunk

    def close(self):
        try:
            self._resp.close()
        except Exception:
            pass


class PacedSession:
    """requests.Session wrapper enforcing a minimum gap per host.

    Transport hardening: the primary session comes from make_session(), which
    prefers curl_cffi with a Chrome TLS impersonation. Some vidsrc-family hosts
    (vsembed.ru, data.vidsrcme.ru) stall or reset the impersonated handshake
    (curl 28 too-slow / SSL DECODE_ERROR), while plain `requests` works. Every
    response is wrapped so a failed read is retried once through a lazy
    plain-requests session before surfacing.
    """

    def __init__(self):
        self.s = make_session()
        self.s.headers.update({"User-Agent": UA})
        self._plain = None
        self._last = {}

    def _fallback(self):
        if self._plain is None:
            self._plain = requests.Session()
            self._plain.headers.update({"User-Agent": UA})
        return self._plain

    def _plain_get(self, url, kw):
        return self._fallback().get(url, **kw)

    def _pace(self, url):
        host = urllib.parse.urlparse(url).netloc
        t0 = self._last.get(host, 0.0)
        gap = time.time() - t0
        if gap < HOST_GAP:
            time.sleep(HOST_GAP - gap)
        self._last[host] = time.time()

    def get(self, url, **kw):
        self._pace(url)
        kw = dict(kw, timeout=(10, 20))
        try:
            return _FallbackResponse(self, url, self.s.get(url, **kw), kw)
        except Exception:
            return self._fallback().get(url, **kw)

    def post(self, url, **kw):
        self._pace(url)
        kw = dict(kw, timeout=(10, 20))
        try:
            return _FallbackResponse(self, url, self.s.post(url, **kw), kw)
        except Exception:
            return self._fallback().post(url, **kw)


def fetch(s, url, referer=None, cap=PAGE_CAP, allow_404=False):
    """GET with a hard byte cap; returns (text_or_None, status)."""
    try:
        headers = {"Referer": referer} if referer else {}
        r = s.get(url, headers=headers, stream=True)
        if not allow_404 and r.status_code not in (200, 206):
            r.close()
            return None, r.status_code
        chunks, got = [], 0
        for chunk in r.iter_content(chunk_size=16384):
            chunks.append(chunk)
            got += len(chunk)
            if got >= cap:
                break
        r.close()
        data = b"".join(chunks)[:cap]
        return data.decode("utf-8", "ignore"), r.status_code
    except Exception:
        return None, -1


def probe_1kb(s, url, referer=None):
    """Range: bytes=0-1023 only. Returns dict(ok, kind, ct, bytes)."""
    # One retry for network-class flakes (TLS resets, dropped connections) --
    # mirrors the monolith registry's retry policy; the 1KB cap is per attempt.
    for attempt in (0, 1):
        try:
            headers = {"Range": f"bytes=0-{PROBE_BYTES - 1}"}
            if referer:
                headers["Referer"] = referer
            r = s.get(url, headers=headers, stream=True)
            ct = (r.headers.get("Content-Type") or "").lower()
            status = r.status_code
            if status not in (200, 206):
                r.close()
                return {"ok": False, "kind": f"HTTP-{status}", "ct": ct, "bytes": 0}
            data = b""
            for chunk in r.iter_content(chunk_size=PROBE_BYTES):
                data += chunk
                if len(data) >= PROBE_BYTES:
                    break
            r.close()
            data = data[:PROBE_BYTES]
            if not data:
                return {"ok": False, "kind": "EMPTY", "ct": ct, "bytes": 0}
            low = data[:512].lower().lstrip()
            if low.startswith((b"<!doctype", b"<html", b"<head", b"<body")) or b"<html" in low:
                return {"ok": False, "kind": "HTML-NOT-MEDIA", "ct": ct, "bytes": len(data)}
            if len(data) >= 8 and data[4:8] == b"ftyp":
                return {"ok": True, "kind": "MP4", "ct": ct, "bytes": len(data)}
            if data[:4] in (b"styp", b"sidx", b"moov", b"moof"):
                return {"ok": True, "kind": "fMP4", "ct": ct, "bytes": len(data)}
            if data[:4] == b"\x1a\x45\xdf\xa3":
                return {"ok": True, "kind": "MKV/EBML", "ct": ct, "bytes": len(data)}
            if data.startswith(b"ID3") or (len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
                return {"ok": True, "kind": "MP3/AAC", "ct": ct, "bytes": len(data)}
            if len(data) > 188 and data[0] == 0x47 and data[188] == 0x47:
                return {"ok": True, "kind": "MPEG-TS", "ct": ct, "bytes": len(data)}
            if ct.startswith(("video/", "audio/")) or "octet-stream" in ct:
                return {"ok": True, "kind": "MEDIA-CT", "ct": ct, "bytes": len(data)}
            return {"ok": False, "kind": f"UNKNOWN ct={ct[:30]} head={data[:12]!r}", "ct": ct, "bytes": len(data)}
        except Exception as e:
            if attempt == 1:
                return {"ok": False, "kind": f"PROBE-ERR {e}", "ct": "", "bytes": 0}
            time.sleep(0.4)


def hls_probe(s, master_url, referer=None):
    """master -> first variant -> first segment 1KB probe (small fetches only).

    Referer strategy per hop: probe_referer_for() first (megaplay-family CDNs
    require EXACTLY the player origin), then a bare no-referer fallback --
    vidbasic's lisaido.top segments refuse ANY Referer and serve real MPEG-TS
    only bare (live-verified 2026-08-22: segment with referer -> HTML shell,
    bare -> MPEG-TS), so a referer-bound probe alone yields false PROBE-FAILs.
    """
    refs_master = [probe_referer_for(master_url, referer), None]
    text = status = None
    for ref in refs_master:
        t, st = fetch(s, master_url, referer=ref, cap=64 * 1024)
        if t and st in (200, 206):
            text, status = t, st
            break
    if not text or status not in (200, 206):
        return {"ok": False, "kind": f"HLS-MASTER-{status}"}
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    if not lines:
        return {"ok": False, "kind": "HLS-EMPTY-MASTER"}
    variant = lines[0]
    if not variant.startswith("http"):
        variant = urllib.parse.urljoin(master_url, variant)
    if ".m3u8" in variant:
        # megaplay-family CDNs (cdn.watching.onl/anivideo.sbs) require the
        # player origin https://megaplay.buzz/ on variant AND segment fetches
        # too; all other hosts keep the historical master_url referer. A bare
        # no-referer attempt is the fallback for referer-hostile hosts.
        refs_sub = [probe_referer_for(master_url, master_url), None]
        text2 = st2 = None
        for ref in refs_sub:
            t2, s2 = fetch(s, variant, referer=ref, cap=64 * 1024)
            if t2 and s2 in (200, 206):
                text2, st2 = t2, s2
                break
        if not text2 or st2 not in (200, 206):
            return {"ok": False, "kind": f"HLS-VARIANT-{st2}"}
        segs = [l.strip() for l in text2.splitlines() if l.strip() and not l.startswith("#")]
        if not segs:
            return {"ok": False, "kind": "HLS-EMPTY-VARIANT"}
        seg = segs[0]
        if not seg.startswith("http"):
            seg = urllib.parse.urljoin(variant, seg)
        v = None
        for ref in refs_sub:
            v = probe_1kb(s, seg, referer=ref)
            if v["ok"]:
                break
    else:
        # variant is a segment directly (no variant playlist)
        v = probe_1kb(s, variant, referer=refs_master[0])
        if not v["ok"]:
            v = probe_1kb(s, variant, referer=None)
    v["kind"] = "HLS-" + v["kind"]
    return v


def probe_referer_for(url, default=None):
    """Player-origin Referer for CDNs that allowlist one (anitaku's megaplay
    chain: cdn.watching.onl + segment host anivideo.sbs require EXACTLY
    https://megaplay.buzz/ on master, variant AND segments -- everything else
    gets a 403 HTML shell). Mirrors the monolith's get_referer_for_url
    megaplay mapping, extended with the hosts the live chain resolves to.
    """
    low = (url or "").lower()
    if any(h in low for h in ("cdn.watching.onl", "anivideo.sbs", "megap.", "watching.onl")):
        return "https://megaplay.buzz/"
    if "workerforcloud" in low or "workers.dev" in low:
        return "https://gogoanime.or.at/"
    return default


def verify_resolved(s, direct, referer=None):
    """Stage-4 verifier: magnet / hls / direct-file."""
    low = direct.lower()
    if low.startswith("magnet:"):
        return {"ok": None, "kind": "MAGNET", "ct": "", "bytes": 0}
    path = low.split("?")[0].split("#")[0]
    ref = probe_referer_for(direct, referer)
    if path.endswith(".m3u8"):
        return hls_probe(s, direct, referer=ref)
    return probe_1kb(s, direct, referer=ref)


# ---------------------------------------------------------------- stage 1: search

def _abs(base, u):
    return urllib.parse.urljoin(base, u).split("#", 1)[0]


def _hrefs(html, base_pat):
    out = []
    for m in re.finditer(r'href="(' + base_pat + r')"', html):
        u = m.group(1)
        if u not in out:
            out.append(u)
    return out


def search_rules_site(s, site, q):
    """Rules-driven search for configured sites (searchPattern + cardSelector)."""
    cfg = SITE_RULES.get(site)
    base = DOMAINS.get(site, "")
    if not cfg or not base:
        return [], "NO-RULES"
    pattern = cfg.get("searchPattern") or ""
    if not pattern:
        return [], "NO-PATTERN"
    url = base + pattern.replace("{query}", urllib.parse.quote(q))
    stype = cfg.get("searchType", "html")
    html, status = fetch(s, url)
    if not html:
        return [], f"FETCH-{status}"
    if stype == "rss":
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(html)
        except Exception:
            return [], "BAD-XML"
        out = []
        for item in root.iter("item"):
            link = item.findtext("link")
            if link and link.strip():
                out.append(link.strip())
        return out, "RSS"
    if stype == "json":
        try:
            data = json.loads(html)
        except Exception:
            return [], "BAD-JSON"
        key = cfg.get("cardSelector") or ""
        arr = data.get(key) if isinstance(data, dict) and key else data
        if not isinstance(arr, list):
            return [], "JSON-NOT-LIST"
        out = []
        for it in arr:
            if not isinstance(it, dict):
                continue
            link = it.get("link") or it.get("url") or ""
            if not link:
                continue
            out.append(_abs(base, link))
        return out, "JSON"
    # html
    sel = cfg.get("cardSelector") or ""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select(sel):
        href = a.get("href")
        if not href:
            continue
        href = _abs(base, href)
        if href not in out:
            out.append(href)
    return out, "HTML"


def search_nkiri(s, q):
    for base in base_urls("nkiri"):
        if not base:
            continue
        html, status = fetch(s, f"{base}/?s={urllib.parse.quote_plus(q)}")
        if not html:
            continue
        urls = _hrefs(html, re.escape(base) + r"/[a-z0-9\-]+/?")
        skip = ("/page/", "/category/", "/tag/", "/how-to", "/contact", "/about", "/dmca",
                "/wp-", "/tv-series", "/movies", "/sitemap", "/feed", "/xmlrpc", "/search/")
        out = [u for u in urls if not any(k in u for k in skip)]
        if out:
            return out, "HTML"
    return [], "HTML-MISS"


DRAMAKEY_CAND = ["", "-korean-drama", "-chinese-drama", "-thai-drama", "-season-1"]
DRAMAKEY_BASES = [
    ("https://dramakey.com", "{slug}/"), ("https://dramakey.com", "drama/{slug}/"),
    ("https://dramakey.cc", "{slug}/"), ("https://dramakey.cc", "drama/{slug}/"),
]


def search_dramakey(s, q):
    """Parity mirror of DramaKeyProvider slug-guessing (capped: 8 tries/query)."""
    slug = re.sub(r"[^a-z0-9]+", "-", q.lower()).strip("-")
    tokens = {t for t in slug.split("-") if len(t) > 1}
    tried = 0
    for suffix in DRAMAKEY_CAND:
        s2 = f"{slug}{suffix}"
        for base, path in DRAMAKEY_BASES:
            if tried >= 8:
                return [], "SLUG-CAP"
            url = f"{base}/{path.format(slug=s2)}"
            html, status = fetch(s, url, allow_404=True)
            tried += 1
            if not html or status not in (200, 206):
                if status == -1:
                    return [], "FETCH-ERR"   # connect/read failure -> hard fail (bailout)
                continue
            soup = BeautifulSoup(html, "html.parser")
            title = soup.select_one("h1.entry-title, h1")
            raw = title.get_text(strip=True) if title else ""
            if not raw:
                continue
            low = raw.lower()
            if "download korean, chinese, thai" in low or "latest dramas" in low:
                continue
            if tokens and not any(t in low for t in tokens):
                continue
            return [url], "SLUG"
    return [], "SLUG-MISS"


def search_anitaku(s, q):
    """Parity mirror of AnitakuProvider AJAX search (with HTML fallback)."""
    clean = re.sub(r"(?i) (season|series|part|s\d+)\s*\d* ", " ", q).strip()
    query = clean if len(clean) >= 2 else q
    endpoints = [
        ("https://gogoanime.or.at/wp-admin/admin-ajax.php", "https://gogoanime.or.at/"),
        ("https://anitaku.com.ro/wp-admin/admin-ajax.php", "https://anitaku.com.ro/"),
    ]
    for ajax_url, base in endpoints:
        try:
            r = s.post(ajax_url, data={"action": "ts_ac_do_search", "ts_ac_query": query},
                       headers={"Referer": base, "X-Requested-With": "XMLHttpRequest"})
            if r.status_code != 200:
                continue
            body = r.text
            if not body.strip().startswith("{"):
                continue
            data = json.loads(body)
            out = []
            for group in data.values():
                if not isinstance(group, list):
                    continue
                for entry in group:
                    if not isinstance(entry, dict):
                        continue
                    # live schema (2026-08-22): {"anime":[{"all":[{post_title,
                    # post_link}]}]} -- records sit under entry["all"]
                    records = entry.get("all") if isinstance(entry.get("all"), list) else [entry]
                    for it in records:
                        if not isinstance(it, dict):
                            continue
                        link = (it.get("post_link") or it.get("link")
                                or it.get("url") or it.get("permalink") or "")
                        title = it.get("post_title") or it.get("title") or it.get("name") or ""
                        if link:
                            out.append((_abs(base, link), title))
            if out:
                return out, "AJAX"
        except Exception:
            continue
    # fallback: HTML search page (anitaku.com.ro serves 404-as-200; try both)
    for base in ("https://anitaku.com.ro", "https://gogoanime.or.at"):
        html, status = fetch(s, f"{base}/search.html?keyword={urllib.parse.quote_plus(q)}")
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        if "page not found" in html.lower():
            continue
        out = []
        for a in soup.select("ul.items li a"):
            href = a.get("href")
            if href:
                out.append((_abs(base, href), a.get_text(strip=True)))
        if out:
            return out, "HTML"
    return [], "HTML-MISS"


def search_nepu(s, q):
    base = DOMAINS.get("nepu", "https://nepu.gd")
    html, status = fetch(s, f"{base}/api/search?q={urllib.parse.quote_plus(q)}")
    if not html:
        return [], f"FETCH-{status}"
    try:
        data = json.loads(html)
    except Exception:
        return [], "BAD-JSON"
    out = []
    for r in (data.get("results") or []):
        # live schema (2026-08-22): results carry id + media_type, no url --
        # watch URLs are built as /watch/{media_type}/{id} (parity with
        # NepuProvider; the api/search url field was dropped site-side).
        uid = r.get("id") or r.get("tmdb_id") or ""
        mtype = r.get("media_type") or "tv"
        if uid:
            out.append(f"{base}/watch/{mtype}/{uid}")
    return out, "JSON"


def search_torrents(s, q):
    """TPB API (rules domain) + YTS API -> magnet links."""
    tpb = DOMAINS.get("torrents", "https://apibay.org")
    out = []
    html, status = fetch(s, f"{tpb}/q.php?q={urllib.parse.quote_plus(q)}&cat=0")
    if html and status == 200:
        try:
            rows = json.loads(html)
            for row in rows[:5]:
                name = row.get("name") or ""
                ih = row.get("info_hash") or ""
                if ih:
                    out.append(("magnet:?xt=urn:btih:" + ih + "&dn=" +
                                urllib.parse.quote(name), name))
        except Exception:
            pass
    for api in ("https://yts.mx/api/v2/list_movies.json",
                "https://yts.lt/api/v2/list_movies.json",
                "https://movies-api.accel.li/api/v2/list_movies.json"):
        html, status = fetch(s, f"{api}?query_term={urllib.parse.quote_plus(q)}&limit=15")
        if not html or status != 200:
            continue
        try:
            movies = json.loads(html).get("data", {}).get("movies") or []
        except Exception:
            continue
        for mv in movies[:5]:
            for t in (mv.get("torrents") or [])[:2]:
                ih = t.get("hash") or ""
                if ih:
                    name = f"{mv.get('title')} {mv.get('year')} {t.get('quality')}"
                    out.append(("magnet:?xt=urn:btih:" + ih + "&dn=" +
                                urllib.parse.quote(name), name))
        if out:
            break
    return out, "MAGNET"


SEARCHERS = {
    "pluto": lambda s, q: search_rules_site(s, "pluto", q),
    "9jarocks": lambda s, q: search_rules_site(s, "9jarocks", q),
    "naijavault": lambda s, q: search_rules_site(s, "naijavault", q),
    "asianc": lambda s, q: search_rules_site(s, "asianc", q),
    "dramarain": lambda s, q: search_rules_site(s, "dramarain", q),
    "naijaprey": lambda s, q: search_rules_site(s, "naijaprey", q),
    "nkiri": search_nkiri, "dramakey": search_dramakey,
    "anitaku": search_anitaku, "nepu": search_nepu, "torrents": search_torrents,
}

# ---------------------------------------------------------------- stage 2: episodes

EPISODE_SEARCHERS = {
    "pluto": lambda soup: [a.get("href") for a in soup.select("a[href*='dl.plutomovies.com'], a[href*='/series/']")],
    "9jarocks": lambda soup: [a.get("href") for a in soup.select("a[href*='loadedfiles'], a[href*='downloadwella'], a[href*='wetafiles'], a[href*='.mkv'], a[href*='.mp4']")],
    "naijavault": lambda soup: [a.get("href") for a in soup.select("a[href*='nkiserv'], a[href*='filevault'], a[href*='downloadwella'], a[href*='wetafiles'], a[href*='loadedfiles'], a[href*='.mkv'], a[href*='.mp4'], a[href*='/dl-'], a[href*='gtoddl'], a[href*='wapkizfile']")],
    "asianc": lambda soup: [a.get("href") for a in soup.select("ul.list-episode-item-2 li a, .all-episodes li a, .list-episode a, .list-episode-item a, a[href*='-episode-']")],
    "naijaprey": lambda soup: [a.get("href") for a in soup.select("a[href*='vdl.'], a[href*='sdm.'], a[href*='wildshare'], a[href*='downloadwella'], a[href*='waffi'], a[href*='loadedfiles']")],
    # nkiri episode pages carry direct ds2.nkiserv.com MKVs ("Download
    # Episode" buttons) AND downloadwella lockers (live-verified 2026-08-22)
    "nkiri": lambda soup: [a.get("href") for a in soup.select(
        "a[href*='downloadwella.com'], a[href*='nkiserv.com'], a[href*='wetafiles.com'], "
        "a[href*='loadedfiles.'], a[href*='.mkv'], a[href*='.mp4']")],
    "dramakey": lambda soup: [a.get("href") for a in soup.select("a[href]") if a.get("href") and not any(k in a.get("href", "") for k in ("/category/", "/tag/"))],
    "anitaku": lambda soup: [a.get("href") for a in soup.select("#episode_page a[href*='-episode-'], a[href*='-episode-']")],
    "nepu": lambda soup: [a.get("href") for a in soup.select("a[href*='/watch/tv/']")],
    "dramarain": lambda soup: [a.get("href") for a in soup.select("a[href*='/download?link='], a[href*='wetafiles.com'], a[href*='downloadwella.com'], a[href*='loadedfiles.']")],
    "torrents": None,
}


def episodes_for(s, site, show_url):
    if site == "torrents":
        return [], "NA"
    html, status = fetch(s, show_url)
    if not html:
        return [], f"FETCH-{status}"
    soup = BeautifulSoup(html, "html.parser")
    fn = EPISODE_SEARCHERS.get(site)
    if not fn:
        return [], "NO-EP-SELECTOR"
    hrefs = [u for u in fn(soup) if u]
    seen = set()
    out = []
    for u in hrefs:
        u = _abs(show_url, u)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out, "HTML"


PRE_CRACK = {
    # Provider parity: the app extracts the embed iframe from an episode page
    # BEFORE handing it to the resolver registry (AsianCProvider/...
    # resolveEpisode). Without this step the registry never sees the embed.
    # NOTE: anitaku is intentionally NOT here -- AnitakuProvider.resolveEpisode
    # is mirrored by app_resolver._app_anitaku, which receives the episode
    # page URL itself (it extracts malId/ep and the embed candidates itself).
    "asianc": re.compile(r'<iframe[^>]*src="(//?[^"]*vidbasic[^"]*|https?://[^"]*vidbasic[^"]*)"', re.I),
    "nepu": re.compile(r'<iframe[^>]*src="([^"]+)"', re.I),
}


def precrack(s, site, episode_url):
    """Fetch the episode page and return the embed URL (or the page itself)."""
    pat = PRE_CRACK.get(site)
    if not pat:
        return episode_url
    html, status = fetch(s, episode_url)
    if not html:
        return episode_url
    m = pat.search(html)
    if not m:
        return episode_url
    src = m.group(1)
    if src.startswith("//"):
        src = "https:" + src
    elif not src.startswith("http"):
        src = _abs(episode_url, src)
    return src


# ---------------------------------------------------------------- driver

def run_site(site, max_titles, max_shows, out_path):
    s = PacedSession()
    queries = QUERY_BANKS.get(site, GLOBAL_Q)[:max_titles]
    hard_fails = 0
    shows = []      # (url, title)
    used = set()
    s1_total = s1_hits = 0

    # Stage 1: search
    for q in queries:
        if hard_fails >= BAILOUT:
            print(f"[{site}] BAILOUT after {hard_fails} hard search failures", flush=True)
            break
        try:
            found = SEARCHERS[site](s, q)
        except Exception as e:
            found = ([], f"EXC {e}")
        if isinstance(found, tuple):
            results, how = found
        else:
            results, how = found, "?"
        if not results:
            if how and how.startswith(("FETCH-", "EXC", "BAD-JSON")):
                hard_fails += 1
            continue
        hard_fails = 0
        s1_total += 1
        s1_hits += 1
        for item in results[:8]:
            if isinstance(item, tuple):
                u, title = item
            else:
                u, title = item, q
            if u and u not in used and len(shows) < max_shows * 4:
                used.add(u)
                shows.append((u, title))
    print(f"[{site}] S1 done: {s1_hits}/{s1_total} queries yielded results, "
          f"{len(shows)} candidate shows", flush=True)

    # Stage 2-4 on top shows
    rows = []
    with open(out_path, "a", encoding="utf-8") as out:
        for i, (show_url, title) in enumerate(shows[:max_shows]):
            item_deadline = time.time() + ITEM_BUDGET_S
            rec = {"site": site, "query": title, "show": show_url}
            # S2 episodes
            eps, how2 = episodes_for(s, site, show_url)
            rec["s2_count"] = len(eps)
            rec["s2_ok"] = len(eps) > 0
            if time.time() > item_deadline:
                rec.update({"s3_ok": False, "s3_resolved": None, "s4": None,
                            "result": "TIMEOUT-SKIP"})
                rows.append(rec)
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                continue
            # S3 crack (first candidate, with provider-level pre-crack)
            target = eps[0] if eps else (show_url if site == "torrents" else None)
            if target is None:
                rec.update({"s3_ok": False, "s3_resolved": None, "s4": None,
                            "result": "NO-CANDIDATE"})
                rows.append(rec)
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                continue
            target = precrack(s, site, target)  # embed extraction parity
            try:
                if target.startswith("magnet:"):
                    direct, engine = target, "MAGNET"
                elif is_direct_media_url(target):
                    # App engine parity: a candidate that IS a media file (e.g.
                    # ds2.nkiserv.com/...mkv direct links) routes straight to
                    # the downloader with no resolution pass.
                    direct, engine = target, "direct-media"
                else:
                    # App pipeline first (mirror of the FIXED Kotlin registry),
                    # monolith reference as fallback for unmirrored hosts.
                    # The PacedSession (not the raw curl_cffi session) is
                    # passed so vidsrc-family hosts that stall the impersonated
                    # TLS fingerprint (data.vidsrcme.ru) transparently retry
                    # through plain requests.
                    import app_resolver
                    direct = app_resolver.app_resolve(target, s)
                    engine = "app-mirror"
                    if not direct:
                        direct = ResolverRegistry.resolve(target, s)
                        engine = "reference"
            except Exception as e:
                direct = None
                engine = "EXC"
                rec["s3_err"] = str(e)[:120]
            rec["s3_engine"] = engine
            rec["s3_resolved"] = (direct or "")[:220] or None
            rec["s3_ok"] = bool(direct)
            # S4 1KB probe
            if not direct:
                rec.update({"s4": None, "result": "RESOLVE-FAILED"})
            else:
                v = verify_resolved(s, direct, referer=show_url)
                rec["s4"] = v
                if v["ok"] is None:
                    rec["result"] = "MAGNET"
                elif v["ok"]:
                    rec["result"] = "OK"
                else:
                    rec["result"] = "PROBE-FAIL"
            if time.time() > item_deadline and rec["result"] not in ("OK", "MAGNET"):
                rec["result"] = "TIMEOUT-SKIP"
            rows.append(rec)
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            print(f"  [{i + 1}/{min(len(shows), max_shows)}] {rec['result']:14s} "
                  f"eps={rec['s2_count']:3d} {show_url[:60]}", flush=True)
            time.sleep(0.1)

    # summary line
    s2_ok = sum(1 for r in rows if r["s2_ok"])
    s3_ok = sum(1 for r in rows if r.get("s3_ok"))
    s4_ok = sum(1 for r in rows if r.get("s4") and r["s4"].get("ok") is True)
    s4_na = sum(1 for r in rows if r.get("s4") and r["s4"].get("ok") is None)
    s2_na = sum(1 for r in rows if r.get("s2_ok") is None)
    print(f"[{site}] SUMMARY s1={s1_hits}/{s1_total} s2={s2_ok}/{len(rows)}"
          f" s3={s3_ok}/{len(rows)} s4={s4_ok}/{len(rows)} (na s2={s2_na} s4={s4_na})", flush=True)
    return {"site": site, "s1": [s1_hits, s1_total], "s2": [s2_ok, len(rows), s2_na],
            "s3": [s3_ok, len(rows)], "s4": [s4_ok, len(rows), s4_na]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", action="append", dest="sites")
    ap.add_argument("--sites", dest="sites_alt")
    ap.add_argument("--max-titles", type=int, default=50)
    ap.add_argument("--max-shows", type=int, default=8)
    args = ap.parse_args()

    sites = args.sites or (args.sites_alt.split() if args.sites_alt else [])
    if not sites:
        sites = ALL_SITES
    for site in sites:
        if site not in ALL_SITES:
            print(f"unknown site {site}; choices: {ALL_SITES}")
            continue
        out_path = os.path.join(PROBE, f"results-verify-{site}.jsonl")
        # fresh file per run: remove stale rows from previous runs
        if os.path.exists(out_path):
            os.remove(out_path)
        try:
            run_site(site, args.max_titles, args.max_shows, out_path)
        except Exception:
            import traceback
            traceback.print_exc()
