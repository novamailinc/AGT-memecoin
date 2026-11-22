#!/usr/bin/env python3
"""Deep parse of treasury candidate 4p9UoNhA's full (640-sig) history:
outbound SOL transfers, dispatcher funding events, and any direct
interaction with the KIMCHI/KWH/EU1M mints."""
import json, os, sys, time
sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
from concurrent.futures import ThreadPoolExecutor
import funding_trace as ft
import parse_gentle

OUT = os.path.expanduser("~/workspace/wallet-hunt")
ADDR = "4p9UoNhARWTkEwnZ1z2gUTbagd4ixUA1e5HyE5RjEDxW"
MINTS = {"KIMCHI": "7thgKgtK1rp1UtLpWUZEuu9vakedTMoZzuyps6RGpump",
         "KWH": "5hyyjwZqJgnPZorMdb7X4r2J1dT3VujKVkLD8Ptbpump",
         "EU1M": "EU1MHvQvBoJ7DnLorurze1hwR2JWuyoKyAmkwymwpump"}
SYSTEM = "11111111111111111111111111111111"

def all_sigs(addr):
    out, before = [], None
    while True:
        params = [addr, {"limit": 1000}]
        if before:
            params[1]["before"] = before
        page = ft.paced_rpc("getSignaturesForAddress", params) or []
        if not page:
            break
        out.extend(page)
        if len(page) < 1000:
            break
        before = page[-1]["signature"]
    return out

def analyze(tx):
    if not tx:
        return None
    meta = tx.get("meta") or {}
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [a.get("pubkey") if isinstance(a, dict) else a
            for a in (msg.get("accountKeys") or [])]
    instrs = list(msg.get("instructions") or [])
    for inner in meta.get("innerInstructions") or []:
        instrs.extend(inner.get("instructions") or [])
    transfers = []
    for ins in instrs:
        parsed = ins.get("parsed") or {}
        if not isinstance(parsed, dict):
            continue
        info = parsed.get("info") or {}
        if parsed.get("type") in ("transfer", "transferChecked") \
                and info.get("source") == ADDR:
            transfers.append({"to": info.get("destination"),
                              "sol": (info.get("lamports") or
                                      (info.get("amount") or 0)) / 1e9
                              if info.get("lamports") else
                              (info.get("tokenAmount") or {}).get("uiAmount")})
    mint_hits = {}
    for name, mint in MINTS.items():
        evs = [e for e in parse_gentle.events_of(mint, "", tx.get("blockTime"), tx)
               if e["owner"] == ADDR]
        if evs:
            mint_hits[name] = {"n": len(evs),
                               "net_tokens": round(sum(e["token_delta"] for e in evs))}
    return {"transfers": transfers, "mint_hits": mint_hits,
            "err": bool(meta.get("err"))}

def main():
    sigs = all_sigs(ADDR)
    ok = [s for s in sigs if not s.get("err")]
    print(f"{len(sigs)} sigs ({len(ok)} ok)", flush=True)
    with ThreadPoolExecutor(max_workers=3) as ex:
        txs = list(ex.map(ft.fetch_tx_v1, [s["signature"] for s in ok]))
    big_transfers, mint_activity, dispatcher_flows = [], {}, []
    for s, tx in zip(ok, txs):
        a = analyze(tx)
        if not a:
            continue
        ts = s.get("blockTime")
        for t in a["transfers"]:
            if t["sol"] and t["sol"] >= 5:
                big_transfers.append({"ts": ts, "to": t["to"], "sol": t["sol"],
                                      "sig": s["signature"]})
            if t["to"] == "9n66tKbFpGvTybzpRUB6pzdCpUCVk4gmV92G45wqo5rZ":
                dispatcher_flows.append({"ts": ts, "sol": t["sol"],
                                         "dir": "out", "sig": s["signature"]})
        for name, hit in a["mint_hits"].items():
            mint_activity.setdefault(name, []).append(
                {"ts": ts, **hit, "sig": s["signature"]})
    # inbound from dispatcher
    print(f"\nbig outbound transfers (>=5 SOL): {len(big_transfers)}")
    for t in sorted(big_transfers, key=lambda x: -x["sol"])[:25]:
        print(f"  ts={t['ts']} to={t['to']} sol={t['sol']:.2f}")
    print(f"\nflows to 9n66tKbF: {dispatcher_flows}")
    print(f"\ndirect mint activity by 4p9 itself:")
    for name, hits in mint_activity.items():
        print(f"  {name}: {len(hits)} txs, net {sum(h['net_tokens'] for h in hits):,} tokens")
        for h in hits[:6]:
            print(f"     ts={h['ts']} net={h['net_tokens']:,}")
    json.dump({"big_transfers": big_transfers, "mint_activity": mint_activity,
               "dispatcher_flows": dispatcher_flows},
              open(f"{OUT}/funding_deep_4p9.json", "w"), indent=1)
    print("\nsaved funding_deep_4p9.json")

if __name__ == "__main__":
    main()
