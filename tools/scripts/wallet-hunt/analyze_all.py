#!/usr/bin/env python3
"""Cross-coin analysis: buyer sets, intersections, KIMCHI fingerprint candidates.
Excludes pool/program/mint accounts. Reads *_events_gentle.json + progress files."""
import json, os
from collections import defaultdict

OUT = os.path.expanduser("~/workspace/wallet-hunt")
COINS = {
    "KIMCHI": {"mint": "7thgKgtK1rp1UtLpWUZEuu9vakedTMoZzuyps6RGpump", "grad": 1792540200,
               "ds": "dexscreener_kimchi.json"},
    "KWH": {"mint": "5hyyjwZqJgnPZorMdb7X4r2J1dT3VujKVkLD8Ptbpump", "grad": 1792664400,
            "ds": "dexscreener_kwh.json"},
    "EU1M": {"mint": "EU1MHvQvBoJ7DnLorurze1hwR2JWuyoKyAmkwymwpump", "grad": 1792644669,
             "ds": "dexscreener_eu1m.json"},
}
GLOBAL_EXCLUDE = {
    "11111111111111111111111111111111", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun program
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # pumpswap program
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # raydium authority (generic)
}

def load_coin(name, cfg):
    ev_path = f"{OUT}/{name}_events_gentle.json"
    prog_path = f"{OUT}/{name}_gentle_progress.json"
    sig_path = f"{OUT}/{name}_sigs_v2.json"
    if not os.path.exists(ev_path):
        return None
    events = json.load(open(ev_path))
    prog = json.load(open(prog_path)) if os.path.exists(prog_path) else {}
    total = len(json.load(open(sig_path))) if os.path.exists(sig_path) else None
    exclude = set(GLOBAL_EXCLUDE) | {cfg["mint"]}
    dsp = f"{OUT}/{cfg['ds']}"
    if os.path.exists(dsp):
        for p in (json.load(open(dsp)).get("pairs") or []):
            if p.get("pairAddress"):
                exclude.add(p["pairAddress"])
    stats = {}
    buyers = set()
    for e in events:
        o = e["owner"]
        if o in exclude:
            continue
        s = stats.setdefault(o, {"net": 0.0, "gross_buy": 0.0, "sol_spent": 0.0,
                                 "n_buys": 0, "max_single": 0.0, "first_ts": None})
        s["net"] += e["token_delta"]
        if e["token_delta"] > 0:
            s["gross_buy"] += e["token_delta"]
            s["n_buys"] += 1
            s["max_single"] = max(s["max_single"], e["token_delta"])
            if e["sol_delta"] is not None and e["sol_delta"] < 0:
                s["sol_spent"] += -e["sol_delta"]
            if s["first_ts"] is None or (e["ts"] and e["ts"] < s["first_ts"]):
                s["first_ts"] = e["ts"]
            if e["token_delta"] > 1000:
                buyers.add(o)
    return {"events": len(events), "parsed": prog.get("done"), "total_sigs": total,
            "buyers": buyers, "stats": stats, "excluded": sorted(exclude)}

def main():
    data = {}
    for name, cfg in COINS.items():
        d = load_coin(name, cfg)
        if d:
            data[name] = d
            print(f"{name}: parsed {d['parsed']}/{d['total_sigs']} sigs, "
                  f"{d['events']} events, {len(d['buyers'])} buyers (excl. pools/programs)")
    names = list(data)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            inter = data[a]["buyers"] & data[b]["buyers"]
            print(f"∩ {a}∩{b}: {len(inter)} wallets")
    if len(names) == 3:
        inter3 = data["KIMCHI"]["buyers"] & data["KWH"]["buyers"] & data["EU1M"]["buyers"]
        print(f"∩ ALL THREE: {len(inter3)} wallets: {sorted(inter3)}")
    # KIMCHI fingerprint candidates
    print("\n--- KIMCHI fingerprint candidates (single 4.2M-5.8M or net 4.2M-5.8M) ---")
    k = data.get("KIMCHI")
    if k:
        cands = []
        for o, s in k["stats"].items():
            if 4_200_000 <= s["max_single"] <= 5_800_000 or 4_200_000 <= s["net"] <= 5_800_000:
                cands.append({"wallet": o,
                              "max_single": round(s["max_single"]),
                              "net": round(s["net"]), "gross_buy": round(s["gross_buy"]),
                              "sol_spent": round(s["sol_spent"], 4), "n_buys": s["n_buys"],
                              "first_ts": s["first_ts"],
                              "in_KWH": o in data.get("KWH", {}).get("buyers", set()),
                              "in_EU1M": o in data.get("EU1M", {}).get("buyers", set())})
        cands.sort(key=lambda c: -(c["in_KWH"] + c["in_EU1M"]))
        for c in cands:
            print(json.dumps(c))
        json.dump({"candidates": cands,
                   "coverage": {n: {"parsed": data[n]["parsed"], "total": data[n]["total_sigs"],
                                    "buyers": len(data[n]["buyers"])} for n in names}},
                  open(f"{OUT}/analysis.json", "w"), indent=1)
    print("ANALYSIS DONE")

if __name__ == "__main__":
    main()
