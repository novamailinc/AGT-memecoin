#!/usr/bin/env python3
"""Aggregate djo_*_events.json into per-wallet / per-coin sample tables.
Usage: djo_analyze.py <wallet> [wallet ...]
Writes djo_sample.json (merged across all wallets given + any existing
djo_*_events.json files) and prints a digest.
SOL in/out per mint: sum of wallet SOL deltas on txs where that mint's
token balance moved. Caveat: SOL delta includes tx fees (typically
<0.01 SOL) and is None when the wallet index could not be resolved;
multiples are computed ONLY on fully-exited mints with buy_sol > 0.
DexScreener migration lags are added by djo_dex.py, not here."""
import json, os, sys
from collections import defaultdict
from datetime import datetime, timezone

OUT = os.path.expanduser("~/workspace/wallet-hunt")
DJOF = "DjoDYTgjHnZb8eFu16MqXdAQbk1xvs4BhRXigr5ieYnB"
MINT_3SVJ = "3SVjZ7BfT9FxiR49nZc7UdBmLgzRLJY7XBZDKhEFpump"

def iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def analyze_wallet(wallet):
    tag = wallet[:6]
    ev_path = f"{OUT}/djo_{tag}_events.json"
    sig_path = f"{OUT}/djo_{tag}_sigs.json"
    if not os.path.exists(ev_path):
        return None
    events = json.load(open(ev_path))
    sigs = json.load(open(sig_path)) if os.path.exists(sig_path) else []
    by_mint = defaultdict(list)
    topups, outflows = [], []
    for e in events:
        for m, d in (e.get("tokens") or {}).items():
            by_mint[m].append(e)
        for t in (e.get("transfers") or []):
            if t["destination"] == wallet and t["source"] != wallet:
                topups.append({"ts": e["ts"], "from": t["source"],
                               "sol": (t["lamports"] or 0) / 1e9, "sig": e["sig"]})
            if t["source"] == wallet and t["destination"] != wallet:
                outflows.append({"ts": e["ts"], "to": t["destination"],
                                 "sol": (t["lamports"] or 0) / 1e9, "sig": e["sig"]})
    mints = {}
    for mint, evs in by_mint.items():
        evs.sort(key=lambda x: (x["ts"] or 0, x["sig"]))
        buys = [e for e in evs if e["tokens"][mint] > 0]
        sells = [e for e in evs if e["tokens"][mint] < 0]
        if not buys:
            continue
        fb = buys[0]
        gross_buy = sum(e["tokens"][mint] for e in buys)
        gross_sell = -sum(e["tokens"][mint] for e in sells)
        buy_sol = sum(-e["sol_delta"] for e in buys
                      if e.get("sol_delta") is not None and e["sol_delta"] < 0)
        buy_sol_n = sum(1 for e in buys if e.get("sol_delta") is not None
                        and e["sol_delta"] < 0)
        sell_sol = sum(e["sol_delta"] for e in sells
                       if e.get("sol_delta") is not None and e["sol_delta"] > 0)
        # full exit: first ts where running balance <= 0.5% of gross buy,
        # after at least one sell
        bal, exit_ts = 0.0, None
        for e in evs:
            bal += e["tokens"][mint]
            if (e["ts"] and e["ts"] >= fb["ts"] and bal <= 0.005 * gross_buy
                    and exit_ts is None
                    and any(s["ts"] and s["ts"] <= e["ts"] for s in sells)):
                exit_ts = e["ts"]
        fully_exited = exit_ts is not None
        mult = None
        if fully_exited and buy_sol > 0:
            mult = sell_sol / buy_sol
        mints[mint] = {
            "first_buy_ts": fb["ts"], "first_buy_iso": iso(fb["ts"]),
            "first_buy_sol": (-fb["sol_delta"] if fb.get("sol_delta") is not None
                              and fb["sol_delta"] < 0 else None),
            "buys": len(buys), "gross_buy_tokens": gross_buy, "buy_sol": round(buy_sol, 4),
            "buy_sol_known_n": buy_sol_n,
            "sells": len(sells), "gross_sell_tokens": gross_sell,
            "sell_sol": round(sell_sol, 4),
            "net_tokens": round(gross_buy - gross_sell, 4),
            "still_holding": round(gross_buy - gross_sell, 4) > 0.005 * gross_buy,
            "first_sell_ts": sells[0]["ts"] if sells else None,
            "first_sell_iso": iso(sells[0]["ts"]) if sells else None,
            "first_sell_lag_s": (sells[0]["ts"] - fb["ts"]) if sells and sells[0]["ts"] and fb["ts"] else None,
            "full_exit_ts": exit_ts, "full_exit_iso": iso(exit_ts),
            "full_exit_lag_s": (exit_ts - fb["ts"]) if exit_ts and fb["ts"] else None,
            "hold_s": ((exit_ts or evs[-1]["ts"]) - fb["ts"]) if fb["ts"] and evs[-1]["ts"] else None,
            "realized_mult": round(mult, 4) if mult is not None else None,
            "pnl_sol": round(sell_sol - buy_sol, 4) if fully_exited else None,
            "last_ts": evs[-1]["ts"],
        }
    djo_topups = [t for t in topups if t["from"] == DJOF]
    span = None
    if sigs:
        ts_list = [s.get("blockTime") for s in sigs if s.get("blockTime")]
        if ts_list:
            span = {"first_ts": min(ts_list), "last_ts": max(ts_list),
                    "first_iso": iso(min(ts_list)), "last_iso": iso(max(ts_list))}
    return {
        "wallet": wallet, "tag": tag,
        "walked_sigs": len(sigs), "events": len(events),
        "activity_span": span,
        "distinct_mints_touched": len(by_mint),
        "distinct_mints_bought": len(mints),
        "djo_topups": djo_topups,
        "djo_topup_total_sol": round(sum(t["sol"] for t in djo_topups), 4),
        "other_topups": [t for t in topups if t["from"] != DJOF],
        "outflows": outflows,
        "mints": mints,
    }

def main():
    wallets = sys.argv[1:]
    per_wallet = {}
    for w in wallets:
        r = analyze_wallet(w)
        if r:
            per_wallet[w] = r
            print(f"[{r['tag']}] sigs={r['walked_sigs']} events={r['events']} "
                  f"bought={r['distinct_mints_bought']} "
                  f"djo_topups={len(r['djo_topups'])} "
                  f"({r['djo_topup_total_sol']} SOL)", flush=True)
    # cross-wallet sample table
    coin_wallets = defaultdict(dict)
    for w, r in per_wallet.items():
        for mint, st in r["mints"].items():
            coin_wallets[mint][w] = st
    coins = {}
    for mint, ws in coin_wallets.items():
        fbs = [st["first_buy_ts"] for st in ws.values() if st["first_buy_ts"]]
        coins[mint] = {
            "wallets": list(ws.keys()),
            "n_wallets": len(ws),
            "first_buy_ts": min(fbs) if fbs else None,
            "first_buy_iso": iso(min(fbs)) if fbs else None,
            "first_buy_spread_s": (max(fbs) - min(fbs)) if len(fbs) > 1 else 0,
            "total_buy_sol": round(sum(st["buy_sol"] for st in ws.values()), 4),
            "total_sell_sol": round(sum(st["sell_sol"] for st in ws.values()), 4),
            "all_exited": all(not st["still_holding"] for st in ws.values()),
            "per_wallet": {w: {"first_buy_iso": st["first_buy_iso"],
                                "buy_sol": st["buy_sol"], "buys": st["buys"],
                                "sell_sol": st["sell_sol"], "sells": st["sells"],
                                "realized_mult": st["realized_mult"],
                                "pnl_sol": st["pnl_sol"],
                                "full_exit_lag_s": st["full_exit_lag_s"],
                                "still_holding": st["still_holding"]}
                           for w, st in ws.items()},
        }
    out = {
        "method": "public Solana RPC getSignaturesForAddress + getTransaction jsonParsed; "
                  "SOL in/out = wallet SOL balance delta on txs moving that mint (includes "
                  "tx fees, typically <0.01 SOL); multiples only on fully-exited mints.",
        "caveats": ["SOL deltas are None when wallet index unresolvable (rare); "
                    "buy_sol then understates by that tx.",
                    "Token deltas use owner-matched pre/post balances; multi-mint txs "
                    "attribute SOL delta to each mint touched (rare for these wallets).",
                    "3SVJ buy/sell detail already covered in 3SVJ_report.md; included "
                    "here for the sample table only."],
        "wallets": per_wallet,
        "coins": coins,
        "coin_count": len(coins),
        "multi_wallet_coins": {m: c for m, c in coins.items() if c["n_wallets"] > 1},
    }
    json.dump(out, open(f"{OUT}/djo_sample.json", "w"), indent=1)
    print(f"\ncoins in sample: {len(coins)} "
          f"(multi-wallet: {sum(1 for c in coins.values() if c['n_wallets'] > 1)})", flush=True)
    print(f"wrote {OUT}/djo_sample.json", flush=True)

if __name__ == "__main__":
    main()
