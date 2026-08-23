/**
 * vsdec.js — transparent decryption of the API's encrypted `stream_urls`.
 *
 * The stream-data API returns plain JSON, EXCEPT `data.stream_urls` which — when
 * protection is enabled — is a single encrypted string (base64 ChaCha20
 * nonce||ciphertext) instead of an array, plus a top-level:
 *     vs: { w: <window>, wasm_url: "https://.../<w>.wasm?_=<ts>" }   (preferred)
 *  or vs: { w: <window>, wasm: "<base64 wasm>" }                     (inline fallback)
 * carrying the per-5-minute-window ChaCha20 decryptor. This decrypts the string
 * in the browser via WebAssembly back into the URL array. Plain responses (where
 * stream_urls is already an array) pass through unchanged — backward safe.
 *
 * Exposes: window.vsFetchJSON(url, opts) -> Promise<object>
 */
(function () {
    'use strict';

    var moduleCache = {};   // cache key -> Promise<WebAssembly.Module>

    function b64(s) {
        var bin = atob(s), u = new Uint8Array(bin.length);
        for (var i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
        return u;
    }

    // Compile once per window (instantiate fresh per call → fresh linear memory,
    // so the bump allocator never accumulates across calls).
    function moduleFromBytes(w, bytes) {
        var k = 'b:' + (w == null ? Math.random() : w);
        if (moduleCache[k]) return moduleCache[k];
        moduleCache[k] = WebAssembly.compile(bytes);
        return moduleCache[k];
    }
    function moduleFromUrl(w, url) {
        // Keyed by window (not the ?_=<ts> URL) so it's fetched once per window;
        // the ?_= param busts the browser HTTP cache on the first fetch.
        var k = 'u:' + (w == null ? url : w);
        if (moduleCache[k]) return moduleCache[k];
        var opts = { credentials: 'omit' };
        var p = WebAssembly.compileStreaming
            ? WebAssembly.compileStreaming(fetch(url, opts)).catch(function () {
                return fetch(url, opts).then(function (r) { return r.arrayBuffer(); }).then(function (b) { return WebAssembly.compile(b); });
              })
            : fetch(url, opts).then(function (r) { return r.arrayBuffer(); }).then(function (b) { return WebAssembly.compile(b); });
        moduleCache[k] = p;
        return p;
    }

    function decryptWith(modPromise, encB64) {
        return modPromise
            .then(function (mod) { return WebAssembly.instantiate(mod, {}); })
            .then(function (inst) {
                var ex = inst.exports, enc = b64(encB64);
                var ptr = ex.alloc(enc.length);
                new Uint8Array(ex.memory.buffer, ptr, enc.length).set(enc);
                var outLen = ex.decrypt(ptr, enc.length);
                return new TextDecoder().decode(new Uint8Array(ex.memory.buffer, ptr + 12, outLen));
            });
    }

    function maybeDecrypt(j) {
        if (!(j && j.vs && j.data && typeof j.data.stream_urls === 'string')) return j;
        var vs = j.vs;
        var modP = vs.wasm_url ? moduleFromUrl(vs.w, vs.wasm_url)
                 : vs.wasm ? moduleFromBytes(vs.w, b64(vs.wasm))
                 : null;
        if (!modP) return j;
        return decryptWith(modP, j.data.stream_urls).then(function (txt) {
            j.data.stream_urls = txt.split('\n').filter(function (s) { return s; });
            return j;
        });
    }

    window.vsFetchJSON = function (url, opts) {
        return fetch(url, opts || { credentials: 'omit', headers: { accept: 'application/json' } })
            .then(function (r) { return r.json(); })
            .then(maybeDecrypt);
    };
})();
