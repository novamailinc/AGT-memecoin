#!/usr/bin/env python3
"""wallet_side.py — decisive per-wallet check: walk each lead wallet's own
tx history (desc, newest first) back across each coin's graduation window
and parse only the in-window txs for that mint's balance deltas.
Complements foot_check.py's mint-side parse."""
import json, os, sys, time
sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
from concurrent.futures import ThreadPoolExecutor
import hunt2
import parse_gentle

OUT = os.path.expanduser("~/workspace/wallet-hunt")
COINS = [
    {"name": "ODIE",   "mint": "6LGGCwCUHDvcipZNtqWrb4MRtsPygNGbUx1df7LYpump", "grad": 1792760440},
    {"name": "SHADOW", "mint": "F9Yz51QZpvS5fWdqgmt8wgEWHjg2E5YdwLvSjsrGpump", "grad": 1792602825},
]
CANDS = ["F9MxbtHQ8998RpMxUw7K7ED8qynTHQxcWuvvtEC7qqp1",
         "EedNgv1wGdCMayQ6znioCgVHjaqo7YBgdwq8GFUoMuRE"]

def desc_walk(addr, t_lo, max_pages=600):
    """Newest->oldest until blockTime < t_lo. Returns (entries, capped)."""
    out, token = [], None
    for page in range(max_pages):
        opts = {"sortOrder": "desc", "limit": 100}
        if token:
            opts["paginationToken"] = token
        res = hunt2.rpc("getTransactionsForAddress", [addr, opts])
        data = (res or {}).get("data") or []
        if not data:
            return out, False
        out.extend(data)
        token = (res or {}).get("paginationToken")
        last_ts = data[-1].get("blockTime") or 0
        if page % 20 == 0:
            print(f"    desc page {page}: total {len(out)} last_ts {last_ts}", flush=True)
        if last_ts and last_ts < t_lo:
            return out, False
        if not token:
            return out, False
        time.sleep(0.05)
    return out, True

def main():
    results = {}
    for w in CANDS:
        results[w] = {}
        for coin in COINS:
            name, mint, grad = coin["name"], coin["mint"], coin["grad"]
            lo, hi = grad - 600, grad + 7200
            print(f"\n== wallet {w[:8]} x {name}: desc walk to {lo}", flush=True)
            entries, capped = desc_walk(w, lo)
            in_win = [e for e in entries if not e.get("err")
                      and e.get("blockTime") and lo <= e["blockTime"] <= hi]
            print(f"  walked {len(entries)} entries (capped={capped}); "
                  f"{len(in_win)} ok txs inside [{lo},{hi}]", flush=True)
            evs = []
            if in_win:
                with ThreadPoolExecutor(max_workers=3) as ex:
                    txs = list(ex.map(parse_gentle.fetch_tx, [e["signature"] for e in in_win]))
                for e, tx in zip(in_win, txs):
                    if tx:
                        evs.extend(x for x in parse_gentle.events_of(mint, e["signature"],
                                                                     e.get("blockTime"), tx)
                                   if x["owner"] == w)
            buys = [x for x in evs if x["token_delta"] > 0]
            sol = sum(-x["sol_delta"] for x in buys if x["sol_delta"] is not None and x["sol_delta"] < 0)
            res = {"walked_entries": len(entries), "capped": capped,
                   "in_window_txs": len(in_win), "mint_events": len(evs),
                   "buys": len(buys),
                   "first_buy_ts": min((x["ts"] for x in buys), default=None),
                   "gross_buy_tokens": round(sum(x["token_delta"] for x in buys)),
                   "sol_spent": round(sol, 4),
                   "buy_events": buys[:10]}
            if res["first_buy_ts"]:
                res["first_buy_offset_s"] = res["first_buy_ts"] - grad
            results[w][name] = res
            print(f"  -> {json.dumps({k: v for k, v in res.items() if k != 'buy_events'})}", flush=True)
    json.dump(results, open(f"{OUT}/wallet_side.json", "w"), indent=1)
    print("WALLET SIDE DONE", flush=True)

if __name__ == "__main__":
    main()
