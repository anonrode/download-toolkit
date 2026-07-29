# Operating Charter — download-toolkit

You are being given broad authority over this codebase. That trust is conditional on one thing: **you make changes that survive contact with a real download.** This project has no meaningful test suite and no CI. The only thing standing between a bad edit and a user with 51 failed episodes is your discipline. Read this whole file before you touch anything. It is not generic advice — it is the exact shape of *this* code and the exact ways edits to it have gone wrong before.

---

## 0. The prime directives (read these even if you read nothing else)

1. **Never claim something works because it parses.** Python compiling, or an `import` succeeding, tells you almost nothing here. A resolver that returns `None` on every call still imports fine. An extractor that finds zero episodes still runs to completion and prints a clean summary. "Syntactically valid" and "does the job" are separated by the entire network. See §7 for what verification actually means.
2. **Verify every external assumption against the live world BEFORE you write code against it.** Every API endpoint, every JSON field name, every CSS selector, every HTML structure. Do not invent an endpoint and hope. Do not assume a `<div class="episode">` exists because it would be convenient. `curl` / a probe first, code second. A guessed API that happens to be right is still a bug you got lucky on.
3. **A silent `return None` is the most dangerous line you can write here.** Almost every resolver and helper swallows exceptions and returns `None`/`[]`. That `None` travels all the way down to "Could not resolve link — download failed" with *no stack trace, no cause, nothing*. If you introduce a `NameError`, a wrong method name, a bad field access inside a `try: … except Exception: return None`, it degrades silently into a failed episode that looks like a dead link. Assume every `except` you write or move will hide your mistake. Test the *happy path end to end*, not just that it doesn't crash.
4. **Fix bugs at the layer they live, and check one layer down.** The failures in this codebase are almost never where they appear. "Download failed" has been, in order: a resolver returning None, a resolver returning a URL the downloader couldn't fetch (missing Referer → 403), a correctly-resolved URL whose token aged out, and a correctly-fetched file with the wrong episode number in its name. When you fix "the download," confirm the *bytes actually arrive*, not that the URL looks plausible.
5. **Preserve the resumability contract.** A user can Ctrl-C, lose Wi-Fi, or close the app at any instant. Nothing you do may turn "interrupted" into "lost work" or "silently skipped." The three-bucket resume state (§5) is load-bearing. Don't route around it.
6. **Never add AI attribution to commits.** No `Co-Authored-By`, no "Generated with", nothing. Commit messages describe the change, nothing else. This is a hard rule.

---

## 1. What this codebase is

A Python CLI that downloads whole series (and social videos) from scraper sites. One pipeline, three stages:

```
detect_site(url) → extract_<site>(url, session, ctx) → [per episode] → ResolverRegistry.resolve(link, session) → download_file(direct_url, …)
                    └── extractor: scrape series page,          └── resolver: turn an           └── downloader: pick aria2c
                        find episode links, name them,              intermediate host link           (files) or yt-dlp (HLS),
                        loop, resolve+download each                 into a direct CDN URL            put Referer/UA on the CLI
```

- **Entry point / routing**: `src/extractors/__init__.py` — `SITE_MAP` (domain → extractor fn), `detect_site(url, disabled)`, `process_link_queue(links, session, ctx)` (the top loop; catches `NetworkAbort` → pause, `Exception` → traceback+failed, infers success from resume state).
- **Shared extractor surface**: `src/extractors/base.py` — every extractor starts with `from .base import *`. This re-exports everything an extractor needs (downloader functions, domain constants, helpers). `__all__` is computed as every non-dunder global. **If you add a helper to `base.py` and want extractors to use it, that's automatic; if you add one elsewhere, it won't be in scope.**
- **Per-site extractors**: `src/extractors/{nkiri,jarocks,naijaprey,myasiantv,dramarain,naijavault,plutomovies,social}.py`.
- **Resolvers**: `src/resolvers.py` — `BaseResolver` subclasses + `ResolverRegistry`.
- **Downloader**: `src/downloader.py` (~3200 lines) — the backends, resume state, receipts, progress UI, referer logic, `Prefetcher`.
- **App state**: `src/state.py` — `AppState`, and `make_ctx(cfg)` which builds the `ctx` dict threaded through everything.

Environment quirks that have bitten edits (do not relearn these the hard way):
- **Native Windows Python.** Run from repo root, or with `PYTHONPATH=.`. Paths in code that writes files must be Windows/relative — not `/tmp`, not `/c/…`.
- **bash `cd` does NOT persist between separate shell calls.** Chain with `&&` in one command.
- **yt-dlp is the `yt_dlp` *module*, not a PATH binary.** ffmpeg is auto-installed at runtime and is *not* on PATH in a dev shell — so the ffmpeg gate inside `download_with_ytdlp` returns early in a bare dev environment. Account for that when testing HLS locally.
- **`BASE_DIR`**: `/storage/emulated/0/Anon` on Android, else `~/Downloads/Anon`.

---

## 2. The extractor contract (what every `extract_<site>` must do)

Signature is fixed: `def extract_<site>(url, session, ctx=None):`. Study `src/extractors/dramarain.py` and `jarocks.py` as the reference implementations — they encode the full pattern. The non-negotiable steps, in order:

1. **Unpack ctx once:** `stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)`. `_ctx` fills safe defaults, so this never `KeyError`s. Never read `ctx['stop']` directly — go through `_ctx` (or the `_stopped(ctx)` / `_wait(ctx)` helpers).
2. **Derive name + folder:** `slug = url_slug(url)` → strip category suffixes → `clean_name` → `folder = os.path.join(BASE_DIR, safe_filename(name))`.
3. **Fetch with a referer:** `r = safe_get(session, url, referer=site_referer)`; `if r is None: return`. `safe_get` already retries 3× and follows `window.location.href` JS redirects — do not reimplement that.
4. **Parse into `[(label, href), …]`** and **dedup by href** (`list(dict.fromkeys(...))`). A page often exposes the same episode twice (quality variants); not deduping double-counts and corrupts episode indexing.
5. **Filter to the requested range:** `links = _filter_by_episode_range(links, ctx)`; if empty, `safe_print(render_message('no_episodes_in_range'))` and return.
6. **Build a work-list, applying local skip checks first:** for each link compute `fbase = safe_filename(f"{name} {ep_label}")`, check `already_downloaded(folder, fbase+'.mp4', series_url=url)` (and `.mkv`); if done, `summary.add_skipped()` and continue. Only *unskipped* episodes go into the work-list. This matters because it means the `Prefetcher` never wastes a resolve on an episode you're going to skip.
7. **Per episode: resolve, then download.** Resolve through `resolve_with_retry(lambda u: ResolverRegistry.resolve(u, session), ep_url, ctx)` — **not** a bare `ResolverRegistry.resolve`. The retry wrapper is what turns "network blipped → episode failed" into "waited → downloaded" (§4). On a `None` return: `record_episode_failure(url, name, safe_filename(f"{fbase}.mp4"), summary, fbase)` and continue — **do not** just `summary.add_failed()` (that failure vanishes on exit and never shows in `resume`).
8. **Download:** `download_file(direct, folder, safe_filename(f"{fbase}.{ext}"), summary, series_url=url, series_name=name, bandwidth_limit=bw, quality=quality, current_process=cur_proc, stop_flag=stop, pause_flag=pause, wait_fn=ctx.get('wait'), source_url=ep_url)`. Pass **all** of these — `series_url`/`source_url` drive resume + self-healing; dropping them silently breaks resume.
9. **Honor stop/pause every iteration:** `if _stopped(ctx): break` at the top of the loop, `_wait(ctx)` right after.
10. **Close out:** `if summary.failed == 0 and not _stopped(ctx): mark_series_complete(url)` then `summary.report()`.

**Episode numbering is a real, recurring bug source.** The source site can point episode N's button at episode M's file. If a page carries authoritative per-episode numbers (e.g. dramakey.cc's `.episode-item` → `.episode-number`), key the filename off *that*, not off the `SxxExx` embedded in the URL — trusting the URL tag renames E04 to "S01E05", collides with the real E05, and drops one. `_episode_label(url, text, i)` is the fallback ladder (URL SxxExx → link text → `episode-N`); use the authoritative source when the page gives you one, `_episode_label` otherwise. When two episodes share an href, warn — don't silently merge.

**Multi-layout extractors** (dramarain is the model) try methods in order and `return` on the first that yields links. When you add a new layout, add it as a new method block *before* the generic fallback and after the more specific ones — same ordering discipline as resolvers (§3). Don't reorder existing method blocks without understanding which pages each catches.

---

## 3. The resolver contract (`src/resolvers.py`)

A resolver is a `BaseResolver` subclass with two **staticmethods**:

```python
class FooResolver(BaseResolver):
    @staticmethod
    def can_resolve(url: str) -> bool:
        return 'foo.com' in urlparse(url).netloc.lower()
    @staticmethod
    def resolve(url: str, session) -> str:   # return a direct URL, or None
        try:
            ...
        except requests.RequestException as e:
            if _is_network_error(e):
                raise            # ← MANDATORY: transient drop must re-raise
            safe_print("      [!] Foo: …")
            return None
        except Exception as e:
            safe_print(f"      [!] Foo: {e}")
            return None
```

Rules that are easy to get wrong:

- **`can_resolve` matches on `netloc`, not substring-of-whole-URL.** Matching the raw URL string catches your host name appearing in a query param and hijacks unrelated links.
- **Re-raise on `_is_network_error(e)`; return `None` on a real miss.** This distinction is the whole game. The registry (`ResolverRegistry.resolve`) wraps each resolver in a 3× network-aware retry: a re-raised network error triggers `_resolver_wait_for_network()` and a retry; a clean `None` fails fast as "genuinely gone." If you swallow a network error into `None`, one dropped packet permanently fails the episode. If you re-raise a real "not found," you hang retrying a dead link.
- **Registry order is load-bearing.** `ResolverRegistry.RESOLVERS` is tried top to bottom; the first `can_resolve` wins. Specific hosts (KisskhMegaplay, LightDL, FivePlay, …) **must** sit *before* `EmbedResolver`, which is greedy and will swallow anything iframe-shaped. Adding a resolver = insert it in the list *above* `EmbedResolver`, at a position reflecting its specificity. Forgetting this makes your resolver dead code that never runs, with no error.
- **The registry recurses** (`resolve(res, session, _depth+1)`) up to depth 5, so a resolver may return another intermediate link and it'll get resolved again. It has a fast-path: a URL whose *path* ends in `.mp4/.mkv/.m3u8/.webm` and whose host is **not** in `resolver_domains` short-circuits to a passthrough. If you add a host that appends filenames to its URLs (so the path ends `.mp4` but still needs resolving), add it to `resolver_domains` or the fast-path will return the un-resolved link.
- **The final fallback is `return url`** (passthrough). So a URL that matches no resolver is handed to the downloader as-is. That's intentional for direct CDN links — but it means "my resolver never ran" looks identical to "this was already direct." Confirm your resolver is actually being entered (log it) when debugging.

**The insertion hazard, stated plainly** (this exact bug happened): when adding a class near an existing one, it is fatally easy to paste it *inside* another class's method — between an `except` block and its `return None`. It stays syntactically valid and silently guts the class it landed in (steals its `except`, drops its `return`). After adding any resolver, verify structurally (AST or careful read) that **each** class owns its own `can_resolve` + `resolve` + both `except` handlers, and that no method got split.

---

## 4. Network-aware failure handling (why the retry wrappers exist)

The device is assumed to be on flaky mobile data. The design goal: **one network blip must never fail a whole batch; a real outage must pause cleanly and stay 100% resumable.**

- `_is_network_error(exc)` (resolvers) walks the `__cause__/__context__` chain for connectivity markers (ConnectionError, timeouts, DNS). This is how transient ≠ permanent is decided everywhere.
- `resolve_with_retry(resolve_fn, ep_url, ctx)` (base.py) is the extractor-side wrapper: link found → return it; `None` + network **up** → real miss, return `None`; `None` + network **down** → `wait_or_abort(ctx)` then retry the *same* episode; outage past `NETWORK_ABORT_SECONDS` (120s) → raise `NetworkAbort`.
- `NetworkAbort` propagates to `process_link_queue`, which pauses the series cleanly. **Do not catch `NetworkAbort` inside an extractor** — let it bubble.
- `wait_for_network()` (downloader, inside the download loop) can block *forever* — correct there. `_resolver_wait_for_network()` (resolvers) is deliberately *bounded* (~60s) so a resolve can't hang the whole app behind a captive portal. Don't "unify" these; the asymmetry is intentional.

If you add a new network call anywhere in the resolve/download path, decide explicitly: is a failure here transient (→ re-raise / wait) or permanent (→ None / fail)? Defaulting to "swallow and return None" quietly breaks the batch-resilience guarantee.

---

## 5. Resume state, receipts, and self-healing (`src/downloader.py`)

State lives in `.resume_state.json` with **three buckets per series**: done / current / failed. The API:
- `mark_series_waiting_for_network(url)` — called by `process_link_queue` *before* extraction, so an interrupted series is visible to `resume`.
- `mark_episode_current` / `mark_episode_done` / `mark_episode_failed` — per-episode transitions.
- `mark_series_complete(url)` — clears the series; only call it when `summary.failed == 0` and not stopped.
- `load_resume_state()` — `process_link_queue` infers success by checking whether `url` is *still* in the resume state after the extractor returns. So: **if your extractor finishes a series but forgets `mark_series_complete`, the queue reports it as failed** even though every file downloaded.

`already_downloaded(folder, filename, series_url=…)` is the skip check: it consults the **receipt** system first (respecting "paused" so it never deletes a partial), then the filesystem. It also self-heals — a file present on disk but missing from receipts gets reconciled. Always pass `series_url` so the receipt path is used, not just a naked filesystem check.

Don't hand-edit the JSON schema or bypass these functions. If you need a new state transition, add it alongside the existing `mark_*` functions and keep the three-bucket invariant.

---

## 6. Download routing, and the Referer/UA trap

`download_file(...)` routes: magnet → aria2c torrent path; `is_streaming_link(url)` (`.m3u8` / `manifest`) → `download_with_ytdlp`; everything else → `download_with_aria2c`.

**The trap that has cost multiple sessions:** aria2c and yt-dlp run as **separate subprocesses**. They *cannot see* `session.headers`. Any hotlink protection (Referer / Origin / User-Agent) the CDN requires **must be passed on the command line**, or you get a 403 on a URL that resolved perfectly and works in a browser. `get_referer_for_url(url)` gives the right referer (special-cases vikingfile / kissorgrab / kwik, else `base_domain + '/'`). When you add or edit a download command, if the source uses hotlink protection, the command needs `--referer` (+ often `--user-agent UA_DESKTOP` and `--add-header 'Origin: …'`). A resolved-but-403 m3u8 is *not* a resolver bug — it's a missing header on the yt-dlp/aria2c command line.

yt-dlp progress specifics (already tuned, don't regress): the HLS path uses `--progress-template` with an `@@DLP@@` sentinel + `--newline` (NOT `--no-progress`, which *suppresses* the template), parsed by `_ytdlp_parse_progress` and driven through `LiveProgress`. `download_social_ytdlp` is a *separate* path (IG/FB/YouTube) left on external aria2c on purpose — don't fold the two together.

---

## 7. What "verified" means here (the bar you must clear before saying "done")

"Done" is not "it runs." For any change, clear the applicable rungs:

1. **Syntax + structure.** `python -m py_compile`. For added classes/functions, an AST check that the structure is what you intended (each class has its own methods; nothing got nested by accident — see §3).
2. **External assumptions probed live.** Every endpoint / selector / field you coded against, confirmed against the real site *before or right after* writing — with `curl`, a `session.get`, or a tiny probe script. "The JS bundle actually defines this endpoint," "the page really has this class," "this field is in the JSON." Assume nothing.
3. **Full-chain end to end.** Parsing ≠ downloading; resolving ≠ fetchable. Run the actual path: does the extractor find the *right count* of episodes with the *right names*? Does the resolver return a URL that returns **HTTP 200/206 with a video content-type** (a ranged `bytes=0-0` GET is the cheap probe)? Does at least one real download write real bytes (yt-dlp `--test`, or a few MB via aria2c)?
4. **Failure + interruption paths.** Does a dead/expired link fail *cleanly* (recorded, not crashed)? Does Ctrl-C stop without corrupting state? Is the series still resumable after?

If you can't run a rung (e.g. ffmpeg gate in a bare shell), say so explicitly — don't imply you verified something you didn't. **Report outcomes honestly:** if a probe timed out, say it timed out; if you skipped end-to-end because the site was down, say that. Never round "looks right" up to "works."

---

## 8. Failure modes seen in this codebase (learn from these, don't repeat them)

- **Class pasted inside another class's method.** Guts the host class silently, stays valid Python. → §3 insertion hazard; AST-verify after adding classes.
- **Invented API that happened to be real.** A resolver was written against a guessed `/api/...` endpoint and guessed JSON field names. It turned out correct — *by luck*. The fix wasn't the code, it was: probe the live API first, then keep the code. Do the probe every time; luck is not a methodology.
- **Resolved URL, 403 on download.** m3u8 resolved fine; yt-dlp had no `--referer` → 403. Bug was one layer downstream of where it looked. → §6.
- **Silent `return None` degradation.** A wrong API call / NameError inside `except Exception: return None` presents as "dead link." → §0.3, test the happy path.
- **Episode mislabeling / dropping.** Trusting the URL's `SxxExx` when the page had authoritative numbers → collisions and dropped episodes. → §2 numbering.
- **`summary.add_failed()` without `record_episode_failure`.** Failures that vanish on exit and never appear in `resume`. → §2.7.

---

## 9. Working rules

- **Change the minimum.** Match the surrounding code's idiom, comment density, and naming. This codebase comments the *why* (especially the non-obvious network/resume reasoning) heavily — preserve those comments; if you change the behavior they describe, update them.
- **Don't reorder `RESOLVERS` or extractor method blocks** without articulating which URLs each position catches.
- **Don't reimplement** what `base.py` already gives you (`safe_get`, `_episode_label`, `resolve_with_retry`, `_filter_by_episode_range`, the `Prefetcher` pattern). Reuse it.
- **Ask before large refactors.** Full control means you *can* touch anything; it doesn't mean every change should be big. Prefer surgical fixes to the specific broken path. If a real refactor is warranted, describe the blast radius first.
- **Commits:** describe the change and the user-visible symptom it fixes. No AI attribution (§0.6).
- **When you're unsure whether something transient-vs-permanent, direct-vs-intermediate, or authoritative-vs-guessed** — stop and probe the live world (§7.2) rather than picking the convenient assumption. The convenient assumption is what produces the silent failures this whole document exists to prevent.
