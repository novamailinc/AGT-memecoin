#!/usr/bin/env python3
"""Resumable multi-mint event parser for ONE wallet.
Usage: parse_wallet.py <wallet> <tag>
Reads <tag>_sigs.json (from walk_wallet.py), fetches each tx via
parse_gentle.fetch_tx (shared adaptive pacer), extracts the wallet's
per-mint token deltas + its SOL delta per tx — the hunt event shape
{sig, ts, owner, token_delta, sol_delta} plus a "mint" field.
Saves events + progress every 250 completions; resumable."""
import json, os, sys, time
sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
from concurrent.futures import ThreadPoolExecutor, as_completed
import parse_gentle

OUT = os.path.expanduser("~/workspace/wallet-hunt")

def wallet_events(wallet, sig, ts, tx):
    if not tx:
        return []
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []
    def amt(b):
        try:
            return int(b["uiTokenAmount"]["amount"]) / (10 ** int(b["uiTokenAmount"]["decimals"]))
        except Exception:
            return 0.0
    pre, post = {}, {}
    for b in meta.get("preTokenBalances") or []:
        if b.get("owner") == wallet and b.get("mint"):
            pre[b["mint"]] = pre.get(b["mint"], 0.0) + amt(b)
    for b in meta.get("postTokenBalances") or []:
        if b.get("owner") == wallet and b.get("mint"):
            post[b["mint"]] = post.get(b["mint"], 0.0) + amt(b)
    keys = (tx.get("transaction", {}).get("message", {}) or {}).get("accountKeys") or []
    acct = [a.get("pubkey") if isinstance(a, dict) else a for a in keys]
    pre_b, post_b = meta.get("preBalances") or [], meta.get("postBalances") or []
    sol = None
    if wallet in acct:
        idx = acct.index(wallet)
        if idx < len(pre_b) and idx < len(post_b):
            sol = (post_b[idx] - pre_b[idx]) / 1e9
    evs = []
    for mint in set(pre) | set(post):
        d = post.get(mint, 0.0) - pre.get(mint, 0.0)
        if d == 0:
            continue
        evs.append({"sig": sig, "ts": ts, "owner": wallet, "mint": mint,
                    "token_delta": d, "sol_delta": sol})
    return evs

def main():
    wallet, tag = sys.argv[1], sys.argv[2]
    sigs = [s for s in json.load(open(f"{OUT}/{tag}_sigs.json")) if not s.get("err")]
    ev_path = f"{OUT}/{tag}_events.json"
    prog_path = f"{OUT}/{tag}_parse_progress.json"
    events, done = [], 0
    if os.path.exists(prog_path):
        done = json.load(open(prog_path))["done"]
        events = json.load(open(ev_path)) if os.path.exists(ev_path) else []
        print(f"resume parse {tag} at {done}/{len(sigs)}", flush=True)
    todo = sigs[done:]
    print(f"{tag}: parsing {len(todo)} txs (total ok {len(sigs)})", flush=True)
    t0 = time.time(); completed = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(parse_gentle.fetch_tx, s["signature"]): s for s in todo}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                tx = fut.result()
            except Exception:
                tx = None
            events.extend(wallet_events(wallet, s["signature"], s.get("blockTime"), tx))
            completed += 1
            if completed % 250 == 0:
                json.dump(events, open(ev_path, "w"))
                json.dump({"done": done + completed}, open(prog_path, "w"))
                el = time.time() - t0
                print(f"  progress {done+completed}/{len(sigs)} "
                      f"({completed/el:.2f}/s, interval {parse_gentle.PACER.interval:.2f}s) "
                      f"events={len(events)}", flush=True)
    json.dump(events, open(ev_path, "w"))
    json.dump({"done": len(sigs)}, open(prog_path, "w"))
    print(f"{tag} PARSE DONE: {len(events)} events from {len(sigs)} txs", flush=True)

if __name__ == "__main__":
    main()
