#!/usr/bin/env python3
"""Funding-angle candidate builder: wallets that took REAL size early
(>=1M tokens gross within [migration, migration+300s]) on KIMCHI/KWH/EU1M,
plus per-coin top-10 buyers in that window regardless of the cut.
Consumes saved event files only — no refetching."""
import json, os
from collections import defaultdict

OUT = os.path.expanduser("~/workspace/wallet-hunt")

COINS = {
    "KIMCHI": {"mint": "7thgKgtK1rp1UtLpWUZEuu9vakedTMoZzuyps6RGpump",
               "mig": 1792540200, "ds": "dexscreener_kimchi.json",
               "files": ["KIMCHI_events_gentle.json"]},
    "KWH": {"mint": "5hyyjwZqJgnPZorMdb7X4r2J1dT3VujKVkLD8Ptbpump",
            "mig": 1792664448, "ds": "dexscreener_kwh.json",
            "files": ["KWH_events_win.json", "KWH_events_gentle.json"]},
    "EU1M": {"mint": "EU1MHvQvBoJ7DnLorurze1hwR2JWuyoKyAmkwymwpump",
             "mig": 1792644669, "ds": "dexscreener_eu1m.json",
             "files": ["EU1M_events_gentle.json"]},
}
GLOBAL_EXCLUDE = {
    "11111111111111111111111111111111", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
}
WIN = 300
CUT = 1_000_000

def load_events(files):
    seen, evs = set(), []
    for f in files:
        p = os.path.join(OUT, f)
        if not os.path.exists(p):
            continue
        for e in json.load(open(p)):
            k = (e["sig"], e["owner"])
            if k in seen:
                continue
            seen.add(k)
            evs.append(e)
    return evs

def main():
    per_coin = {}
    for name, cfg in COINS.items():
        exclude = set(GLOBAL_EXCLUDE) | {cfg["mint"]}
        dsp = os.path.join(OUT, cfg["ds"])
        if os.path.exists(dsp):
            for p in (json.load(open(dsp)).get("pairs") or []):
                if p.get("pairAddress"):
                    exclude.add(p["pairAddress"])
        evs = load_events(cfg["files"])
        lo, hi = cfg["mig"], cfg["mig"] + WIN
        stats = {}
        for e in evs:
            if not (lo <= e["ts"] <= hi):
                continue
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
                    s["first_buy_rel"] = e["ts"] - cfg["mig"]
        per_coin[name] = stats
        big = {o: s for o, s in stats.items() if s["gross_buy"] >= CUT}
        top10 = sorted(stats.items(), key=lambda x: -x[1]["gross_buy"])[:10]
        print(f"{name}: window [{lo},{hi}] buyers={len(stats)} "
              f">=1M gross: {len(big)}")
        for o, s in top10:
            print(f"   top10 {o} gross={s['gross_buy']:,.0f} net={s['net']:,.0f} "
                  f"sol={s['sol_spent']:.3f} first=+{s['first_buy_rel']}s")

    cands = {}
    for name, stats in per_coin.items():
        ranked = sorted(stats.items(), key=lambda x: -x[1]["gross_buy"])
        keep = {o for o, s in stats.items() if s["gross_buy"] >= CUT}
        keep |= {o for o, _ in ranked[:10]}
        for o in keep:
            s = stats[o]
            c = cands.setdefault(o, {"wallet": o, "coins": {}})
            c["coins"][name] = {"gross_buy": round(s["gross_buy"]),
                                "net": round(s["net"]),
                                "sol_spent": round(s["sol_spent"], 4),
                                "n_buys": s["n_buys"],
                                "first_buy_ts": s["first_buy_ts"],
                                "first_buy_rel_s": s["first_buy_rel"],
                                "max_single": round(s["max_single"])}
    out = sorted(cands.values(),
                 key=lambda c: -max(v["gross_buy"] for v in c["coins"].values()))
    print(f"\nTOTAL CANDIDATES (deduped): {len(out)}")
    for c in out:
        print(c["wallet"], {k: v["gross_buy"] for k, v in c["coins"].items()})
    json.dump({"window_s": WIN, "cut_tokens": CUT,
               "candidates": out},
              open(os.path.join(OUT, "funding_candidates.json"), "w"), indent=1)
    print("saved funding_candidates.json")

if __name__ == "__main__":
    main()
