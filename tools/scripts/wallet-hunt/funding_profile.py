#!/usr/bin/env python3
"""Profile a funder address: balance, activity rate, age, and a sample of
who it funds (distinct recipients in its recent txs + overlap with the
candidate set). Usage: funding_profile.py <address> [label]"""
import json, os, sys, time
sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
import funding_trace as ft

OUT = os.path.expanduser("~/workspace/wallet-hunt")
SYSTEM = "11111111111111111111111111111111"

def outbound_recipients(funder, tx):
    """Wallets that received SOL from `funder` in tx (transfers, account
    creations, wSOL-close sweeps where funder paid)."""
    if not tx:
        return set()
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return set()
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [a.get("pubkey") if isinstance(a, dict) else a
            for a in (msg.get("accountKeys") or [])]
    fee_payer = keys[0] if keys else None
    instrs = list(msg.get("instructions") or [])
    for inner in meta.get("innerInstructions") or []:
        instrs.extend(inner.get("instructions") or [])
    out = set()
    for ins in instrs:
        parsed = ins.get("parsed") or {}
        if not isinstance(parsed, dict):
            continue
        info = parsed.get("info") or {}
        typ = parsed.get("type")
        if typ == "transfer" and info.get("source") == funder:
            if info.get("destination"):
                out.add(info["destination"])
        elif typ in ("createAccount", "createAccountWithSeed") \
                and (info.get("source") == funder or fee_payer == funder):
            if info.get("newAccount"):
                out.add(info["newAccount"])
        elif typ == "closeAccount" and fee_payer == funder:
            if info.get("destination"):
                out.add(info["destination"])
    out.discard(funder)
    return out

def main():
    addr = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else ""
    prof = {"address": addr, "label": label}
    bal = ft.paced_rpc("getBalance", [addr])
    prof["balance_sol"] = (bal or {}).get("value", 0) / 1e9 if bal else None
    # age: first-ever tx
    r = ft.paced_rpc("getTransactionsForAddress",
                     [addr, {"sortOrder": "asc", "limit": 100}])
    data = (r or {}).get("data") or []
    prof["first_tx_ts"] = data[0].get("blockTime") if data else None
    prof["history_complete_on_page1"] = bool(data) and not r.get("paginationToken")
    # activity: newest 1000 sigs
    sigs = ft.paced_rpc("getSignaturesForAddress", [addr, {"limit": 1000}]) or []
    prof["sigs_sampled"] = len(sigs)
    if sigs:
        ts = [s.get("blockTime") or 0 for s in sigs]
        prof["newest_sig_ts"] = max(ts)
        prof["oldest_of_newest1000_ts"] = min(ts)
        span_h = (max(ts) - min(ts)) / 3600.0
        prof["newest1000_span_hours"] = round(span_h, 2)
        prof["tx_per_day_est"] = round(len(sigs) / max(span_h, 0.01) * 24) \
            if span_h > 0 else None
    # fundee sample from recent txs
    cands = {c["wallet"] for c in
             json.load(open(f"{OUT}/funding_candidates.json"))["candidates"]}
    sample = sigs[:40]
    recipients = set()
    n_parsed = 0
    for s in sample:
        tx = ft.fetch_tx_v1(s["signature"])
        if tx:
            n_parsed += 1
            recipients |= outbound_recipients(addr, tx)
    prof["fundee_sample"] = {"txs_parsed": n_parsed,
                             "distinct_recipients": len(recipients),
                             "recipients_in_candidate_set":
                                 sorted(recipients & cands),
                             "recipients_sample": sorted(recipients)[:50]}
    print(json.dumps(prof, indent=1))
    json.dump(prof, open(f"{OUT}/funding_profile_{addr[:8]}.json", "w"), indent=1)

if __name__ == "__main__":
    main()
