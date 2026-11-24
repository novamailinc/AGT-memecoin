#!/usr/bin/env python3
"""3SVJ funding analysis via Solana PUBLIC RPC (Helius capped).
Birth funding for every >=1M candidate + top-up variant for
fingerprint/suffix suspects. Reuses funding_trace.find_inflow
(pure function on standard getTransaction output; no Helius use).
Input: 3SVJ_buyers.json. Output: 3SVJ_funding.json (resumable).
"""
import json, os, sys, time, urllib.request, urllib.error

W = os.path.expanduser("~/workspace/wallet-hunt")
sys.path.insert(0, W)
from funding_trace import find_inflow

RPC = "https://api.mainnet-beta.solana.com"
MIG = 1792871066
MAX_WALK_PAGES = 60
MAX_TOPUP_TXS = 200

def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for i in range(3):
        try:
            req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as r:
                j = json.loads(r.read())
            if "error" in j:
                return None
            return j.get("result")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                if i == 2: return "RATE_LIMITED"
                time.sleep(10 * (i + 1))
            else:
                time.sleep(5)
        except Exception:
            time.sleep(5)
    return None

def fetch_tx(sig):
    return rpc("getTransaction", [sig, {"encoding": "jsonParsed",
                                        "maxSupportedTransactionVersion": 1}])

def oldest_sigs(wallet):
    """Walk wallet sigs to the oldest page. Returns (list, reached_end)."""
    all_sigs, before = [], None
    for _ in range(MAX_WALK_PAGES):
        cfg = {"limit": 1000}
        if before: cfg["before"] = before
        res = rpc("getSignaturesForAddress", [wallet, cfg])
        if res == "RATE_LIMITED" or not res:
            return all_sigs, False
        all_sigs.extend(res)
        before = res[-1]["signature"]
        time.sleep(0.4)
        if len(res) < 1000:
            return all_sigs, True
    return all_sigs, False

def birth(wallet):
    sigs, reached_end = oldest_sigs(wallet)
    if not sigs:
        return {"wallet": wallet, "funder": None, "error": "no sigs walked"}
    # Scan forward from oldest: first SOL inflow = birth funding.
    asc = sorted(sigs, key=lambda s: (s.get("slot", 0)))
    tried = 0
    for s in asc:
        if s.get("err"): continue
        tried += 1
        if tried > 30: break
        tx = fetch_tx(s["signature"])
        hit = find_inflow(wallet, tx) if tx else None
        if hit:
            funder, amt, how = hit
            return {"wallet": wallet, "funder": funder, "fund_amount_sol": amt,
                    "funding_ts": s.get("blockTime"), "funding_sig": s["signature"],
                    "fund_how": how, "reached_end": reached_end,
                    "total_sigs_walked": len(sigs)}
        time.sleep(0.3)
    return {"wallet": wallet, "funder": None,
            "error": f"no inflow in first {tried} oldest txs",
            "reached_end": reached_end, "total_sigs_walked": len(sigs)}

def topups(wallet, first_buy_ts):
    """SOL inflows in the 24h before first_buy_ts. Returns funder histogram."""
    funders, before, seen = {}, None, 0
    for _ in range(30):
        cfg = {"limit": 1000}
        if before: cfg["before"] = before
        res = rpc("getSignaturesForAddress", [wallet, cfg])
        if res == "RATE_LIMITED" or not res: break
        stop = False
        for s in res:
            bt = s.get("blockTime") or 0
            if bt < first_buy_ts - 86400:
                stop = True
                break
            if bt <= first_buy_ts and not s.get("err"):
                seen += 1
                if seen > MAX_TOPUP_TXS:
                    stop = True
                    break
                tx = fetch_tx(s["signature"])
                hit = find_inflow(wallet, tx) if tx else None
                if hit:
                    funders[hit[0]] = funders.get(hit[0], 0) + 1
                time.sleep(0.2)
        before = res[-1]["signature"]
        time.sleep(0.4)
        if stop: break
    return {"wallet": wallet, "topup_funders": funders, "txs_checked": seen}

def main():
    buyers = json.load(open(os.path.join(W, "3SVJ_buyers.json")))
    refs = json.load(open(os.path.join(W, "DKYC_refsets.json")))
    lineage = {refs["treasury"], refs["seeder"], refs["dispatcher"]}
    farm, bundle = set(refs["farm_shells"]), set(refs["kimchi_bundle_28s"])
    cands = buyers["candidates"]
    stats = buyers["stats"]
    out_path = os.path.join(W, "3SVJ_funding.json")
    done = {}
    if os.path.exists(out_path):
        try: done = {d["wallet"]: d for d in json.load(open(out_path))}
        except Exception: done = {}
    # Seed from priority traces (same birth() results, avoid re-tracing).
    prio_path = os.path.join(W, "3SVJ_funding_priority.json")
    if os.path.exists(prio_path):
        try:
            for d in json.load(open(prio_path)):
                done.setdefault(d["wallet"], d)
        except Exception: pass
    todo = [c for c in cands if c not in done]
    print(f"birth-tracing {len(todo)} candidates ({len(cands)} total)", flush=True)
    for i, w in enumerate(todo):
        r = birth(w)
        r["in_farm_shells"] = w in farm
        r["in_kimchi_bundle"] = w in bundle
        r["funder_in_lineage"] = r.get("funder") in lineage
        done[w] = r
        if i % 10 == 0:
            json.dump(list(done.values()), open(out_path, "w"))
            print(f"traced {i + 1}/{len(todo)}", flush=True)
    json.dump(list(done.values()), open(out_path, "w"))
    # Top-up variant for fingerprint hits + suffix matches.
    hits = [h["wallet"] for h in buyers.get("attribution_hits", [])]
    for suf in ("Qxdc", "WPvD"):
        hits += [o for o in stats if o.endswith(suf)]
    hits = sorted(set(hits))
    print(f"top-up variant for {len(hits)} suspects", flush=True)
    tops = []
    for w in hits:
        fbt = stats[w]["first_buy_ts"]
        if fbt is None: continue
        t = topups(w, fbt)
        t["topup_funder_in_lineage"] = any(f in lineage for f in t["topup_funders"])
        tops.append(t)
        print(f"  {w}: funders={t['topup_funders']}", flush=True)
    json.dump({"birth": list(done.values()), "topups": tops},
              open(os.path.join(W, "3SVJ_funding_full.json"), "w"))
    print("DONE", flush=True)

if __name__ == "__main__":
    main()
