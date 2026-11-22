#!/usr/bin/env python3
"""V1-aware spot check: re-fetch a wallet's window sigs with
maxSupportedTransactionVersion=1 and report any token deltas for the
target mints owned by the wallet. Closes the parse_gentle v0 blind spot
for the decisive KIMCHI/KWH absence claims.
Usage: trio_v1_check.py <sigs_json> <wallet> <mint1> [mint2 ...]
Prints one line per (sig, mint) hit plus a summary."""
import json, os, sys, time, urllib.request
sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
import hunt2
from concurrent.futures import ThreadPoolExecutor, as_completed
import parse_gentle

KEY = open(os.path.expanduser("~/.forex-agent/helius/api_key_discovery")).read().strip()
URL = f"https://mainnet.helius-rpc.com/?api-key={KEY}"

def fetch_v1(sig):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                       "params": [sig, {"encoding": "json",
                                        "maxSupportedTransactionVersion": 1}]}).encode()
    parse_gentle.PACER.wait()
    for attempt in range(4):
        try:
            req = urllib.request.Request(URL, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
            if d.get("error"):
                parse_gentle.PACER.on_429()
                time.sleep(1.0 * (attempt + 1))
                continue
            return d.get("result")
        except Exception:
            parse_gentle.PACER.on_429()
            time.sleep(1.0 * (attempt + 1))
    return None

def main():
    sig_path, wallet = sys.argv[1], sys.argv[2]
    mints = set(sys.argv[3:])
    sigs = [s for s in json.load(open(sig_path)) if not s.get("err")]
    print(f"{sig_path}: v1-checking {len(sigs)} sigs for mints {sorted(mints)}", flush=True)
    hits, got, failed = [], 0, 0
    def amt(b):
        try:
            return int(b["uiTokenAmount"]["amount"]) / (10 ** int(b["uiTokenAmount"]["decimals"]))
        except Exception:
            return 0.0
    def work(s):
        tx = fetch_v1(s["signature"])
        if not tx:
            return (s, None)
        meta = tx.get("meta") or {}
        if meta.get("err"):
            return (s, [])
        pre, post = {}, {}
        for b in meta.get("preTokenBalances") or []:
            if b.get("owner") == wallet and b.get("mint") in mints:
                pre[b["mint"]] = pre.get(b["mint"], 0.0) + amt(b)
        for b in meta.get("postTokenBalances") or []:
            if b.get("owner") == wallet and b.get("mint") in mints:
                post[b["mint"]] = post.get(b["mint"], 0.0) + amt(b)
        out = []
        for m in set(pre) | set(post):
            d = post.get(m, 0.0) - pre.get(m, 0.0)
            if d != 0:
                out.append({"sig": s["signature"], "ts": s.get("blockTime"), "mint": m,
                            "token_delta": d})
        return (s, out)
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(work, s): s for s in sigs}
        for fut in as_completed(futs):
            s, res = fut.result()
            if res is None:
                failed += 1
            else:
                got += 1
                hits.extend(res)
    print(f"fetched ok {got}, failed {failed}")
    for h in hits:
        print("HIT", json.dumps(h))
    print(f"TOTAL HITS: {len(hits)}")

if __name__ == "__main__":
    main()
