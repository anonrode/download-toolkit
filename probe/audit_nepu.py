"""Manual nepu audit driver: same semantics as probe_harness (search -> first
candidate -> resolve -> 100KB verify) but adapted to nepu's current site shape.
Records BOTH the reference-as-is result (tokenless master fetch status) and the
healthy-chain result (node argv fix + generate.php token + stamped HLS probe).
Appends one JSON line per item to results-nepu-{series,movie}.jsonl.
"""
import sys, re, json, time, base64, subprocess, tempfile, os
sys.path.insert(0, r'C:\Users\Anon\download-toolkit')
from src.downloader import make_session

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
SERIES_Q = ["cobra kai", "vincenzo", "squid game", "money heist", "breaking bad",
            "stranger things", "the heirs", "h2o just add water", "abbott elementary",
            "the witcher", "peaky blinders", "lupin", "narcos", "dark", "mr robot"]
MOVIES_Q = ["uncharted", "divergent", "the impossible", "godzilla", "titanic",
            "venom", "joker", "interstellar", "gladiator", "the batman",
            "top gun maverick", "black panther", "fast furious", "morbius", "tenet"]
JS = ("const fs=require('fs');"
      "const enc=Buffer.from(process.argv[1],'base64');"
      "const wasm=fs.readFileSync(process.argv[2]);"
      "WebAssembly.instantiate(wasm,{}).then(i=>{"
      "const {alloc,decrypt,memory}=i.instance.exports;"
      "const p=alloc(enc.length);"
      "new Uint8Array(memory.buffer,p,enc.length).set(enc);"
      "const l=decrypt(p,enc.length);"
      "process.stdout.write(new TextDecoder().decode(new Uint8Array(memory.buffer,p+12,l)));"
      "}).catch(()=>{process.exit(1);});")


def get(s, url, referer=None, cap=300 * 1024):
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    try:
        r = s.get(url, timeout=20, headers=h, stream=True)
        d = b""
        for ch in r.iter_content(16384):
            d += ch
            if len(d) >= cap:
                break
        r.close()
        return r.status_code, d
    except Exception as e:
        return None, str(e).encode()


def head100(s, url, referer=None):
    h = {"User-Agent": UA, "Range": "bytes=0-102399"}
    if referer:
        h["Referer"] = referer
    try:
        r = s.get(url, timeout=25, headers=h, stream=True)
        d = b""
        for ch in r.iter_content(16384):
            d += ch
            if len(d) >= 100 * 1024:
                break
        r.close()
        d = d[:100 * 1024]
        ct = (r.headers.get("Content-Type") or "").lower()
        if not d:
            return {"ok": False, "kind": "EMPTY", "status": r.status_code}
        low = d[:1200].lower().lstrip()
        if low.startswith((b"<!doctype", b"<html")) or b"<html" in low:
            return {"ok": False, "kind": "HTML-NOT-MEDIA", "status": r.status_code}
        if len(d) >= 12 and d[4:8] == b"ftyp":
            return {"ok": True, "kind": "MP4", "status": r.status_code}
        if d.startswith(b"\x1aE\xdf\xa3"):
            return {"ok": True, "kind": "MKV/EBML", "status": r.status_code}
        if d[0:1] == b"\x47" and len(d) > 188 and d[188:189] == b"\x47":
            ct_mis = " ct-mismatch(text/html)" if ct.startswith("text/") else ""
            return {"ok": True, "kind": "MPEG-TS" + ct_mis, "status": r.status_code}
        if d[:4] in (b"styp", b"sidx", b"moov"):
            return {"ok": True, "kind": "fMP4", "status": r.status_code}
        return {"ok": False, "kind": f"UNKNOWN ct={ct[:40]} head={d[:16]!r}", "status": r.status_code}
    except Exception as e:
        return {"ok": False, "kind": f"FETCH-ERR {e}"}


def decrypt_node(enc_b64, wasm_bytes):
    """VidsrcResolver's node fast-path with corrected argv indices."""
    with tempfile.NamedTemporaryFile(suffix='.wasm', delete=False) as f:
        f.write(wasm_bytes)
        wp = f.name
    try:
        res = subprocess.run(['node', '-e', JS, enc_b64, wp], capture_output=True, text=True, timeout=15)
        if res.returncode == 0 and res.stdout.strip():
            urls = [u.strip() for u in res.stdout.split('\n') if u.strip().startswith('http')]
            return urls[0] if urls else None
        return None
    finally:
        os.unlink(wp)


def hls_probe_tok(s, master_url, token):
    q = ("&" if "?" in master_url else "?") + "token=" + token
    st, d = get(s, master_url + q, referer="https://cloudorchestranova.com/", cap=200 * 1024)
    if st != 200:
        return {"ok": False, "kind": f"HLS-MASTER-{st}"}
    lines = [l.strip() for l in d.decode("utf-8", "ignore").splitlines() if l.strip() and not l.startswith("#")]
    if not lines:
        return {"ok": False, "kind": "HLS-EMPTY-MASTER"}
    variant = lines[0]
    if "__TOKEN__" in variant:
        variant = variant.replace("__TOKEN__", token)
    if not variant.startswith("http"):
        variant = master_url.rsplit("/", 1)[0] + "/" + variant
    if ".m3u8" in variant:
        vq = variant if "token=" in variant else variant + q
        st2, d2 = get(s, vq, referer=master_url, cap=200 * 1024)
        if st2 != 200:
            return {"ok": False, "kind": f"HLS-VARIANT-{st2}"}
        segs = [l.strip() for l in d2.decode("utf-8", "ignore").splitlines() if l.strip() and not l.startswith("#")]
        if not segs:
            return {"ok": False, "kind": "HLS-EMPTY-VARIANT"}
        seg = segs[0]
        if not seg.startswith("http"):
            seg = vq.rsplit("/", 1)[0] + "/" + seg.split("?")[0]
    else:
        seg = variant
    if "__TOKEN__" in seg:
        seg = seg.replace("__TOKEN__", token)
    elif "token=" not in seg:
        seg += ("&" if "?" in seg else "?") + "token=" + token
    v = head100(s, seg, referer=master_url)
    v["kind"] = "HLS-" + v["kind"]
    return v


def audit_nepu(want_type, queries, out_path):
    s = make_session()
    ok = fail = nulls = 0
    out = open(out_path, "a", encoding="utf-8")
    tested = 0
    for q in queries:
        if tested >= 15:
            break
        want_mt = "tv" if want_type == "series" else "movie"
        st, d = get(s, f"https://nepu.gd/api/search?q={q.replace(' ', '+')}")
        try:
            recs = json.loads(d).get("results", [])
        except Exception:
            recs = []
        item = next((r for r in recs if r.get("media_type") == want_mt), None)
        if not item:
            print(f"  [{q}] no {want_mt} result", flush=True)
            continue
        tid = item["id"]
        watch = f"https://nepu.gd/watch/tv/{tid}/1/1" if want_type == "series" else f"https://nepu.gd/watch/movie/{tid}"
        rec = {"site": "nepu", "type": want_type, "query": q, "show": watch,
               "title": item.get("title"), "note": "manual-drive; harness search broke on API shape change"}
        # hop 1: watch page iframe
        stw, dw = get(s, watch, referer="https://nepu.gd/")
        iframe = None
        if stw == 200:
            m = re.search(r'<iframe[^>]+id="playerFrame"[^>]+src="([^"]+)"', dw.decode("utf-8", "ignore"))
            iframe = m.group(1) if m else None
        rec["iframe"] = bool(iframe)
        # hop 2: api.php exactly as reference calls it
        if want_type == "series":
            api = f"https://data.vidsrcme.ru/api.php?type=tv&tmdb={tid}&season=1&episode=1&stream_urls"
        else:
            api = f"https://data.vidsrcme.ru/api.php?type=movie&tmdb={tid}&stream_urls"
        sta, da = get(s, api, referer="https://cloudorchestranova.com/", cap=64 * 1024)
        rec["api_status"] = sta
        master = None
        try:
            data = json.loads(da)
            sd = data.get("data", {}).get("stream_urls")
            if isinstance(sd, list) and sd:
                master = sd[0]
            elif isinstance(sd, str):
                wurl = data.get("vs", {}).get("wasm_url")
                wb = get(s, wurl, referer="https://cloudorchestranova.com/", cap=1024 * 1024)[1]
                master = decrypt_node(sd, wb)
        except Exception as e:
            rec["dec_err"] = str(e)[:80]
        rec["resolved"] = (master or "")[:160] or None
        if not master:
            rec["result"] = "RESOLVE-FAILED"
            rec["fail_hop"] = "api/decrypt"
            nulls += 1
            fail += 1
        else:
            str_, dt_ = get(s, master, referer="https://cloudorchestranova.com/", cap=2048)
            rec["reference_tokenless_status"] = str_
            origin = master.rsplit("/pl/", 1)[0]
            stg, dg = get(s, origin + "/generate.php", referer="https://cloudorchestranova.com/", cap=4096)
            tok = dg.decode("utf-8", "ignore").strip() if stg == 200 else ""
            rec["generate_status"] = stg
            if not tok:
                rec["result"] = "FAIL"
                rec["verify"] = {"ok": False, "kind": f"NO-TOKEN gen={stg}"}
                fail += 1
            else:
                v = hls_probe_tok(s, master, tok)
                rec["verify"] = v
                if v["ok"]:
                    rec["result"] = "OK"
                    ok += 1
                else:
                    rec["result"] = "FAIL"
                    fail += 1
        tested += 1
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()
        print(f"  [{tested:2d}] {rec['result']:14s} {(rec.get('title') or '')[:38]:38s} "
              f"api={sta} ref_st={rec.get('reference_tokenless_status')} "
              f"{rec.get('verify', {}).get('kind', '')}", flush=True)
        time.sleep(0.6)
    out.close()
    print(f"[nepu/{want_type}] DONE ok={ok} fail={fail} (resolve-null={nulls})", flush=True)


if __name__ == "__main__":
    print("=== NEPU SERIES ===", flush=True)
    audit_nepu("series", SERIES_Q, r"C:\Users\Anon\download-toolkit\probe\results-nepu-series.jsonl")
    print("=== NEPU MOVIES ===", flush=True)
    audit_nepu("movie", MOVIES_Q, r"C:\Users\Anon\download-toolkit\probe\results-nepu-movie.jsonl")
