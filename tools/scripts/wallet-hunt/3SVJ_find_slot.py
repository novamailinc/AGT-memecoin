#!/usr/bin/env python3
"""Find the 3SVJ migration slot by binary-searching getBlock blockTime.
Migration blockTime target: 1792871066 (DexScreener pairCreatedAt,
confirmed by grad_raw entry at +2s). Prints the slot whose blockTime
is closest to but <= target, plus the first slot above target.
"""
import json, os, urllib.request, urllib.error, time

KEY = open(os.path.expanduser("~/.forex-agent/helius/api_key_discovery")).read().strip()
RPC = f"https://mainnet.helius-rpc.com/?api-key={KEY}"
TARGET = 1792871066

def rpc(method, params, tries=6):
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

def bt(slot):
    b = rpc("getBlock", [slot, {"transactionDetails": "none", "maxSupportedTransactionVersion": 1}])
    return None if b is None else b.get("blockTime")

def main():
    cur = rpc("getSlot", [])
    print("current slot:", cur, flush=True)
    # Anchor: DKYC migration slot 457253920 at 1792070830.
    est = 457253920 + int((TARGET - 1792070830) * 2.5)
    print("estimate:", est, "blockTime:", bt(est), flush=True)
    lo, hi = est - 20000, est + 20000
    # Invariant: bt(lo) <= TARGET <= bt(hi) (widen if broken)
    while (b := bt(lo)) is not None and b > TARGET:
        lo -= 20000
    while (b := bt(hi)) is not None and b < TARGET:
        hi += 20000
    print(f"bracket: lo={lo} bt={bt(lo)} hi={hi} bt={bt(hi)}", flush=True)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        m = bt(mid)
        if m is None:
            hi = mid
        elif m <= TARGET:
            lo = mid
        else:
            hi = mid
    print(f"MIGRATION SLOT: lo={lo} bt={bt(lo)} | first_above={hi} bt={bt(hi)}", flush=True)

if __name__ == "__main__":
    main()
