#!/usr/bin/env python3
"""repair_pass.py <COIN> <lo_offset> <hi_offset> — re-fetch, with the v1-aware
robust fetcher, every in-window sig in [grad+lo_offset, grad+hi_offset] that
produced no events in {COIN}_foot_events.json; append any events found and
record resolved sigs in {COIN}_resolved.json. Run ONLY after the main driver
has finished that coin (it rewrites the events file per chunk)."""
import json, os, sys, time
sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
from concurrent.futures import ThreadPoolExecutor
import foot_check3  # reuse fetch_tx_robust (v1-aware) + COINS config
import parse_gentle

OUT = os.path.expanduser("~/workspace/wallet-hunt")

def main():
    name = sys.argv[1]
    lo_off, hi_off = int(sys.argv[2]), int(sys.argv[3])
    coin = [c for c in foot_check3.COINS if c["name"] == name][0]
    mint, grad = coin["mint"], coin["grad"]
    sigs = json.load(open(f"{OUT}/{name}_foot_sigs.json"))
    lo, hi = grad + lo_off, grad + hi_off
    in_range = [s for s in sigs if s.get("blockTime") and lo <= s["blockTime"] <= hi]
    ev_path = f"{OUT}/{name}_foot_events.json"
    events = json.load(open(ev_path))
    have = {e["sig"] for e in events}
    res_path = f"{OUT}/{name}_resolved.json"
    resolved = set(json.load(open(res_path))) if os.path.exists(res_path) else set()
    todo = [s for s in in_range if s["signature"] not in have and s["signature"] not in resolved]
    print(f"{name} range [{lo},{hi}]: {len(in_range)} sigs, {len(have & {s['signature'] for s in in_range})} "
          f"with events, {len(resolved)} already resolved, {len(todo)} to re-fetch", flush=True)
    new_events, still_failed = [], 0
    B = 500
    for i in range(0, len(todo), B):
        batch = todo[i:i + B]
        with ThreadPoolExecutor(max_workers=3) as ex:
            txs = list(ex.map(foot_check3.fetch_tx_robust, [s["signature"] for s in batch]))
        for s, tx in zip(batch, txs):
            if tx is None:
                still_failed += 1
                continue
            resolved.add(s["signature"])
            new_events.extend(parse_gentle.events_of(mint, s["signature"], s.get("blockTime"), tx))
        print(f"  batch {i//B + 1}: +{len(new_events)} new events total, still_failed={still_failed}", flush=True)
    if new_events:
        events.extend(new_events)
        json.dump(events, open(ev_path, "w"))
    json.dump(sorted(resolved), open(res_path, "w"))
    print(f"{name} REPAIR DONE: +{len(new_events)} events appended, "
          f"{still_failed} sigs still unfetchable, events total {len(events)}", flush=True)

if __name__ == "__main__":
    main()
