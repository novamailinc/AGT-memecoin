#!/usr/bin/env python3
"""Window-narrowed KWH parse (sigs in [mig-600, mig+7200] snapshotted to
KWH_win_sigs.json). Reuses parse_gentle.fetch_tx / events_of verbatim (same
Pacer, same extraction). Resumable via KWH_win_progress.json; writes ONLY
KWH_events_win.json / KWH_win_progress.json — never touches the gentle files
used by the running full-parse chain."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from concurrent.futures import ThreadPoolExecutor, as_completed
import parse_gentle as pg

OUT = os.path.expanduser("~/workspace/wallet-hunt")
MINT = "5hyyjwZqJgnPZorMdb7X4r2J1dT3VujKVkLD8Ptbpump"
MIG = 1792664400

def main():
    sigs = [s for s in json.load(open(f"{OUT}/KWH_win_sigs.json")) if not s.get("err")]
    ev_path = f"{OUT}/KWH_events_win.json"
    prog_path = f"{OUT}/KWH_win_progress.json"
    events, done = [], 0
    if os.path.exists(prog_path):
        done = json.load(open(prog_path))["done"]
        events = json.load(open(ev_path)) if os.path.exists(ev_path) else []
    todo = sigs[done:]
    print(f"KWH-win: resume at {done}/{len(sigs)}; parsing {len(todo)} txs; "
          f"seeded events {len(events)}", flush=True)
    t0 = time.time()
    completed = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(pg.fetch_tx, s["signature"]): s for s in todo}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                tx = fut.result()
            except Exception:
                tx = None
            evs = pg.events_of(MINT, s["signature"], s.get("blockTime"), tx)
            events.extend(evs)
            for e in evs:
                if 4_200_000 <= e["token_delta"] <= 5_800_000:
                    print(f"  *** SINGLE HIT: {json.dumps(e)}", flush=True)
            completed += 1
            if completed % 250 == 0:
                json.dump(events, open(ev_path, "w"))
                json.dump({"done": done + completed}, open(prog_path, "w"))
                el = time.time() - t0
                last_ts = s.get("blockTime")
                print(f"  progress {done+completed}/{len(sigs)} "
                      f"({completed/el:.2f}/s this run, interval {pg.PACER.interval:.2f}s, "
                      f"last_ts {last_ts} = mig{last_ts-MIG:+d}s)", flush=True)
    json.dump(events, open(ev_path, "w"))
    json.dump({"done": len(sigs)}, open(prog_path, "w"))
    print(f"KWH-win DONE: {len(sigs)} sigs parsed, {len(events)} events", flush=True)

if __name__ == "__main__":
    main()
