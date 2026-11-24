#!/usr/bin/env python3
"""scan_fingerprint.py — scan the REPAIRED buyer sets (originals + v1 sidecars)
for the friend's fingerprint and near misses:
  strict: window net in [4.2M, 5.8M] AND gross >= 4.2M AND sol_spent in
          [1.2, 4.0]  (his ~4.97M KIMCHI @ ~2.24 SOL, held)
  loose:  window net in [4.0M, 6.2M] regardless of SOL (SOL attribution is
          unreliable in routed/bundle txs) — 'held' judged by net ~= gross
          (no net selling inside the window)
Also reports any loose/strict wallet's presence in the other coins' sets.
Uses diff_v1.build(True) so conventions match funding_candidates.py exactly.
"""
import os, sys
sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
import diff_v1

def main():
    stats, cands = diff_v1.build(True)
    print(f"repaired candidate total: {len(cands)}")
    hits = {}
    for coin, st in stats.items():
        for o, s in st.items():
            if s["gross_buy"] <= 0:
                continue
            net, gross, sol = s["net"], s["gross_buy"], s["sol_spent"]
            strict = 4.2e6 <= net <= 5.8e6 and gross >= 4.2e6 and 1.2 <= sol <= 4.0
            loose = 4.0e6 <= net <= 6.2e6
            if strict or loose:
                hits.setdefault(o, {})[coin] = {
                    "gross": round(gross), "net": round(net),
                    "sol": round(sol, 4), "n_buys": s["n_buys"],
                    "first_rel": s["first_buy_rel"],
                    "held_ratio": round(net / gross, 3) if gross else None,
                    "strict": strict}
    print(f"\nfingerprint hits (strict or loose): {len(hits)}")
    for o, coins in sorted(hits.items()):
        print(f"  {o}")
        for coin, h in coins.items():
            print(f"    {coin}: gross={h['gross']:,} net={h['net']:,} "
                  f"sol={h['sol']} n_buys={h['n_buys']} "
                  f"first=+{h['first_rel']}s held_ratio={h['held_ratio']} "
                  f"strict={h['strict']}")
        others = [c for c in stats if c not in coins
                  and o in stats[c] and stats[c][o]["gross_buy"] > 0]
        if others:
            print(f"    also a window buyer on: {others} "
                  f"(gross: {[round(stats[c][o]['gross_buy']) for c in others]})")
    if not hits:
        print("  (none)")

if __name__ == "__main__":
    main()
