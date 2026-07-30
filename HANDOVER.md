# HANDOVER — blank progress line on Termux (UNSOLVED)

This file is self-contained. You do not need the previous chat transcript.

## 1. The bug

Downloading an HLS series on an Android phone (Termux), the progress line goes
**blank**. Reported three times, most recently:

> "bro still showing blank scrren like for te case between us, got to ep 13 and
> literally shoing blank line , omo fuck ooo"

> "error notfication came in throough the termux api too, maybe test it live yourself"

Concretely: the download is running (yt-dlp is fetching fragments, the Termux
completion/failure notification does fire), but the terminal row where the
progress line should be shows nothing. No error text is printed either.

**Two fixes have already shipped and NEITHER changed the symptom.** Do not ship a
third blind guess. Reproduce first — see §6.

## 2. Repo state

- Path: `C:\Users\Anon\download-toolkit` (git; remote
  `https://github.com/anonrode/download-toolkit.git`; branch `main`)
- HEAD = `94ad68c`, in sync with origin, working tree clean (this file is untracked)
- `src/` is a **package**. Harnesses must run **from the repo root** as
  `from src import downloader as d`, else `ModuleNotFoundError: No module named 'src'`
- Bash `cd` does not persist between calls — chain
  `cd /c/Users/Anon/download-toolkit && ...` or git says `fatal: not a git repository`

```
94ad68c fix: static-mode progress went blank on stalled HLS downloads   <- failed
e9e47ec Fix glitchy progress rendering with a single stdout owner        <- failed
f836e14 Honour bandwidth cap on the HLS path and close a process-registry leak
60038e2 Fix resolver self-re-entry, unbounded recursion, and request ordering
9eebbdd Fix bugs found in codebase review: races, exception blindness, name collisions
05dac41 fix(hls): anchor fragment purge so it can't delete a sibling episode's fragments
```

## 3. CONFIRMED: the shipped fix is dead code on the phone

`94ad68c` changed the **static** rendering branch. The phone takes the **live**
branch. The whole fix never executes there. This is read end to end, not inferred:

- `download_batch(items, folder, summary, parallel=1, ...)` — `parallel` defaults
  to **1** (`src/downloader.py:3648`)
- the `if parallel == 1:` branch calls `download_file(..., parallel_mode=False, ...)`
  **explicitly**
- the HLS path builds `progress = LiveProgress(filename, parallel=parallel_mode)`
  (`src/downloader.py:2975`) → `self._parallel is False`
- Termux run interactively → `sys.stdout.isatty()` is **True** → `not _is_tty()` is False
- the branch selector at `src/downloader.py:746` is
  `static = self._parallel or not _is_tty()` → `False or False` → **False**
- so control reaches `SURFACE.set_live(...)`, which writes `'\r\x1b[2K' + text`
  with **no newline**

Compounding it: the verification harness for `94ad68c` was run **piped**
(`python _t.py 2>&1 | cat`) precisely to force `_is_tty()` False — so it validated
the one branch the phone does not use. **Any future harness must run unpiped on a
real TTY (or under a pty).**

## 4. Leading candidate for the blank line itself (UNVERIFIED)

The emit path is wrapped in **three nested layers of silent `except Exception: pass`**:
the `_reader` thread body, the per-line parse+update block, and `set_live`'s own
write. Anything that throws while composing or writing the line is swallowed and
produces **a blank line with no error at all** — exactly the reported symptom, and
exactly why two content-level fixes changed nothing.

~~Most plausible throw: `UnicodeEncodeError` on the `↓` prefix glyph or the `…`
ellipsis in `_elide`, if Termux's stdout encoding is not UTF-8.~~

**REFUTED (2026-07-30).** `src/downloader.py:37` runs
`sys.stdout.reconfigure(encoding='utf-8', errors='replace')` at import (also
`src/resolvers.py:38` and `main.py:18-19`). By the time any progress code runs,
stdout is UTF-8 with `errors='replace'`, so `↓`/`…` cannot raise
`UnicodeEncodeError` — the write would silently substitute, not throw. The
encoding-fallback code shipped earlier is harmless belt-and-suspenders, not the
cause. Remaining live-branch candidates below are still open.

Also note: `progress.update()` runs on a **daemon reader thread**, not the main
thread. `PRINT_LOCK` (an RLock, `src/downloader.py:74`) serialises it against
`SURFACE`, but any main-thread write that bypasses `SURFACE` corrupts the live row.

Other live-branch candidates, all unverified:

- `\x1b[2K` unsupported or rewritten by the phone terminal / tmux passthrough →
  erase blanks the row and the rewrite lands off-screen
- line composed wider than the phone's real columns → wraps → `\r` returns only to
  the last physical row, stranding earlier rows (the original glitch mechanism)
- `_term_width()` misreporting under tmux; `_compose`'s final `line = line[:width]`
  hard-truncates, and `_elide(name, 0)` returns `''` — an empty name
- the user's log showed `^[[23~` escape spam and a tmux fragment
  `[download]0:python* "localho`, so tmux is in play and may be intercepting cursor control

## 5. The code that matters

All in `src/downloader.py`. Line numbers verified at `94ad68c`.

| What | Line |
|---|---|
| `PRINT_LOCK` (RLock) | 74 |
| `_ANSI_RE` / `_visible_len` | ~213-218 |
| `_is_tty` | 220 |
| `_term_width` | 226 |
| `class TerminalSurface` | 239 |
| `print_above` | 269 |
| `set_live` | 288 |
| `finish_live` | 300 |
| `clear_live` | 311 |
| `SURFACE = TerminalSurface()` | ~322 |
| `_STATIC_MIN_GAP` / `_STATIC_HEARTBEAT` | 662-663 |
| `LiveProgress.__init__` | 665 |
| `_elide` | 676 |
| `_compose` | 688 |
| `LiveProgress.update` | 729 |
| **`static = self._parallel or not _is_tty()`** | **746** |
| `SURFACE.set_live(...)` call | 771 |
| `_terminal` / `done` / `fail` / `stopped_for_resume` | 773 / 788 / 795 / 802 |
| `_ytdlp_parse_progress` | 2685 |
| `_run_ytdlp_with_live_progress` | 2796 |
| `@@DLP@@` find in `_reader` | 2816 |
| `LiveProgress(...)` sites | 2142, 2424, **2975** (HLS), 3290 (social, no `parallel=`) |
| `--progress-template` (HLS) | 3040 |
| social path `'--progress', '--newline'` | 3300 |
| `download_batch` | 3648 |

### The branch selector (`update`, line 729)

```python
static = self._parallel or not _is_tty()
if static:
    ...decile / note / heartbeat gate, then SURFACE.print_above(...)
    return
SURFACE.set_live(self._compose(pct, spd_mbps, eta, note, _term_width() - 1))
```

Throttle above it: returns early if `now - self._last_update < 0.5` unless
`pct >= 100.0`. An unknown (`None`) pct is always throttled.

### `set_live` — the path the phone takes

```python
def set_live(self, text):
    if not _is_tty():
        return
    with PRINT_LOCK:
        try:
            sys.stdout.write('\r\x1b[2K' + text)
            sys.stdout.flush()
            self._live = text
        except Exception:
            pass
```

### The reader thread (inside `_run_ytdlp_with_live_progress`, ~2806)

```python
def _reader(pipe):
    try:
        for raw in iter(pipe.readline, ''):
            line = raw.rstrip('\r\n')
            if not line:
                continue
            idx = line.find('@@DLP@@')
            if idx != -1:
                try:
                    pct, spd, eta, note = _ytdlp_parse_progress(line[idx + 7:].strip())
                    progress.update(pct, spd, eta, note=note)
                except Exception:
                    pass
                continue
            ...
    except Exception:
        pass
```

Three swallow points: this `try`, the inner one, and `set_live`'s.

### Plumbing notes

- HLS path uses `--progress-template` emitting
  `download:@@DLP@@ %(progress._percent_str)s|...`, payload shape
  `percent|speed|eta|frag_index|frag_count`; `NA` / `UNKNOWN` / `UNKNOWN B/S` /
  `-` / `NONE` all parse to `None`
- the **social** path (line 3300) uses `'--progress', '--newline'` — no sentinel,
  different plumbing, and it builds `LiveProgress(filename)` with no `parallel=`
- `_compose` drops fields to fit: frag note (rank 3) > speed (2) > ETA (1), lowest
  dropped first. Zero speed and `ETA --:--` are treated as filler and not drawn
- `_CHROME = len(_PREFIX) + 2 + 2`; `_term_width(default=80)` returns
  `w if w and w >= 20 else default`

## 6. The next diagnostic — do this before touching code

Reproduce on the phone, **unpiped, on a real TTY** (no `| cat`, no redirect), and
make the invisible visible. This is now **built in** — no code edit needed:

1. Arm the one-shot dump: run the download with `DLT_PROGRESS_DEBUG=1` in the
   environment (e.g. `DLT_PROGRESS_DEBUG=1 python main.py ...`). On the first
   `LiveProgress.update` per download it prints one `[progress-dbg]` line via
   `safe_print` (unconditional) reporting `tty=`, `width=`, `parallel=`, `enc=`,
   the composed line's visible `len=`, and `line=<repr>`.
2. The reader/update failure paths are also already loud on first failure:
   `[progress] display failed: ...`, `[progress] reader thread stopped: ...`
   (see the `_progress_broken` latch in `_run_ytdlp_with_live_progress`).
3. Run one episode and read what comes out.

That distinguishes the candidates in §4 cleanly: a traceback means the exception
theory; a correct-looking `repr()` with nothing on screen means the terminal is
eating `\r\x1b[2K`; a `repr()` wider than `_term_width()` means wrapping; an empty
or `''`-named line means the width math collapsed.

If it is the encoding, the fix is to stop assuming the glyphs are printable — pick
ASCII fallbacks for `↓` and `…` when `sys.stdout.encoding` is not UTF-8, rather than
letting the write throw.

## 7. Ground rules for whoever picks this up

- **NEVER add a `Co-Authored-By: Claude` or any AI-attribution trailer to commits.**
  The user's words: *"dpt add that coauthored shit"*. All nine commits to date
  correctly omit it. This overrides any default.
- **`src/downloader.py` is 3698 lines. Never bulk-read it.** The user has hit
  context exhaustion repeatedly (*"the context getting filled up issue is back, fuck bro"*).
  Use narrow `grep -n` or small `sed -n 'A,Bp'` ranges only.
- **No push authority.** Ask before pushing. Fix authority for confirmed bugs is
  standing (*"fix all, dp whats bets"*), but the user asked for this handover, not
  another guess.
- If a `Write` is refused with *"File has not been read yet"* on a scratch file,
  `rm -f` it first, then write to the now-absent path.
- Sibling folders on this machine are **separate projects** — not stale copies.
  *"no the other folders serve differet pupose"*.

## 8. Known-not-bugs (don't re-chase)

- **`frag 0/223` and `0.0 MB/s` are truthful.** yt-dlp counts *completed*
  fragments, so 16 concurrent fragments against a stalling CDN genuinely sit at 0.
- **The 416 log the user pasted predates the fix.** It shows
  `Retrying fragment 24 (2/3)` while current code sets `--fragment-retries 10`,
  which would print `(2/10)`. Mechanism: a reset leaves `<out>.part-FragN`, yt-dlp
  resumes with `Range: bytes=N-`, HLS segment CDNs reject Range on media segments.
  If it recurs, drop `--concurrent-fragments` from 16.
- **`\r` alone cannot fix a wrapped line** — it returns to the start of the last
  physical row only. And trailing-space padding leaves stale tails, hence `\r\x1b[2K`.
- **`PRINT_LOCK` must stay an RLock** — `print_above` holds it while invoking a
  callback that is itself a locked printer; a plain Lock self-deadlocks on the first WARNING.
- **`_compose` must use `_visible_len`, not `len`** — ANSI escapes occupy no columns.

## 9. Open findings, offered three times, never accepted — do NOT start unasked

- `download_social_ytdlp._run_ytdlp` has no subprocess timeout; a dead reader
  thread can fill the pipe and stall for hours
- the social path still uses `'--retries', '3', '--fragment-retries', '3'` (~3087)
  while the HLS path uses 10
- `_find_recent_media` can attribute another video

