#!/usr/bin/env python3
"""One-off diagnostic: where do Base/ETH/locked-liq candidates die in the funnel?
Uses the feed scripts' own stage functions. Read-only: no cards, no state writes."""
import os, sys, json
from collections import Counter

os.environ.setdefault("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"

def load_feed(name):
    path = f"{REPO}/scripts/{name}"
    src = open(path).read().split('if __name__ == "__main__":')[0]
    g = {"__name__": "diag", "__file__": path}
    exec(compile(src, path, "exec"), g)
    return g

def funnel(name, pages):
    g = load_feed(name)
    pools = g["get_pools"](pages)
    c = Counter()
    c["fetched"] = len(pools)
    survivors = []
    for p in pools:
        ok, why = g["passes_prefilter"](p)
        if not ok:
            c[f"prefilter:{why}"] += 1; continue
        ok, why = g["passes_manipulation"](p)
        if not ok:
            c[f"manipulation:{why}"] += 1; continue
        c["passed_pre_stages"] += 1
        survivors.append(p)
    # safety on a sample (expensive): first 8 survivors
    for p in survivors[:8]:
        safe, reason, intel = g["check_safety"](p["mint"])
        c["safety_pass" if safe else f"safety_block:{reason}"] += 1
        if not safe: continue
        ev = g["positive_evidence"](p)
        if not ev:
            c["no_positive_evidence"] += 1; continue
        ok, why = g["continuation_ok"](p)
        if not ok:
            c[f"continuation:{why}"] += 1; continue
        score, reasons = g["score_pool"](p, ev)
        c["score_pass" if score >= g["MIN_SCORE"] else "score_fail"] += 1
        if score >= g["MIN_SCORE"]:
            c["WINNERS"] += 1
            print(f"  WINNER {name}: {p.get('name')} score={score:.0f}")
    print(f"== {name} funnel (pages {pages}) ==")
    for k, v in c.most_common():
        print(f"  {k}: {v}")
    # dex distribution of fetched pools
    dexes = Counter(p.get("dex", "?") for p in pools)
    print(f"  dex mix: {dict(dexes.most_common(8))}")
    ages = [p.get("age_min", -1) for p in pools]
    ages = [a for a in ages if a >= 0]
    if ages:
        print(f"  age_min: min={min(ages):.0f} med={sorted(ages)[len(ages)//2]:.0f} max={max(ages):.0f}")

if __name__ == "__main__":
    funnel("base-runner-feed", (1, 2, 3, 4))
    print()
    funnel("eth-runner-feed", (1, 2, 3))
