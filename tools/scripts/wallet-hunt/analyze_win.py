#!/usr/bin/env python3
"""STEP 2/3 analysis: buyer sets + intersections over window [mig-3600, mig+7200].
Two conventions:
 (a) raw  — any event with token_delta>0 in window, no exclusions (comparable
     with the parent's 2-way run: KIMCHI 8324 / EU1M 695 / inter 25).
 (b) clean — analyze_all.py exclusions (GLOBAL_EXCLUDE + mint + DexScreener
     pair addresses) and buyer requires a single buy event > 1000 tokens.
KWH reads KWH_events_win.json (+progress for coverage)."""
import json, os
from collections import defaultdict

OUT = os.path.expanduser("~/workspace/wallet-hunt")
COINS = {
    "KIMCHI": {"mint": "7thgKgtK1rp1UtLpWUZEuu9vakedTMoZzuyps6RGpump", "grad": 1792540200,
               "events": "KIMCHI_events_gentle.json", "ds": "dexscreener_kimchi.json"},
    "KWH": {"mint": "5hyyjwZqJgnPZorMdb7X4r2J1dT3VujKVkLD8Ptbpump", "grad": 1792664400,
            "events": "KWH_events_win.json", "ds": "dexscreener_kwh.json"},
    "EU1M": {"mint": "EU1MHvQvBoJ7DnLorurze1hwR2JWuyoKyAmkwymwpump", "grad": 1792644669,
             "events": "EU1M_events_gentle.json", "ds": "dexscreener_eu1m.json"},
}
GLOBAL_EXCLUDE = {
    "11111111111111111111111111111111", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
}
LEADS = ["F9MxbtHQ8998RpMxUw7K7ED8qynTHQxcWuvvtEC7qqp1",
         "EedNgv1wGdCMayQ6znioCgVHjaqo7YBgdwq8GFUoMuRE"]

def load(name, cfg):
    events = json.load(open(f"{OUT}/{cfg['events']}"))
    excl = set(GLOBAL_EXCLUDE) | {cfg["mint"]}
    dsp = f"{OUT}/{cfg['ds']}"
    if os.path.exists(dsp):
        for p in (json.load(open(dsp)).get("pairs") or []):
            if p.get("pairAddress"):
                excl.add(p["pairAddress"])
    mig = cfg["grad"]
    raw, clean = {}, {}
    for e in events:
        ts = e.get("ts")
        if ts is None or not (mig - 3600 <= ts <= mig + 7200):
            continue
        if e["token_delta"] <= 0:
            continue
        o = e["owner"]
        for book in (raw,):
            s = book.setdefault(o, {"first_ts": ts, "tokens": 0.0, "sol": 0.0, "n": 0})
            s["tokens"] += e["token_delta"]; s["n"] += 1
            if ts < s["first_ts"]:
                s["first_ts"] = ts
            if e["sol_delta"] is not None and e["sol_delta"] < 0:
                s["sol"] += -e["sol_delta"]
        if o not in excl:
            s = clean.setdefault(o, {"first_ts": ts, "tokens": 0.0, "sol": 0.0, "n": 0,
                                     "max_single": 0.0})
            s["tokens"] += e["token_delta"]; s["n"] += 1
            s["max_single"] = max(s["max_single"], e["token_delta"])
            if ts < s["first_ts"]:
                s["first_ts"] = ts
            if e["sol_delta"] is not None and e["sol_delta"] < 0:
                s["sol"] += -e["sol_delta"]
    clean_buyers = {o for o, s in clean.items() if s["max_single"] > 1000}
    ts_all = [e["ts"] for e in events if e.get("ts")]
    return {"raw": raw, "clean": clean, "clean_buyers": clean_buyers,
            "n_events": len(events),
            "cov": (min(ts_all) - mig, max(ts_all) - mig) if ts_all else None}

def fmt(book, w, mig):
    s = book[w]
    return (f"first {s['first_ts']-mig:+d}s, tok {s['tokens']:,.0f}, "
            f"SOL {s['sol']:.4f}, buys {s['n']}")

def main():
    data = {n: load(n, c) for n, c in COINS.items()}
    prog = json.load(open(f"{OUT}/KWH_win_progress.json")) if os.path.exists(f"{OUT}/KWH_win_progress.json") else {}
    total_win = len(json.load(open(f"{OUT}/KWH_win_sigs.json")))
    print(f"KWH window parse coverage: {prog.get('done')}/{total_win} sigs "
          f"({100*(prog.get('done') or 0)/total_win:.1f}%)")
    for n, d in data.items():
        print(f"{n}: {d['n_events']} events, raw buyers {len(d['raw'])}, "
              f"clean buyers {len(d['clean_buyers'])}, event ts coverage vs mig: {d['cov']}")
    for conv in ("raw", "clean"):
        get = (lambda d: set(d["raw"])) if conv == "raw" else (lambda d: d["clean_buyers"])
        kk = get(data["KIMCHI"]) & get(data["KWH"])
        ke = get(data["KIMCHI"]) & get(data["EU1M"])
        k3 = get(data["KIMCHI"]) & get(data["KWH"]) & get(data["EU1M"])
        print(f"\n=== convention {conv}: KIMCHI∩KWH {len(kk)} | KIMCHI∩EU1M {len(ke)} | 3-WAY {len(k3)}")
        book = (lambda d: d["raw"]) if conv == "raw" else (lambda d: d["clean"])
        for label, inter in (("KIMCHI∩KWH", kk), ("3-WAY", k3)):
            for w in sorted(inter):
                print(f" [{label}] {w}")
                for n in ("KIMCHI", "KWH", "EU1M"):
                    if w in book(data[n]):
                        print(f"    {n}: {fmt(book(data[n]), w, COINS[n]['grad'])}")
    # STEP 3 fingerprint: KIMCHI gross accumulation 4.0M-6.2M (full KIMCHI stats)
    print("\n=== STEP 3 fingerprint (KIMCHI gross-buy accumulation 4.0M-6.2M, complete KIMCHI parse) ===")
    for w, s in sorted(data["KIMCHI"]["raw"].items(), key=lambda x: -x[1]["tokens"]):
        if 4_000_000 <= s["tokens"] <= 6_200_000:
            in_kwh_raw = w in data["KWH"]["raw"]
            in_kwh_clean = w in data["KWH"]["clean_buyers"]
            in_eu = w in data["EU1M"]["raw"]
            print(f" {w}: KIMCHI {fmt(data['KIMCHI']['raw'], w, COINS['KIMCHI']['grad'])} | "
                  f"in KWH raw={in_kwh_raw} clean={in_kwh_clean} | in EU1M raw={in_eu}")
            if in_kwh_raw:
                print(f"    KWH: {fmt(data['KWH']['raw'], w, COINS['KWH']['grad'])}")
    print("\n=== leads on KWH ===")
    for w in LEADS:
        print(f" {w}: KWH raw={w in data['KWH']['raw']} clean={w in data['KWH']['clean_buyers']}"
              + (f" | {fmt(data['KWH']['raw'], w, COINS['KWH']['grad'])}" if w in data["KWH"]["raw"] else ""))
    print("ANALYSIS-WIN DONE")

if __name__ == "__main__":
    main()
