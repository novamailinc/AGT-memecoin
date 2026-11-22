#!/usr/bin/env python3
"""Analyze a parsed wallet history (<tag>_events.json from parse_wallet.py).
Usage: analyze_wallet.py <wallet> <tag> [dex_mint_cap]
- Per-mint: first buy/sell, sizes, SOL in/out, hold + full-exit lags.
- DexScreener (batched 30 mints/req, cached to <tag>_dex.json): migration_ts
  = earliest PumpSwap pair creation; launch_ts = earliest pair creation.
  Entry lag = first_buy_ts - migration_ts (negative = bought on the curve).
- Writes <tag>_summary.json and prints a human-readable digest."""
import json, os, sys, time, urllib.request, urllib.error
from collections import defaultdict

OUT = os.path.expanduser("~/workspace/wallet-hunt")
DEX = "https://api.dexscreener.com/latest/dex/tokens/"

def pct(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]

def med(vals):
    return pct(sorted(vals), 0.5)

def dex_fetch(batch):
    """Batched DexScreener call via curl (urllib gets its connections to
    DexScreener dropped through this proxy; curl works). On failure,
    bisects the batch down to singles.
    Returns dict mint -> pairs (possibly empty list), omitting failures."""
    if not batch:
        return {}
    pairs = None
    for attempt in range(2):
        try:
            import subprocess
            url = DEX + ",".join(batch)
            r = subprocess.run(["curl", "-sS", "-m", "30", "--compressed", url],
                               capture_output=True, timeout=40)
            d = json.loads(r.stdout.decode())
            pairs = d.get("pairs") or []
            break
        except Exception as e:
            print(f"  dex batch({len(batch)}) attempt {attempt + 1} failed ({e})", flush=True)
            time.sleep(1.5 * (attempt + 1))
    if pairs is None:
        if len(batch) == 1:
            return {}
        mid = len(batch) // 2
        out = dex_fetch(batch[:mid])
        out.update(dex_fetch(batch[mid:]))
        return out
    by_mint = defaultdict(list)
    for p in pairs:
        by_mint[(p.get("baseToken") or {}).get("address")].append(p)
    return {m: by_mint.get(m, []) for m in batch}

def dex_lookup(mints):
    cache_path = f"{OUT}/{TAG}_dex.json"
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    todo = [m for m in mints if m not in cache]
    for i in range(0, len(todo), 10):
        batch = todo[i:i + 10]
        got = dex_fetch(batch)
        for m in batch:
            if m not in got:
                continue  # failure: leave uncached for a later run
            ps = got[m]
            mig = [p["pairCreatedAt"] / 1000 for p in ps
                   if p.get("dexId") == "pumpswap" and p.get("pairCreatedAt")]
            launch = [p["pairCreatedAt"] / 1000 for p in ps if p.get("pairCreatedAt")]
            cache[m] = {"migration_ts": min(mig) if mig else None,
                        "launch_ts": min(launch) if launch else None,
                        "dexes": sorted({p.get("dexId") for p in ps if p.get("dexId")}),
                        "pairs": len(ps)}
        json.dump(cache, open(cache_path, "w"))
        print(f"  dex {min(i + 10, len(todo))}/{len(todo)}", flush=True)
        time.sleep(1.1)
    return cache

TAG = None

def main():
    global TAG
    wallet, TAG = sys.argv[1], sys.argv[2]
    cap = int(sys.argv[3]) if len(sys.argv) > 3 else 200
    events = json.load(open(f"{OUT}/{TAG}_events.json"))
    sigs = json.load(open(f"{OUT}/{TAG}_sigs.json"))
    ok_sigs = [s for s in sigs if not s.get("err")]
    span = (min(s["blockTime"] for s in ok_sigs if s.get("blockTime")),
            max(s["blockTime"] for s in ok_sigs if s.get("blockTime"))) if ok_sigs else (None, None)

    by_mint = defaultdict(list)
    for e in events:
        by_mint[e["mint"]].append(e)
    mints = {}
    for mint, evs in by_mint.items():
        evs.sort(key=lambda x: (x["ts"] or 0, x["sig"]))
        buys = [e for e in evs if e["token_delta"] > 0]
        sells = [e for e in evs if e["token_delta"] < 0]
        if not buys:
            continue
        fb = buys[0]
        gross_buy = sum(e["token_delta"] for e in buys)
        gross_sell = -sum(e["token_delta"] for e in sells)
        buy_sol = sum(-e["sol_delta"] for e in buys if e["sol_delta"] is not None and e["sol_delta"] < 0)
        sell_sol = sum(e["sol_delta"] for e in sells if e["sol_delta"] is not None and e["sol_delta"] > 0)
        # full exit: first ts where running balance <= 0.5% of gross buy after first buy
        bal, exit_ts = 0.0, None
        for e in evs:
            bal += e["token_delta"]
            if e["ts"] and e["ts"] >= fb["ts"] and bal <= 0.005 * gross_buy and exit_ts is None \
               and any(s["ts"] and s["ts"] <= e["ts"] for s in sells):
                exit_ts = e["ts"]
        mints[mint] = {
            "first_buy_ts": fb["ts"], "first_buy_sol": (-fb["sol_delta"] if fb["sol_delta"] is not None
                                                         and fb["sol_delta"] < 0 else None),
            "buys": len(buys), "gross_buy_tokens": gross_buy, "buy_sol": buy_sol,
            "sells": len(sells), "gross_sell_tokens": gross_sell, "sell_sol": sell_sol,
            "net_tokens": gross_buy - gross_sell,
            "first_sell_ts": sells[0]["ts"] if sells else None,
            "first_sell_lag_s": (sells[0]["ts"] - fb["ts"]) if sells and sells[0]["ts"] and fb["ts"] else None,
            "full_exit_ts": exit_ts,
            "full_exit_lag_s": (exit_ts - fb["ts"]) if exit_ts and fb["ts"] else None,
            "last_ts": evs[-1]["ts"],
        }
    bought = sorted(mints.items(), key=lambda kv: -(kv[1]["first_buy_ts"] or 0))
    pump_mints = [m for m, _ in bought if m.endswith("pump")]
    lookup_order = [m for m, _ in bought][:cap]
    dex = dex_lookup(lookup_order)
    lags, curve_n, within60, ungrad = [], 0, 0, 0
    for m, st in bought:
        info = dex.get(m)
        st["is_pump"] = m.endswith("pump")
        if m not in dex and m in lookup_order:
            st["entry_class"] = "dex lookup failed (uncached)"
            continue
        if info is None:
            st["entry_class"] = "no_dex_lookup (beyond cap)"
            continue
        st["migration_ts"] = info["migration_ts"]; st["launch_ts"] = info["launch_ts"]
        if info["migration_ts"] and st["first_buy_ts"]:
            lag = st["first_buy_ts"] - info["migration_ts"]
            st["entry_lag_s"] = lag
            lags.append(lag)
            if lag < 0:
                curve_n += 1; st["entry_class"] = "curve (pre-migration)"
            elif lag <= 60:
                within60 += 1; st["entry_class"] = "graduation <=60s"
            else:
                st["entry_class"] = "post-migration >60s"
        else:
            ungrad += 1
            st["entry_class"] = "no PumpSwap pair (ungraduated at lookup)"
            if info["launch_ts"] and st["first_buy_ts"]:
                st["entry_vs_launch_s"] = st["first_buy_ts"] - info["launch_ts"]
    sold = [st for _, st in bought if st["sells"] > 0]
    prog_path = f"{OUT}/{TAG}_parse_progress.json"
    parse_done = json.load(open(prog_path))["done"] if os.path.exists(prog_path) else None
    ev_span = (min(e["ts"] for e in events if e.get("ts")),
               max(e["ts"] for e in events if e.get("ts"))) if events else (None, None)
    summary = {
        "wallet": wallet, "tag": TAG,
        "walked_sigs": len(sigs), "ok_sigs": len(ok_sigs), "events": len(events),
        "parse_done_txs": parse_done, "event_span_ts": ev_span,
        "activity_span_ts": span,
        "distinct_mints_with_events": len(by_mint),
        "distinct_mints_bought": len(bought),
        "pump_mints_bought": len(pump_mints),
        "dex_lookups_done": len(lookup_order),
        "entry_lags_s": {"n": len(lags), "median": med(lags), "p25": pct(sorted(lags), 0.25),
                          "p75": pct(sorted(lags), 0.75),
                          "pct_within_60s_post": (100.0 * within60 / len(lags)) if lags else None,
                          "pct_curve": (100.0 * curve_n / len(lags)) if lags else None,
                          "curve_n": curve_n, "within60_n": within60, "ungraduated_n": ungrad},
        "first_buy_sol": {"n": len([1 for _, st in bought if st["first_buy_sol"] is not None]),
                          "median": med([st["first_buy_sol"] for _, st in bought
                                         if st["first_buy_sol"] is not None])},
        "mints_with_sells": len(sold),
        "first_sell_lag_s": {"median": med([st["first_sell_lag_s"] for _, st in bought
                                            if st["first_sell_lag_s"] is not None]),
                             "p25": pct(sorted([st["first_sell_lag_s"] for _, st in bought
                                                if st["first_sell_lag_s"] is not None]), 0.25),
                             "p75": pct(sorted([st["first_sell_lag_s"] for _, st in bought
                                                if st["first_sell_lag_s"] is not None]), 0.75)},
        "full_exit_lag_s": {"n": len([1 for _, st in bought if st["full_exit_lag_s"] is not None]),
                            "median": med([st["full_exit_lag_s"] for _, st in bought
                                           if st["full_exit_lag_s"] is not None])},
        "total_buy_sol": round(sum(st["buy_sol"] for _, st in bought), 4),
        "total_sell_sol": round(sum(st["sell_sol"] for _, st in bought), 4),
        "mints": dict(bought),
    }
    json.dump(summary, open(f"{OUT}/{TAG}_summary.json", "w"), indent=1)
    print(json.dumps({k: v for k, v in summary.items() if k != "mints"}, indent=1), flush=True)

if __name__ == "__main__":
    main()
