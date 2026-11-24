#!/usr/bin/env python3
"""3SVJ window-tx parse via Solana PUBLIC RPC (Helius capped).
Input: 3SVJ_pool_sigs.json (pool sigs, blockTime in [MIG, MIG+3600]).
Fetches non-error txs (v1-aware, jsonParsed) and extracts token
events via parse_gentle.events_of. Resumable; gentle pacing.
Usage: python3 3SVJ_parse.py [max_minutes]  (default: whole list)
Output: 3SVJ_events.json, 3SVJ_parse_progress.json.
"""
import json, os, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

W = os.path.expanduser("~/workspace/wallet-hunt")
sys.path.insert(0, W)
from parse_gentle import events_of

RPC = "https://api.mainnet-beta.solana.com"
MINT = "3SVjZ7BfT9FxiR49nZc7UdBmLgzRLJY7XBZDKhEFpump"
MIG, WIN_END = 1792871066, 1792871366

def fetch_tx(sig):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                       "params": [sig, {"encoding": "jsonParsed",
                                        "maxSupportedTransactionVersion": 1}]}).encode()
    for i in range(4):
        try:
            req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read()).get("result")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(min(60, 10 * (i + 1)))
            else:
                time.sleep(2 * (i + 1))
        except Exception:
            time.sleep(2 * (i + 1))
    return None

def main():
    sigs = json.load(open(os.path.join(W, "3SVJ_pool_sigs.json")))["sigs"]
    window = [s for s in sigs if not s.get("err") and MIG <= s["blockTime"] <= WIN_END]
    print(f"pool sigs: {len(sigs)}, window non-error to fetch: {len(window)}", flush=True)
    ev_path = os.path.join(W, "3SVJ_events.json")
    events = []
    if os.path.exists(ev_path):
        try: events = json.load(open(ev_path))
        except Exception: events = []
    done = {e["sig"] for e in events}
    todo = [s for s in window if s["sig"] not in done]
    print(f"resuming with {len(events)} events, remaining {len(todo)}", flush=True)
    bt_of = {s["sig"]: s["blockTime"] for s in todo}
    n = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for sig, tx in ex.map(lambda s: (s["sig"], fetch_tx(s["sig"])), todo):
            evs = events_of(MINT, sig, bt_of[sig], tx) if tx else []
            events.extend(evs); n += 1
            if n % 100 == 0:
                json.dump(events, open(ev_path, "w"))
                print(f"fetched {n}/{len(todo)} events {len(events)}", flush=True)
            time.sleep(0.1)
    json.dump(events, open(ev_path, "w"))
    print(f"DONE fetched {n} events {len(events)}", flush=True)

if __name__ == "__main__":
    main()
