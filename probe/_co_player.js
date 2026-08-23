/**
 * JW Player-style custom player (hls.js based).
 *
 * Flow:
 *   1. Fetch stream data from CONFIG.api (data.vidsrcme.ru).
 *   2. If gen_token_url is present, generate a token in the browser (IP-bound)
 *      and apply it to every stream URL (replace __TOKEN__ or append ?token=).
 *   3. Play via hls.js (or native HLS), with multi-server fallback.
 *   4. Full subtitle system: OpenSubtitles search + file_name auto-match,
 *      wyzie (Latin) / cache.php (foreign) routing, overlay rendering, persistence.
 */
(function () {
    'use strict';

    var CONFIG = window.CONFIG || {};

    /* ---------- element refs ---------- */
    var $ = function (id) { return document.getElementById(id); };
    var wrap = $('player'), video = $('video');
    var poster = $('poster'), spinner = $('spinner'), bigPlay = $('bigPlay');
    var errorBox = $('error'), errorText = $('errorText'), titleEl = $('title');
    var controls = $('controls');
    var seek = $('seek'), rail = seek.querySelector('.jw-rail');
    var buffered = $('buffered'), played = $('played'), knob = $('knob');
    var tip = $('tip'), thumb = $('thumb'), thumbImg = $('thumbImg'), thumbTime = $('thumbTime');
    var thumbBox = thumb.querySelector('.jw-thumb-box');
    var playBtn = $('play'), iPlay = $('iPlay'), iPause = $('iPause');
    var iBigPlay = $('iBigPlay'), iBigPause = $('iBigPause');
    var rew = $('rew'), fwd = $('fwd');
    var muteBtn = $('mute'), iVol = $('iVol'), iMute = $('iMute'), volume = $('volume');
    var curEl = $('cur'), durEl = $('dur');
    var ccBtn = $('ccBtn'), setBtn = $('setBtn'), fsBtn = $('fsBtn'), iFs = $('iFs'), iFsExit = $('iFsExit');
    var subText = $('subText');

    // iOS detection — used by both the player and subtitles.js (exposed on window.__JW)
    var IS_IOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
                 (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

    var state = {
        allStreams: [], streamIdx: 0, hls: null, token: '',
        qualities: [], cues: [], thumbs: [], thumbBase: '',
        subtitles: [], activeSub: -1, currentCues: null, subOffset: 0,
        booted: false,
        season: (CONFIG.season != null ? +CONFIG.season : null),
        episode: (CONFIG.episode != null ? +CONFIG.episode : null),
        eps: null, resumeAt: parseFloat(CONFIG.startAt) > 0 ? parseFloat(CONFIG.startAt) : 0,
        uiVisible: false, castUrl: null,
        hlsUrl: null  // raw HLS URL (needed by iOS subtitle proxy)
    };

    // On iOS, native HLS handles ABR automatically — quality level switching via
    // API is not possible. Hide the gear button immediately so it never appears.
    if (IS_IOS && setBtn) setBtn.style.display = 'none';

    var IS_TV = CONFIG.mediaType === 'tv' && !!CONFIG.streamBase;

    // Landing mode: show a poster + play button and wait for a user click
    // before fetching any stream data (matches the vidapi/vaplayer flow).
    var LANDING = !!CONFIG.landing;

    /* ---------- helpers ---------- */
    function fmt(s) {
        s = Math.max(0, Math.floor(s || 0));
        var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
        var mm = (h && m < 10 ? '0' : '') + m, xx = (x < 10 ? '0' : '') + x;
        return (h ? h + ':' : '') + mm + ':' + xx;
    }
    function pad2(n) { n = String(n); return n.length < 2 ? '0' + n : n; }
    function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
    function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
    function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
    // Show/hide the buffering spinner. Kept in the DOM (not removed) so it can be
    // re-shown whenever the video stalls/buffers later in playback.
    function setLoading(on) { wrap.classList.toggle('loading', !!on); if (spinner) spinner.hidden = !on; }
    function hideSpinner() { setLoading(false); }
    // Attach/replace a native <source> so Safari AirPlay can stream video (not just
    // audio) even while HLS.js drives local playback via MSE. Safari only.
    function setAirplaySource(url) {
        if (!video.canPlayType('application/vnd.apple.mpegurl')) return;
        var old = video.querySelector('source[data-airplay]');
        if (old && old.parentNode) old.parentNode.removeChild(old);
        var src = document.createElement('source');
        src.setAttribute('src', url);
        src.setAttribute('type', 'application/vnd.apple.mpegurl');
        src.setAttribute('data-airplay', '1');
        video.appendChild(src);
    }
    function showError(msg) { setLoading(false); signalReady(); if (errorText && msg) errorText.textContent = msg; if (errorBox) errorBox.hidden = false; }
    function hideError() { if (errorBox) errorBox.hidden = true; }
    function post(p) { try { if (window.parent !== window) window.parent.postMessage(p, '*'); } catch (e) {} }
    // Tell the Turnstile gate overlay (if any) it can go away — playback is
    // ready to show, or an error is being displayed.
    function signalReady() { try { window.dispatchEvent(new Event('vs:playerready')); } catch (e) {} }

    if (!CONFIG.api && !CONFIG.streamBase) { showError('This media is unavailable.'); return; }

    /* ============================================================
     * Token + stream sources
     * ============================================================ */
    function parseToken(text) {
        if (text == null) return '';
        var t = String(text).trim();
        if (t && (t.charAt(0) === '{' || t.charAt(0) === '[')) {
            try { var j = JSON.parse(t); if (typeof j === 'string') return j; if (j && typeof j === 'object') return j.token || j.data || j.string || j.result || ''; } catch (e) {}
        }
        return t;
    }
    function fetchToken(url) {
        return fetch(url, { credentials: 'omit' }).then(function (r) { return r.ok ? r.text() : ''; }).then(parseToken).catch(function () { return ''; });
    }
    function applyToken(url, token) {
        if (!token) return url;
        if (url.indexOf('__TOKEN__') > -1) return url.split('__TOKEN__').join(token);
        return url + (url.indexOf('?') > -1 ? '&' : '?') + 'token=' + token;
    }
    // origin (protocol + host) of a stream URL, used to key per-host tokens.
    function originOf(u) {
        try { var x = new URL(u, location.href); return x.protocol + '//' + x.host; } catch (e) { return ''; }
    }
    // Lazily resolve one stream's host token and start playback. Instead of
    // pinging every host's /generate.php up front, we fetch the token for a host
    // only at the moment we're about to request its playlist (initial source or a
    // fallback). Tokens are IP-bound and short-lived, so fetching just-in-time
    // also keeps them fresh. hls.js's variant/segment requests come back already
    // carrying the token (host-rewritten), so stamping the master URL here covers
    // the whole playlist chain for that host.
    function loadStream(idx, isRetry) {
        var raw = state.allStreams[idx];
        if (!raw) { showError('No playable source.'); return; }
        setLoading(true);
        var origin = originOf(raw);
        var tokenReq = origin ? fetchToken(origin + '/generate.php') : Promise.resolve('');
        tokenReq.then(function (tk) {
            state.token = tk || '';
            initHLS(applyToken(raw, tk), isRetry);
        });
    }

    /* ============================================================
     * HLS playback + server fallback
     * ============================================================ */
    function initHLS(url, isRetry) {
        if (!url) { showError('No playable source.'); return; }
        state.castUrl = url;
        state.hlsUrl  = url;  // raw URL stored for iOS subtitle proxy
        setLoading(true);

        // On iOS use native HLS directly — HLS.js via MSE prevents the native
        // fullscreen player from seeing EXT-X-MEDIA subtitle tracks.
        if (IS_IOS && video.canPlayType('application/vnd.apple.mpegurl')) {
            if (state.hls) { try { state.hls.destroy(); } catch (e) {} state.hls = null; }
            video.src = url;
            video.load();
            video.addEventListener('loadedmetadata', function () { onReady(isRetry); }, { once: true });
            video.addEventListener('error', function () { if (!tryNextServer()) allServersFailed('Failed to load video.'); }, { once: true });
            return;
        }

        if (window.Hls && Hls.isSupported()) {
            if (state.hls) { try { state.hls.destroy(); } catch (e) {} }
            // Segment / playlist load resilience: on an error response (5xx, etc.)
            // retry up to 3 times with a growing delay (1.5s → 3s → 4.5s) before
            // hls.js declares it fatal and we fall over to the next server.
            var errRetry = {
                maxNumRetry: 3, retryDelayMs: 1500, maxRetryDelayMs: 8000, backoff: 'linear',
                // Also retry rate-limit (429) and 5xx responses (hls.js otherwise
                // treats 4xx as fatal). Retries are spaced by the linear backoff
                // above (~1.5s / 3s / 4.5s) before failing over to the next server.
                shouldRetry: function (retryConfig, retryCount, isTimeout, loaderResponse, retry) {
                    var code = loaderResponse && loaderResponse.code;
                    if (code === 429) return true;               // rate limited
                    if (code >= 500 && code < 600) return true;  // server error
                    return retry !== undefined ? retry : false;  // else hls.js default
                }
            };
            var toRetry  = { maxNumRetry: 2, retryDelayMs: 0, maxRetryDelayMs: 0, backoff: 'linear' };
            // Prefetch at most MAX_SEG segments ahead, loaded one at a time
            // (hls.js loads fragments serially by default), so we never burst a
            // batch of requests and trip a stream host's rate limit. The forward
            // buffer is in seconds, so the exact 6-segment window is applied on
            // LEVEL_LOADED once the segment (target) duration is known.
            var MAX_SEG = 6;
            state.hls = new Hls({
                maxBufferLength: 30,
                maxMaxBufferLength: 30,
                // never fetch more than one fragment at a time
                maxFragLookUpTolerance: 0.25,
                startFragPrefetch: false,
                // Lean toward the best available quality. capLevelToPlayerSize:false
                // stops hls.js from limiting the level to the (often small) iframe
                // size; the optimistic default bandwidth estimate and higher
                // up-switch factor make ABR pick/keep high levels. ABR is still on,
                // so it will drop a step if the network truly can't sustain it and
                // then climb back up.
                startLevel: -1,
                capLevelToPlayerSize: false,
                abrEwmaDefaultEstimate: 5000000,
                abrBandWidthFactor: 0.95,
                abrBandWidthUpFactor: 0.9,
                // media segments (the quality's .ts/.m4s chunks)
                fragLoadPolicy: {
                    default: { maxTimeToFirstByteMs: 9000, maxLoadTimeMs: 120000, timeoutRetry: toRetry, errorRetry: errRetry }
                },
                // per-quality media playlists (.m3u8)
                playlistLoadPolicy: {
                    default: { maxTimeToFirstByteMs: 9000, maxLoadTimeMs: 60000, timeoutRetry: toRetry, errorRetry: errRetry }
                }
            });
            state.hls.loadSource(url);
            state.hls.attachMedia(video);
            // AirPlay fix (desktop Safari): HLS.js plays via MSE, which AirPlay can
            // only remote as *audio*. Attaching a native <source> with the raw HLS
            // URL lets Safari hand the full video stream to the AirPlay device.
            // The <source> is ignored during normal MSE playback (video.src is set).
            setAirplaySource(url);
            state.hls.on(Hls.Events.MANIFEST_PARSED, function (e, data) {
                state.qualities = data.levels || [];
                if (state.manualHeight != null && state.qualities.length) {
                    // The viewer locked a specific quality — re-apply it to this
                    // (possibly new, post-failover/reload) stream and keep ABR OFF
                    // so it always stays on that quality.
                    var mi = levelIndexForHeight(state.manualHeight);
                    if (mi >= 0) state.hls.currentLevel = mi;
                } else if (state.qualities.length > 1) {
                    // Auto: open at the best available quality; ABR stays enabled
                    // and only steps down if the connection can't sustain it.
                    var topLevel = state.qualities.length - 1;
                    state.hls.startLevel = topLevel;
                    state.hls.nextLevel = topLevel;
                }
                buildSettings();
                onReady(isRetry);
            });
            // Cap the forward buffer to ~MAX_SEG segments once we know the segment
            // duration, so hls.js keeps at most 6 loaded ahead and fetches the
            // next one only as the buffer drains (serially).
            state.hls.on(Hls.Events.LEVEL_LOADED, function (e, data) {
                var td = data && data.details && data.details.targetduration;
                if (td > 0) {
                    var win = Math.round(MAX_SEG * td);
                    state.hls.config.maxBufferLength = win;
                    state.hls.config.maxMaxBufferLength = win;
                }
            });
            state.hls.on(Hls.Events.ERROR, function (e, data) {
                if (!data.fatal) return;
                if (data.type === Hls.ErrorTypes.NETWORK_ERROR) { if (!tryNextServer()) allServersFailed('Network error — all servers failed.'); }
                else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) { try { state.hls.recoverMediaError(); } catch (x) { if (!tryNextServer()) allServersFailed('Playback error.'); } }
                else { if (!tryNextServer()) allServersFailed('Fatal playback error.'); }
            });
        } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
            video.src = url;
            video.addEventListener('loadedmetadata', function () { onReady(isRetry); }, { once: true });
            video.addEventListener('error', function () { if (!tryNextServer()) allServersFailed('Failed to load video.'); }, { once: true });
        } else {
            showError('HLS not supported.');
        }
    }

    function tryNextServer() {
        if (state.streamIdx < state.allStreams.length - 1) {
            state.streamIdx++;
            loadStream(state.streamIdx, true);
            return true;
        }
        return false;
    }

    // Every stream URL errored. Reload the player to pull a fresh set of
    // stream_urls (the backend rotates hosts per request). Retry up to 3× with a
    // 1.5s delay; after that give up and show the error. The attempt counter
    // survives reloads (sessionStorage) and is cleared once playback succeeds.
    var ALLFAIL_KEY = 'vs_allfail_tries';
    function allServersFailed(msg) {
        var n = 0;
        try { n = parseInt(sessionStorage.getItem(ALLFAIL_KEY) || '0', 10) || 0; } catch (e) {}
        if (n < 3) {
            try { sessionStorage.setItem(ALLFAIL_KEY, String(n + 1)); } catch (e) {}
            setLoading(true);
            setTimeout(function () { try { location.reload(); } catch (e) { location.href = location.href; } }, 1500);
        } else {
            try { sessionStorage.removeItem(ALLFAIL_KEY); } catch (e) {}
            showError(msg || 'This media is unavailable.');
        }
    }

    function onReady(isRetry) {
        try { sessionStorage.removeItem(ALLFAIL_KEY); } catch (e) {}  // playback ok -> reset retry counter
        hideError();
        hideSpinner();
        signalReady();
        wrap.classList.remove('loading');
        if (poster) poster.classList.add('hidden');
        // Resume to the saved position (TV) once the duration is known.
        if (!isRetry && state.resumeAt > 2) {
            var target = state.resumeAt; state.resumeAt = 0;
            var doSeek = function () { try { if (video.duration && target < video.duration - 5) video.currentTime = target; } catch (e) {} };
            if (video.duration) doSeek(); else video.addEventListener('loadedmetadata', doSeek, { once: true });
        }
        if (CONFIG.autoplay) tryAutoplay();
        else { updatePlayUI(); showUI(); }   // autoplay=0: stay paused but SHOW the play button + controls (esp. mobile, which has no hover)
        if (!isRetry) setTimeout(loadSubtitlesAuto, 400);
    }

    function tryAutoplay() {
        var p = video.play();
        if (p && p.catch) p.catch(function () {
            // Autoplay with sound blocked — retry muted.
            video.muted = true; updateVol();
            video.play().catch(function () { showBigPlay(true); });
        });
    }

    /* ============================================================
     * Controls
     * ============================================================ */
    function showBigPlay(show) { bigPlay.classList.toggle('hidden', !show); }
    function updatePlayUI() {
        var paused = video.paused;
        iPlay.classList.toggle('hidden', !paused);
        iPause.classList.toggle('hidden', paused);
        if (iBigPlay) iBigPlay.classList.toggle('hidden', !paused);
        if (iBigPause) iBigPause.classList.toggle('hidden', paused);
        wrap.classList.toggle('playing', !paused);
        showBigPlay(paused);
        if (paused) setLoading(false);
    }
    function togglePlay() { if (video.paused) video.play(); else video.pause(); }

    function updateVol() {
        var m = video.muted || video.volume === 0;
        iVol.classList.toggle('hidden', m);
        iMute.classList.toggle('hidden', !m);
        volume.value = m ? 0 : video.volume;
    }

    function updateTime() {
        var d = video.duration || 0, t = video.currentTime || 0;
        if (d) { played.style.width = (t / d * 100) + '%'; knob.style.left = (t / d * 100) + '%'; }
        curEl.textContent = fmt(t);
        durEl.textContent = fmt(d);
        renderSubtitle(t);
    }
    function updateBuffered() {
        try {
            var d = video.duration || 0;
            if (d && video.buffered.length) {
                var end = video.buffered.end(video.buffered.length - 1);
                buffered.style.width = Math.min(100, end / d * 100) + '%';
            }
        } catch (e) {}
    }

    /* seek / scrub */
    var scrubbing = false;
    var touchScrub = window.matchMedia('(pointer: coarse)').matches;

    function seekRatio(clientX) {
        var r = rail.getBoundingClientRect();
        return Math.min(1, Math.max(0, (clientX - r.left) / r.width));
    }
    function updateScrubVisual(ratio) {
        played.style.width = (ratio * 100) + '%';
        knob.style.left   = (ratio * 100) + '%';
    }
    function previewAt(clientX) {
        var d = video.duration || 0; if (!d) return;
        var ratio = seekRatio(clientX), t = ratio * d;
        var r = seek.getBoundingClientRect();
        var x = ratio * r.width;
        if (state.thumbs.length) {
            showThumb(t, x);
        } else {
            tip.hidden = false; tip.textContent = fmt(t); tip.style.left = clampLeft(tip, x) + 'px';
        }
    }
    seek.addEventListener('pointermove', function (e) { if (!scrubbing) previewAt(e.clientX); });
    seek.addEventListener('pointerleave', function () { if (!scrubbing) { tip.hidden = true; thumb.hidden = true; } });
    seek.addEventListener('pointerdown', function (e) {
        e.preventDefault();
        scrubbing = true;
        var d = video.duration || 0;
        var ratio = seekRatio(e.clientX);
        updateScrubVisual(ratio);
        if (d) previewAt(e.clientX);
        if (!touchScrub && d) video.currentTime = ratio * d;

        function onMove(ev) {
            ev.preventDefault();
            var r = seekRatio(ev.clientX);
            updateScrubVisual(r);
            previewAt(ev.clientX);
            if (!touchScrub) { var d2 = video.duration || 0; if (d2) video.currentTime = r * d2; }
        }
        function onUp(ev) {
            var d2 = video.duration || 0;
            if (d2) { var r = seekRatio(ev.clientX); video.currentTime = r * d2; updateScrubVisual(r); }
            tip.hidden = true; thumb.hidden = true;
            scrubbing = false;
            document.removeEventListener('pointermove', onMove);
            document.removeEventListener('pointerup',   onUp);
            document.removeEventListener('pointercancel', onCancel);
        }
        function onCancel() {
            var d2 = video.duration || 0;
            if (d2) updateScrubVisual(video.currentTime / d2);
            tip.hidden = true; thumb.hidden = true;
            scrubbing = false;
            document.removeEventListener('pointermove', onMove);
            document.removeEventListener('pointerup',   onUp);
            document.removeEventListener('pointercancel', onCancel);
        }
        document.addEventListener('pointermove',   onMove,    { passive: false });
        document.addEventListener('pointerup',     onUp);
        document.addEventListener('pointercancel', onCancel);
    }, { passive: false });

    /* button wiring */
    // Before boot in landing mode, the big play / video surface kicks off the
    // stream-data fetch instead of toggling an (empty) video element.
    function primaryAction() {
        if (LANDING && !state.booted) { boot(); return; }
        togglePlay();
    }
    playBtn.addEventListener('click', togglePlay);
    bigPlay.addEventListener('click', primaryAction);
    // On touch devices the centre bigPlay button handles play/pause;
    // tapping the video surface only toggles on pointer-fine (desktop) devices.
    if (!window.matchMedia('(pointer: coarse)').matches) {
        video.addEventListener('click', primaryAction);
    }
    rew.addEventListener('click', function () { video.currentTime = Math.max(0, video.currentTime - 10); });
    fwd.addEventListener('click', function () { video.currentTime = Math.min(video.duration || 1e9, video.currentTime + 10); });
    muteBtn.addEventListener('click', function () { video.muted = !video.muted; updateVol(); });
    volume.addEventListener('input', function () { video.muted = false; video.volume = parseFloat(volume.value); updateVol(); });

    // ── Realtime keep-alive ping ──────────────────────────────────────────────
    // While the video is ACTUALLY playing, GET <current stream host>/rt_ping.php
    // on an interval. Stops the moment playback pauses/ends/stalls-to-pause or
    // hasn't started. Fire-and-forget (no-cors), recomputed each tick so it
    // follows the current host after a failover.
    var PING_INTERVAL_MS = 60000;
    var pingTimer = null;
    function rtPing() {
        try {
            var origin = originOf(state.hlsUrl || state.castUrl || '');
            if (origin) fetch(origin + '/rt_ping.php', { method: 'GET', mode: 'no-cors', credentials: 'omit', cache: 'no-store' }).catch(function () {});
        } catch (e) {}
    }
    function startPing() { if (pingTimer || video.paused || video.ended) return; rtPing(); pingTimer = setInterval(rtPing, PING_INTERVAL_MS); }
    function stopPing()  { if (pingTimer) { clearInterval(pingTimer); pingTimer = null; } }

    video.addEventListener('play', function () { updatePlayUI(); emit('playing'); cancelUpNext(); });
    video.addEventListener('playing', startPing);
    video.addEventListener('pause', stopPing);
    video.addEventListener('ended', stopPing);
    video.addEventListener('pause', function () { updatePlayUI(); if (!video.ended) emit('paused'); });
    video.addEventListener('timeupdate', function () { updateTime(); emitProgress(); });
    video.addEventListener('progress', updateBuffered);
    video.addEventListener('volumechange', updateVol);
    video.addEventListener('seeked', function () { emit('seeked'); setLoading(false); });
    video.addEventListener('ended', function () { emit('completed'); if (IS_TV && CONFIG.autoNext) scheduleNextEpisode(); });
    video.addEventListener('waiting', function () { if (!video.paused) setLoading(true); });
    video.addEventListener('stalled', function () { if (!video.paused) setLoading(true); });
    video.addEventListener('playing', function () { setLoading(false); });
    video.addEventListener('canplay', function () { setLoading(false); });
    video.addEventListener('seeking', function () { if (!video.paused) setLoading(true); });

    /* fullscreen */
    function isFs() { return document.fullscreenElement || document.webkitFullscreenElement; }
    function enterVideoFs() {
        if (video.webkitSupportsFullscreen && video.webkitEnterFullscreen) {
            video.webkitEnterFullscreen();
        }
    }

    function toggleFs() {
        try {
            if (isFs() || video.webkitDisplayingFullscreen) {
                try { (document.exitFullscreen || document.webkitExitFullscreen).call(document); } catch (e) {}
                try { video.webkitExitFullscreen(); } catch (e) {}
                return;
            }
            if (IS_IOS) {
                // Assert subtitle track mode before entering fullscreen.
                // addTrack() already created a fresh element with mode='showing' when the
                // subtitle was selected — we just re-assert it here in case iOS cleared it.
                // No remove/re-add = no VTT re-download on every fullscreen entry.
                try { for (var i = 0; i < video.textTracks.length; i++) video.textTracks[i].mode = 'showing'; } catch (e) {}
                enterVideoFs();
                return;
            }
            var el = wrap;
            if (el.requestFullscreen) el.requestFullscreen();
            else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
            else enterVideoFs();
        } catch (e) { try { enterVideoFs(); } catch (e2) {} }
    }
    fsBtn.addEventListener('click', toggleFs);
    document.addEventListener('fullscreenchange', fsUI);
    document.addEventListener('webkitfullscreenchange', fsUI);
    // iOS native video fullscreen events
    var subsDiv = document.getElementById('subs');
    video.addEventListener('webkitbeginfullscreen', function () {
        iFs.classList.add('hidden'); iFsExit.classList.remove('hidden');
        // Hide custom div overlay; let native WebKit text tracks render in fullscreen.
        if (subsDiv) subsDiv.style.visibility = 'hidden';
        video.classList.add('ios-native-fs'); // activates ::cue CSS visibility
        // Re-assert showing in case iOS reset track mode on src change.
        try { for (var i = 0; i < video.textTracks.length; i++) video.textTracks[i].mode = 'showing'; } catch (e) {}
    });
    video.addEventListener('webkitendfullscreen', function () {
        iFs.classList.remove('hidden'); iFsExit.classList.add('hidden');
        // Restore custom overlay. Tracks stay 'showing' — native ::cue is hidden
        // outside fullscreen via CSS (ios-native-fs class removed). The custom
        // overlay resumes rendering subtitles from state.currentCues.
        if (subsDiv) subsDiv.style.visibility = '';
        video.classList.remove('ios-native-fs');
    });
    function fsUI() { var f = !!isFs(); iFs.classList.toggle('hidden', f); iFsExit.classList.toggle('hidden', !f); }

    /* show/hide UI */
    // Tell the parent when the player UI is visible, so its title + season/
    // episode bar can follow the same show/hide as the player controls.
    function postUI(v) { if (state.uiVisible === v) return; state.uiVisible = v; post({ type: 'PLAYER_UI', visible: v }); }
    var idleTimer;
    function showUI() {
        wrap.classList.add('show-ui'); wrap.classList.remove('mouse-idle');
        postUI(true);
        clearTimeout(idleTimer);
        idleTimer = setTimeout(function () {
            if (!video.paused && !anyMenuOpen()) { wrap.classList.remove('show-ui'); wrap.classList.add('mouse-idle'); postUI(false); }
        }, 2800);
    }
    wrap.addEventListener('pointermove', showUI);
    wrap.addEventListener('pointerdown', showUI);
    video.addEventListener('pause', function () { wrap.classList.add('show-ui'); });
    showUI();

    /* keyboard */
    document.addEventListener('keydown', function (e) {
        if (/input|select|textarea/i.test((e.target && e.target.tagName) || '')) return;
        switch (e.key) {
            case ' ': case 'k': e.preventDefault(); togglePlay(); break;
            case 'ArrowLeft': video.currentTime = Math.max(0, video.currentTime - 5); showUI(); break;
            case 'ArrowRight': video.currentTime = Math.min(video.duration || 1e9, video.currentTime + 5); showUI(); break;
            case 'ArrowUp': video.volume = Math.min(1, video.volume + 0.1); video.muted = false; updateVol(); showUI(); break;
            case 'ArrowDown': video.volume = Math.max(0, video.volume - 0.1); updateVol(); showUI(); break;
            case 'f': toggleFs(); break;
            case 'm': video.muted = !video.muted; updateVol(); break;
            case 'Escape': closeMenus(); break;
        }
    });

    /* parent -> player + progress events */
    // Map a stream level to the closest standard resolution label (e.g. a
    // 640x266 widescreen level -> "360p"). Width is the stable dimension for
    // letterboxed/anamorphic sources, so match on it when present.
    var STD_RES = [
        { w: 256,  h: 144,  l: '144p'  },
        { w: 426,  h: 240,  l: '240p'  },
        { w: 640,  h: 360,  l: '360p'  },
        { w: 854,  h: 480,  l: '480p'  },
        { w: 1280, h: 720,  l: '720p'  },
        { w: 1920, h: 1080, l: '1080p' },
        { w: 2560, h: 1440, l: '1440p' },
        { w: 3840, h: 2160, l: '2160p' }
    ];
    function qualityLabel(level) {
        if (!level) return '';
        var w = level.width || 0, h = level.height || 0;
        if (w || h) {
            var best = null, bestD = Infinity;
            for (var i = 0; i < STD_RES.length; i++) {
                var d = w ? Math.abs(STD_RES[i].w - w) : Math.abs(STD_RES[i].h - h);
                if (d < bestD) { bestD = d; best = STD_RES[i]; }
            }
            if (best) return best.l;
        }
        return level.bitrate ? Math.round(level.bitrate / 1000) + 'k' : '';
    }

    // Build the VidAPI-compatible PLAYER_EVENT payload (see vidapi.ru/api).
    function qualityInfo() {
        var avail = [], q = null, levels = state.qualities || [];
        if (state.hls && levels.length) {
            var seen = {};
            levels.forEach(function (l) {
                var lab = qualityLabel(l);
                if (lab && !seen[lab]) { seen[lab] = 1; avail.push(lab); }
            });
            avail.sort(function (a, b) { return (parseInt(b, 10) || 0) - (parseInt(a, 10) || 0); });
            var idx = state.hls.currentLevel; if (idx < 0) idx = state.hls.loadLevel; if (idx < 0) idx = 0;
            var L = levels[idx];
            if (L) q = { label: qualityLabel(L), width: L.width || 0, height: L.height || 0 };
        }
        return { quality: q, avail: avail };
    }
    function emit(status) {
        var qi = qualityInfo();
        post({
            type: 'PLAYER_EVENT',
            data: {
                player_info: {
                    imdb: CONFIG.imdb || null,
                    tmdb: CONFIG.tmdb || null,
                    mediaType: CONFIG.mediaType || 'movie',
                    season: (state.season != null ? state.season : null),
                    episode: (state.episode != null ? state.episode : null),
                    title: state.title || null,
                    poster: state.poster || null
                },
                player_status: status,
                player_progress: Math.round((video.currentTime || 0) * 10) / 10,
                player_duration: Math.round(video.duration || 0),
                quality: qi.quality,
                availableQualities: qi.avail
            }
        });
    }
    var lastProg = 0;
    function emitProgress() {
        if (video.paused) return;
        if (Date.now() - lastProg < 5000) return;
        lastProg = Date.now();
        emit('playing');
        saveProgress();
    }
    window.addEventListener('message', function (e) {
        if (e.source === window || e.source !== window.parent) return;
        var m = e.data; try { if (typeof m === 'string') m = JSON.parse(m); } catch (x) { return; }
        if (!m) return;
        // Parent's season/episode dropdown -> switch episode.
        if (m.type === 'TV_SET' && IS_TV) {
            var s = +m.season, ep = +m.episode;
            if (s && ep && (s !== state.season || ep !== state.episode)) loadEpisode(s, ep, 0);
            return;
        }
        if (m.player !== true || !m.action) return;
        if (m.action === 'play') video.play();
        else if (m.action === 'pause') video.pause();
        else if (m.action === 'mute') { video.muted = true; updateVol(); }
        else if (m.action === 'unmute') { video.muted = false; updateVol(); }
        else if (m.action.indexOf('seek') === 0) { var s = m.action.match(/seek([+-]?)([0-9]+)/); if (s) video.currentTime = s[1] === '-' ? video.currentTime - (+s[2]) : (s[1] === '+' ? video.currentTime + (+s[2]) : +s[2]); }
    });

    /* ============================================================
     * Settings menu (quality)
     * ============================================================ */
    var settingsMenu = null;

    function anyMenuOpen() {
        return (settingsMenu && settingsMenu.classList.contains('is-open')) || (window.__ccMenu && window.__ccMenu.classList.contains('is-open'));
    }
    function closeMenus() {
        if (settingsMenu) settingsMenu.classList.remove('is-open');
        if (window.closeCC) window.closeCC();
    }

    function menuRow(label, opts) {
        var el = document.createElement('div');
        el.className = 'jw-row' + (opts.active ? ' active' : '');
        el.innerHTML = '<svg class="jw-check" viewBox="0 0 24 24"><path d="M9 16.2L4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z"/></svg><span class="jw-row-label"></span>'
            + (opts.val ? '<span class="jw-row-val"></span>' : '');
        el.querySelector('.jw-row-label').textContent = label;
        if (opts.val) el.querySelector('.jw-row-val').textContent = opts.val;
        el.addEventListener('click', opts.onClick);
        return el;
    }

    // Map a stored quality height back to a level index in the current stream
    // (a stream after failover may have a different level set). Prefers an exact
    // height match, else the closest.
    function levelIndexForHeight(h) {
        var levels = state.qualities || [];
        var exact = -1, closest = -1, cd = Infinity;
        for (var i = 0; i < levels.length; i++) {
            var lh = levels[i].height || 0;
            if (lh === h) { exact = i; break; }
            var d = Math.abs(lh - h);
            if (d < cd) { cd = d; closest = i; }
        }
        return exact >= 0 ? exact : closest;
    }

    function buildSettings() {
        if (settingsMenu) { try { settingsMenu.remove(); } catch (e) {} }

        var menu = document.createElement('div');
        menu.className = 'jw-menu';
        var head = document.createElement('div');
        head.className = 'jw-menu-head';
        head.innerHTML = '<span>Settings</span>';
        var close = document.createElement('button'); close.className = 'jw-menu-close'; close.innerHTML = '&times;';
        close.addEventListener('click', function () { menu.classList.remove('is-open'); });
        head.appendChild(close);
        var body = document.createElement('div'); body.className = 'jw-menu-body';
        var scroll = document.createElement('div'); scroll.className = 'jw-results';

        // Quality (HLS.js only — not available on iOS native HLS)
        if (state.hls && state.qualities.length) {
            var qsec = document.createElement('div'); qsec.className = 'jw-sec'; qsec.textContent = 'Quality'; scroll.appendChild(qsec);
            var cur = state.hls.currentLevel;
            scroll.appendChild(menuRow('Auto', { active: cur === -1 || state.hls.autoLevelEnabled, onClick: function () { state.manualHeight = null; state.hls.currentLevel = -1; buildSettings(); openMenu(settingsMenu); } }));
            state.qualities.forEach(function (lv, i) {
                var label = qualityLabel(lv);
                scroll.appendChild(menuRow(label, { active: !state.hls.autoLevelEnabled && cur === i, onClick: function () { state.manualHeight = lv.height || 0; state.hls.currentLevel = i; buildSettings(); openMenu(settingsMenu); } }));
            });
        }

        body.appendChild(scroll);
        menu.appendChild(head); menu.appendChild(body);
        playerHost().appendChild(menu);
        settingsMenu = menu;
    }

    function playerHost() { return isFs() || wrap; }
    function openMenu(menu) { closeMenus(); menu.classList.add('is-open'); showUI(); }

    setBtn.addEventListener('click', function () {
        if (!settingsMenu) buildSettings();
        if (!settingsMenu) return;
        settingsMenu.classList.contains('is-open') ? settingsMenu.classList.remove('is-open') : openMenu(settingsMenu);
    });
    document.addEventListener('click', function (e) {
        if (settingsMenu && settingsMenu.classList.contains('is-open') && !settingsMenu.contains(e.target) && !setBtn.contains(e.target)) settingsMenu.classList.remove('is-open');
    }, true);

    /* ============================================================
     * Thumbnails (seek hover preview)
     * ============================================================ */
    var imgCache = {};          // url -> { state:'loading'|'loaded'|'error' }
    var curThumbUrl = '', curThumbCue = null;
    var THUMB_W = 168;          // preview width; tiles are scaled to this

    function ensureImg(url) {
        var e = imgCache[url];
        if (e) return e;
        e = imgCache[url] = { state: 'loading' };
        var im = new Image();
        im.onload = function () {
            e.state = 'loaded';
            if (!thumb.hidden && curThumbUrl === url && curThumbCue) renderThumb(curThumbCue);
        };
        im.onerror = function () { e.state = 'error'; };
        im.src = url;
        return e;
    }

    // Box size for a cue (uses only the VTT x/y/w/h — no natural-size lookup).
    function thumbDims(c) {
        if (c && c.xywh && c.xywh.w) { var s = THUMB_W / c.xywh.w; return { s: s, w: THUMB_W, h: Math.round(c.xywh.h * s) }; }
        return { s: 1, w: THUMB_W, h: Math.round(THUMB_W * 9 / 16) };
    }

    // Render the tile at native w×h and transform:scale() it into the sized box.
    function renderThumb(c) {
        thumb.classList.remove('loading');
        var d = thumbDims(c);
        thumbBox.style.width = d.w + 'px';
        thumbBox.style.height = d.h + 'px';
        var st = thumbImg.style;
        st.backgroundImage = 'url("' + c.url + '")';
        st.transformOrigin = 'top left';
        if (c.xywh) {
            st.width = c.xywh.w + 'px';
            st.height = c.xywh.h + 'px';
            st.backgroundSize = 'auto';
            st.backgroundPosition = '-' + c.xywh.x + 'px -' + c.xywh.y + 'px';
            st.transform = 'scale(' + d.s + ')';
        } else {
            st.width = d.w + 'px';
            st.height = d.h + 'px';
            st.backgroundSize = 'cover';
            st.backgroundPosition = 'center';
            st.transform = 'none';
        }
    }

    // Keep a hover overlay centered on x but clamped inside the seek bar so it
    // never spills off the far left/right of the player.
    function clampLeft(el, x) {
        var half = (el.offsetWidth || 0) / 2;
        var sw = seek.clientWidth || seek.getBoundingClientRect().width || 0;
        return Math.max(half, Math.min(sw - half, x));
    }

    function showThumb(t, x) {
        var c = null;
        for (var i = 0; i < state.thumbs.length; i++) { if (t >= state.thumbs[i].start && t < state.thumbs[i].end) { c = state.thumbs[i]; break; } }
        if (!c) { thumb.hidden = true; tip.hidden = false; tip.textContent = fmt(t); tip.style.left = clampLeft(tip, x) + 'px'; return; }
        tip.hidden = true;
        thumb.hidden = false;
        thumbTime.textContent = fmt(t);
        curThumbUrl = c.url; curThumbCue = c;

        var e = ensureImg(c.url);
        if (e.state === 'loaded') {
            renderThumb(c);
        } else {
            // still downloading — show a black box (sized to the tile) + spinner
            thumb.classList.add('loading');
            var d = thumbDims(c);
            thumbBox.style.width = d.w + 'px';
            thumbBox.style.height = d.h + 'px';
            thumbImg.style.backgroundImage = 'none';
            thumbImg.style.transform = 'none';
        }
        // Position after sizing so the clamp uses the real box width.
        thumb.style.left = clampLeft(thumb, x) + 'px';
    }

    function parseThumbs(vtt, base) {
        var out = [], lines = vtt.replace(/\r/g, '').split('\n'), i = 0;
        function abs(u) { try { return new URL(u, base).href; } catch (e) { return u; } }
        while (i < lines.length) {
            if (lines[i].indexOf('-->') > -1) {
                var ab = lines[i].split('-->'); var st = tc(ab[0]); var en = tc(ab[1]);
                i++; var url = (lines[i] || '').trim();
                var xywh = null, hash = url.indexOf('#xywh=');
                if (hash > -1) { var p = url.slice(hash + 6).split(','); xywh = { x: +p[0], y: +p[1], w: +p[2], h: +p[3] }; url = url.slice(0, hash); }
                out.push({ start: st, end: en, url: abs(url), xywh: xywh });
            }
            i++;
        }
        return out;
    }
    function tc(t) { t = (t || '').trim().split(' ')[0].replace(',', '.'); var p = t.split(':').map(parseFloat); if (p.length === 3) return p[0] * 3600 + p[1] * 60 + p[2]; if (p.length === 2) return p[0] * 60 + p[1]; return p[0] || 0; }

    /* ============================================================
     * Subtitles  (loaded from subtitles.js section below)
     * ============================================================ */
    function renderSubtitle(t) {
        if (!subText) return;
        var cues = state.currentCues;
        if (!cues) { if (subText.textContent) subText.textContent = ''; return; }
        // Apply the user's timing offset: a positive value DELAYS subtitles (shifts
        // them later), a negative value shows them EARLIER.
        var tt = t - (state.subOffset || 0);
        var txt = '';
        for (var i = 0; i < cues.length; i++) { if (tt >= cues[i].start && tt <= cues[i].end) { txt = cues[i].text; break; } }
        txt = txt.replace(/[\u200B-\u200F\u202A-\u202E\u2066-\u2069\uFEFF\u00AD]/g, '');
        if (txt !== subText.dataset.cur) { subText.dataset.cur = txt; subText.innerHTML = txt.replace(/\n/g, '<br>'); }
    }

    // expose for the subtitle module
    // Reload native-HLS video on iOS with a new URL (used by subtitle proxy).
    // Preserves playback position and play/pause state.
    function iosReloadSrc(newUrl) {
        if (!IS_IOS) return;
        var t = video.currentTime, playing = !video.paused;
        video.src = newUrl;
        video.load();
        video.addEventListener('loadedmetadata', function () {
            try { video.currentTime = t; } catch (e) {}
            if (playing) video.play().catch(function () {});
        }, { once: true });
    }

    window.__JW = {
        CONFIG: CONFIG, state: state, $: $, esc: esc, fmt: fmt, lsGet: lsGet, lsSet: lsSet,
        video: video, ccBtn: ccBtn, playerHost: playerHost, openMenu: openMenu, closeMenus: closeMenus,
        showUI: showUI, renderSubtitle: renderSubtitle, tc: tc,
        IS_IOS: IS_IOS, iosReloadSrc: iosReloadSrc
    };

    /* ============================================================
     * Boot
     * ============================================================ */
    // Apply the lightweight metadata (title + backdrop) shared by both the
    // metadata-only and full stream-data responses.
    function applyMeta(d) {
        if (!d) return;
        if (d.backdrop && poster) { state.poster = d.backdrop; poster.style.backgroundImage = 'url("' + d.backdrop + '")'; }
        if (d.title) {
            state.showTitle = d.title;                 // bare show/movie name
            emitTitle();
        }
    }
    // The player itself doesn't render a title; only the parent frame / browser
    // tab get it. For TV it's episode-aware and zero-padded (e.g. "… S01 E01").
    function emitTitle() {
        var t = state.showTitle || '';
        if (!t) return;
        if (IS_TV && state.season && state.episode) t += ' \u00B7 S' + pad2(state.season) + ' E' + pad2(state.episode);
        state.title = t;
        document.title = t;
        post({ type: 'PLAYER_TITLE', title: t });
    }

    // Landing screen: fetch metadata only (no &stream_urls) so we can show the
    // poster/title without resolving the actual stream sources yet.
    // Fetch + transparently decrypt the stream-data API (see vsdec.js). Falls
    // back to a plain fetch if vsdec.js isn't present.
    function apiJSON(url) {
        if (window.vsFetchJSON) return window.vsFetchJSON(url);
        return fetch(url, { credentials: 'omit', headers: { accept: 'application/json' } }).then(function (r) { return r.json(); });
    }

    function loadMeta() {
        if (!CONFIG.metaApi) return;
        apiJSON(CONFIG.metaApi)
            .then(function (json) { if (json && json.data) applyMeta(json.data); })
            .catch(function () {});
    }

    function start(data) {
        var d = data.data || {};
        applyMeta(d);

        // thumbnails
        var thumbsUrl = data.thumbnails_url || d.thumbnails_url || '';
        if (thumbsUrl) fetch(thumbsUrl).then(function (r) { return r.ok ? r.text() : ''; }).then(function (t) { if (t) state.thumbs = parseThumbs(t, thumbsUrl); }).catch(function () {});

        // subtitle context
        if (window.JWSubs) window.JWSubs.setup(data);

        // Store the raw (unstamped) URLs. The per-host /generate.php token is
        // fetched lazily right before each host's playlist is requested (see
        // loadStream), so we never ping every host up front — only the host we
        // actually play, at the moment we play it. Tokens are per-host and
        // optional; the multi-server fallback in initHLS handles any host that
        // ultimately rejects playback.
        var streams = (d.stream_urls || []).filter(Boolean);
        if (!streams.length) { showError('No stream found.'); return; }

        state.allStreams = streams;
        state.streamIdx = 0;
        loadStream(0, false);
    }

    function loadSubtitlesAuto() { if (window.JWSubs) window.JWSubs.auto(); }

    // Fetch a stream-data response and begin playback.
    function fetchStream(url) {
        hideError();   // clear any previous "unavailable"/"all servers failed" overlay (e.g. on episode switch)
        setLoading(true);
        apiJSON(url)
            .then(function (json) {
                var code = json && (json.status_code || json.status);
                if (!json || (code && String(code) !== '200') || !(json.data && json.data.stream_urls)) { showError('This media is unavailable.'); return; }
                start(json);
            })
            .catch(function () { showError('This media is unavailable.'); });
    }

    /* ---------- TV: season/episode handling ---------- */
    function tvStoreKey() { return 'vs_tv_' + (CONFIG.mediaId || 'x'); }
    function movStoreKey() { return 'vs_mov_' + (CONFIG.mediaId || 'x'); }
    function apiFor(s, e) { return CONFIG.streamBase + '&season=' + encodeURIComponent(s) + '&episode=' + encodeURIComponent(e) + '&stream_urls'; }
    function epList(s) { return ((state.eps && state.eps[String(s)]) || []).slice().sort(function (a, b) { return (+a) - (+b); }); }
    function seasonList() { return Object.keys(state.eps || {}).map(Number).filter(function (n) { return !isNaN(n); }).sort(function (a, b) { return a - b; }); }

    // Remember the last-watched episode + position for this show.
    function saveProgress() {
        var t = Math.round(video.currentTime || 0);
        if (IS_TV) {
            if (!state.season || !state.episode) return;
            lsSet(tvStoreKey(), JSON.stringify({ s: state.season, e: state.episode, t: t, at: Date.now() }));
        } else {
            // Movie continue-watching: remember the last position for this title.
            lsSet(movStoreKey(), JSON.stringify({ t: t, at: Date.now() }));
        }
    }

    // Where to start: explicit URL episode > saved progress > first episode.
    function resolveStart() {
        if (state.season && state.episode) return { s: state.season, e: state.episode, t: 0 };
        try {
            var raw = lsGet(tvStoreKey());
            if (raw) {
                var p = JSON.parse(raw);
                if (p && p.s && p.e && state.eps && state.eps[String(p.s)] && state.eps[String(p.s)].indexOf(String(p.e)) > -1) {
                    return { s: +p.s, e: +p.e, t: +p.t || 0 };
                }
            }
        } catch (e) {}
        var seasons = seasonList();
        var s = seasons.length ? seasons[0] : 1;
        var eps = epList(s);
        return { s: s, e: eps.length ? +eps[0] : 1, t: 0 };
    }

    // The season/episode dropdowns live on the parent page. Send it the map +
    // current selection; it sends back TV_SET to switch episodes.
    function postTvInfo() { post({ type: 'TV_INFO', eps: state.eps || {}, season: state.season, episode: state.episode }); }
    function postTvState() { post({ type: 'TV_STATE', season: state.season, episode: state.episode }); }

    function loadEpisode(s, e, t) {
        state.season = s; state.episode = e; state.resumeAt = t || 0;
        state.currentCues = null;              // drop previous episode's subtitles
        emitTitle();
        postTvState();
        saveProgress();
        showUI();
        fetchStream(apiFor(s, e));
    }
    // TV boot: load the season/episode map, resolve the start episode, hand the
    // map to the parent for its dropdowns, then load that episode.
    function bootTv() {
        apiJSON(CONFIG.streamBase)
            .then(function (json) {
                var d = (json && json.data) || {};
                state.eps = d.eps || {};
                applyMeta(d);
                var st = resolveStart();
                state.season = st.s; state.episode = st.e;
                postTvInfo();
                loadEpisode(st.s, st.e, st.t);
            })
            .catch(function () { showError('This media is unavailable.'); });
    }

    // ── Auto-next episode ────────────────────────────────────────────────────

    // Ensure the full season/episode map is loaded (may be absent on direct URLs).
    function ensureEps() {
        if (state.eps && Object.keys(state.eps).length) return Promise.resolve();
        return apiJSON(CONFIG.streamBase)
            .then(function (json) { state.eps = ((json && json.data) || {}).eps || {}; })
            .catch(function () {});
    }

    // Return the next {s, e} from the eps map after the current episode, or null.
    function nextEp() {
        var seasons = seasonList(); // sorted numerically as Numbers
        var s = state.season, e = state.episode;
        if (!s || !e || !seasons.length) return null;
        var sIdx = seasons.indexOf(+s);
        if (sIdx === -1) return null;
        var eps = epList(s).map(Number); // numeric, sorted
        var eIdx = eps.indexOf(+e);
        if (eIdx === -1) return null;
        if (eIdx < eps.length - 1) return { s: +s, e: eps[eIdx + 1] };
        // Try next season
        for (var si = sIdx + 1; si < seasons.length; si++) {
            var nextEps = epList(seasons[si]).map(Number);
            if (nextEps.length) return { s: seasons[si], e: nextEps[0] };
        }
        return null; // last episode of the show
    }

    var upnextTimer = null;

    function cancelUpNext() {
        clearInterval(upnextTimer);
        upnextTimer = null;
        var el = $('upnext');
        if (el) el.style.display = 'none';
    }

    function scheduleNextEpisode() {
        if (!IS_TV) return;
        var DELAY = 8; // seconds before auto-loading
        ensureEps().then(function () {
            var next = nextEp();
            if (!next) return; // last episode — nothing to do
            var el = $('upnext'), titleEl2 = $('upnextTitle'),
                prog = $('upnextProgress'), playBtn = $('upnextPlay'), cancelBtn = $('upnextCancel');
            if (!el) return;
            if (titleEl2) titleEl2.textContent = 'S' + pad2(next.s) + ' E' + pad2(next.e);
            if (prog) { prog.style.transition = 'none'; prog.style.transform = 'scaleX(1)'; }
            el.style.display = '';
            // Start progress bar animation
            setTimeout(function () {
                if (prog) { prog.style.transition = 'transform ' + DELAY + 's linear'; prog.style.transform = 'scaleX(0)'; }
            }, 50);
            var remaining = DELAY;
            upnextTimer = setInterval(function () {
                remaining--;
                if (remaining <= 0) {
                    cancelUpNext();
                    loadEpisode(next.s, next.e, 0);
                }
            }, 1000);
            if (playBtn) {
                playBtn.onclick = function () { cancelUpNext(); loadEpisode(next.s, next.e, 0); };
            }
            if (cancelBtn) cancelBtn.onclick = cancelUpNext;
        });
    }

    // ─────────────────────────────────────────────────────────────────────────

    function boot() {
        if (state.booted) return;
        state.booted = true;
        wrap.classList.remove('landing');
        showBigPlay(false);
        setLoading(true);
        showUI();
        if (IS_TV) {
            if (state.season && state.episode) {
                // Explicit episode in the URL: just play it, no season/episode
                // selector (the eps map isn't loaded).
                fetchStream(apiFor(state.season, state.episode));
            } else {
                // Bare show: load the eps map, resolve the start episode, and
                // show the season/episode dropdowns.
                bootTv();
            }
        } else {
            // Continue watching (movies): resume from the saved position unless an
            // explicit startAt was passed. TV resumes via resolveStart().
            if (!(parseFloat(CONFIG.startAt) > 0)) {
                try {
                    var mp = JSON.parse(lsGet(movStoreKey()) || 'null');
                    if (mp && mp.t > 2) state.resumeAt = mp.t;
                } catch (e) {}
            }
            fetchStream(CONFIG.api);
        }
    }

    // The player never shows its own title — only the parent page does. The
    // title bar is revealed solely to host the season/episode dropdowns (index).
    if (titleEl) titleEl.style.display = 'none';

    updateVol();
    if (LANDING) {
        // Landing screen: show the poster + big play button. Only the light
        // metadata call runs now; stream sources are fetched on the play click.
        showBigPlay(true);
        loadMeta();
    } else if (CONFIG.turnstile) {
        // Turnstile gate: wait for the invisible check to verify before booting.
        window.addEventListener('vs:verified', function () { boot(); }, { once: true });
    } else {
        boot();
    }

    /* ============================================================
     * AirPlay (Safari / iOS) — native browser API
     * ============================================================ */
    (function () {
        var airBtn = $('airBtn');
        if (!airBtn) return;
        if (typeof video.webkitShowPlaybackTargetPicker !== 'function') return;
        video.addEventListener('webkitplaybacktargetavailabilitychanged', function (e) {
            airBtn.style.display = e.availability === 'available' ? '' : 'none';
        });
        airBtn.addEventListener('click', function () {
            video.webkitShowPlaybackTargetPicker();
        });
    })();

    /* ============================================================
     * Chromecast — Google Cast SDK (loaded async in render.php)
     * __onGCastApiAvailable is called by the SDK when ready.
     * ============================================================ */
    function castCurrentMedia() {
        try {
            var session = cast.framework.CastContext.getInstance().getCurrentSession();
            if (!session || !state.castUrl) return;
            var mediaInfo = new chrome.cast.media.MediaInfo(state.castUrl, 'application/x-mpegURL');
            mediaInfo.metadata = new chrome.cast.media.GenericMediaMetadata();
            var request = new chrome.cast.media.LoadRequest(mediaInfo);
            request.currentTime = video.currentTime;
            session.loadMedia(request).catch(function () {});
            if (!video.paused) video.pause();
        } catch (e) {}
    }

    window.__onGCastApiAvailable = function (isAvailable) {
        if (!isAvailable) return;
        try {
            cast.framework.CastContext.getInstance().setOptions({
                receiverApplicationId: cast.framework.CastContext.DEFAULT_MEDIA_RECEIVER_APP_ID
            });
            var castBtn = $('castBtn');
            if (!castBtn) return;
            castBtn.style.display = '';

            cast.framework.CastContext.getInstance().addEventListener(
                cast.framework.CastContextEventType.SESSION_STATE_CHANGED,
                function (e) {
                    var ss = cast.framework.SessionState;
                    var active = e.sessionState === ss.SESSION_STARTED ||
                                 e.sessionState === ss.SESSION_RESUMED;
                    wrap.classList.toggle('casting', active);
                    if (active) castCurrentMedia();
                }
            );

            castBtn.addEventListener('click', function () {
                var ctx = cast.framework.CastContext.getInstance();
                if (ctx.getCastState() === cast.framework.CastState.CONNECTED) {
                    ctx.endCurrentSession(true);
                } else {
                    ctx.requestSession().catch(function () {});
                }
            });
        } catch (e) {}
    };
})();
