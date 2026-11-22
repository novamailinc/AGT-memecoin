#!/usr/bin/env python3
"""Chronological chunk scan of KIMCHI_sigs_v2.json with early fingerprint reporting."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collections import defaultdict
import hunt2

OUT = hunt2.OUT
MINT = "7thgKgtK1rp1UtLpWUZEuu9vakedTMoZzuyps6RGpump"
sigs = json.load(open(f"{OUT}/KIMCHI_sigs_v2.json"))
sigs = [s for s in sigs if not s.get("err")]
print(f"scanning {len(sigs)} sigs in chunks", flush=True)

net, gross, solspent = defaultdict(float), defaultdict(float), defaultdict(float)
all_events = []
CH = 3000
for i in range(0, len(sigs), CH):
    chunk = sigs[i:i + CH]
    ev = hunt2.parse_events(MINT, chunk)
    all_events.extend(ev)
    for e in ev:
        o = e["owner"]
        net[o] += e["token_delta"]
        if e["token_delta"] > 0:
            gross[o] += e["token_delta"]
            if e["sol_delta"] is not None and e["sol_delta"] < 0:
                solspent[o] += -e["sol_delta"]
        if 4_200_000 <= e["token_delta"] <= 5_800_000:
            print(f"  *** SINGLE HIT chunk@{i}: {json.dumps(e)}", flush=True)
    ts_last = chunk[-1].get("blockTime")
    print(f"chunk @{i} done (last_ts {ts_last}); cum-net in range so far:", flush=True)
    for o, v in sorted(net.items(), key=lambda x: -x[1]):
        if 4_200_000 <= v <= 5_800_000:
            print(f"    CUM {o} net={v:,.0f} gross={gross[o]:,.0f} sol={solspent[o]:.4f}", flush=True)
    json.dump(all_events, open(f"{OUT}/KIMCHI_events_chunked.json", "w"))
print("CHUNK SCAN DONE", flush=True)
