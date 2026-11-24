#!/usr/bin/env python3
"""Top-up variant for the 12 priority 3SVJ wallets (public RPC).
For each: SOL inflows in the 24h before its first window buy, with
funders flagged against the farm lineage. Imports topups() from
3SVJ_funding. Output: 3SVJ_topups_priority.json."""
import json, os, sys
sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
from importlib import import_module

W = os.path.expanduser("~/workspace/wallet-hunt")

def main():
    f = import_module("3SVJ_funding")
    buyers = json.load(open(os.path.join(W, "3SVJ_buyers.json")))
    stats = buyers["stats"]
    refs = json.load(open(os.path.join(W, "DKYC_refsets.json")))
    lineage = {refs["treasury"], refs["seeder"], refs["dispatcher"]}
    prio = json.load(open(os.path.join(W, "3SVJ_funding_priority.json")))
    wallets = [d["wallet"] for d in prio]
    out = os.path.join(W, "3SVJ_topups_priority.json")
    done = {}
    if os.path.exists(out):
        try: done = {d["wallet"]: d for d in json.load(open(out))}
        except Exception: done = {}
    for w in wallets:
        if w in done:
            print(f"skip {w} (done)", flush=True)
            continue
        fbt = stats[w]["first_buy_ts"]
        t = f.topups(w, fbt) if fbt else {"wallet": w, "error": "no first buy ts"}
        t["topup_funder_in_lineage"] = any(x in lineage for x in t.get("topup_funders", {}))
        done[w] = t
        json.dump(list(done.values()), open(out, "w"))
        print(f"{w}: funders={t.get('topup_funders')} lineage={t['topup_funder_in_lineage']}", flush=True)
    print("DONE priority top-ups", flush=True)

if __name__ == "__main__":
    main()
