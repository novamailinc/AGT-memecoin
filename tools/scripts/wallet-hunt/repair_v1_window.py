#!/usr/bin/env python3
"""repair_v1_window.py <COIN> — v1-aware repair pass for the funding-hunt
candidate window [mig, mig+300], modeled on repair_pass.py.

Differences from repair_pass.py (which serves the ODIE/SHADOW foot files):
- Coins are the funding-hunt coins (config mirrors funding_candidates.py).
- Inputs are the snapshot copies in v1repair/ (taken 2026-10-24 ~01:59 UTC,
  exactly the files funding_candidates.json was built from). A live KWH
  re-walk/parse process may overwrite the originals; this pass never does.
- Repair output goes to sidecar files ({COIN}_events_v1repair.json,
  {COIN}_v1repair_resolved.json) instead of mutating the gentle event files,
  so nothing races the running KWH parse chain. The diff step loads
  originals + sidecars with funding_candidates.py's own dedupe.

For every ok sig in [mig, mig+300] that produced no events in the coin's
original event files, re-fetch with foot_check3.fetch_tx_robust
(maxSupportedTransactionVersion: 1) and extract with parse_gentle.events_of.
Resumable: resolved sigs and recovered events persist after every batch.
"""
import json, os, sys, time
sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
from concurrent.futures import ThreadPoolExecutor
import foot_check3  # fetch_tx_robust (v1-aware)
import parse_gentle

BASE = os.path.expanduser("~/workspace/wallet-hunt")
SNAP = os.path.join(BASE, "v1repair")

COINS = {
    "KIMCHI": {"mint": "7thgKgtK1rp1UtLpWUZEuu9vakedTMoZzuyps6RGpump",
               "mig": 1792540200,
               "sigfiles": ["KIMCHI_sigs_v2.json"],
               "evfiles": ["KIMCHI_events_gentle.json"]},
    "KWH": {"mint": "5hyyjwZqJgnPZorMdb7X4r2J1dT3VujKVkLD8Ptbpump",
            "mig": 1792664448,
            "sigfiles": ["KWH_sigs_v2.json", "KWH_win_sigs.json"],
            "evfiles": ["KWH_events_win.json", "KWH_events_gentle.json"]},
    "EU1M": {"mint": "EU1MHvQvBoJ7DnLorurze1hwR2JWuyoKyAmkwymwpump",
             "mig": 1792644669,
             "sigfiles": ["EU1M_sigs_v2.json"],
             "evfiles": ["EU1M_events_gentle.json"]},
}
WIN = 300

def main():
    name = sys.argv[1]
    cfg = COINS[name]
    mint, mig = cfg["mint"], cfg["mig"]
    lo, hi = mig, mig + WIN
    sigmap = {}
    for f in cfg["sigfiles"]:
        for s in json.load(open(os.path.join(SNAP, f))):
            if s.get("err"):
                continue
            bt = s.get("blockTime")
            if bt and lo <= bt <= hi:
                sigmap[s["signature"]] = bt
    have = set()
    for f in cfg["evfiles"]:
        for e in json.load(open(os.path.join(SNAP, f))):
            if lo <= e["ts"] <= hi:
                have.add(e["sig"])
    ev_path = os.path.join(SNAP, f"{name}_events_v1repair.json")
    res_path = os.path.join(SNAP, f"{name}_v1repair_resolved.json")
    new_events = json.load(open(ev_path)) if os.path.exists(ev_path) else []
    resolved = set(json.load(open(res_path))) if os.path.exists(res_path) else set()
    done_sigs = {e["sig"] for e in new_events} | resolved
    todo = [(sig, bt) for sig, bt in sorted(sigmap.items(), key=lambda x: x[1])
            if sig not in have and sig not in done_sigs]
    print(f"{name} window [{lo},{hi}]: {len(sigmap)} ok sigs, {len(have)} with "
          f"events, {len(resolved)} already resolved, {len(new_events)} events "
          f"already recovered, {len(todo)} to re-fetch", flush=True)
    still_failed = 0
    fetched_ok = 0
    B = 500
    t0 = time.time()
    for i in range(0, len(todo), B):
        batch = todo[i:i + B]
        with ThreadPoolExecutor(max_workers=3) as ex:
            txs = list(ex.map(foot_check3.fetch_tx_robust, [s for s, _ in batch]))
        for (sig, bt), tx in zip(batch, txs):
            if tx is None:
                still_failed += 1
                continue
            fetched_ok += 1
            resolved.add(sig)
            new_events.extend(parse_gentle.events_of(mint, sig, bt, tx))
        json.dump(new_events, open(ev_path, "w"))
        json.dump(sorted(resolved), open(res_path, "w"))
        print(f"  batch {i//B + 1}/{(len(todo)+B-1)//B}: recovered events total "
              f"{len(new_events)}, fetched_ok={fetched_ok}, "
              f"still_failed={still_failed}, "
              f"{(i+len(batch))/(time.time()-t0):.2f} sig/s", flush=True)
    print(f"{name} V1 REPAIR DONE: {len(new_events)} events recovered from "
          f"{len({e['sig'] for e in new_events})} sigs; {fetched_ok} fetched ok, "
          f"{still_failed} still unfetchable", flush=True)

if __name__ == "__main__":
    main()
