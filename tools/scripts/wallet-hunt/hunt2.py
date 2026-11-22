#!/usr/bin/env python3
"""Wallet hunt v2: getTransactionsForAddress sortOrder=asc walks from mint creation
forward through the graduation window (v1 walked newest-back and hit a 120k cap).
Fingerprint: KIMCHI buy ~4.97M tokens near graduation 2026-10-20 23:50 UTC.
Cross-check: same wallet bought KWH near 2026-10-22 10:20 UTC graduation."""
import json, os, time, urllib.request, urllib.error
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

HOME = os.path.expanduser("~")
OUT = os.path.expanduser("~/workspace/wallet-hunt")

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
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read())
            if "error" in d:
                raise RuntimeError(str(d["error"])[:200])
            return d.get("result")
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(1.2 * (i + 1))

def asc_walk(mint, t_stop, max_pages=3000):
    """Walk signatures oldest->newest until blockTime > t_stop. Returns list."""
    out, token = [], None
    for page in range(max_pages):
        opts = {"sortOrder": "asc", "limit": 100}
        if token:
            opts["paginationToken"] = token
        res = rpc("getTransactionsForAddress", [mint, opts])
        data = (res or {}).get("data") or []
        if not data:
            break
        out.extend(data)
        token = (res or {}).get("paginationToken")
        last_ts = data[-1].get("blockTime") or 0
        if page % 20 == 0:
            print(f"  walk page {page}: total {len(out)} last_ts {last_ts} token={bool(token)}", flush=True)
        if last_ts and last_ts > t_stop:
            break
        if not token:
            break
        time.sleep(0.05)
    return out

def fetch_tx(sig):
    try:
        return rpc("getTransaction", [sig, {"encoding": "jsonParsed",
                                             "maxSupportedTransactionVersion": 0}])
    except Exception:
        return None

def parse_events(mint, sigs):
    """Per-owner token delta for mint + SOL delta, per tx. Threaded."""
    sig_list = [s["signature"] for s in sigs]
    ts_of = {s["signature"]: s.get("blockTime") for s in sigs}
    events = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        txs = list(ex.map(fetch_tx, sig_list))
    for sig, tx in zip(sig_list, txs):
        if not tx:
            continue
        meta = tx.get("meta") or {}
        if meta.get("err"):
            continue
        def amt(b):
            try:
                return int(b["uiTokenAmount"]["amount"]) / (10 ** int(b["uiTokenAmount"]["decimals"]))
            except Exception:
                return 0.0
        pre, post = {}, {}
        for b in meta.get("preTokenBalances") or []:
            if b.get("mint") == mint and b.get("owner"):
                pre[b["owner"]] = pre.get(b["owner"], 0.0) + amt(b)
        for b in meta.get("postTokenBalances") or []:
            if b.get("mint") == mint and b.get("owner"):
                post[b["owner"]] = post.get(b["owner"], 0.0) + amt(b)
        owners = set(pre) | set(post)
        if not owners:
            continue
        keys = (tx.get("transaction", {}).get("message", {}) or {}).get("accountKeys") or []
        acct = [a.get("pubkey") if isinstance(a, dict) else a for a in keys]
        pre_b, post_b = meta.get("preBalances") or [], meta.get("postBalances") or []
        for owner in owners:
            d = post.get(owner, 0.0) - pre.get(owner, 0.0)
            if d == 0:
                continue
            sol = None
            if owner in acct:
                idx = acct.index(owner)
                if idx < len(pre_b) and idx < len(post_b):
                    sol = (post_b[idx] - pre_b[idx]) / 1e9
            events.append({"sig": sig, "ts": ts_of.get(sig), "owner": owner,
                           "token_delta": d, "sol_delta": sol})
    return events

def analyze(name, mint, grad_ts, events):
    w0, w1 = grad_ts - 2700, grad_ts + 2700
    win = [e for e in events if e["ts"] and w0 <= e["ts"] <= w1]
    single = [e for e in win if 4_200_000 <= e["token_delta"] <= 5_800_000]
    net = defaultdict(float)
    gross_buy = defaultdict(float)
    sol_spent = defaultdict(float)
    for e in win:
        net[e["owner"]] += e["token_delta"]
        if e["token_delta"] > 0:
            gross_buy[e["owner"]] += e["token_delta"]
            if e["sol_delta"] is not None and e["sol_delta"] < 0:
                sol_spent[e["owner"]] += -e["sol_delta"]
    cum = [{"owner": o, "net": round(net[o]), "gross_buy": round(gross_buy[o]),
            "sol_spent": round(sol_spent[o], 4)}
           for o in net if 4_200_000 <= net[o] <= 5_800_000]
    print(f"\n== {name}: window events {len(win)}, single-tx fingerprint hits {len(single)}, "
          f"cumulative-net hits {len(cum)}", flush=True)
    for e in single:
        print("  SINGLE:", json.dumps(e), flush=True)
    for c in cum:
        print("  CUM:", json.dumps(c), flush=True)
    buyers = {e["owner"] for e in win if e["token_delta"] > 1000}
    return {"window": [w0, w1], "single_hits": single, "cum_hits": cum,
            "buyers": sorted(buyers), "n_window_events": len(win)}

def main():
    jobs = [
        ("KIMCHI", "7thgKgtK1rp1UtLpWUZEuu9vakedTMoZzuyps6RGpump", 1792540200),
        ("KWH", "5hyyjwZqJgnPZorMdb7X4r2J1dT3VujKVkLD8Ptbpump", 1792664400),
    ]
    results = {}
    for name, mint, grad_ts in jobs:
        w1 = grad_ts + 2700
        print(f"\n===== {name} asc walk mint -> {w1}", flush=True)
        sigs = asc_walk(mint, w1)
        ok = [s for s in sigs if not s.get("err")]
        print(f"== {name}: {len(sigs)} sigs ({len(ok)} ok), first_ts "
              f"{sigs[0].get('blockTime') if sigs else None}, last_ts "
              f"{sigs[-1].get('blockTime') if sigs else None}", flush=True)
        json.dump(ok, open(f"{OUT}/{name}_sigs_v2.json", "w"))
        # first tx detail (creation check)
        if ok:
            tx0 = fetch_tx(ok[0]["signature"])
            if tx0:
                desc = json.dumps(tx0)[:600]
                print(f"== {name} first tx: {desc}", flush=True)
        print(f"== {name}: parsing {len(ok)} txs...", flush=True)
        t = time.time()
        events = parse_events(mint, ok)
        print(f"== {name}: {len(events)} owner-events in {time.time()-t:.0f}s", flush=True)
        json.dump(events, open(f"{OUT}/{name}_events_v2.json", "w"))
        results[name] = analyze(name, mint, grad_ts, events)
    k_buyers = set(results["KIMCHI"]["buyers"])
    w_buyers = set(results["KWH"]["buyers"])
    inter = sorted(k_buyers & w_buyers)
    print(f"\n===== INTERSECTION: {len(inter)} wallets bought both in windows", flush=True)
    cand = []
    for src in (results["KIMCHI"]["single_hits"],):
        for e in src:
            cand.append({"wallet": e["owner"], "kind": "single-tx", "kimchi_delta": e["token_delta"],
                         "sol_delta": e["sol_delta"], "ts": e["ts"], "sig": e["sig"],
                         "also_bought_kwh": e["owner"] in w_buyers})
    for c in results["KIMCHI"]["cum_hits"]:
        cand.append({"wallet": c["owner"], "kind": "cumulative-net", "kimchi_net": c["net"],
                     "kimchi_gross_buy": c["gross_buy"], "sol_spent": c["sol_spent"],
                     "also_bought_kwh": c["owner"] in w_buyers})
    for c in cand:
        print(json.dumps(c), flush=True)
    json.dump({"candidates": cand, "intersection": inter,
               "kimchi": {k: v for k, v in results["KIMCHI"].items() if k != "buyers"},
               "kwh": {k: v for k, v in results["KWH"].items() if k != "buyers"},
               "kwh_buyers_count": len(w_buyers), "kimchi_buyers_count": len(k_buyers)},
              open(f"{OUT}/candidates_v2.json", "w"), indent=1)
    print("DONE", flush=True)

if __name__ == "__main__":
    main()
