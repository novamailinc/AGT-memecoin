#!/usr/bin/env python3
"""DKYC window parse: v1-aware fetch + event extraction for [mig, mig+300].
Resumable: DKYC_events.json + DKYC_parse_progress.json.
Usage: dkyc_parse.py [mig]  (default mig = DexScreener pairCreatedAt)"""
import json, os, sys, time
sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
from concurrent.futures import ThreadPoolExecutor, as_completed
import foot_check3, parse_gentle

BASE = os.path.expanduser("~/workspace/wallet-hunt")
MINT = "DKYcjobF7HPtL9RyQBRdeRQ2iWvizpfbgWwPWNXdpump"
MIG = int(sys.argv[1]) if len(sys.argv) > 1 else 1792070830
WIN = 300

def main():
    sigs = json.load(open(os.path.join(BASE, "DKYC_sigs.json")))
    lo, hi = MIG, MIG + WIN
    todo = [s for s in sigs if s.get("blockTime") and lo <= s["blockTime"] <= hi and not s.get("err")]
    ev_path = os.path.join(BASE, "DKYC_events.json")
    prog_path = os.path.join(BASE, "DKYC_parse_progress.json")
    events, done = [], 0
    if os.path.exists(prog_path):
        prog = json.load(open(prog_path))
        done = prog["done"]
        events = json.load(open(ev_path)) if os.path.exists(ev_path) else []
        print(f"resume at {done}/{len(todo)}", flush=True)
    todo = todo[done:]
    print(f"DKYC: parsing {len(todo)} txs", flush=True)
    completed = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(foot_check3.fetch_tx_robust, s["signature"]): s for s in todo}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                tx = fut.result()
            except Exception:
                tx = None
            evs = parse_gentle.events_of(MINT, s["signature"], s.get("blockTime"), tx)
            events.extend(evs)
            completed += 1
            if completed % 250 == 0:
                json.dump(events, open(ev_path, "w"))
                json.dump({"done": done + completed}, open(prog_path, "w"))
                print(f"  progress {done+completed} ({completed/max(time.time()-t0,1):.2f}/s)", flush=True)
    json.dump(events, open(ev_path, "w"))
    json.dump({"done": done + completed}, open(prog_path, "w"))
    print(f"DKYC PARSE DONE: {len(events)} events from {done+completed} txs", flush=True)

if __name__ == "__main__":
    main()
