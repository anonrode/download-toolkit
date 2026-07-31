# download-toolkit — agent entry point

**Read the full operating charter before touching anything: [`.agents/AGENTS.md`](.agents/AGENTS.md).**

That file is the authoritative brief for any coding agent working on this repo (Claude, Gemini/Antigravity, or other). It covers the architecture, the extractor/resolver contracts, the network-resilience and resume invariants, what "verified" means here (this project has no CI or test suite), the exact failure modes prior edits have hit, and — in §10/§11 — the current site+resolver map and the freshest traps (asianc chain, mirror resolvers, quality cap, HLS resume purge, size display).

Hard rules that override any default:
- **Never add AI attribution to commits** (no `Co-Authored-By`, no "Generated with"). Commit messages describe the change only.
- **Verify against the live world before coding against it** — every endpoint, selector, and JSON field. A silent `return None` here degrades into a failed episode with no stack trace.
- **`src/downloader.py` is ~3900 lines — never bulk-read it.** Use narrow `grep -n` / small ranges.
- **Ask before pushing** and before large refactors.
