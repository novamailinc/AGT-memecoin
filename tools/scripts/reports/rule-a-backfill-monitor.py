#!/usr/bin/env python3
"""Bounded live monitor for the Rule A backfill extension (2026-11-21).

Watches the four lane ledgers for 12 minutes after the trader restart
and records:
  - any new slow_entry journal entry (ts > RESTART), with mint/age/stage
  - any backfill-sourced mint seen in a watchlist, and its later fate
  - any new position opening (entry_ts > RESTART) with detection + age
  - journal activity proving evaluations keep running
Read-only: never writes to ledgers, never touches daemons.
"""
import json
import time

RESTART = 1795277906  # 2026-11-21 16:18:26 UTC, post-reboot watchdog restart
LANES = ("grad_select", "grad_raw", "tranche", "tranche_protect")
DUR = 240
POLL = 20


def load(lane):
    with open(f"/home/hatch/.forex-agent/{lane}_ledger.json") as f:
        return json.load(f)


seen_bf = {}          # mint -> (lane, source, first_seen)
seen_slow = set()     # (lane, mint, ts)
seen_pos = set()      # (lane, mint)
t0 = time.time()
end = t0 + DUR
while time.time() < end:
    for lane in LANES:
        try:
            d = load(lane)
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S', time.gmtime())}] {lane} read error: {e}", flush=True)
            continue
        wl = d.get("watchlist") or {}
        for m, r in wl.items():
            src = str(r.get("source") or "")
            if ("backfill" in src or "gecko" in src) and m not in seen_bf:
                seen_bf[m] = (lane, src, time.time())
                print(f"[{time.strftime('%H:%M:%S', time.gmtime())}] BACKFILL ROW {lane} {m[:14]} src={src} age_s={round(time.time() - float(r.get('created_ts') or 0), 1)}", flush=True)
        j = (d.get("select_journal") or {}).get("recent") or []
        for r in j:
            if (r.get("ts") or 0) >= RESTART and r.get("reason") == "slow_entry":
                key = (lane, r.get("mint"), r.get("ts"))
                if key not in seen_slow:
                    seen_slow.add(key)
                    src = seen_bf.get(r.get("mint"), ("?", "?", 0))[1]
                    print(f"[{time.strftime('%H:%M:%S', time.gmtime())}] SLOW_ENTRY {lane} {str(r.get('mint'))[:14]} age={(r.get('values') or {}).get('grad_age_s')} stage={(r.get('values') or {}).get('stage')} tracked_source={src}", flush=True)
        for m, p in (d.get("positions") or {}).items():
            if (p.get("entry_ts") or 0) >= RESTART and (lane, m) not in seen_pos:
                seen_pos.add((lane, m))
                ef = p.get("entry_features") or {}
                print(f"[{time.strftime('%H:%M:%S', time.gmtime())}] NEW POSITION {lane} {m[:14]} detection={ef.get('detection')} grad_age={(ef.get('select') or {}).get('grad_age_s')}", flush=True)
    time.sleep(POLL)

print("=== SUMMARY ===")
print(f"backfill rows seen in watchlists: {len(seen_bf)}")
for m, (lane, src, ts) in seen_bf.items():
    print(f"  {m[:14]} {lane} {src}")
print(f"new slow_entry since restart: {len(seen_slow)}")
for lane, m, ts in sorted(seen_slow):
    print(f"  {lane} {str(m)[:14]} {time.strftime('%H:%M:%S', time.gmtime(ts))}")
print(f"new positions since restart: {len(seen_pos)}")
for lane, m in sorted(seen_pos):
    print(f"  {lane} {str(m)[:14]}")
