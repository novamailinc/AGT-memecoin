#!/usr/bin/env python3
"""DKYC buyer set + fingerprint scan, following funding_candidates.py
conventions exactly (same GLOBAL_EXCLUDE + mint + pool exclusions,
same gross/net/sol_spent aggregation, same CUT=1M and top-10 rule).
Input: DKYC_events.json (from dkyc_parse_window.py).
Output: DKYC_buyers.json + printed fingerprint/cross-check report.
"""
import json, os, sys

W = os.path.expanduser("~/workspace/wallet-hunt")
MINT = "DKYcjobF7HPtL9RyQBRdeRQ2iWvizpfbgWwPWNXdpump"
POOL = "7Te1wXcfeQjKLGHXFvxSCnGZTqunoZ9fxBn7KKjrkNEr"
MIG = 1792070830
CUT = 1_000_000

GLOBAL_EXCLUDE = {
    "11111111111111111111111111111111", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
}

def main():
    events = json.load(open(os.path.join(W, "DKYC_events.json")))
    exclude = set(GLOBAL_EXCLUDE) | {MINT, POOL}
    stats = {}
    for e in events:
        o = e["owner"]
        if o in exclude:
            continue
        s = stats.setdefault(o, {"gross_buy": 0.0, "net": 0.0, "sol_spent": 0.0,
                                 "n_buys": 0, "first_buy_ts": None,
                                 "first_buy_rel": None, "max_single": 0.0})
        s["net"] += e["token_delta"]
        if e["token_delta"] > 0:
            s["gross_buy"] += e["token_delta"]
            s["n_buys"] += 1
            s["max_single"] = max(s["max_single"], e["token_delta"])
            if e["sol_delta"] is not None and e["sol_delta"] < 0:
                s["sol_spent"] += -e["sol_delta"]
            if s["first_buy_ts"] is None or e["ts"] < s["first_buy_ts"]:
                s["first_buy_ts"] = e["ts"]
                s["first_buy_rel"] = e["ts"] - MIG
    big = {o: s for o, s in stats.items() if s["gross_buy"] >= CUT}
    top10 = sorted(stats.items(), key=lambda x: -x[1]["gross_buy"])[:10]
    cands = set(big) | {o for o, _ in top10}
    print(f"DKYC: events={len(events)} buyers={len(stats)} "
          f">=1M gross={len(big)} top10-included candidates={len(cands)}")
    print("top10:")
    for o, s in top10:
        print(f"  {o} gross={s['gross_buy']:,.0f} net={s['net']:,.0f} "
              f"sol={s['sol_spent']:.3f} n_buys={s['n_buys']} first=+{s['first_buy_rel']}s")
    # fingerprint scan
    refs = json.load(open(os.path.join(W, "DKYC_refsets.json")))
    farm = set(refs["farm_shells"])
    bundle = set(refs["kimchi_bundle_28s"])
    g14 = refs["g14J5Jta_eu1m_strict"]
    buyers_by_coin = refs["buyers_by_coin"]  # coin -> {wallet: stats-ish}
    hits = []
    for o in cands:
        s = stats[o]
        net, gross, sol = s["net"], s["gross_buy"], s["sol_spent"]
        strict = 4.2e6 <= net <= 5.8e6 and gross >= 4.2e6 and 1.2 <= sol <= 4.0
        loose = 4.0e6 <= net <= 6.2e6
        if strict or loose:
            others = [c for c, m in buyers_by_coin.items()
                      if o in m and m[o].get("gross_buy", 0) > 0]
            hits.append({"wallet": o, "strict": strict, "gross": round(gross),
                         "net": round(net), "sol": round(sol, 4),
                         "n_buys": s["n_buys"], "first_rel": s["first_buy_rel"],
                         "held_ratio": round(net / gross, 3) if gross else None,
                         "in_farm_shells": o in farm,
                         "in_kimchi_bundle": o in bundle,
                         "is_g14": o == g14,
                         "other_coin_buyer": others})
    print(f"\nfingerprint hits (strict or loose): {len(hits)}")
    for h in hits:
        print(" ", json.dumps(h))
    if not hits:
        print("  (none)")
    out = {"stats": {o: {**s, "gross_buy": round(s["gross_buy"]),
                          "net": round(s["net"]),
                          "sol_spent": round(s["sol_spent"], 4)}
                       for o, s in stats.items()},
           "candidates": sorted(cands),
           "fingerprint_hits": hits}
    json.dump(out, open(os.path.join(W, "DKYC_buyers.json"), "w"))
    print(f"\nwrote DKYC_buyers.json: {len(stats)} buyers, "
          f"{len(cands)} candidates, {len(hits)} fingerprint hits")

if __name__ == "__main__":
    main()
