# HANDOVER — Anon Downloader engineering & QA mission (2026-09-08)

Self-contained. You do not need the previous chat transcript.

## 1. The mission

Overnight engineering & QA round across both repos: live-test search at scale,
prove downloads actually work end-to-end, fix every issue found, regression-test,
and produce an honest report ("Be honest. Don't inflate. Don't hide problems.").

## 2. Machine & repos (the machine changed — old paths are dead)

The old user profile was wiped. Everything now lives under the new profile:

- Monolith (PC Python CLI): `C:\Users\user\bb\ANON TOOLS\download-toolkit`
- Android app (Kotlin/Compose): `C:\Users\user\bb\ANON TOOLS\download-toolkit-serverless`
- OTA signing keys + keystore: `C:\Users\user\bb\ANON TOOLS\anon-serverless-app-maintenance-build`
- Installers: `C:\Users\user\bb\setup\`

Toolchain reinstalled after the wipe (all verified working):
Git, Python 3.12 (user PATH), Node 24.18 → `C:\Tools\nodejs`,
aria2 1.37 → `C:\Tools\aria2\aria2-1.37.0-win-64bit-build1`,
FFmpeg → `C:\Tools\ffmpeg\ffmpeg-master-latest-win64-gpl\bin`,
pip deps: requests, beautifulsoup4, yt-dlp, curl_cffi, aiohttp.
Git identity restored: `owoborodebukumi-art <owoborodebukumi@email.com>`.

`src/` is a package: run harnesses from the repo root. Bash `cd` does not
persist between tool calls — chain commands.

## 3. HARD constraints (still binding)

- NO `git push`, NO GitHub Actions builds, no release re-tag without the user's explicit go-ahead. Local commits are fine.
- NO AI attribution in commit messages.
- NEVER revert anything without permission.
- Leave torrents out of everything. Subtitle testing is out of scope.
- Don't touch the "Tiered verification" block in the app's DownloadEngine.kt.
- No fallbacks-for-failures mindset: things must actually work.
- **USER HAS LIMITED MOBILE DATA** — never download full videos/files during
  tests. Prove downloads by resolving to the direct URL and pulling only a few
  MB (or let the user run full downloads themselves). This was learned after
  ~145MB (Vincenzo ep1) + ~123MB (partial Alchemy of Souls ep1) were pulled.

## 4. Monolith: fixes committed this mission (all live-verified)

Monolith HEAD = `cea5084`. App HEAD = `ae8cd3b`. All commits survived the migration.

- `6c727ae` — search+extractors batch: relevance-gate anime rows (they used to
  bypass `_filter_by_relevance`; "it" returned Gundam for a horror query),
  subset-title boost ("Marvel's The Avengers" now finds "The Avengers"),
  Nepu results capped at 6, AsianC slug-token check, JS-redirect stub gate in
  `safe_get`, Wildshare `download_token=` early-return.
- `5051d8c` — nkiri: module-level ThreadPoolExecutor import (UnboundLocalError
  fix on the batch_size==1 path).
- `d3e4cd1` — skip `{year}` slug patterns when query has no year.
- `72e96b1` — reject HTML error pages saved as media on the aria2c path.
- `e55e93f` — nkiri: merge downloadwella + nkiserv link pools.
- `cea5084` — dramakey.com re-added (site is back online) behind a HOST-PARK
  breaker: 3 consecutive all-zero probe rounds → 30-min park; any HTTP answer
  (even 404) = healthy, clears the streak. Includes a gate-bug fix: the
  accumulating streak used to be wiped on every search (until==0 was read as
  "expired park"), which made parking unreachable — verified end-to-end with an
  NXDOMAIN test host (round 4 fires zero probes).
- Earlier app commits: `63a7032` engine/search/UI hardening, `50012c7` dramakey
  base→dramakey.cc rules, `ae8cd3b` stale-results clear + engine proofs.

### Fixes worth remembering (root causes)

- ResolverRegistry was re-resolving WildshareResolver's own `download_token=`
  output → GET streamed a 169MB file into a 20s timeout, 3 retries, every
  episode failed (543s stall, zero downloads). Fixed by early-return.
- `safe_get` followed `window.location.href` inside REAL pages (keyboard-shortcut
  handler) → fetch loop. Fixed with `_looks_like_redirect_stub()` size gate.
- Function-local `from concurrent.futures import ThreadPoolExecutor` inside one
  branch shadows the name for the whole function → UnboundLocalError on other paths.
- Search cache `src/.search_cache.json` stores POST-filter results (24h TTL):
  after ANY relevance/filter fix, evict affected cache keys or the old result
  comes back and the fix "doesn't work".
- `src/search.py` asyncio is lazily imported: module global `asyncio` is None
  until `_ensure_async_imported()` runs — call it in test harnesses before
  touching `_ahead`/`_aprobe_slug` directly.

## 5. Downloads: PROOF

Real end-to-end test on the new machine (2026-09-08):

- `download 1 https://dramakey.cc/chinese/vincenzo/` via the REPL
  (`printf 'download 1 <url>' | python main.py`) — non-interactive episode filter.
- Result: search → dramakey.cc page → extractor → aria2c with 16 connections →
  `C:\Users\user\Downloads\Anon\Vincenzo\Vincenzo S01E01.mkv`, 151,920,186 bytes,
  Done: 1 / Skipped: 0 / Failed: 0 in 6m 5s. ffprobe-verified REAL media:
  matroska, HEVC 864x432 + AAC, duration 5014s (83.5 min — a full episode),
  not an HTML page, not truncated. Run transcript: `scratch/dl_test1.log`.
- Second source (nkiri — different extractor: merged link pools + resolver):
  `download 1 https://thenkiri.com/alchemy-of-souls-s02-complete-korean-drama/`
  resolved to a direct 127MiB .mkv and aria2c was pulling it (16 connections)
  within seconds; user stopped it at ~123MB to save mobile data. The partial +
  its `.aria2` control file sit in `Downloads\Anon\Alchemy Of Souls S02\` and
  are resumable IF the user ever wants to spend the data. Transcript:
  `scratch/dl_test2.log`.
- **Do NOT run further full-file download tests** — see the data constraint in §3.

Monolith CLI essentials: `search <title>`, `fsearch <title> [hint]`,
`download <range> <url>` (e.g. `download 1-5,8 https://...`), `queue`, `resume`,
`cache clear`.

## 6. App parity checks (code-read, 2026-09-08 — both PASS)

1. AsianC slug-mismatch relay: cannot leak to the app. EVERY provider's results
   (AsianC, Anitaku, all) flow through `ProviderRegistry.searchStreaming` →
   `RelevanceScorer.filterAndSort` (min 0.50, token-overlap + substring boost),
   both incrementally and in the final ranking. Zero-overlap rows are dropped.
   The app's 0.50 min (vs monolith 0.60) also means the apostrophe-subset drop
   bug fixed in the monolith can't occur here.
2. Anime routing: AnitakuProvider is in `staticProviders` with default
   `searchEnabled = true` and goes through the same scoring path. There is no
   separate unfiltered anime pipeline in the app.
3. Player system-bar fix (fill-screen dialog + activity bar re-apply on theme
   flip) is ALREADY SHIPPED: commits `61b95bd` + `15259a5`. The saved plan file
   for it is complete; only push/re-tag of it awaits user permission.

## 7. Known unresolved (do not hide these in the report)

- **Nepu/vidsrc resolve flakiness — FIXED 2026-09-11, commit `dfa80ad`.**
  Root cause: `src/resolvers.py` never imported `os`, and `os.unlink(wasm_path)`
  sat after the node decrypt inside a bare-except block — the NameError was
  swallowed every time, so the node result was discarded and every encrypted
  resolve fell into the minutes-slow Python interpreter. Also fixed in the same
  commit: 45s wall-clock budget on the resolve chain, node lookup that works
  off-PATH, a deadline on the Python interpreter, serialized generate.php
  fetch + one spaced retry (prefetch/inline re-resolve were double-hitting it
  → 429 → tokenless masters), untokened masters now fail the resolve instead
  of being returned to 401, nepu prefetcher wait 30s → 75s, dead TRACE-NONE
  prints replaced with real failure reasons. Verified live: resolve in 3.4s,
  token present, master playlist 200 with real HLS body.
- **Blank Termux progress line** (older, unsolved): two shipped fixes didn't
  change the symptom. Full write-up: `HANDOVER-blank-progress-termux.md`.
  Reproduce FIRST before attempting a third fix.
- Site flakiness is real (dramakey.com NXDOMAIN-flaps, transient network blips
  on the new machine) — the park breaker + retries absorb most of it.

## 8. Blocked on the user (explicitly reserved)

- `git push` (both repos), GitHub Actions build, delete+re-tag `v3.1.0`.
- On-device test of the player system-bar fix.

## 9. Scratch / harnesses (reusable)

- `scratch/test_dramakey_readd.py` — cache-evicting live search probe.
- `scratch/probe_extractors.py` — per-site discovery probe (download stubbed).
- `scratch/search_test/` — Agent A's 313-record live sweep: REPORT.md,
  results.jsonl, harness.py, queries.json, seqs.json, analyze.py.
- App repo `probe/results-nightly/` — nightly probe scorecards (untracked).
