#!/usr/bin/env python3
"""Djo operator wallet tracer — public Solana RPC only, strict pacing.
Usage: djo_trace.py <wallet> [wallet ...]
- getSignaturesForAddress (limit 1000, paginated) -> djo_<short>_sigs.json
- getTransaction jsonParsed per sig -> djo_<short>_events.json (saved after every tx)
- PACE: 5.0s between calls. HARD STOP on first 429: save progress, exit 3.
  No retries on 429, no endpoint rotation. Non-429 failures: tx skipped, recorded.
- Events: per tx, wallet SOL delta (via full account list incl. loaded
  addresses) + per-mint token deltas + parsed system transfers to/from wallet.
"""
import json, os, sys, time, subprocess

OUT = os.path.expanduser("~/workspace/wallet-hunt")
RPC = "https://api.mainnet-beta.solana.com"
PACE = 5.0
DJOF = "DjoDYTgjHnZb8eFu16MqXdAQbk1xvs4BhRXigr5ieYnB"

def rpc(method, params):
    """Single-attempt RPC via curl. urllib drops connections through this
    egress proxy; curl does not (verified on the same 6522-byte response
    urllib truncated). Raises RateLimited on HTTP 429, FetchError otherwise."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params})
    try:
        r = subprocess.run(["curl", "-sS", "-m", "90", "-w", "\n%{http_code}",
                            RPC, "-H", "Content-Type: application/json",
                            "-d", body], capture_output=True, timeout=120)
    except Exception as e:
        raise FetchError(f"{method}: {e}")
    out = r.stdout.decode()
    if "\n" not in out:
        raise FetchError(f"{method}: empty response (rc={r.returncode})")
    payload, code = out.rsplit("\n", 1)
    code = code.strip()
    if code == "429":
        raise RateLimited(f"HTTP 429 on {method}")
    if code != "200":
        raise FetchError(f"{method}: HTTP {code}")
    try:
        return json.loads(payload)
    except Exception as e:
        raise FetchError(f"{method}: bad JSON ({e})")

class RateLimited(Exception):
    pass
class FetchError(Exception):
    pass

def short(w):
    return w[:6]

def fetch_sigs(wallet):
    sigs, before = [], None
    while True:
        params = [{"limit": 1000}]
        if before:
            params[0]["before"] = before
        last_err = None
        for _attempt in range(3):
            try:
                d = rpc("getSignaturesForAddress", [wallet] + params)
                break
            except RateLimited:
                raise
            except FetchError as e:
                last_err = e
                time.sleep(PACE)
        else:
            raise FetchError(f"getSignaturesForAddress: {last_err}")
        if d.get("error"):
            raise FetchError(f"getSignaturesForAddress: {d['error']}")
        batch = d["result"] or []
        sigs.extend(batch)
        if len(batch) < 1000:
            break
        before = batch[-1]["signature"]
        time.sleep(PACE)
    return sigs

def extract(wallet, sig, ts, tx):
    """Return event dict or None (no wallet-relevant movement)."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return {"sig": sig, "ts": ts, "err": True}
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = msg.get("accountKeys") or []
    acct = [(k.get("pubkey") if isinstance(k, dict) else k) for k in keys]
    loaded = (meta.get("loadedAddresses") or {})
    acct += loaded.get("writable", []) + loaded.get("readonly", [])
    pre_b, post_b = meta.get("preBalances") or [], meta.get("postBalances") or []
    sol_delta = None
    if wallet in acct and len(pre_b) == len(acct) and len(post_b) == len(acct):
        i = acct.index(wallet)
        sol_delta = (post_b[i] - pre_b[i]) / 1e9
    def amt(b):
        try:
            return int(b["uiTokenAmount"]["amount"]) / (10 ** int(b["uiTokenAmount"]["decimals"]))
        except Exception:
            return 0.0
    pre, post = {}, {}
    for b in meta.get("preTokenBalances") or []:
        if b.get("owner") == wallet:
            pre[b["mint"]] = pre.get(b["mint"], 0.0) + amt(b)
    for b in meta.get("postTokenBalances") or []:
        if b.get("owner") == wallet:
            post[b["mint"]] = post.get(b["mint"], 0.0) + amt(b)
    tokens = {}
    for m in set(pre) | set(post):
        d = post.get(m, 0.0) - pre.get(m, 0.0)
        if d != 0:
            tokens[m] = d
    # parsed system transfers (top-level + inner)
    transfers = []
    def scan(instrs):
        for ins in instrs or []:
            try:
                p = ins.get("parsed") or {}
                if ins.get("program") == "system" and p.get("type") == "transfer":
                    info = p.get("info") or {}
                    if info.get("destination") == wallet or info.get("source") == wallet:
                        transfers.append({"source": info.get("source"),
                                          "destination": info.get("destination"),
                                          "lamports": info.get("lamports")})
            except Exception:
                pass
    scan(msg.get("instructions"))
    for inner in (meta.get("innerInstructions") or []):
        scan(inner.get("instructions"))
    if not tokens and sol_delta is None and not transfers:
        return None
    if not tokens and not transfers and sol_delta is not None and abs(sol_delta) < 0.002:
        return None  # pure fee dust, no movement worth recording
    return {"sig": sig, "ts": ts, "slot": tx.get("slot"),
            "sol_delta": sol_delta, "tokens": tokens, "transfers": transfers}

def main():
    wallets = sys.argv[1:]
    for wallet in wallets:
        tag = short(wallet)
        sig_path = f"{OUT}/djo_{tag}_sigs.json"
        ev_path = f"{OUT}/djo_{tag}_events.json"
        prog_path = f"{OUT}/djo_trace_progress.json"
        prog = json.load(open(prog_path)) if os.path.exists(prog_path) else {}
        state = prog.get(wallet, {"sigs": 0, "txs_done": 0, "failed": []})
        try:
            if not os.path.exists(sig_path):
                print(f"[{tag}] walking signatures...", flush=True)
                sigs = fetch_sigs(wallet)
                json.dump(sigs, open(sig_path, "w"))
                print(f"[{tag}] {len(sigs)} signatures", flush=True)
            else:
                sigs = json.load(open(sig_path))
                print(f"[{tag}] resume: {len(sigs)} signatures on file", flush=True)
            sigs_chron = list(reversed(sigs))
            events = json.load(open(ev_path)) if os.path.exists(ev_path) else []
            done = state.get("txs_done", 0)
            print(f"[{tag}] parsing from tx {done}/{len(sigs_chron)}", flush=True)
            for idx in range(done, len(sigs_chron)):
                s = sigs_chron[idx]
                try:
                    d = rpc("getTransaction", [s["signature"],
                         {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
                    if d.get("error"):
                        state.setdefault("failed", []).append(
                            {"sig": s["signature"], "error": str(d["error"])})
                    else:
                        tx = d.get("result")
                        if tx is None:
                            state.setdefault("failed", []).append(
                                {"sig": s["signature"], "error": "null result"})
                        else:
                            ev = extract(wallet, s["signature"], s.get("blockTime"), tx)
                            if ev:
                                events.append(ev)
                except RateLimited:
                    raise
                except Exception as e:
                    state.setdefault("failed", []).append(
                        {"sig": s["signature"], "error": str(e)[:200]})
                state["txs_done"] = idx + 1
                json.dump(events, open(ev_path, "w"))
                prog[wallet] = state
                json.dump(prog, open(prog_path, "w"))
                time.sleep(PACE)
            state["complete"] = True
            prog[wallet] = state
            json.dump(prog, open(prog_path, "w"))
            print(f"[{tag}] COMPLETE: {len(events)} events, "
                  f"{len(state.get('failed', []))} failed", flush=True)
        except RateLimited as e:
            prog[wallet] = state
            json.dump(prog, open(prog_path, "w"))
            print(f"RATE_LIMITED at wallet {tag} (tx {state.get('txs_done', 0)}): {e}",
                  flush=True)
            sys.exit(3)
    print("ALL WALLETS DONE", flush=True)

if __name__ == "__main__":
    main()
