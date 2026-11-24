#!/usr/bin/env python3
"""Batch-transport variant of parse_kwh_win.py: identical extraction
(parse_gentle.events_of verbatim), identical outputs (KWH_events_win.json /
KWH_win_progress.json), resumable. Fetches getTransaction in JSON-RPC batches
with adaptive backoff: any HTTP 429 shrinks batch size and adds pacing;
persistent 429s degrade to near-gentle rates rather than hammering."""
import json, os, sys, time, urllib.request, urllib.error, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from concurrent.futures import ThreadPoolExecutor
import parse_gentle as pg

OUT = os.path.expanduser("~/workspace/wallet-hunt")
MINT = "5hyyjwZqJgnPZorMdb7X4r2J1dT3VujKVkLD8Ptbpump"
MIG = 1792664400

STATE = {"batch": 15, "pace": 0.15, "lock": threading.Lock(), "n429": 0}

def on_429():
    with STATE["lock"]:
        STATE["n429"] += 1
        STATE["batch"] = max(3, STATE["batch"] // 2)
        STATE["pace"] = min(2.0, STATE["pace"] * 1.8 + 0.1)
        b, p, n = STATE["batch"], STATE["pace"], STATE["n429"]
    print(f"  [429 #{n}] batch -> {b}, pace -> {p:.2f}s", flush=True)

def fetch_batch(chunk):
    """chunk: list of sig dicts. Returns list of (sig_dict, tx_or_None)."""
    for attempt in range(6):
        with STATE["lock"]:
            b, pace = STATE["batch"], STATE["pace"]
        body = json.dumps([{"jsonrpc": "2.0", "id": i, "method": "getTransaction",
                            "params": [s["signature"], {"encoding": "jsonParsed",
                                     "maxSupportedTransactionVersion": 0}]}
                           for i, s in enumerate(chunk)]).encode()
        try:
            time.sleep(pace)
            req = urllib.request.Request(pg.RPC, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.loads(r.read())
            by_id = {x.get("id"): x.get("result") for x in d}
            return [(s, by_id.get(i)) for i, s in enumerate(chunk)]
        except urllib.error.HTTPError as e:
            if e.code == 429:
                on_429()
                time.sleep(min(20, 2 ** attempt))
                continue
            time.sleep(1.0 * (attempt + 1))
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    return [(s, None) for s in chunk]

def main():
    sigs = [s for s in json.load(open(f"{OUT}/KWH_win_sigs.json")) if not s.get("err")]
    ev_path = f"{OUT}/KWH_events_win.json"
    prog_path = f"{OUT}/KWH_win_progress.json"
    done = json.load(open(prog_path))["done"] if os.path.exists(prog_path) else 0
    events = json.load(open(ev_path)) if os.path.exists(ev_path) else []
    todo = sigs[done:]
    print(f"KWH-win-batch: resume at {done}/{len(sigs)}; parsing {len(todo)} txs; "
          f"seeded events {len(events)}", flush=True)
    t0 = time.time()
    completed = 0
    save_lock = threading.Lock()

    def work(chunk):
        nonlocal completed
        res = fetch_batch(chunk)
        evs_all = []
        for s, tx in res:
            evs_all.extend(pg.events_of(MINT, s["signature"], s.get("blockTime"), tx))
            for e in evs_all[-3:]:
                pass
        with save_lock:
            events.extend(evs_all)
            completed += len(chunk)
            for e in evs_all:
                if 4_200_000 <= e["token_delta"] <= 5_800_000:
                    print(f"  *** SINGLE HIT: {json.dumps(e)}", flush=True)
            if completed % 250 < len(chunk) or completed == len(todo):
                json.dump(events, open(ev_path, "w"))
                json.dump({"done": done + completed}, open(prog_path, "w"))
                el = time.time() - t0
                last_ts = chunk[-1].get("blockTime")
                print(f"  progress {done+completed}/{len(sigs)} "
                      f"({completed/el:.2f}/s this run, batch {STATE['batch']}, "
                      f"last_ts {last_ts} = mig{last_ts-MIG:+d}s)", flush=True)
        return len(chunk)

    # chunk dynamically by current batch size at submission time
    chunks = []
    i = 0
    while i < len(todo):
        n = STATE["batch"]
        chunks.append(todo[i:i + n])
        i += n
    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(work, chunks))
    json.dump(events, open(ev_path, "w"))
    json.dump({"done": len(sigs)}, open(prog_path, "w"))
    print(f"KWH-win-batch DONE: {len(sigs)} sigs, {len(events)} events", flush=True)

if __name__ == "__main__":
    main()
