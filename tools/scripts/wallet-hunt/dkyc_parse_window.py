#!/usr/bin/env python3
"""Fetch DKYC migration-window txs (v1-aware) and extract token events.
Input:  DKYC_window_sigs.json (from dkyc_scan.py) — mint list, non-error only.
Output: DKYC_events.json (list of event dicts), DKYC_parse_progress.json.
Reuses foot_check3.fetch_tx_robust (v1-aware) + parse_gentle.events_of.
Resumable: skips sigs already in DKYC_events.json."""
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor

W = os.path.expanduser('~/workspace/wallet-hunt')
sys.path.insert(0, W)
MINT = "DKYcjobF7HPtL9RyQBRdeRQ2iWvizpfbgWwPWNXdpump"
MIG = 1792070830

from foot_check3 import fetch_tx_robust
from parse_gentle import events_of

def main():
    sigs = json.load(open(os.path.join(W, "DKYC_window_sigs.json")))["mint"]
    todo = [s for s in sigs if not s["err"]]
    print(f"window mint sigs: {len(sigs)}, non-error to fetch: {len(todo)}", flush=True)
    ev_path = os.path.join(W, "DKYC_events.json")
    events = []
    if os.path.exists(ev_path):
        try:
            events = json.load(open(ev_path))
            print(f"resuming with {len(events)} events", flush=True)
        except Exception:
            events = []
    done_sigs = {e["sig"] for e in events}
    todo = [s for s in todo if s["sig"] not in done_sigs]
    print(f"remaining to fetch: {len(todo)}", flush=True)
    def fetch_one(s):
        tx = fetch_tx_robust(s["sig"])
        if tx is None: return s["sig"], []
        return s["sig"], events_of(MINT, s["sig"], s["blockTime"], tx)
    n = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        for sig, evs in ex.map(fetch_one, todo):
            events.extend(evs); n += 1
            if n % 100 == 0:
                json.dump(events, open(ev_path, "w"))
                print(f"fetched {n}/{len(todo)} events {len(events)}", flush=True)
    json.dump(events, open(ev_path, "w"))
    json.dump({"n_sigs": len(sigs), "n_fetched": n, "n_events": len(events)}, open(os.path.join(W, "DKYC_parse_progress.json"), "w"))
    print(f"DONE fetched {n} events {len(events)}", flush=True)

if __name__ == "__main__":
    main()
