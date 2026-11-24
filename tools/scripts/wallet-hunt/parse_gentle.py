#!/usr/bin/env python3
"""Rate-aware incremental parser over saved signature lists.
Usage: parse_gentle.py <COIN> <sigslist.json> <mint> [max_txs]
- Global pacing gate, adaptive: interval grows on HTTP 429, decays on success.
- Saves events + progress every 250 completions; resumable.
- Prints single-tx fingerprint hits (4.2M..5.8M) immediately."""
import json, os, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
import threading

HOME = os.path.expanduser("~")
OUT = os.path.expanduser("~/workspace/wallet-hunt")

def load_key():
    for name in ("api_key_discovery", "api_key"):
        p = os.path.join(HOME, ".forex-agent/helius", name)
        if os.path.exists(p):
            return open(p).read().strip()
    raise SystemExit("no key")
KEY = load_key()
RPC = f"https://mainnet.helius-rpc.com/?api-key={KEY}"

class Pacer:
    def __init__(self, interval=0.45):
        self.interval = interval
        self.lock = threading.Lock()
        self.next_ok = 0.0
        self.ok_streak = 0
    def wait(self):
        with self.lock:
            now = time.time()
            t = max(now, self.next_ok)
            self.next_ok = t + self.interval
        if t > now:
            time.sleep(t - now)
    def on_429(self):
        with self.lock:
            self.interval = min(4.0, self.interval * 1.6 + 0.1)
            self.ok_streak = 0
        print(f"  [429] interval -> {self.interval:.2f}s", flush=True)
    def on_ok(self):
        with self.lock:
            self.ok_streak += 1
            if self.ok_streak >= 60 and self.interval > 0.3:
                self.interval = max(0.3, self.interval * 0.85)
                self.ok_streak = 0

PACER = Pacer()

def fetch_tx(sig):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                       "params": [sig, {"encoding": "jsonParsed",
                                        "maxSupportedTransactionVersion": 0}]}).encode()
    for i in range(6):
        PACER.wait()
        try:
            req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as r:
                d = json.loads(r.read())
            PACER.on_ok()
            return d.get("result")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                PACER.on_429()
                time.sleep(min(20, 2 ** i))
                continue
            if i == 5:
                return None
            time.sleep(1.0 * (i + 1))
        except Exception:
            if i == 5:
                return None
            time.sleep(1.0 * (i + 1))
    return None

def events_of(mint, sig, ts, tx):
    if not tx:
        return []
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []
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
    keys = (tx.get("transaction", {}).get("message", {}) or {}).get("accountKeys") or []
    acct = [a.get("pubkey") if isinstance(a, dict) else a for a in keys]
    pre_b, post_b = meta.get("preBalances") or [], meta.get("postBalances") or []
    evs = []
    for owner in set(pre) | set(post):
        d = post.get(owner, 0.0) - pre.get(owner, 0.0)
        if d == 0:
            continue
        sol = None
        if owner in acct:
            idx = acct.index(owner)
            if idx < len(pre_b) and idx < len(post_b):
                sol = (post_b[idx] - pre_b[idx]) / 1e9
        evs.append({"sig": sig, "ts": ts, "owner": owner, "token_delta": d, "sol_delta": sol})
    return evs

def main():
    coin, sigfile, mint = sys.argv[1], sys.argv[2], sys.argv[3]
    max_txs = int(sys.argv[4]) if len(sys.argv) > 4 else 10**9
    sigs = [s for s in json.load(open(sigfile)) if not s.get("err")][:max_txs]
    ev_path = f"{OUT}/{coin}_events_gentle.json"
    prog_path = f"{OUT}/{coin}_gentle_progress.json"
    events, done = [], 0
    if os.path.exists(prog_path):
        prog = json.load(open(prog_path))
        done = prog["done"]
        events = json.load(open(ev_path)) if os.path.exists(ev_path) else []
        print(f"resume {coin} at {done}/{len(sigs)}", flush=True)
    todo = sigs[done:]
    print(f"{coin}: parsing {len(todo)} txs (total {len(sigs)})", flush=True)
    t0 = time.time()
    completed = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(fetch_tx, s["signature"]): s for s in todo}
        for fut, s in list(futs.items()):
            pass  # futures dict preserves submission order; iterate as_completed below instead
        from concurrent.futures import as_completed
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                tx = fut.result()
            except Exception:
                tx = None
            evs = events_of(mint, s["signature"], s.get("blockTime"), tx)
            events.extend(evs)
            for e in evs:
                if 4_200_000 <= e["token_delta"] <= 5_800_000:
                    print(f"  *** SINGLE HIT: {json.dumps(e)}", flush=True)
            completed += 1
            if completed % 250 == 0:
                json.dump(events, open(ev_path, "w"))
                json.dump({"done": done + completed}, open(prog_path, "w"))
                el = time.time() - t0
                print(f"  progress {done+completed}/{len(sigs)} "
                      f"({completed/el:.2f}/s this run, interval {PACER.interval:.2f}s)", flush=True)
    json.dump(events, open(ev_path, "w"))
    json.dump({"done": len(sigs)}, open(prog_path, "w"))
    # final cumulative analysis
    net, gross, solsp = defaultdict(float), defaultdict(float), defaultdict(float)
    for e in events:
        net[e["owner"]] += e["token_delta"]
        if e["token_delta"] > 0:
            gross[e["owner"]] += e["token_delta"]
            if e["sol_delta"] is not None and e["sol_delta"] < 0:
                solsp[e["owner"]] += -e["sol_delta"]
    print(f"\n== {coin} cumulative-net in fingerprint range:", flush=True)
    for o, v in sorted(net.items(), key=lambda x: -x[1]):
        if 4_200_000 <= v <= 5_800_000:
            print(f"  CUM {o} net={v:,.0f} gross={gross[o]:,.0f} sol_spent={solsp[o]:.4f}", flush=True)
    print(f"{coin} GENTLE DONE: {len(events)} events", flush=True)

if __name__ == "__main__":
    main()
