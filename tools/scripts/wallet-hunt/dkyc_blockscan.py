#!/usr/bin/env python3
"""DKYC migration-window buyer set via getBlock scan (bounded, parallel).
Step 1: binary-search the slot containing MIG via getBlock(transactionDetails=none).
Step 2: scan blocks [slot_mig-150, slot_mig+900] with transactionDetails=accounts,
        keep txs whose accountKeys include POOL or MINT.
Step 3 (separate driver): fetch those txs v1-aware and extract events."""
import json, os, urllib.request, urllib.error, time, sys

KEY = open(os.path.expanduser("~/.forex-agent/helius/api_key_discovery")).read().strip()
RPC = f"https://mainnet.helius-rpc.com/?api-key={KEY}"
MINT = "DKYcjobF7HPtL9RyQBRdeRQ2iWvizpfbgWwPWNXdpump"
POOL = "7Te1wXcfeQjKLGHXFvxSCnGZTqunoZ9fxBn7KKjrkNEr"
MIG = 1792070830

def rpc(method, params, tries=6):
    body = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(RPC, data=body, headers={"Content-Type":"application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read()).get("result")
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            time.sleep(min(30, 2**i + 1))
        except Exception as e:
            last = f"{type(e).__name__}:{str(e)[:80]}"
            time.sleep(min(30, 2**i + 1))
    print(f"  RPC {method} FAILED: {last}", flush=True)
    return None

def block_time(slot):
    b = rpc("getBlock", [slot, {"transactionDetails": "none", "maxSupportedTransactionVersion": 1}])
    return b.get("blockTime") if b else None

def slot_at_time(target):
    cur = rpc("getSlot", [])
    print(f"current slot: {cur}", flush=True)
    lo, hi = cur - 4200000, cur  # ~10.2 days back at 400ms/slot
    # find a valid block at lo (scan forward over skipped slots)
    def bt_or_next(slot):
        for d in range(0, 50):
            t = block_time(slot + d)
            if t is not None: return slot + d, t
        return None, None
    lo_s, lo_t = bt_or_next(lo)
    print(f"lo probe: slot {lo_s} time {lo_t}", flush=True)
    if lo_t is None or lo_t > target:
        print("lo probe already past target or invalid", flush=True); return None
    while hi - lo_s > 1:
        mid = (lo_s + hi) // 2
        t = block_time(mid)
        if t is None:
            # skipped slot: search forward a bit for a real block; if none, treat as below
            found = None
            for d in range(1, 40):
                t2 = block_time(mid + d)
                if t2 is not None: found = (mid + d, t2); break
            if found is None:
                hi = mid; continue
            mid, t = found
        if t <= target: lo_s = mid
        else: hi = mid
    print(f"slot at/just before MIG {target}: {lo_s} (blockTime {block_time(lo_s)})", flush=True)
    return lo_s

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "search"
    if mode == "search":
        s = slot_at_time(MIG)
        print("SLOT_MIG:", s)
    elif mode == "probe":
        slot = int(sys.argv[2])
        b = rpc("getBlock", [slot, {"transactionDetails": "accounts", "maxSupportedTransactionVersion": 1}])
        if b is None: print("block None"); sys.exit(0)
        txs = b.get("transactions", [])
        print(f"block {slot} blockTime {b.get('blockTime')} txs {len(txs)}")
        if txs:
            t0 = txs[0]
            print("tx0 keys:", list(t0.keys()))
            print("tx0 accountKeys sample:", (t0.get("accountKeys") or t0.get("transaction", {}).get("accountKeys") or [])[:5])
            hits = [t for t in txs if POOL in (t.get("accountKeys") or []) or MINT in (t.get("accountKeys") or [])]
            print("pool/mint txs in block:", len(hits))
