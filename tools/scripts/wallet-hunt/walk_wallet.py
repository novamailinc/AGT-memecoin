#!/usr/bin/env python3
"""Desc-walk a wallet's signatures via getTransactionsForAddress.
Usage: walk_wallet.py <wallet> <tag> <max_sigs> [days]
Direction: DESC (newest first) — the task bound is 'last 30 days OR first
10,000 signatures'; desc reaches the recent window immediately and stops at
the time cutoff, whereas asc from account creation could burn the whole
budget on ancient history before touching the last 30 days.
Checkpoints every page to <tag>_sigs.json + <tag>_walk_progress.json."""
import json, os, sys, time
sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
import hunt2

OUT = os.path.expanduser("~/workspace/wallet-hunt")

def main():
    wallet, tag = sys.argv[1], sys.argv[2]
    max_sigs = int(sys.argv[3])
    days = float(sys.argv[4]) if len(sys.argv) > 4 else 30.0
    cutoff = int(time.time()) - int(days * 86400)
    sig_path = f"{OUT}/{tag}_sigs.json"
    prog_path = f"{OUT}/{tag}_walk_progress.json"
    sigs, token, done_pages = [], None, 0
    if os.path.exists(prog_path) and os.path.exists(sig_path):
        prog = json.load(open(prog_path))
        if not prog.get("bound"):  # only resume an unfinished walk
            token, done_pages = prog.get("token"), prog.get("pages", 0)
            sigs = json.load(open(sig_path))
            print(f"resume walk {tag} at {len(sigs)} sigs, page {done_pages}", flush=True)
    hit_bound = None
    while True:
        opts = {"sortOrder": "desc", "limit": 100}
        if token:
            opts["paginationToken"] = token
        res = hunt2.rpc("getTransactionsForAddress", [wallet, opts])
        data = (res or {}).get("data") or []
        if not data:
            hit_bound = "exhausted (no more signatures)"
            break
        sigs.extend({"signature": e.get("signature"), "blockTime": e.get("blockTime"),
                     "err": e.get("err")} for e in data)
        _pk = "pagination" + "Token"
        token = (res or {}).get(_pk)
        done_pages += 1
        json.dump(sigs, open(sig_path, "w"))
        json.dump({"pages": done_pages, "token": token, "count": len(sigs)}, open(prog_path, "w"))
        last_ts = data[-1].get("blockTime") or 0
        if done_pages % 10 == 0:
            print(f"  {tag} page {done_pages}: {len(sigs)} sigs last_ts {last_ts}", flush=True)
        if len(sigs) >= max_sigs:
            hit_bound = f"max_sigs {max_sigs}"
            break
        if last_ts and last_ts < cutoff:
            hit_bound = f"time cutoff {cutoff} (30d)"
            break
        if not token:
            hit_bound = "exhausted (no more signatures)"
            break
        time.sleep(0.05)
    json.dump({"pages": done_pages, "token": token, "count": len(sigs), "bound": hit_bound},
              open(prog_path, "w"))
    print(f"{tag} WALK DONE: {len(sigs)} sigs, bound={hit_bound}", flush=True)

if __name__ == "__main__":
    main()
