#!/usr/bin/env python3
"""Merge 7JzHa (asc) + 7JzHd (desc) event files into tag 7JzH for analysis.
Dedupes by (sig, mint). Usage: merge_7jzh.py"""
import json, os
OUT = os.path.expanduser("~/workspace/wallet-hunt")

seen, merged = set(), []
for tag in ("7JzHa", "7JzHd"):
    p = f"{OUT}/{tag}_events.json"
    if not os.path.exists(p):
        print(f"{tag}: no events file yet")
        continue
    evs = json.load(open(p))
    new = 0
    for e in evs:
        k = (e["sig"], e["mint"])
        if k in seen:
            continue
        seen.add(k)
        merged.append(e)
        new += 1
    print(f"{tag}: {len(evs)} events, {new} new")
json.dump(merged, open(f"{OUT}/7JzH_events.json", "w"))
# sigs file for analyze_wallet (same list either direction)
import shutil
shutil.copy(f"{OUT}/7JzHa_sigs.json", f"{OUT}/7JzH_sigs.json")
a = json.load(open(f"{OUT}/7JzHa_parse_progress.json"))["done"] if os.path.exists(f"{OUT}/7JzHa_parse_progress.json") else 0
d = json.load(open(f"{OUT}/7JzHd_parse_progress.json"))["done"] if os.path.exists(f"{OUT}/7JzHd_parse_progress.json") else 0
print(f"asc done={a} desc done={d} union_txs~{min(9950, a + d)} merged events={len(merged)}")
