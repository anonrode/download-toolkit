"""One-off nepu chain tracer. Records status per stage. Deleted after use."""
import sys, re, time, json, base64, subprocess, tempfile, os
sys.path.insert(0, r'C:\Users\Anon\download-toolkit')
from src.downloader import make_session

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
LOG = []

def log(tag, msg):
    line = f"{tag}: {msg}"
    LOG.append(line)
    print(line, flush=True)

def get(s, url, referer=None, cap=300*1024, tries=2):
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    for i in range(tries):
        try:
            r = s.get(url, headers=h, timeout=20, stream=True)
            d = b""
            for c in r.iter_content(16384):
                d += c
                if len(d) >= cap:
                    break
            code = r.status_code
            final = r.url
            r.close()
            return code, dict(r.headers), d[:cap], final
        except Exception as e:
            if i == tries - 1:
                return None, {}, str(e).encode(), url
            time.sleep(0.6)
    return None, {}, b"", url

def node_decrypt(enc_b64, wasm_bytes, argv_enc, argv_wasm):
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
        args = ['node', '-e', js]
        if argv_enc: args.append(enc_b64)
        if argv_wasm: args.append(wp)
        res = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return res
    finally:
        os.unlink(wp)

def main():
    s = make_session()
    show = "https://nepu.gd/watch/tv/77169"
    # hop 1: watch page
    st, hd, d, fin = get(s, show, referer="https://nepu.gd/")
    log("watch", f"status={st} bytes={len(d)} final={fin}")
    txt = d.decode("utf-8", "ignore")
    m = re.search(r'<iframe[^>]+id="playerFrame"[^>]+src="([^"]+)"', txt)
    embed = m.group(1) if m else None
    log("watch-iframe", embed)
    if not embed:
        return

    # hop 2: vidsrc.mov embed
    st, hd, d, fin = get(s, embed, referer=show)
    log("embed1", f"status={st} bytes={len(d)} final={fin}")
    txt = d.decode("utf-8", "ignore")
    m = re.search(r'<iframe[^>]+src="([^"]+)"', txt)
    embed2 = m.group(1) if m else None
    log("embed1-iframe", embed2)
    if not embed2:
        return

    # hop 3: vsembed.ru
    st, hd, d, fin = get(s, embed2, referer=embed)
    log("embed2", f"status={st} bytes={len(d)} final={fin}")
    txt = d.decode("utf-8", "ignore")
    for pat, tag in [(r'api\.php[^"\'\s]*', "api-ref"),
                     (r'https?://[^"\'\s]*wasm[^"\'\s]*', "wasm-ref"),
                     (r'data\.vidsrcme\.ru[^"\'\s]*', "vidsrcme-ref"),
                     (r'cloudorchestranova[^"\'\s]*', "corch-ref"),
                     (r'https?://[^"\'\s]*\.m3u8[^"\'\s]*', "m3u8-ref")]:
        for mm in re.finditer(pat, txt):
            log(f"embed2-{tag}", mm.group(0)[:180])
    # any fetch of the iframe inside vsembed?
    m = re.search(r'<iframe[^>]+src="([^"]+)"', txt)
    if m:
        log("embed2-iframe", m.group(1)[:180])

    # hop 4: api.php directly (as the app does)
    api = ("https://data.vidsrcme.ru/api.php?type=tv&tmdb=77169&season=1&episode=1&stream_urls")
    st, hd, d, fin = get(s, api, referer="https://cloudorchestranova.com/", cap=64*1024)
    log("api", f"status={st} bytes={len(d)} final={fin}")
    try:
        data = json.loads(d)
    except Exception as e:
        log("api-json", f"ERR {e} head={d[:120]!r}")
        return
    sd = data.get("data", {}).get("stream_urls")
    log("api-data", f"stream_urls type={type(sd).__name__}")
    if isinstance(sd, list):
        master = sd[0] if sd else None
        log("api-master", master)
    elif isinstance(sd, str):
        vs = data.get("vs", {})
        wurl = vs.get("wasm_url")
        wb64 = vs.get("wasm")
        log("api-vs", f"wasm_url={str(wurl)[:120]} inline_wasm={'yes' if wb64 else 'no'}")
        if wurl:
            st, hd, wb, fin = get(s, wurl, referer="https://cloudorchestranova.com/", cap=1024*1024)
            log("wasm-fetch", f"status={st} bytes={len(wb)} final={fin}")
        elif wb64:
            wb = base64.b64decode(wb64)
            log("wasm-inline", f"bytes={len(wb)}")
        else:
            log("wasm", "NO WASM SOURCE")
            return
        # try both argv layouts
        r1 = node_decrypt(sd, wb, True, True)
        log("decrypt-argv1-2", f"rc={r1.returncode} out={r1.stdout[:160]!r} err={r1.stderr[:120]!r}")
        r2 = node_decrypt(sd, wb, False, False)  # same script needs argv[1]/argv[2]
        if r1.returncode == 0 and r1.stdout.strip():
            urls = [u.strip() for u in r1.stdout.split("\n") if u.strip().startswith("http")]
            master = urls[0] if urls else None
            log("decrypt-ok", master)
        else:
            # emulate monolith bug: argv[2]/argv[3]
            r3 = node_decrypt(sd, wb, None, None)
            # monolith passes [enc, wasm] but reads argv[2], argv[3]
            with tempfile.NamedTemporaryFile(suffix='.wasm', delete=False) as f:
                f.write(wb)
                wp = f.name
            js_bug = ("const fs=require('fs');"
                      "const enc=Buffer.from(process.argv[2],'base64');"
                      "const wasm=fs.readFileSync(process.argv[3]);"
                      "WebAssembly.instantiate(wasm,{}).then(i=>{"
                      "const {alloc,decrypt,memory}=i.instance.exports;"
                      "const p=alloc(enc.length);"
                      "new Uint8Array(memory.buffer,p,enc.length).set(enc);"
                      "const l=decrypt(p,enc.length);"
                      "process.stdout.write(new TextDecoder().decode(new Uint8Array(memory.buffer,p+12,l)));"
                      "}).catch(()=>{process.exit(1);});")
            r4 = subprocess.run(['node', '-e', js_bug, sd, wp], capture_output=True, text=True, timeout=15)
            os.unlink(wp)
            log("decrypt-monolith-argv", f"rc={r4.returncode} out={r4.stdout[:80]!r} err={r4.stderr[:120]!r}")
            master = None
    else:
        log("api-data", f"unexpected stream_urls repr: {sd!r}"[:160])
        master = None

    if not master:
        log("result", "RESOLVE-FAILED at decrypt")
        return

    # master fetch without token
    st, hd, d, fin = get(s, master, referer="https://cloudorchestranova.com/", cap=64*1024)
    log("master-tokenless", f"status={st} bytes={len(d)}")

    # generate.php token
    origin = re.match(r'(https?://[^/]+)', master).group(1)
    st, hd, d, fin = get(s, origin + "/generate.php", cap=4096)
    tok = d.decode("utf-8", "ignore").strip() if st == 200 else ""
    log("generate.php", f"status={st} token_len={len(tok)}")

    # master with token
    sep = "&" if "?" in master else "?"
    m2 = master + sep + "token=" + tok
    st, hd, d, fin = get(s, m2, referer="https://cloudorchestranova.com/", cap=64*1024)
    log("master-tokened", f"status={st} bytes={len(d)}")
    lines = [l.strip() for l in d.decode("utf-8","ignore").splitlines() if l.strip() and not l.startswith("#")]
    variant = lines[0] if lines else None
    log("variant-line", variant)

    if variant and ".m3u8" in (variant or ""):
        v = variant
        if not v.startswith("http"):
            v = re.match(r'(https?://[^/]+)', m2).group(1) + v
        st, hd, d2, fin = get(s, v, referer=m2, cap=64*1024)
        log("variant", f"status={st} bytes={len(d2)} final={fin}")
        segs = [l.strip() for l in d2.decode("utf-8","ignore").splitlines() if l.strip() and not l.startswith("#")]
        seg = segs[0] if segs else None
        log("segment-line", seg)
        if seg:
            if not seg.startswith("http"):
                seg = re.match(r'(https?://[^/]+)', v).group(1) + seg
            for ref in (None, embed2, show, "https://cloudorchestranova.com/"):
                st, hd, d3, fin = get(s, seg, referer=ref, cap=1024)
                ct = (hd.get("Content-Type") or "")
                log(f"segment-probe ref={ref}", f"status={st} bytes={len(d3)} ct={ct} head={d3[:12]!r}")

    # segments from tokenless master? (maybe master without token embeds no token)
    st, hd, d, fin = get(s, master, referer=None, cap=64*1024)
    log("master-tokenless-noref", f"status={st} bytes={len(d)}")

    print("\n===== SUMMARY =====")
    for l in LOG:
        print(l)

if __name__ == "__main__":
    main()
