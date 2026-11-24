#!/usr/bin/env python3
"""Focused top-up check for the 18 DKYC fingerprint hits only (verdict-critical).
Same method as dkyc_funding.py main(): getSignaturesForAddress paging (newest
first, stop below first_buy-86400), fetch v1, find_inflow. Writes DKYC_topup_hits.json."""
import json, os, sys
sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
from funding_trace import paced_rpc, fetch_tx_v1, find_inflow
W = os.path.expanduser("~/workspace/wallet-hunt")
buyers = json.load(open(f"{W}/DKYC_buyers.json"))
stats = buyers["stats"]
ref = json.load(open(f"{W}/DKYC_refsets.json"))
lineage = {ref["treasury"], ref["seeder"], ref["dispatcher"]}
farm = set(ref["farm_shells"]); bundle = set(ref["kimchi_bundle_28s"])
out = {}
for h in buyers["fingerprint_hits"]:
    w = h["wallet"]
    fb = stats[w]["first_buy_ts"]
    lo, hi = fb - 86400, fb
    sigs, before = [], None
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
        hit = find_inflow(w, tx) if tx else None
        if hit:
            inflows.append({"funder": hit[0], "amount_sol": hit[1], "how": hit[2], "ts": bt, "sig": s["signature"]})
    ext = [x for x in inflows if x["funder"] != w]
    farm_linked = [x for x in ext if x["funder"] in lineage or x["funder"] in farm or x["funder"] in bundle]
    out[w] = {"first_buy_ts": fb, "n_inflows": len(inflows), "n_external": len(ext),
              "external": ext, "farm_linked_topups": farm_linked}
    print(f"{w} inflows={len(inflows)} external={len(ext)} farm_linked={len(farm_linked)}", flush=True)
json.dump(out, open(f"{W}/DKYC_topup_hits.json", "w"), indent=1)
print("wrote DKYC_topup_hits.json", flush=True)
