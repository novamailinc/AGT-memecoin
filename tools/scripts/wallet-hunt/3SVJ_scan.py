#!/usr/bin/env python3
"""3SVJ migration-window block scan (adapted from dkyc_scan.py).
Collects tx sigs whose accountKeys include MINT (primary) or POOL
(cross-check) over slots covering [MIG, MIG+300s].
Range is set by 3SVJ_find_slot.py output; edit START/END below.
"""
import json, os, urllib.request, urllib.error, time
from concurrent.futures import ThreadPoolExecutor

KEY = open(os.path.expanduser("~/.forex-agent/helius/api_key_discovery")).read().strip()
RPC = f"https://mainnet.helius-rpc.com/?api-key={KEY}"
MINT = "3SVjZ7BfT9FxiR49nZc7UdBmLgzRLJY7XBZDKhEFpump"
POOL = "3tXP9X2W1aL9jtZF1tuAwEZMuX4w3PWNvVAHPJjd3MAq"
OUT = os.path.expanduser("~/workspace/wallet-hunt")
LOG = os.path.join(OUT, "3SVJ_scan.log")
PART = os.path.join(OUT, "3SVJ_window_sigs_partial.json")
START, END = 0, 0  # set by finder
MIG, WIN_END = 1792871066, 1792871366

def log(msg):
    with open(LOG, "a") as f: f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

def rpc(method, params, tries=8):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read()).get("result")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(min(60, 5 * (i + 1)))
            else:
                time.sleep(min(30, 2 ** i + 2))
        except Exception:
            time.sleep(min(30, 2 ** i + 2))
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
    assert START and END, "set START/END from 3SVJ_find_slot.py"
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
    mint_u = {s["sig"]: s for s in mint_all}
    pool_u = {s["sig"]: s for s in pool_all}
    json.dump({"mint": list(mint_u.values()), "pool": list(pool_u.values())},
              open(os.path.join(OUT, "3SVJ_window_sigs.json"), "w"))
    print(f"DONE window mint-sigs {len(mint_u)} pool-sigs {len(pool_u)}", flush=True)

if __name__ == "__main__":
    main()
