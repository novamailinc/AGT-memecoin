#!/usr/bin/env python3
"""foot_check2.py — continuation of foot_check.py with coin order swapped
(SHADOW first, then ODIE resumed from its saved chunk progress + sig list).
Identical method: asc walk mint -> window [grad-600, grad+7200], narrowed to
[grad, grad+3600] if >15000 ok sigs; parse_gentle paced fetches; per-owner
buy stats; candidate verdicts; early buyers [grad, grad+300]; cross-coin
early intersection. Writes foot_check.json at the end."""
import json, os, sys, time, urllib.request
sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
from concurrent.futures import ThreadPoolExecutor
import hunt2
import parse_gentle

OUT = os.path.expanduser("~/workspace/wallet-hunt")
COINS = [
    {"name": "SHADOW", "mint": "F9Yz51QZpvS5fWdqgmt8wgEWHjg2E5YdwLvSjsrGpump", "grad": 1792602825},
    {"name": "ODIE",   "mint": "6LGGCwCUHDvcipZNtqWrb4MRtsPygNGbUx1df7LYpump", "grad": 1792760440},
]
CANDS = ["F9MxbtHQ8998RpMxUw7K7ED8qynTHQxcWuvvtEC7qqp1",
         "EedNgv1wGdCMayQ6znioCgVHjaqo7YBgdwq8GFUoMuRE"]
GLOBAL_EXCLUDE = {
    "11111111111111111111111111111111", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
}

def dex_pairs(mint, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(
                f"https://api.dexscreener.com/token-pairs/v1/solana/{mint}",
                headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read()) or []
        except Exception as e:
            print(f"  dexscreener try {i+1} failed: {e}", flush=True)
            time.sleep(3)
    return []

def parse_window(coin, sigs):
    name, mint = coin["name"], coin["mint"]
    ev_path = f"{OUT}/{name}_foot_events.json"
    prog_path = f"{OUT}/{name}_foot_progress.json"
    events, start_chunk, failed = [], 0, 0
    if os.path.exists(prog_path) and os.path.exists(ev_path):
        prog = json.load(open(prog_path))
        start_chunk = prog.get("chunks_done", 0)
        failed = prog.get("failed_fetches", 0)
        events = json.load(open(ev_path))
        print(f"  resume {name} parse at chunk {start_chunk}, {len(events)} events", flush=True)
    chunks = [sigs[i:i + 1000] for i in range(0, len(sigs), 1000)]
    for ci in range(start_chunk, len(chunks)):
        chunk = chunks[ci]
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=3) as ex:
            txs = list(ex.map(parse_gentle.fetch_tx, [s["signature"] for s in chunk]))
        for s, tx in zip(chunk, txs):
            if tx is None:
                failed += 1
                continue
            events.extend(parse_gentle.events_of(mint, s["signature"], s.get("blockTime"), tx))
        json.dump(events, open(ev_path, "w"))
        json.dump({"chunks_done": ci + 1, "total_chunks": len(chunks),
                   "n_sigs": len(sigs), "failed_fetches": failed}, open(prog_path, "w"))
        print(f"  {name} chunk {ci+1}/{len(chunks)} done: {len(events)} events, "
              f"{len(chunk)/(time.time()-t0):.2f} tx/s, failed={failed}", flush=True)
    return events, failed

def analyze(events, exclude):
    stats = {}
    for e in events:
        o = e["owner"]
        if o in exclude:
            continue
        s = stats.setdefault(o, {"net": 0.0, "gross_buy": 0.0, "sol_spent": 0.0,
                                 "n_buys": 0, "n_events": 0, "max_single": 0.0,
                                 "first_buy_ts": None})
        s["net"] += e["token_delta"]
        s["n_events"] += 1
        if e["token_delta"] > 0:
            s["gross_buy"] += e["token_delta"]
            s["n_buys"] += 1
            s["max_single"] = max(s["max_single"], e["token_delta"])
            if e["sol_delta"] is not None and e["sol_delta"] < 0:
                s["sol_spent"] += -e["sol_delta"]
            if e["ts"] and (s["first_buy_ts"] is None or e["ts"] < s["first_buy_ts"]):
                s["first_buy_ts"] = e["ts"]
    return stats

def run_coin(coin):
    name, mint, grad = coin["name"], coin["mint"], coin["grad"]
    print(f"\n===== {name} mint={mint} grad={grad}", flush=True)
    pairs = dex_pairs(mint)
    pair_info, exclude = [], set(GLOBAL_EXCLUDE) | {mint}
    for p in pairs:
        if p.get("pairAddress"):
            exclude.add(p["pairAddress"])
        pair_info.append({"dex": p.get("dexId"), "pair": p.get("pairAddress"),
                          "created_ms": p.get("pairCreatedAt"),
                          "created_s": (p.get("pairCreatedAt") or 0) // 1000 or None})
    print(f"  dexscreener pairs: {json.dumps(pair_info)}", flush=True)
    w1 = grad + 7200
    sig_path = f"{OUT}/{name}_foot_sigs.json"
    ok = None
    if os.path.exists(sig_path):
        saved = json.load(open(sig_path))
        if saved and (saved[-1].get("blockTime") or 0) >= w1:
            ok = saved
            print(f"  reusing saved sig list: {len(ok)} ok sigs", flush=True)
    if ok is None:
        print(f"  asc walk mint -> {w1 + 120}", flush=True)
        sigs = hunt2.asc_walk(mint, w1 + 120)
        ok = [s for s in sigs if not s.get("err")]
        json.dump(ok, open(sig_path, "w"))
        print(f"  walk: {len(sigs)} sigs, {len(ok)} ok", flush=True)
    first_ts = ok[0].get("blockTime") if ok else None
    print(f"  mint first_ts={first_ts} (grad-first={(grad - (first_ts or grad))}s)", flush=True)
    lo, hi, narrowed = grad - 600, grad + 7200, False
    in_win = [s for s in ok if s.get("blockTime") and lo <= s["blockTime"] <= hi]
    if len(in_win) > 15000:
        lo, hi, narrowed = grad, grad + 3600, True
        in_win = [s for s in ok if s.get("blockTime") and lo <= s["blockTime"] <= hi]
    print(f"  window [{lo}, {hi}] narrowed={narrowed}: {len(in_win)} ok sigs to parse", flush=True)
    events, failed = parse_window(coin, in_win)
    stats = analyze(events, exclude)
    cands = {}
    for c in CANDS:
        s = stats.get(c)
        if s and s["n_buys"] > 0:
            cands[c] = {"bought": True, "first_buy_ts": s["first_buy_ts"],
                        "first_buy_offset_s": (s["first_buy_ts"] - grad) if s["first_buy_ts"] else None,
                        "gross_buy_tokens": round(s["gross_buy"]),
                        "max_single_tokens": round(s["max_single"]),
                        "n_buys": s["n_buys"], "sol_spent": round(s["sol_spent"], 4),
                        "net_tokens": round(s["net"])}
        elif s:
            cands[c] = {"bought": False, "present_no_buys": True,
                        "n_events": s["n_events"], "net_tokens": round(s["net"])}
        else:
            cands[c] = {"bought": False, "present_no_buys": False}
    early = {}
    for o, s in stats.items():
        if s["first_buy_ts"] and grad <= s["first_buy_ts"] <= grad + 300 and s["max_single"] > 1000:
            early[o] = {"first_buy_ts": s["first_buy_ts"], "offset_s": s["first_buy_ts"] - grad,
                        "gross_buy_tokens": round(s["gross_buy"]),
                        "sol_spent": round(s["sol_spent"], 4), "n_buys": s["n_buys"]}
    print(f"  {name}: candidates={json.dumps(cands)}", flush=True)
    print(f"  {name}: {len(early)} early buyers", flush=True)
    return {"mint": mint, "grad": grad, "window": [lo, hi], "narrowed": narrowed,
            "walk_ok": len(ok), "mint_first_ts": first_ts, "pairs": pair_info,
            "n_window_sigs": len(in_win), "n_events": len(events),
            "failed_fetches": failed,
            "n_buyers": len([o for o, s in stats.items() if s["n_buys"]]),
            "candidates": cands, "early_buyers": early}

def main():
    results, early_sets = {}, {}
    for coin in COINS:
        r = run_coin(coin)
        results[coin["name"]] = r
        early_sets[coin["name"]] = set(r["early_buyers"])
        json.dump(results, open(f"{OUT}/foot_check.json", "w"), indent=1)
    inter = sorted(early_sets["ODIE"] & early_sets["SHADOW"])
    cross = [{"wallet": w, "ODIE": results["ODIE"]["early_buyers"][w],
              "SHADOW": results["SHADOW"]["early_buyers"][w]} for w in inter]
    results["cross_coin_early_repeat"] = cross
    json.dump(results, open(f"{OUT}/foot_check.json", "w"), indent=1)
    print(f"\n===== CROSS-COIN EARLY REPEAT: {len(cross)}", flush=True)
    for c in cross:
        print(json.dumps(c), flush=True)
    print("FOOT CHECK2 DONE", flush=True)

if __name__ == "__main__":
    main()
