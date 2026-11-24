#!/usr/bin/env python3
"""Parallel scan of DKYC migration-window blocks. Collects tx sigs whose
accountKeys include MINT (primary, hunt-method) or POOL (cross-check).
Range: slots [457253920, 457254770] (migration block + ~850 slots ~ 300s+)."""
import json, os, urllib.request, urllib.error, time
from concurrent.futures import ThreadPoolExecutor

KEY = open(os.path.expanduser("~/.forex-agent/helius/api_key_discovery")).read().strip()
RPC = f"https://mainnet.helius-rpc.com/?api-key={KEY}"
MINT = "DKYcjobF7HPtL9RyQBRdeRQ2iWvizpfbgWwPWNXdpump"
POOL = "7Te1wXcfeQjKLGHXFvxSCnGZTqunoZ9fxBn7KKjrkNEr"
OUT = os.path.expanduser("~/workspace/wallet-hunt")
LOG = os.path.join(OUT, "DKYC_scan.log")
PART = os.path.join(OUT, "DKYC_window_sigs_partial.json")
START, END = 457253920, 457254770
MIG, WIN_END = 1792070830, 1792071130

def log(msg):
    with open(LOG, "a") as f: f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

def rpc(method, params, tries=8):
    body = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(RPC, data=body, headers={"Content-Type":"application/json"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read()).get("result")
        except urllib.error.HTTPError as e:
            time.sleep(min(30, 2**i + 2))
        except Exception:
            time.sleep(min(30, 2**i + 2))
    return None

def scan_slot(slot):
    b = rpc("getBlock", [slot, {"transactionDetails": "accounts", "maxSupportedTransactionVersion": 1}])
    if b is None: return slot, None, [], []
    bt = b.get("blockTime")
    mint_sigs, pool_sigs = [], []
    for t in b.get("transactions", []):
        ks = [k["pubkey"] for k in t["transaction"].get("accountKeys", [])]
        sig = t["transaction"]["signatures"][0]
        err = t["meta"].get("err")
        if MINT in ks: mint_sigs.append({"sig": sig, "slot": slot, "blockTime": bt, "err": bool(err)})
        if POOL in ks: pool_sigs.append({"sig": sig, "slot": slot, "blockTime": bt, "err": bool(err)})
    return slot, bt, mint_sigs, pool_sigs

def main():
    slots = list(range(START, END + 1))
    mint_all, pool_all = [], []
    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for slot, bt, ms, ps in ex.map(scan_slot, slots):
            done += 1
            if bt is not None and MIG <= bt <= WIN_END:
                mint_all.extend(ms); pool_all.extend(ps)
            if done % 50 == 0:
                log(f"scanned {done}/{len(slots)} slots; window mint-sigs {len(mint_all)} pool-sigs {len(pool_all)}")
                json.dump({"mint": mint_all, "pool": pool_all}, open(PART, "w"))
    # dedupe (a tx appears once per block, but be safe)
    mint_u = {s["sig"]: s for s in mint_all}
    pool_u = {s["sig"]: s for s in pool_all}
    out = {"mint": sorted(mint_u.values(), key=lambda s: s["slot"]),
           "pool": sorted(pool_u.values(), key=lambda s: s["slot"])}
    json.dump(out, open(os.path.join(OUT, "DKYC_window_sigs.json"), "w"))
    log(f"DONE slots {done} window mint-sigs {len(out['mint'])} (nonerr {sum(1 for s in out['mint'] if not s['err'])}) pool-sigs {len(out['pool'])}")
    print(f"DONE mint {len(out['mint'])} pool {len(out['pool'])}", flush=True)

if __name__ == "__main__":
    main()
