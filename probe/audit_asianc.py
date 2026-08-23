"""Manual asianc audit driver: search -> drama-detail -> first episode page ->
embed classification (vidbasic vs hglink vs other) -> reference ResolverRegistry
resolve -> 100KB / HLS verify. Appends JSON lines to results-asianc-*.jsonl.
"""
import sys, re, json, time
sys.path.insert(0, r'C:\Users\Anon\download-toolkit')
sys.path.insert(0, r'C:\Users\Anon\download-toolkit\probe')
from src.downloader import make_session
from src.resolvers import ResolverRegistry
import probe_harness as ph

UA = ph.UA
SERIES_Q = ["cobra kai", "vincenzo", "squid game", "money heist", "breaking bad",
            "stranger things", "the heirs", "h2o just add water", "abbott elementary",
            "the witcher", "peaky blinders", "lupin", "narcos", "dark", "mr robot"]
MOVIES_Q_ASIAN = ["train to busan", "godzilla minus one", "parasite", "your name",
                  "the five deadly venoms", "ip man", "oldboy", "the handmaiden",
                  "memories of murder", "burn 2018", "along with the gods", "tazza",
                  "the wreck 2021", "midnight runners", "forgotten 2017"]
SERIES_MARK = re.compile(r"season|episode|-s\d|korean-drama|chinese-drama|thai-drama|tv-series|kdrama|jdrama", re.I)


def get(s, url, referer=None, cap=ph.PAGE_CAP):
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    try:
        r = s.get(url, timeout=20, headers=headers, stream=True)
        d = b""
        for ch in r.iter_content(16384):
            d += ch
            if len(d) >= cap:
                break
        r.close()
        return r.status_code, d.decode("utf-8", "ignore")
    except Exception as e:
        return None, str(e)


def classify_embed(u):
    low = (u or "").lower()
    if any(h in low for h in ("vidbasic.", "vidb.top", "embedload.cfd")):
        return "vidbasic"
    if any(h in low for h in ("hglink", "streamwish", "strwsh", "stwish", "wishembed")):
        return "hglink"
    return "other:" + (u.split("/")[2] if u and "://" in u else "?")[:30]


def audit(want_type, queries, out_path, max_items=15):
    s = make_session()
    out = open(out_path, "a", encoding="utf-8")
    ok = fail = nocand = 0
    tested = 0
    seen_shows = set()
    for q in queries:
        if tested >= max_items:
            break
        st, txt = get(s, f"https://asianc.id/api?a=search&keyword={q.replace(' ', '+')}")
        try:
            data = json.loads(txt)
        except Exception:
            data = []
        cands = []
        for d in data:
            u = d.get("url") or ""
            if u.startswith("/"):
                u = "https://asianc.id" + u
            if "drama-detail" not in u:
                continue
            slug = u.rsplit("/", 1)[-1]
            is_series_bool = bool(SERIES_MARK.search(slug))
            if want_type == "series" and not is_series_bool and len(cands) == 0 and False:
                pass  # asianc dramas lack markers; accept first result anyway (noted)
            if want_type == "movie" and is_series_bool:
                continue
            cands.append(u)
        item = next((u for u in cands if u not in seen_shows), None)
        if not item:
            print(f"  [{q}] no candidate", flush=True)
            continue
        seen_shows.add(item)
        rec = {"site": "asianc", "type": want_type, "query": q, "show": item,
               "note": "manual-drive; harness regexes missed relative episode links"}
        # detail -> episode links
        std, det = get(s, item)
        eps = re.findall(r'href="(/[a-z0-9\-]*episode[^"\']*\.html)"', det or "")
        eps = list(dict.fromkeys(eps))
        rec["episodes_found"] = len(eps)
        if not eps:
            rec.update({"result": "NO-CANDIDATE-LINK"})
            nocand += 1
            fail += 1
        else:
            ep_url = "https://asianc.id" + eps[0]
            rec["episode"] = ep_url
            ste, ep = get(s, ep_url, referer=item)
            embeds = re.findall(r'data-video="([^"]+)"', ep or "")
            embeds += [m for m in re.findall(r'<iframe[^>]+src="([^"]+)"', ep or "")]
            embeds = list(dict.fromkeys(e.strip() for e in embeds if e.strip()))
            if embeds and embeds[0].startswith("//"):
                embeds[0] = "https:" + embeds[0]
            rec["embeds"] = embeds[:4]
            rec["embed_kind"] = classify_embed(embeds[0]) if embeds else "none"
            if not embeds:
                rec.update({"result": "NO-EMBED"})
                fail += 1
            else:
                target = embeds[0]
                direct = None
                try:
                    direct = ResolverRegistry.resolve(target, s)
                except Exception as e:
                    rec["resolve_exc"] = str(e)[:100]
                rec["resolved"] = (direct or "")[:200] or None
                if not direct:
                    rec["result"] = "RESOLVE-FAILED"
                    fail += 1
                elif ".m3u8" in direct.lower():
                    v = ph.hls_probe(s, direct)
                    rec["verify"] = v
                    rec["result"] = "OK" if v["ok"] else "FAIL"
                    ok += v["ok"]
                    fail += (not v["ok"])
                else:
                    v = ph.head_100kb(s, direct)
                    rec["verify"] = v
                    rec["result"] = "OK" if v["ok"] else "FAIL"
                    ok += v["ok"]
                    fail += (not v["ok"])
        tested += 1
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()
        print(f"  [{tested:2d}] {rec['result']:16s} {rec.get('embed_kind','')[:22]:22s} "
              f"{item.rsplit('/',1)[-1][:40]:40s} {rec.get('verify', {}).get('kind', '')}", flush=True)
        time.sleep(0.8)
    out.close()
    print(f"[asianc/{want_type}] DONE ok={ok} fail={fail} no-cand={nocand}", flush=True)


if __name__ == "__main__":
    print("=== ASIANC SERIES ===", flush=True)
    audit("series", SERIES_Q, r"C:\Users\Anon\download-toolkit\probe\results-asianc-series.jsonl")
    print("=== ASIANC MOVIES ===", flush=True)
    audit("movie", MOVIES_Q_ASIAN, r"C:\Users\Anon\download-toolkit\probe\results-asianc-movie.jsonl")
