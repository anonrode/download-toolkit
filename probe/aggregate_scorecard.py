"""Aggregate results-verify-<site>.jsonl into a consolidated scorecard.

Usage: python aggregate_scorecard.py [out.json]
Reads every results-verify-*.jsonl in the probe folder, recomputes stage
2/3/4 stats from rows (stage-1 totals come from the run SUMMARY lines),
and prints + saves the scorecard.
"""
import json
import os
import sys
from collections import Counter

PROBE = r"C:\Users\Anon\download-toolkit\probe"

# Stage-1 stats captured from run SUMMARY lines (queries-with-results / total)
S1 = {
    "9jarocks": [48, 48], "naijavault": [49, 49], "asianc": [46, 46],
    "dramarain": [47, 47], "anitaku": [40, 40], "nepu": [44, 44],
    "pluto": [45, 45], "naijaprey": [47, 47], "torrents": [49, 49],
    "nkiri": [0, 50], "dramakey": [0, 25],
}

KIND_LABEL = {
    "OK": "probe OK", "MAGNET": "magnet (N/A)", "RESOLVE-FAILED": "crack failed",
    "NO-CANDIDATE": "no episode links", "PROBE-FAIL": "probe failed",
}


def load_rows(site):
    path = os.path.join(PROBE, f"results-verify-{site}.jsonl")
    rows = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main():
    scorecard = {}
    for site in sorted(os.listdir(PROBE)):
        if not site.startswith("results-verify-") or not site.endswith(".jsonl"):
            continue
        name = site[len("results-verify-"):-len(".jsonl")]
        rows = load_rows(name)
        n = len(rows)
        kinds = Counter(r.get("result") for r in rows)
        s2_ok = sum(1 for r in rows if r.get("s2_ok"))
        s3_ok = sum(1 for r in rows if r.get("s3_ok"))
        s4 = [r.get("s4") for r in rows if r.get("s4")]
        s4_ok = sum(1 for v in s4 if v.get("ok") is True)
        s4_na = sum(1 for v in s4 if v.get("ok") is None)
        s4_fail = sum(1 for v in s4 if v.get("ok") is False)
        probe_kinds = Counter(v.get("kind", "") for v in s4)
        engines = Counter(r.get("s3_engine") for r in rows if r.get("s3_engine"))
        scorecard[name] = {
            "rows": n, "s1": S1.get(name), "s2_ok": s2_ok, "s3_ok": s3_ok,
            "s4_ok": s4_ok, "s4_na": s4_na, "s4_fail": s4_fail,
            "results": dict(kinds), "probe_kinds": dict(probe_kinds),
            "engines": dict(engines),
        }

    out_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(PROBE, "verify-scorecard.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(scorecard, f, indent=2, ensure_ascii=False)

    print(f"{'site':<12}{'S1':>8}{'S2':>7}{'S3':>7}{'S4ok':>7}{'S4na':>6}{'S4fail':>8}   results")
    print("-" * 100)
    for name, s in scorecard.items():
        s1 = f"{s['s1'][0]}/{s['s1'][1]}" if s["s1"] else "  n/a"
        res = ", ".join(f"{k}:{v}" for k, v in sorted(s["results"].items()))
        print(f"{name:<12}{s1:>8}{s['s2_ok']:>5}/{s['rows']:<2}{s['s3_ok']:>5}/{s['rows']:<2}"
              f"{s['s4_ok']:>6}/{s['rows']:<2}{s['s4_na']:>6}{s['s4_fail']:>8}   {res}")
    print("\nSaved:", out_path)


if __name__ == "__main__":
    main()
