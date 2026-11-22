#!/usr/bin/env python3
"""3SVJ first-hour parse via Solana PUBLIC RPC.
Fetches hour-minus-window non-error pool txs (window txs already in
3SVJ_events.json) and extracts token events. Resumable.
Input: 3SVJ_pool_sigs.json. Output: 3SVJ_events_hour.json
(seeded with window events, so it is the full first-hour set).
"""
import json, os, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

W = os.path.expanduser("~/workspace/wallet-hunt")
sys.path.insert(0, W)
from parse_gentle import events_of

RPC = "https://api.mainnet-beta.solana.com"
MINT = "3SVjZ7BfT9FxiR49nZc7UdBmLgzRLJY7XBZDKhEFpump"
MIG, HOUR_END, WIN_END = 1792871066, 1792871066 + 3600, 1792871366

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
    hour = [s for s in sigs if not s.get("err") and WIN_END < s["blockTime"] <= HOUR_END]
    print(f"hour-minus-window non-error to fetch: {len(hour)}", flush=True)
    ev_path = os.path.join(W, "3SVJ_events_hour.json")
    events = []
    if os.path.exists(ev_path):
        try: events = json.load(open(ev_path))
        except Exception: events = []
    if not events:
        # Seed with window events so the file is the full first-hour set.
        try: events = json.load(open(os.path.join(W, "3SVJ_events.json")))
        except Exception: events = []
    done = {e["sig"] for e in events}
    todo = [s for s in hour if s["sig"] not in done]
    # Oldest-first: if the run stops early, the covered region is the
    # early hour (+300s forward) — the highest-value region for the
    # suffix test — instead of the late hour.
    todo.reverse()
    print(f"seeded {len(events)} events, remaining {len(todo)}", flush=True)
    bt_of = {s["sig"]: s["blockTime"] for s in todo}
    n = 0
    with ThreadPoolExecutor(max_workers=14) as ex:
        for sig, tx in ex.map(lambda s: (s["sig"], fetch_tx(s["sig"])), todo):
            evs = events_of(MINT, sig, bt_of[sig], tx) if tx else []
            events.extend(evs); n += 1
            if n % 200 == 0:
                json.dump(events, open(ev_path, "w"))
                print(f"fetched {n}/{len(todo)} events {len(events)}", flush=True)
    json.dump(events, open(ev_path, "w"))
    print(f"DONE fetched {n} events {len(events)}", flush=True)

if __name__ == "__main__":
    main()
