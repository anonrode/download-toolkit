# ANONRODE — Re-test Prompt (Suites 5, 7, 3 only)

## Why this re-test exists
The previous run had bad data:
- Suite 5 (speed): all times were 0.01s — every search was a cache hit, not live
- Suite 7 (resolver): all URLs were fake placeholder `loadedfiles.net/abcd123`
- Suite 3 (tag recursion): results look mocked (every row exactly "15 posts")

This prompt fixes all three. DO NOT use cached results.

---

## BEFORE STARTING — clear the search cache

```bash
cd ~/download-toolkit
python -c "import json,os; open('src/.search_cache.json','w').write('{}'); print('cache cleared')"
```

Confirm it prints `cache cleared` before running any search.

---

## RE-TEST SUITE 5 — Full vs fast search speed (live, no cache)

For each title below, run BOTH `search <title>` AND `fsearch <title>`.
Use a stopwatch or `time` command to measure wall-clock seconds.
Clear the cache between each pair so both are live fetches.

```bash
# Clear cache before each title pair:
python -c "open('src/.search_cache.json','w').write('{}')"
```

Titles to test (10 total):
1. Fast and Furious
2. Avengers
3. Spider-Man
4. Naruto
5. Money Heist
6. Squid Game
7. Anikulapo
8. Jenifa
9. Attack on Titan
10. Prison Break

### Record (CSV)
```
title, full_search_seconds, fast_search_seconds, full_result_count,
fast_result_count, results_differ, sites_in_full_not_in_fast
```

**Important:**
- `full_search_seconds` = wall clock from pressing Enter to results appearing
- `fast_search_seconds` = same for fsearch
- `sites_in_full_not_in_fast` = which site names appear in full results but NOT
  in fast results (expected: NaijaVault, NaijaPrey, 9jaRocks since they are
  RSS-only and not slug-probed)
- If both return identical results and identical speed, note that explicitly —
  it may mean fsearch is falling back to full search

---

## RE-TEST SUITE 7 — Resolver robustness (real URLs only)

From the Suite 1 results, pick 5 real 9jaRocks URLs that have loadedfiles links.
Use these exact URLs from the previous Suite 2 results:
- `https://www.my9jarocks.bz/videodownload/fast-and-furious-2001-2017-collection-id163674.html`
- `https://www.my9jarocks.bz/videodownload/the-avengers-earths-mightiest-heroes-season-1-2-complete-id388793.html`
- Plus 3 more 9jaRocks URLs from any Suite 1 result

For each URL, paste it at the `>` prompt in the app. Let the extractor run and
find the loadedfiles links. Then cancel the download with `0` or Ctrl+C
AFTER the resolver has attempted to resolve (you'll see either a direct CDN URL
printed, or `[X] Could not extract`).

### Record (CSV)
```
title, loadedfiles_url_from_page, resolver_outcome,
outcome_detail, time_to_resolve_seconds
```

**Field definitions:**
- `loadedfiles_url_from_page` — the actual loadedfiles URL found on the page
  (e.g. `https://loadedfiles.net/abc123def456`) NOT a placeholder
- `resolver_outcome` — one of: `resolved` / `null` / `dead_skipped` / `error`
- `outcome_detail` — if resolved: the CDN domain returned (e.g. `cdn.filevault.com.ng`);
  if null: what the app printed; if dead_skipped: the error message printed;
  if error: the exception text
- `time_to_resolve_seconds` — actual measured seconds, not 0.05

---

## RE-TEST SUITE 3 — Tag/category hub recursion (verify real behaviour)

The app handles `/category/` recursion in NaijaVault and NaijaPrey.
It does NOT handle `/tag/` pages. This test confirms both.

### Test A — NaijaPrey /tag/ pages (should NOT recurse)
Paste each URL at the `>` prompt:
- `https://www.naijaprey.tv/tag/fast-furious-series/`
- `https://www.naijaprey.tv/tag/avengers/`

Expected: app should say "no episode links found" or similar — it does NOT
recurse into tag pages. Record exactly what message is printed.

### Test B — NaijaPrey /category/ pages (should recurse)
Find a real NaijaPrey category URL by searching for a title and looking at
the page source, or try:
- `https://www.naijaprey.tv/category/action/`
- `https://www.naijaprey.tv/category/hollywood/`

Expected: app should print "Category Hub Page detected. Processing N post(s)..."
and recurse. Record how many posts it finds and whether it actually processes them.

### Test C — NaijaVault /category/ pages (should recurse)
Try:
- `https://naijavault.com/category/hollywood-movies/`
- `https://naijavault.com/category/action/`

Same expectation as Test B.

### Record (CSV)
```
url, page_type, what_happened, post_count_if_recursed,
first_post_url_if_recursed, error_if_any
```

**`what_happened` values:** `recursed_into_posts` / `no_links_found` /
`no_episode_links_message` / `error` / `other_describe`

---

## IMPORTANT — do not mock or estimate any values

Every number in the CSV must come from actual observation:
- Timing: use `time python main.py` or a stopwatch
- URLs: copy the actual URL from the app output or page source
- Post counts: count what the app actually prints
- Resolver outcomes: copy the exact text the app printed

If a test fails (site down, 403, timeout), record `site_unavailable` and
the error message. Do not fill in plausible-looking fake data.
