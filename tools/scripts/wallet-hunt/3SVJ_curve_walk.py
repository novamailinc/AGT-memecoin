#!/usr/bin/env python3
"""3SVJ curve-phase signature walk via Solana PUBLIC RPC.
Walks the MINT's signatures backward from the migration tx
(oldest pool-window sig, which includes the mint), collecting
pre-migration (bonding-curve) sigs. Stops at empty page (coin
creation reached), MAX_PAGES, or persistent 429.
"""
import json, os, urllib.request, urllib.error, time

RPC = "https://api.mainnet-beta.solana.com"
MINT = "3SVjZ7BfT9FxiR49nZc7UdBmLgzRLJY7XBZDKhEFpump"
MIG = 1792871066
CURSOR = "3esH2HcJHAu8MVFbaLGu7NkJQE7GtjjNyyapuDNeZ65Y5y1RqzY9rUhfdFbMAMSnACftCCG7CAZf7YdJf4M8wF57"
OUT = os.path.expanduser("~/workspace/wallet-hunt")
LOG = os.path.join(OUT, "3SVJ_curve_walk.log")
OUTP = os.path.join(OUT, "3SVJ_curve_sigs.json")
MAX_PAGES = 150
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
    kept, before, pages = [], CURSOR, 0
    earliest = None
    while pages < MAX_PAGES:
        res = rpc("getSignaturesForAddress", [MINT, {"limit": 1000, "before": before}])
        if res == "RATE_LIMITED":
            log("RATE LIMITED persistently — stopping, saving partial")
            break
        if not res:
            log("empty page — coin creation reached (or list end)")
            break
        pages += 1
        for s in res:
            bt = s.get("blockTime")
            if bt is None: continue
            if bt < MIG:
                kept.append({"sig": s["signature"], "slot": s["slot"],
                             "blockTime": bt, "err": s.get("err") is not None})
                earliest = bt if earliest is None else min(earliest, bt)
        log(f"page {pages}: {len(res)} sigs, oldest_bt={res[-1].get('blockTime')}, kept={len(kept)}, earliest={earliest}")
        json.dump({"sigs": kept, "pages": pages, "earliest_bt": earliest,
                   "cursor": res[-1]["signature"]}, open(OUTP, "w"))
        before = res[-1]["signature"]
        time.sleep(PACE)
    log(f"DONE pages={pages} kept={len(kept)} earliest_bt={earliest}")
    json.dump({"sigs": kept, "pages": pages, "earliest_bt": earliest}, open(OUTP, "w"))

if __name__ == "__main__":
    main()
