# ANONRODE — Master Field Test Prompt for Antigravity

## Context
You are testing a Python terminal media downloader at `~/download-toolkit`
(or `/storage/emulated/0/download-toolkit` on Termux/Android).
Run it with: `python main.py`

Commands used in this test:
- `search <title>` — full search (async, all sources)
- `fsearch <title>` — fast search (wave1 slug-probe only, skips RSS)
- Pick `0` to cancel at every download prompt — DO NOT download anything.

Sites searched: 9jaRocks (my9jarocks.bz), NaijaPrey (naijaprey.tv),
NaijaVault (naijavault.com), PlutoMovies (plutomovies.com),
NKiri (thenkiri.com), DramaKey.cc, DramaKey.com, DramaRain (dramarain.com).

---

## TEST SUITE 1 — Search quality across 100 titles

For each title below, run `search <title>`, record results, pick 0.

### Titles to test
Fast and Furious, Avengers, Spider-Man, Batman, Iron Man, Thor, Black Panther,
Jurassic Park, Mission Impossible, James Bond, Harry Potter, Lord of the Rings,
Transformers, Pirates of the Caribbean, The Matrix, John Wick, The Hobbit,
X-Men, Deadpool, Guardians of the Galaxy, Captain America, Doctor Strange,
Ant-Man, Aquaman, Wonder Woman, Suicide Squad, The Dark Knight, Inception,
Interstellar, Tenet, Oppenheimer, Barbie, Top Gun, Avatar, Titanic, Gladiator,
The Godfather, Scarface, Goodfellas, Pulp Fiction, Fight Club, Shawshank Redemption,
Forrest Gump, The Lion King, Toy Story, Finding Nemo, Shrek, Despicable Me,
Minions, Kung Fu Panda, How to Train Your Dragon, Moana, Frozen, Encanto,
Coco, Soul, Luca, Turning Red, Lightyear, Elemental, Raya, Onward, Brave,
Tangled, Wreck-It Ralph, Zootopia, Big Hero 6, Inside Out, The Incredibles,
Cars, Up, Wall-E, Megamind, The Bad Guys, Puss in Boots, Madagascar,
Monsters Inc, Mulan, Aladdin, Beauty and the Beast, The Little Mermaid,
Cinderella, Pinocchio, Dumbo, Naruto, Dragon Ball, One Piece, Attack on Titan,
Demon Slayer, My Hero Academia, Death Note, Bleach, Fullmetal Alchemist,
Money Heist, Squid Game, Stranger Things, Breaking Bad, Game of Thrones,
The Witcher, Peaky Blinders, Prison Break, 24, Power

### Record per title (CSV output)
```
title, result_count, sites_returned, has_collection_result, collection_keywords_found,
results_in_numeric_order, dead_links_in_results, pluto_series_vs_episode_noise,
search_time_seconds, any_error_or_crash
```

**Field definitions:**
- `sites_returned` — comma-separated list e.g. "9jaRocks,NaijaPrey,PlutoMovies"
- `has_collection_result` — true if any result title contains: collection, complete,
  saga, all parts, season X-Y, or a year range like 2001-2017
- `collection_keywords_found` — the actual keyword(s) found e.g. "Collection", "Complete"
- `results_in_numeric_order` — for franchises with numbered entries (1,2,3...),
  are they listed 1→N or jumbled? Values: ordered / jumbled / N/A
- `dead_links_in_results` — did the app print "[!] File removed by host" during
  the search or immediately on picking? true/false
- `pluto_series_vs_episode_noise` — for PlutoMovies results, count how many are
  `/series/` level vs individual episode pages (e.g. "2 series, 1 episode")
- `search_time_seconds` — wall-clock seconds from hitting Enter to results appearing

---

## TEST SUITE 2 — Collection/hub page detection

For each title that returned a collection result in Suite 1, pick that result (then
cancel the download). Record what the extractor does with it.

Also manually test these known collection URLs by pasting them directly at the `>` prompt:
- `https://www.my9jarocks.bz/videodownload/fast-and-furious-2001-2017-collection-id163674.html`
- Any 9jaRocks result whose title contains "Collection" or "Complete"

### Record per collection pick
```
title, url, extractor_used, files_found_count, any_dead_links_skipped,
error_message_if_any
```

---

## TEST SUITE 3 — Tag/category hub recursion

NaijaPrey has `/tag/` hub pages (e.g. `/tag/fast-furious-series/`) and NaijaVault
has `/category/` pages. The app currently handles `/category/` recursion but NOT `/tag/`.

For each of these, paste the URL directly at the `>` prompt and record what happens:
- `https://www.naijaprey.tv/tag/fast-furious-series/`
- `https://www.naijaprey.tv/tag/avengers/` (or whatever tag exists for Avengers)
- Any NaijaVault `/category/` URL you find in search results

### Record
```
url, what_happened (recursed_into_posts / no_links_found / error / other),
post_count_if_recursed
```

---

## TEST SUITE 4 — Dead link detection

9jaRocks sometimes links directly to `loadedfiles.net/error?e=File+has+been+removed.`
The app now skips these before resolving. Verify this works at scale.

For 10 random 9jaRocks results from Suite 1, pick each one (cancel download after
the extractor runs). Record:
```
title, url, dead_links_skipped_count, message_printed, files_actually_attempted
```

---

## TEST SUITE 5 — Search speed: full vs fast

For 10 titles (mix of Hollywood movies and Nigerian content), run both
`search <title>` and `fsearch <title>`. Record:
```
title, full_search_seconds, fast_search_seconds, full_result_count, fast_result_count,
results_differ (yes/no), which_sites_missing_in_fast
```

Expected: `fsearch` should be faster but may miss RSS-only sources (NaijaVault,
NaijaPrey, 9jaRocks) since they don't appear in slug-probe wave1.

---

## TEST SUITE 6 — NaijaVault "no links" diagnosis

NaijaVault now prints a smarter message when a page has no download links
(detects iframes/embeds and "coming soon" text). Test this:

1. Search for a title where NaijaVault returns a result
2. Pick the NaijaVault result
3. If it says "No download links found", record the full message printed

```
title, naijavault_url, message_printed, had_iframe_detected, had_coming_soon_detected
```

---

## TEST SUITE 7 — Resolver robustness spot-check

For 5 titles where 9jaRocks returns results with loadedfiles links, pick the result
and let the resolver run (cancel before actual download starts if possible, or let
it resolve and then Ctrl+C). Record:

```
title, loadedfiles_url, resolver_outcome (resolved/null/error/dead_skipped),
error_message_if_any, time_to_resolve_seconds
```

---

## TEST SUITE 8 — Config and state sanity

After running all the above searches (no downloads), check:

1. Does `.search_cache.json` exist in `src/`? How many entries?
   Run: `python -c "import json; d=json.load(open('src/.search_cache.json')); print(len(d))"`

2. Is `.resume_state.json` clean (no phantom entries from cancelled picks)?
   Run: `python -c "import json; d=json.load(open(open('src/../').read()))"`
   Actually: check `~/.config/anonrode/.resume_state.json` — should be empty `{}`
   since nothing was downloaded.

3. Does `.download_receipts.json` have any entries from the test run?

Record: `cache_entry_count, resume_state_clean (yes/no), receipts_count`

---

## THINGS I AM SPECIFICALLY CURIOUS ABOUT

These are architectural questions — record observations even if not explicitly
covered by the test suites above:

1. **Result ordering** — are franchise results (Fast Furious 1-8, Spider-Man 1-3)
   ever returned in numeric order, or always jumbled? Is there any pattern to
   which site returns them in order?

2. **PlutoMovies noise** — does PlutoMovies return individual episode pages mixed
   with series-level pages? How bad is the noise for a title like "Avengers"?

3. **NaijaVault 403** — does the app successfully fetch NaijaVault search results
   (it uses a proper session with headers)? Or does it silently return 0 NaijaVault
   results for some queries?

4. **Search cache hit rate** — after running Suite 1 once, run 10 of the same
   titles again. Do they return instantly (cache hit) or re-fetch?

5. **DramaKey/DramaRain results** — do any of the 100 titles return DramaKey or
   DramaRain results? These sites have no server search (slug-probe only) so they
   should only appear for exact-match titles.

6. **"File removed" frequency** — across all 9jaRocks results in Suite 1, roughly
   what % of links are dead (error?e= URLs)? This tells us how stale 9jaRocks'
   content is.

7. **Search crash/hang** — does any title cause the search to hang, crash, or
   return an exception? Record the full traceback if so.

8. **fsearch missing sources** — confirm that `fsearch` never returns NaijaVault,
   NaijaPrey, or 9jaRocks results (these are RSS-only, not slug-probed).

---

## OUTPUT FORMAT

Deliver results as:
1. One CSV file per test suite (suite1.csv, suite2.csv, etc.)
2. A short summary paragraph per suite noting the most interesting findings
3. A final "Top Issues Found" section listing anything broken, slow, or surprising

Do not include download file contents. Do not store any media. Cancel all download
prompts with `0` or Ctrl+C.
