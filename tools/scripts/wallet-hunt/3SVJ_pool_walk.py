#!/usr/bin/env python3
"""3SVJ pool signature walk via Solana PUBLIC RPC (Helius discovery key
is 429-capped, so this route replaces the Helius blockscan).
Walks the PumpSwap pool's signatures newest->oldest, collecting sigs
with blockTime in [MIG, MIG+3600] (300s window + first hour).
Gentle pacing (~1.2s/call); stops on persistent 429s.
"""
import json, os, urllib.request, urllib.error, time

RPC = "https://api.mainnet-beta.solana.com"
POOL = "3tXP9X2W1aL9jtZF1tuAwEZMuX4w3PWNvVAHPJjd3MAq"
MIG, HOUR_END = 1792871066, 1792871066 + 3600
OUT = os.path.expanduser("~/workspace/wallet-hunt")
LOG = os.path.join(OUT, "3SVJ_pool_walk.log")
OUTP = os.path.join(OUT, "3SVJ_pool_sigs.json")
MAX_PAGES = 500
PACE = 1.2

def log(msg):
    with open(LOG, "a") as f: f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    print(msg, flush=True)

def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for i in range(3):
        try:
            req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as r:
                j = json.loads(r.read())
            if "error" in j:
                log(f"rpc error: {j['error']}")
                return None
            return j.get("result")
        except urllib.error.HTTPError as e:
            log(f"HTTP {e.code} attempt {i}")
            if e.code == 429:
                if i == 2: return "RATE_LIMITED"
                time.sleep(10 * (i + 1))
            else:
                time.sleep(5)
        except Exception as e:
            log(f"{type(e).__name__} attempt {i}")
            time.sleep(5)
    return None

def main():
    kept, before, pages = [], None, 0
    # Resume: if a prior run saved a cursor, continue from it.
    if os.path.exists(OUTP):
        try:
            prev = json.load(open(OUTP))
            if prev.get("cursor") and prev.get("pages", 0) > 0:
                kept = prev.get("sigs", [])
                before = prev["cursor"]
                pages = prev["pages"]
                log(f"resuming from page {pages} cursor, kept={len(kept)}")
        except Exception:
            pass
    newest_bt, reached_mig = None, False
    while pages < MAX_PAGES:
        params = [{"limit": 1000, "before": before} if before else {"limit": 1000}]
        # getSignaturesForAddress(address, config)
        res = rpc("getSignaturesForAddress", [POOL, params[0]])
        if res == "RATE_LIMITED":
            log("RATE LIMITED persistently — stopping walk, saving partial")
            break
        if not res:
            log("empty/failed page — stopping")
            break
        pages += 1
        if newest_bt is None:
            newest_bt = res[0].get("blockTime")
        oldest_bt = res[-1].get("blockTime")
        for s in res:
            bt = s.get("blockTime")
            if bt is None: continue
            if MIG <= bt <= HOUR_END:
                kept.append({"sig": s["signature"], "slot": s["slot"],
                             "blockTime": bt, "err": s.get("err") is not None})
        log(f"page {pages}: {len(res)} sigs, oldest_bt={oldest_bt}, kept={len(kept)}")
        json.dump({"sigs": kept, "pages": pages,
                   "cursor": res[-1]["signature"]}, open(OUTP, "w"))
        if oldest_bt is not None and oldest_bt < MIG:
            reached_mig = True
            break
        before = res[-1]["signature"]
        time.sleep(PACE)
    log(f"DONE pages={pages} kept={len(kept)} reached_mig={reached_mig} newest_bt={newest_bt}")
    json.dump({"sigs": kept, "pages": pages, "reached_mig": reached_mig},
              open(OUTP, "w"))

if __name__ == "__main__":
    main()
