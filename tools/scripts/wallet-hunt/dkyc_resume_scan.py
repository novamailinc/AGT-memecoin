#!/usr/bin/env python3
"""Resume the DKYC migration-window block scan from the cancelled run.

Reuses dkyc_scan.py's scan_slot (getBlock accounts, v1-aware).
Starts at slot 457254470 (partial max was 457254469) and scans forward
until a block's blockTime exceeds WIN_END, then stops (window filter
keeps only MIG <= bt <= WIN_END, so overshoot is harmless).
Merges into DKYC_window_sigs_partial.json every 50 slots (dedupe by sig)
and writes the final DKYC_window_sigs.json at the end.
"""
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
from dkyc_scan import scan_slot

W = os.path.expanduser("~/workspace/wallet-hunt")
MIG, WIN_END = 1792070830, 1792071130
PART = os.path.join(W, "DKYC_window_sigs_partial.json")
FINAL = os.path.join(W, "DKYC_window_sigs.json")
LOG = os.path.join(W, "DKYC_resume_scan.log")

def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def main():
    data = json.load(open(PART))
    mint = {s["sig"]: s for s in data["mint"]}
    pool = {s["sig"]: s for s in data["pool"]}
    start = max(s["slot"] for s in data["mint"] + data["pool"]) + 1
    log(f"resume: mint={len(mint)} pool={len(pool)} start_slot={start}")
    slot = start
    done = 0
    stop = False
    while not stop:
        batch = list(range(slot, slot + 50))
        with ThreadPoolExecutor(max_workers=4) as ex:
            for s_slot, bt, ms, ps in ex.map(scan_slot, batch):
                done += 1
                if bt is not None and MIG <= bt <= WIN_END:
                    for s in ms:
                        mint.setdefault(s["sig"], s)
                    for s in ps:
                        pool.setdefault(s["sig"], s)
                if bt is not None and bt > WIN_END:
                    stop = True
        slot += 50
        json.dump({"mint": sorted(mint.values(), key=lambda s: s["slot"]),
                   "pool": sorted(pool.values(), key=lambda s: s["slot"])},
                  open(PART, "w"))
        bts = [s["blockTime"] for s in mint.values() if s.get("blockTime")]
        log(f"scanned through slot {slot-1} ({done} slots this run); "
            f"window mint-sigs {len(mint)} pool-sigs {len(pool)}; "
            f"max window bt {max(bts) if bts else None}")
        if slot - start > 4000:
            log("SAFETY STOP: 4000 slots scanned without passing WIN_END")
            break
    out = {"mint": sorted(mint.values(), key=lambda s: s["slot"]),
           "pool": sorted(pool.values(), key=lambda s: s["slot"])}
    json.dump(out, open(FINAL, "w"))
    nonerr = sum(1 for s in out["mint"] if not s["err"])
    log(f"DONE window mint-sigs {len(out['mint'])} (nonerr {nonerr}) "
        f"pool-sigs {len(out['pool'])} slots_scanned_this_run {done}")
    print(f"DONE mint {len(out['mint'])} pool {len(out['pool'])}", flush=True)

if __name__ == "__main__":
    main()
