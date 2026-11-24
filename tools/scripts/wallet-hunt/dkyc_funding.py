#!/usr/bin/env python3
"""DKYC funding analysis: birth funding for every >=1M candidate, farm
lineage flags, dispatcher-pattern detection (common funder histogram),
and the top-up variant for suspects (SOL inflows in the 24h before
their first DKYC buy, via getSignaturesForAddress + find_inflow).
Reuses funding_trace.trace_wallet / find_inflow / paced_rpc (v1-aware,
discovery key via parse_gentle.RPC).
Input: DKYC_buyers.json. Output: DKYC_funding.json + printed summary.
Resumable: reuses DKYC_funding.json entries already present.
"""
import json, os, sys, time

sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
import funding_trace
from funding_trace import paced_rpc, fetch_tx_v1, find_inflow

W = os.path.expanduser("~/workspace/wallet-hunt")
MIG = 1792070830

def main():
    buyers = json.load(open(os.path.join(W, "DKYC_buyers.json")))
    refs = json.load(open(os.path.join(W, "DKYC_refsets.json")))
    cands = buyers["candidates"]
    stats = buyers["stats"]
    lineage = {refs["treasury"], refs["seeder"], refs["dispatcher"]}
    farm = set(refs["farm_shells"])
    bundle = set(refs["kimchi_bundle_28s"])
    out_path = os.path.join(W, "DKYC_funding.json")
    done = {}
    if os.path.exists(out_path):
        try:
            done = {d["wallet"]: d for d in json.load(open(out_path))}
            print(f"resume: {len(done)} already traced", flush=True)
        except Exception:
            done = {}
    todo = [c for c in cands if c not in done]
    print(f"tracing birth funding for {len(todo)} candidates "
          f"({len(cands)} total)", flush=True)
    for i, w in enumerate(todo):
        try:
            done[w] = funding_trace.trace_wallet(w)
        except Exception as e:
            done[w] = {"wallet": w, "error": f"{type(e).__name__}: {e}"[:200]}
        if (i + 1) % 25 == 0:
            json.dump(list(done.values()), open(out_path, "w"))
            print(f"  traced {i+1}/{len(todo)}", flush=True)
    json.dump(list(done.values()), open(out_path, "w"))

    # flags + dispatcher histogram
    from collections import Counter
    funders = Counter()
    for w, d in done.items():
        f = d.get("funder")
        if f:
            funders[f] += 1
    print("\ncommon funders (>=3 candidates):")
    for f, n in funders.most_common():
        if n >= 3:
            tags = []
            if f in lineage:
                tags.append("LINEAGE-DIRECT")
            if f in farm:
                tags.append("FARM-SHELL")
            if f in bundle:
                tags.append("KIMCHI-BUNDLE")
            print(f"  {f} funds {n} candidates {' '.join(tags)}")
    print("\ndirect lineage hits:")
    n_lineage = 0
    for w, d in done.items():
        f = d.get("funder")
        if f in lineage or f in farm or f in bundle:
            n_lineage += 1
            print(f"  {w} <- {f} ({d.get('fund_amount_sol')} SOL, "
                  f"ts={d.get('funding_ts')}, how={d.get('fund_how')}) "
                  f"{'LINEAGE' if f in lineage else ''}"
                  f"{'FARM-SHELL' if f in farm else ''}"
                  f"{'BUNDLE' if f in bundle else ''}")
    if not n_lineage:
        print("  (none)")
    # suspects for top-up variant
    suspects = set()
    for h in buyers["fingerprint_hits"]:
        suspects.add(h["wallet"])
    for w, d in done.items():
        f = d.get("funder")
        if f in lineage or f in farm or f in bundle or funders.get(f, 0) >= 3:
            suspects.add(w)
    print(f"\nsuspects for top-up variant: {len(suspects)}")
    topups = {}
    for w in sorted(suspects):
        first_buy = stats[w]["first_buy_ts"]
        lo, hi = first_buy - 86400, first_buy
        # getSignaturesForAddress newest-first; page until below lo
        sigs = []
        before = None
        for _ in range(10):
            params = {"limit": 1000}
            if before:
                params["before"] = before
            r = paced_rpc("getSignaturesForAddress", [w, params])
            if not r:
                break
            batch = r if isinstance(r, list) else r.get("data", [])
            if not batch:
                break
            sigs.extend(batch)
            before = batch[-1]["signature"]
            oldest = batch[-1].get("blockTime")
            if oldest and oldest < lo:
                break
        inflows = []
        for s in sigs:
            bt = s.get("blockTime")
            if not bt or not (lo <= bt <= hi) or s.get("err"):
                continue
            tx = fetch_tx_v1(s["signature"])
            hit = find_inflow(w, tx)
            if hit:
                inflows.append({"funder": hit[0], "amount_sol": hit[1],
                                "how": hit[2], "ts": bt, "sig": s["signature"]})
        topups[w] = inflows
        print(f"  {w}: {len(inflows)} pre-buy inflow(s) in 24h window")
        for inf in inflows:
            tags = ("LINEAGE-DIRECT" if inf["funder"] in lineage else
                    "FARM-SHELL" if inf["funder"] in farm else
                    "BUNDLE" if inf["funder"] in bundle else "")
            print(f"    <- {inf['funder']} {inf['amount_sol']} SOL "
                  f"ts={inf['ts']} {inf['how']} {tags}")
    json.dump({"traces": list(done.values()),
               "funder_histogram": funders.most_common(20),
               "topups": topups},
              open(os.path.join(W, "DKYC_funding_full.json"), "w"))
    print("\nwrote DKYC_funding.json + DKYC_funding_full.json")

if __name__ == "__main__":
    main()
