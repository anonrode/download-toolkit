"""Live nepu chain tracer (fix-agent). Records status/response at EVERY stage.
Plain requests (no curl_cffi) with retries. Deleted after use."""
import re, time, json, base64, subprocess, tempfile, os, urllib.parse
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_last = {}


def paced(url):
    host = urllib.parse.urlparse(url).netloc
    t0 = _last.get(host, 0.0)
    gap = time.time() - t0
    if gap < 0.15:
        time.sleep(0.15 - gap)
    _last[host] = time.time()


def get(url, referer=None, cap=300 * 1024, tries=2):
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    for i in range(tries):
        try:
            paced(url)
            r = requests.get(url, headers=h, timeout=20, stream=True)
            d = b""
            for c in r.iter_content(16384):
                d += c
                if len(d) >= cap:
                    break
            code, hdrs, fin = r.status_code, dict(r.headers), r.url
            r.close()
            return code, hdrs, d[:cap], fin
        except Exception as e:
            if i == tries - 1:
                return None, {}, str(e).encode(), url
            time.sleep(0.6)
    return None, {}, b"", url


def log(tag, msg):
    print(f"{tag}: {msg}", flush=True)


def node_decrypt(enc_b64, wasm_bytes):
    """Monolith layout: argv[2]=enc argv[3]=wasm via ['node','-e',JS,enc,wasm]."""
    with tempfile.NamedTemporaryFile(suffix='.wasm', delete=False) as f:
        f.write(wasm_bytes)
        wp = f.name
    try:
        js = ("const fs=require('fs');"
              "const enc=Buffer.from(process.argv[1],'base64');"
              "const wasm=fs.readFileSync(process.argv[2]);"
              "WebAssembly.instantiate(wasm,{}).then(i=>{"
              "const {alloc,decrypt,memory}=i.instance.exports;"
              "const p=alloc(enc.length);"
              "new Uint8Array(memory.buffer,p,enc.length).set(enc);"
              "const l=decrypt(p,enc.length);"
              "process.stdout.write(new TextDecoder().decode(new Uint8Array(memory.buffer,p+12,l)));"
              "}).catch(()=>{process.exit(1);});")
        res = subprocess.run(['node', '-e', js, enc_b64, wp],
                             capture_output=True, text=True, timeout=15)
        return res
    finally:
        os.unlink(wp)


def main():
    show = "https://nepu.gd/watch/tv/77169"
    # --- hop 0: show page ---
    st, hd, d, fin = get(show, referer="https://nepu.gd/")
    log("show", f"status={st} bytes={len(d)}")
    txt = d.decode("utf-8", "ignore")
    m = re.search(r'<iframe[^>]+id="playerFrame"[^>]+src="([^"]+)"', txt)
    iframe = m.group(1) if m else None
    log("show-iframe-playerFrame", iframe)
    if not iframe:
        for mm in re.finditer(r'<iframe[^>]+src="([^"]+)"', txt):
            log("show-iframe-all", mm.group(1)[:160])

    # --- hop 0b: episode page (what verify_batch S2/S3 actually uses) ---
    ep = "https://nepu.gd/watch/tv/77169/1/1"
    st, hd, d, fin = get(ep, referer=show)
    log("ep-page", f"status={st} bytes={len(d)}")
    txt = d.decode("utf-8", "ignore")
    m = re.search(r'<iframe[^>]+id="playerFrame"[^>]+src="([^"]+)"', txt)
    ep_iframe = m.group(1) if m else None
    log("ep-iframe-playerFrame", ep_iframe)
    if not ep_iframe:
        for mm in re.finditer(r'<iframe[^>]+src="([^"]+)"', txt):
            log("ep-iframe-all", mm.group(1)[:160])
    # episode anchor links on show page (S2 selector)
    st, hd, d2, fin = get(show, referer="https://nepu.gd/")
    txt2 = d2.decode("utf-8", "ignore")
    ep_links = re.findall(r'href="(/watch/tv/[^"]+)"', txt2)
    log("show-ep-links", f"count={len(ep_links)} first={ep_links[0] if ep_links else None}")

    embed = ep_iframe or iframe
    if not embed:
        log("FATAL", "no iframe found")
        return
    # normalize
    if embed.startswith("//"):
        embed = "https:" + embed
    elif not embed.startswith("http"):
        embed = urllib.parse.urljoin(ep, embed)

    # --- hop 1: embed iframe page (vidsrc.mov/embed/...) ---
    st, hd, d, fin = get(embed, referer=ep)
    log("embed1", f"status={st} bytes={len(d)} final={fin}")
    txt = d.decode("utf-8", "ignore")
    m = re.search(r'<iframe[^>]+src="([^"]+)"', txt)
    embed2 = m.group(1) if m else None
    log("embed1-iframe", embed2)
    if not embed2:
        # maybe it IS the player (no nested iframe) -- look for api refs
        for pat, tag in [(r'api\.php[^"\'\s]*', "api-ref"),
                         (r'data\.vidsrcme\.ru[^"\'\s]*', "vidsrcme-ref"),
                         (r'https?://[^"\'\s]*\.m3u8[^"\'\s]*', "m3u8-ref")]:
            for mm in re.finditer(pat, txt):
                log(f"embed1-{tag}", mm.group(0)[:160])
        # fall through: api directly from embed host
        embed2 = embed

    # --- hop 2: player page (vsembed.ru or similar) ---
    if embed2.startswith("//"):
        embed2 = "https:" + embed2
    elif not embed2.startswith("http"):
        embed2 = urllib.parse.urljoin(embed, embed2)
    st, hd, d, fin = get(embed2, referer=embed)
    log("embed2", f"status={st} bytes={len(d)} final={fin} err={d.decode('utf-8','ignore')[:120] if st is None else ''}")
    txt = d.decode("utf-8", "ignore")
    m = re.search(r'<iframe[^>]+src="([^"]+)"', txt)
    embed3 = m.group(1) if m else None
    log("embed2-iframe", embed3)
    for pat, tag in [(r'api\.php[^"\'\s]*', "api-ref"),
                     (r'https?://[^"\'\s]*wasm[^"\'\s]*', "wasm-ref"),
                     (r'data\.vidsrcme\.ru[^"\'\s]*', "vidsrcme-ref"),
                     (r'cloudorchestranova[^"\'\s]*', "corch-ref")]:
        for mm in re.finditer(pat, txt):
            log(f"embed2-{tag}", mm.group(0)[:160])

    # TMDB id from whichever URL carries it
    m = re.search(r'/(?:movie|tv)/(\d+)(?:/(\d+)/(\d+))?', embed2 or embed)
    if not m:
        m = re.search(r'/(?:movie|tv)/(\d+)(?:/(\d+)/(\d+))?', embed)
    tmdb = m.group(1) if m else None
    log("tmdb", f"id={tmdb} season={m.group(2) if m else None} ep={m.group(3) if m else None}")

    # --- hop 3: api.php ---
    api = (f"https://data.vidsrcme.ru/api.php?type=tv&tmdb={tmdb}&season=1&episode=1&stream_urls")
    st, hd, d, fin = get(api, referer="https://cloudorchestranova.com/", cap=64 * 1024)
    log("api", f"status={st} bytes={len(d)} final={fin} ct={(hd.get('Content-Type') or '')}")
    try:
        data = json.loads(d)
        log("api-json", f"keys={list(data.keys())}")
    except Exception as e:
        log("api-json", f"ERR {e} head={d[:150]!r}")
        return
    sd = data.get("data", {}).get("stream_urls")
    log("api-stream_urls", f"type={type(sd).__name__} len={len(sd) if isinstance(sd, (str, list)) else '?'}")
    if isinstance(sd, list):
        master = sd[0] if sd else None
    elif isinstance(sd, str):
        vs = data.get("vs", {})
        wurl = vs.get("wasm_url")
        wb64 = vs.get("wasm")
        log("api-vs", f"w={vs.get('w')} wasm_url={str(wurl)[:120]} inline={'yes' if wb64 else 'no'}")
        if wurl:
            st, hd, wb, fin = get(wurl, referer="https://cloudorchestranova.com/", cap=1024 * 1024)
            log("wasm-fetch", f"status={st} bytes={len(wb)} final={fin}")
            if st != 200:
                return
        elif wb64:
            wb = base64.b64decode(wb64)
            log("wasm-inline", f"bytes={len(wb)}")
        else:
            log("wasm", "NO WASM SOURCE")
            return
        r = node_decrypt(sd, wb)
        log("decrypt", f"rc={r.returncode} out={r.stdout[:200]!r} err={r.stderr[:150]!r}")
        if r.returncode == 0 and r.stdout.strip():
            urls = [u.strip() for u in r.stdout.split("\n") if u.strip().startswith("http")]
            master = urls[0] if urls else None
            log("decrypt-urls", f"count={len(urls)}")
            for u in urls:
                log("decrypt-url", u)
        else:
            master = None
    else:
        master = None
    if not master:
        log("FATAL", "no master URL")
        return

    # --- hop 4: master + generate.php token ---
    st, hd, d, fin = get(master, referer="https://cloudorchestranova.com/", cap=64 * 1024)
    log("master-tokenless", f"status={st} bytes={len(d)}")

    origin = re.match(r'(https?://[^/]+)', master).group(1)
    tok = ""
    for ref in (None, "https://cloudorchestranova.com/"):
        st, hd, d, fin = get(origin + "/generate.php", referer=ref, cap=4096)
        t = d.decode("utf-8", "ignore").strip() if st == 200 and d else ""
        log(f"generate.php ref={ref}", f"status={st} token_len={len(t)} head={d[:60]!r}")
        if t and not tok:
            tok = t

    # re-request generate once more (it may be IP/rate bound)
    time.sleep(2.0)
    st, hd, d, fin = get(origin + "/generate.php", cap=4096)
    t2 = d.decode("utf-8", "ignore").strip() if st == 200 and d else ""
    log("generate.php retry", f"status={st} token_len={len(t2)}")
    if not tok and t2:
        tok = t2

    if not tok:
        log("FATAL", "no token")
        return

    sep = "&" if "?" in master else "?"
    m2 = master + sep + "token=" + tok
    st, hd, d, fin = get(m2, referer="https://cloudorchestranova.com/", cap=64 * 1024)
    log("master-tokened", f"status={st} bytes={len(d)} final={fin}")
    txtm = d.decode("utf-8", "ignore")
    lines = [l.strip() for l in txtm.splitlines() if l.strip() and not l.startswith("#")]
    log("master-lines", f"count={len(lines)}")
    for l_ in lines[:4]:
        log("master-line", l_[:400])
        log("master-line-has-token", f"{'token=' in l_}")
    variant = lines[0] if lines else None
    if not variant:
        log("FATAL", "no variant line")
        return
    if not variant.startswith("http"):
        variant = urllib.parse.urljoin(m2, variant)
    log("variant-url", variant[:240])

    st, hd, d2, fin = get(variant, referer=m2, cap=64 * 1024)
    log("variant", f"status={st} bytes={len(d2)} final={fin}")
    txtv = d2.decode("utf-8", "ignore")
    segs = [l.strip() for l in txtv.splitlines() if l.strip() and not l.startswith("#")]
    log("variant-segs", f"count={len(segs)}")
    seg = segs[0] if segs else None
    if not seg:
        log("FATAL", "no segment line")
        return
    if not seg.startswith("http"):
        seg = urllib.parse.urljoin(variant, seg)
    log("segment-url", seg[:240])

    # --- hop 5: segment probes (1KB cap) ---
    for ref in (None, "https://cloudorchestranova.com/"):
        st, hd, d3, fin = get(seg, referer=ref, cap=1024)
        ct = (hd.get("Content-Type") or "")
        head = d3[:12]
        ok = st in (200, 206) and len(d3) > 0
        sync = (d3[188] == 0x47) if len(d3) > 188 else None
        log(f"segment ref={ref}", f"status={st} bytes={len(d3)} ct={ct} head={head!r} b188=0x47:{sync}")


if __name__ == "__main__":
    main()
