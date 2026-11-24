#!/usr/bin/env python3
"""DKYC signature walk, hardened: per-page progress files + log, so a kill
never loses work. Resumes from DKYC_sigs_partial.json if present."""
import json, os, urllib.request, urllib.error, time, sys

MINT = "DKYcjobF7HPtL9RyQBRdeRQ2iWvizpfbgWwPWNXdpump"
KEY = open(os.path.expanduser("~/.forex-agent/helius/api_key_discovery")).read().strip()
RPC = f"https://mainnet.helius-rpc.com/?api-key={KEY}"
OUT = os.path.expanduser("~/workspace/wallet-hunt")
MIG_CAND = 1792070830
PART = os.path.join(OUT, "DKYC_sigs_partial.json")
LOG = os.path.join(OUT, "DKYC_walk.log")

def log(msg):
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

def rpc_page(before):
    params = {"limit": 1000}
    if before: params["before"] = before
    body = json.dumps({"jsonrpc":"2.0","id":1,"method":"getSignaturesForAddress","params":[MINT, params]}).encode()
    last = None
    for i in range(8):
        try:
            req = urllib.request.Request(RPC, data=body, headers={"Content-Type":"application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read()).get("result") or [], None
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            time.sleep(min(30, 2**i + 2))
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:100]}"
            time.sleep(min(30, 2**i + 2))
    return None, last

def main():
    sigs, before = [], None
    if os.path.exists(PART):
        sigs = json.load(open(PART))
        if sigs: before = sigs[-1]["signature"]
        log(f"resume: {len(sigs)} sigs, cursor={str(before)[:16]}")
    pages = 0
    while True:
        batch, err = rpc_page(before)
        if batch is None:
            log(f"PAGE FAILED after retries (cursor={str(before)[:16]}): {err}; sleeping 60s and retrying same cursor")
            time.sleep(60)
            continue
        pages += 1
        if not batch:
            log("walk ended: empty page"); break
        sigs.extend(batch)
        before = batch[-1]["signature"]
        oldest = batch[-1].get("blockTime")
        json.dump(sigs, open(PART, "w"))
        log(f"page {pages}: total={len(sigs)} oldest={oldest} ({time.strftime('%m-%d %H:%M', time.gmtime(oldest)) if oldest else '?'})")
        if oldest and oldest < MIG_CAND - 3600:
            log("reached before migration window"); break
        if len(batch) < 1000:
            log("walk ended: short page"); break
        time.sleep(0.2)
    json.dump(sigs, open(os.path.join(OUT, "DKYC_sigs.json"), "w"))
    win = [s for s in sigs if s.get("blockTime") and MIG_CAND <= s["blockTime"] <= MIG_CAND + 300 and not s.get("err")]
    err_win = [s for s in sigs if s.get("blockTime") and MIG_CAND <= s["blockTime"] <= MIG_CAND + 300 and s.get("err")]
    log(f"DONE total={len(sigs)} window_nonerr={len(win)} window_err={len(err_win)}")
    print(f"DONE total={len(sigs)} window_nonerr={len(win)} window_err={len(err_win)}", flush=True)

if __name__ == "__main__":
    main()
