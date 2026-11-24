#!/usr/bin/env python3
"""3SVJ buyer set + attribution scans (adapted from dkyc_buyers.py).
Input: 3SVJ_events.json. Output: 3SVJ_buyers.json + printed report.
Tests: (a) farm-lineage membership (shells/bundle/treasury-seeder-
dispatcher), (b) suffix leads Qxdc/WPvD, (c) fingerprint bands,
(d) cross-coin buyer history (KIMCHI/KWH/EU1M sets).
"""
import json, os, sys

W = os.path.expanduser("~/workspace/wallet-hunt")
MINT = "3SVjZ7BfT9FxiR49nZc7UdBmLgzRLJY7XBZDKhEFpump"
POOL = "3tXP9X2W1aL9jtZF1tuAwEZMuX4w3PWNvVAHPJjd3MAq"
MIG = 1792871066
CUT = 1_000_000

GLOBAL_EXCLUDE = {
    "11111111111111111111111111111111", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
}

def main():
    events = json.load(open(os.path.join(W, "3SVJ_events.json")))
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
    print(f"3SVJ: events={len(events)} buyers={len(stats)} "
          f">=1M gross={len(big)} candidates={len(cands)}")
    print("top10:")
    for o, s in top10:
        print(f"  {o} gross={s['gross_buy']:,.0f} net={s['net']:,.0f} "
              f"sol={s['sol_spent']:.3f} n_buys={s['n_buys']} first=+{s['first_buy_rel']}s")
    refs = json.load(open(os.path.join(W, "DKYC_refsets.json")))
    farm, bundle = set(refs["farm_shells"]), set(refs["kimchi_bundle_28s"])
    g14 = refs["g14J5Jta_eu1m_strict"]
    buyers_by_coin = refs["buyers_by_coin"]
    # (b) suffix leads across ALL window buyers (not just candidates)
    for suffix in ("Qxdc", "WPvD"):
        hits = [o for o in stats if o.endswith(suffix)]
        print(f"suffix {suffix}: {hits if hits else '(none in window buyers)'}")
    # (a)+(c) farm + fingerprint over candidates
    hits = []
    for o in cands:
        s = stats[o]
        net, gross, sol = s["net"], s["gross_buy"], s["sol_spent"]
        strict = 4.2e6 <= net <= 5.8e6 and gross >= 4.2e6 and 1.2 <= sol <= 4.0
        loose = 4.0e6 <= net <= 6.2e6
        others = [c for c, m in buyers_by_coin.items() if o in m]
        rec = {"wallet": o, "strict": strict, "loose": loose,
               "gross": round(gross), "net": round(net), "sol": round(sol, 4),
               "n_buys": s["n_buys"], "first_rel": s["first_buy_rel"],
               "held_ratio": round(net / gross, 3) if gross else None,
               "in_farm_shells": o in farm, "in_kimchi_bundle": o in bundle,
               "is_g14": o == g14, "other_coin_buyer": others}
        if strict or loose or rec["in_farm_shells"] or rec["in_kimchi_bundle"] or others:
            hits.append(rec)
    print(f"\nattribution hits: {len(hits)}")
    for h in hits:
        print(" ", json.dumps(h))
    out = {"stats": {o: {**s, "gross_buy": round(s["gross_buy"]),
                          "net": round(s["net"]),
                          "sol_spent": round(s["sol_spent"], 4)}
                       for o, s in stats.items()},
           "candidates": sorted(cands),
           "attribution_hits": hits}
    json.dump(out, open(os.path.join(W, "3SVJ_buyers.json"), "w"))
    print(f"\nwrote 3SVJ_buyers.json: {len(stats)} buyers, "
          f"{len(cands)} candidates, {len(hits)} attribution hits")

if __name__ == "__main__":
    main()
