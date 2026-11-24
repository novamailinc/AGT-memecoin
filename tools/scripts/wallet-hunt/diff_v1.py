#!/usr/bin/env python3
"""diff_v1.py — rebuild the funding candidate set twice with
funding_candidates.py's exact conventions (same window, cut, exclusions,
dedupe): once from the snapshot originals only (baseline; must reproduce the
383 in funding_candidates.json), once from originals + v1-repair sidecars.
Prints and saves the diff: per-coin buyer counts, new candidates, removed
candidates, and material changes to existing candidates' numbers.
Material = gross_buy change >=10% AND >=100k tokens, or net sign flip, or
sol_spent change >=0.5 SOL.
"""
import json, os, sys
sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
import importlib.util
spec = importlib.util.spec_from_file_location(
    "fc", os.path.expanduser("~/workspace/wallet-hunt/funding_candidates.py"))
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)  # safe: main() guarded

SNAP = os.path.expanduser("~/workspace/wallet-hunt/v1repair")
OUT = os.path.expanduser("~/workspace/wallet-hunt")

def build(extra_repair):
    per_coin = {}
    for name, cfg in fc.COINS.items():
        exclude = set(fc.GLOBAL_EXCLUDE) | {cfg["mint"]}
        dsp = os.path.join(SNAP, os.path.basename(cfg["ds"])) \
            if os.path.exists(os.path.join(SNAP, os.path.basename(cfg["ds"]))) \
            else os.path.join(OUT, cfg["ds"])
        if os.path.exists(dsp):
            for p in (json.load(open(dsp)).get("pairs") or []):
                if p.get("pairAddress"):
                    exclude.add(p["pairAddress"])
        files = [os.path.join(SNAP, f) for f in cfg["files"]]
        if extra_repair:
            rp = os.path.join(SNAP, f"{name}_events_v1repair.json")
            if os.path.exists(rp):
                files.append(rp)
        # load_events with explicit paths (fc.load_events joins OUT itself)
        seen, evs = set(), []
        for p in files:
            if not os.path.exists(p):
                continue
            for e in json.load(open(p)):
                k = (e["sig"], e["owner"])
                if k in seen:
                    continue
                seen.add(k)
                evs.append(e)
        lo, hi = cfg["mig"], cfg["mig"] + fc.WIN
        stats = {}
        for e in evs:
            if not (lo <= e["ts"] <= hi):
                continue
            o = e["owner"]
            if o in exclude:
                continue
            s = stats.setdefault(o, {"gross_buy": 0.0, "net": 0.0,
                                     "sol_spent": 0.0, "n_buys": 0,
                                     "first_buy_ts": None, "first_buy_rel": None,
                                     "max_single": 0.0})
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
    cands = {}
    for name, stats in per_coin.items():
        ranked = sorted(stats.items(), key=lambda x: -x[1]["gross_buy"])
        keep = {o for o, s in stats.items() if s["gross_buy"] >= fc.CUT}
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
    return per_coin, cands

def main():
    base_stats, base = build(False)
    new_stats, new = build(True)
    print(f"baseline candidates: {len(base)}  (funding_candidates.json: "
          f"{len(json.load(open(os.path.join(SNAP,'funding_candidates.json')))['candidates'])})")
    print(f"after-repair candidates: {len(new)}")
    for name in fc.COINS:
        b, n = base_stats[name], new_stats[name]
        bb = sum(1 for s in b.values() if s["gross_buy"] >= fc.CUT)
        nb = sum(1 for s in n.values() if s["gross_buy"] >= fc.CUT)
        print(f"{name}: buyers {len(b)} -> {len(n)}; >=1M gross {bb} -> {nb}")
    added = sorted(set(new) - set(base))
    removed = sorted(set(base) - set(new))
    print(f"\nNEW candidates ({len(added)}):")
    for w in added:
        print("  ", w, {k: v["gross_buy"] for k, v in new[w]["coins"].items()},
              {k: (v["net"], v["sol_spent"], v["first_buy_rel_s"])
               for k, v in new[w]["coins"].items()})
    print(f"REMOVED candidates ({len(removed)}): {removed}")
    print("\nMATERIAL changes to existing candidates:")
    n_mat = 0
    for w in sorted(set(base) & set(new)):
        for coin in set(base[w]["coins"]) | set(new[w]["coins"]):
            b = base[w]["coins"].get(coin)
            n = new[w]["coins"].get(coin)
            if b is None or n is None:
                print(f"  {w} {coin}: coin membership changed {b} -> {n}")
                n_mat += 1
                continue
            dg = n["gross_buy"] - b["gross_buy"]
            if (abs(dg) >= 100_000 and abs(dg) >= 0.10 * max(b["gross_buy"], 1)) \
               or (b["net"] == 0) != (n["net"] == 0) \
               or (b["net"] > 0) != (n["net"] > 0) \
               or abs(n["sol_spent"] - b["sol_spent"]) >= 0.5:
                print(f"  {w} {coin}: gross {b['gross_buy']:,} -> {n['gross_buy']:,}; "
                      f"net {b['net']:,} -> {n['net']:,}; "
                      f"sol {b['sol_spent']} -> {n['sol_spent']}; "
                      f"n_buys {b['n_buys']} -> {n['n_buys']}")
                n_mat += 1
    if not n_mat:
        print("  (none)")
    json.dump({"baseline_n": len(base), "after_n": len(new),
               "added": [new[w] for w in added],
               "removed": removed},
              open(os.path.join(SNAP, "v1_diff.json"), "w"), indent=1)
    print("\nsaved v1repair/v1_diff.json")

if __name__ == "__main__":
    main()
