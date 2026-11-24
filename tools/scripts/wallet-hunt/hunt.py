#!/usr/bin/env python3
"""Find the friend's bot wallet: fingerprint = bought ~4.97M KIMCHI near graduation
(2026-10-20 ~23:50 UTC) for ~$238. Cross-check: also bought KWH near its graduation
(2026-10-22 ~10:20 UTC). Public on-chain data only."""
import json, os, sys, time, urllib.request, urllib.error

HOME = os.path.expanduser("~")
OUT = os.path.expanduser("~/workspace/wallet-hunt")
os.makedirs(OUT, exist_ok=True)

def load_key():
    for name in ("api_key_discovery", "api_key"):
        p = os.path.join(HOME, ".forex-agent/helius", name)
        if os.path.exists(p):
            return open(p).read().strip()
    raise SystemExit("no helius key")

KEY = load_key()
RPC = f"https://mainnet.helius-rpc.com/?api-key={KEY}"

def rpc(method, params, tries=5):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                d = json.loads(r.read())
            if "error" in d:
                raise RuntimeError(str(d["error"])[:200])
            return d.get("result")
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))

def walk_signatures(mint, stop_ts, max_pages=120):
    """Walk signatures newest->oldest until blockTime < stop_ts. Returns list oldest-last."""
    sigs, before = [], None
    for page in range(max_pages):
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        res = rpc("getSignaturesForAddress", [mint, opts]) or []
        if not res:
            break
        sigs.extend(res)
        before = res[-1]["signature"]
        earliest = res[-1].get("blockTime") or 0
        print(f"  page {page}: +{len(res)} total {len(sigs)} earliest {earliest}", flush=True)
        if earliest and earliest < stop_ts:
            break
        time.sleep(0.25)
    return sigs

def parse_buyers(mint, sigs):
    """For each tx, compute per-owner token delta for mint. Return list of events."""
    events = []
    for i, s in enumerate(sigs):
        sig = s["signature"]
        try:
            tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        except Exception as e:
            print(f"  tx {i} fetch fail: {e}", flush=True)
            continue
        if not tx:
            continue
        meta = tx.get("meta") or {}
        if meta.get("err"):
            continue
        pre = {(b["accountIndex"], b.get("owner")): b for b in meta.get("preTokenBalances") or [] if b.get("mint") == mint}
        post = {(b["accountIndex"], b.get("owner")): b for b in meta.get("postTokenBalances") or [] if b.get("mint") == mint}
        keys = (tx.get("transaction", {}).get("message", {}) or {}).get("accountKeys") or []
        def amt(b):
            try:
                return int(b["uiTokenAmount"]["amount"]) / (10 ** int(b["uiTokenAmount"]["decimals"]))
            except Exception:
                return 0.0
        owners = {}
        for k in set(pre) | set(post):
            owner = k[1]
            if not owner:
                continue
            d = amt(post.get(k, {})) - amt(pre.get(k, {})) if k in post or k in pre else 0.0
            if k in post and k not in pre:
                d = amt(post[k])
            if k in pre and k not in post:
                d = -amt(pre[k])
            if d != 0:
                owners[owner] = owners.get(owner, 0.0) + d
        # SOL delta for owners present as account keys
        pre_b = meta.get("preBalances") or []
        post_b = meta.get("postBalances") or []
        acct_list = [a.get("pubkey") if isinstance(a, dict) else a for a in keys]
        for owner, d in owners.items():
            sol = None
            if owner in acct_list:
                idx = acct_list.index(owner)
                if idx < len(pre_b) and idx < len(post_b):
                    sol = (post_b[idx] - pre_b[idx]) / 1e9
            events.append({"sig": sig, "ts": s.get("blockTime"), "owner": owner,
                           "token_delta": d, "sol_delta": sol})
        if i % 200 == 0:
            print(f"  parsed {i}/{len(sigs)}", flush=True)
        time.sleep(0.12)
    return events

def main():
    jobs = [
        ("KIMCHI", "7thgKgtK1rp1UtLpWUZEuu9vakedTMoZzuyps6RGpump", 1792537000, 1792540200 - 900, 1792540200 + 2700),
        ("KWH", "5hyyjwZqJgnPZorMdb7X4r2J1dT3VujKVkLD8Ptbpump", 1792663800, 1792664400 - 600, 1792664400 + 2700),
    ]
    all_events = {}
    for name, mint, stop_ts, w0, w1 in jobs:
        print(f"== {name} {mint} walk back to {stop_ts}", flush=True)
        sigs = walk_signatures(mint, stop_ts)
        json.dump(sigs, open(f"{OUT}/{name}_sigs.json", "w"))
        window = [s for s in sigs if s.get("blockTime") and w0 <= s["blockTime"] <= w1 and not s.get("err")]
        print(f"== {name}: {len(sigs)} sigs total, {len(window)} in window [{w0},{w1}]", flush=True)
        events = parse_buyers(mint, window)
        json.dump(events, open(f"{OUT}/{name}_buyers.json", "w"))
        all_events[name] = events
        buys = [e for e in events if e["token_delta"] > 1000]
        print(f"== {name}: {len(buys)} buy events", flush=True)
    # KIMCHI fingerprint: single-tx delta 4.2M..5.8M, or cumulative in window near 4.97M
    k = all_events.get("KIMCHI", [])
    print("\n--- KIMCHI single-tx buys 3M..7M ---", flush=True)
    for e in sorted(k, key=lambda x: -x["token_delta"]):
        if 3_000_000 <= e["token_delta"] <= 7_000_000:
            print(json.dumps(e), flush=True)
    from collections import defaultdict
    cum = defaultdict(float)
    for e in k:
        if e["token_delta"] > 0:
            cum[e["owner"]] += e["token_delta"]
    print("\n--- KIMCHI cumulative window buys 4M..6M ---", flush=True)
    for o, v in sorted(cum.items(), key=lambda x: -x[1]):
        if 4_000_000 <= v <= 6_000_000:
            print(o, round(v), flush=True)
    w = all_events.get("KWH", [])
    kwh_buyers = {e["owner"] for e in w if e["token_delta"] > 1000}
    print(f"\n--- KWH window buyers: {len(kwh_buyers)} ---", flush=True)
    inter = set(cum) & kwh_buyers
    print("--- INTERSECTION KIMCHI-window buyers ∩ KWH-window buyers ---", flush=True)
    for o in sorted(inter):
        print(o, "kimchi_cum=", round(cum[o]), flush=True)
    json.dump({"kimchi_cum": {o: v for o, v in cum.items() if v > 100000},
               "kwh_buyers": sorted(kwh_buyers), "intersection": sorted(inter)},
              open(f"{OUT}/summary.json", "w"), indent=1)
    print("DONE", flush=True)

if __name__ == "__main__":
    main()
