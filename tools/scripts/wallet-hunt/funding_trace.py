#!/usr/bin/env python3
"""Funding-source tracer for wallet-hunt candidates.
For each candidate wallet: fetch its FIRST page of transactions
(getTransactionsForAddress, sortOrder=asc) and scan the earliest txs for
the first SOL inflow -> funder address, amount, timestamp, walk depth.
Paced via parse_gentle.PACER; resumable via funding_trace.json."""
import json, os, sys, time, urllib.request, urllib.error
sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
from concurrent.futures import ThreadPoolExecutor, as_completed
import parse_gentle

OUT = os.path.expanduser("~/workspace/wallet-hunt")
RPC = parse_gentle.RPC
SYSTEM = "11111111111111111111111111111111"
MAX_FETCH = 15  # full txs scanned per wallet while hunting the first inflow

def paced_rpc(method, params, tries=5):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    for i in range(tries):
        parse_gentle.PACER.wait()
        try:
            req = urllib.request.Request(RPC, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as r:
                d = json.loads(r.read())
            if "error" in d:
                raise RuntimeError(str(d["error"])[:160])
            parse_gentle.PACER.on_ok()
            return d.get("result")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                parse_gentle.PACER.on_429()
            if i == tries - 1:
                return None
            time.sleep(min(15, 1.5 * (i + 1)))
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(1.2 * (i + 1))
    return None

def fetch_tx_v1(sig):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                       "params": [sig, {"encoding": "jsonParsed",
                                        "maxSupportedTransactionVersion": 1}]}).encode()
    for i in range(5):
        parse_gentle.PACER.wait()
        try:
            req = urllib.request.Request(RPC, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
            res = d.get("result")
            if res is not None:
                parse_gentle.PACER.on_ok()
                return res
            time.sleep(0.8 * (i + 1))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                parse_gentle.PACER.on_429()
            time.sleep(min(12, 1.5 * (i + 1)))
        except Exception:
            time.sleep(1.2 * (i + 1))
    return None

def find_inflow(wallet, tx):
    """Return (funder, amount_sol, how) if `wallet` receives SOL in tx."""
    if not tx:
        return None
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return None
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [a.get("pubkey") if isinstance(a, dict) else a
            for a in (msg.get("accountKeys") or [])]
    if wallet not in keys:
        return None
    idx = keys.index(wallet)
    pre_b, post_b = meta.get("preBalances") or [], meta.get("postBalances") or []
    if idx >= len(pre_b) or idx >= len(post_b):
        return None
    delta = (post_b[idx] - pre_b[idx]) / 1e9
    if delta <= 0:
        return None
    # Exact routes: parsed system/token instructions moving SOL into the
    # wallet (top-level + inner instructions).
    instrs = list(msg.get("instructions") or [])
    for inner in meta.get("innerInstructions") or []:
        instrs.extend(inner.get("instructions") or [])
    fee_payer = keys[0] if keys else None
    created_here = set()
    for ins in instrs:
        parsed = ins.get("parsed") or {}
        if not isinstance(parsed, dict):
            continue
        info = parsed.get("info") or {}
        if parsed.get("type") in ("createAccount", "createAccountWithSeed"):
            if info.get("newAccount"):
                created_here.add(info["newAccount"])
    for ins in instrs:
        parsed = ins.get("parsed") or {}
        if not isinstance(parsed, dict):
            continue
        info = parsed.get("info") or {}
        typ = parsed.get("type")
        prog = ins.get("programId") or ins.get("program")
        if typ == "transfer" and info.get("destination") == wallet \
                and prog in (SYSTEM, "system"):
            return info.get("source"), (info.get("lamports") or 0) / 1e9, "system_transfer"
        if typ == "createAccount" and info.get("newAccount") == wallet:
            return info.get("source"), (info.get("lamports") or 0) / 1e9, "create_account"
        if typ == "createAccountWithSeed" and info.get("newAccount") == wallet:
            return info.get("source") or fee_payer, \
                (info.get("lamports") or 0) / 1e9, "create_account_with_seed"
        if typ == "transferWithSeed" and info.get("destination") == wallet:
            return info.get("source"), (info.get("lamports") or 0) / 1e9, "transfer_with_seed"
        if typ == "closeAccount" and info.get("destination") == wallet:
            # wSOL unwrap sweep. If the closed account was created in this
            # same tx, the fee payer economically supplied the SOL; else the
            # closed account's owner controlled the wrapped funds.
            if info.get("account") in created_here:
                return fee_payer, delta, "wsol_close_same_tx"
            return info.get("owner"), delta, "wsol_close_owner"
    # Fallback: largest balance decrease among other accounts.
    best, best_drop = None, 0.0
    for j, k in enumerate(keys):
        if j == idx or j >= len(pre_b) or j >= len(post_b):
            continue
        drop = (pre_b[j] - post_b[j]) / 1e9
        if drop > best_drop:
            best, best_drop = k, drop
    if best:
        return best, delta, "balance_delta_inferred"
    return keys[0] if keys else None, delta, "fee_payer_fallback"

def trace_wallet(wallet):
    res = {"wallet": wallet, "funder": None, "fund_amount_sol": None,
           "funding_ts": None, "funding_sig": None, "fund_how": None,
           "txs_walked": None, "wallet_first_tx_ts": None,
           "page1_entries": None, "has_more_history": None,
           "lifetime_txs_exact": None, "error": None}
    r = paced_rpc("getTransactionsForAddress",
                  [wallet, {"sortOrder": "asc", "limit": 100}])
    if r is None:
        res["error"] = "asc list fetch failed"
        return res
    data = r.get("data") or []
    res["page1_entries"] = len(data)
    res["has_more_history"] = bool(r.get("paginationToken"))
    if not data:
        res["error"] = "no transactions found"
        return res
    if not r.get("paginationToken") and len(data) < 100:
        res["lifetime_txs_exact"] = len(data)
    res["wallet_first_tx_ts"] = data[0].get("blockTime")
    walked = 0
    for e in data[:MAX_FETCH]:
        walked += 1
        if e.get("err"):
            continue
        tx = fetch_tx_v1(e["signature"])
        hit = find_inflow(wallet, tx)
        if hit:
            funder, amt, how = hit
            res.update({"funder": funder, "fund_amount_sol": amt,
                        "funding_ts": e.get("blockTime"),
                        "funding_sig": e["signature"], "fund_how": how,
                        "txs_walked": walked})
            return res
    res["txs_walked"] = walked
    res["error"] = f"no SOL inflow in first {walked} txs"
    return res

def main():
    cands = json.load(open(f"{OUT}/funding_candidates.json"))["candidates"]
    size_band = {"AcfFHST9VCz8Dif6gNDWfNciGehRm5SHusyJBLp2Mzf5",
                 "EedNgv1wGdCMayQ6znioCgVHjaqo7YBgdwq8GFUoMuRE",
                 "mATtYiy3Cn1Bihg3aMaWDzWsWN33txxtnhDyJcFFiWZ",
                 "F9MxbtHQ8998RpMxUw7K7ED8qynTHQxcWuvvtEC7qqp1",
                 "49KoNxaFNfVCw977s1gKchEWrcHnsnBSkLZySqDKDD5a",
                 "8cxwmRSYdaU6VwJbt3ePPZRt7fhrCh6CWsgtw9CNTXRD",
                 "vZ5q6i1VuP6FDyCUcph14vGL9A5rkN9f5J1BZ1Fhe94",
                 "J9SRCXBQvH3S1YxyBKtuV4gvN2oaVhqWh13n1HjeQJiM"}
    def tier(c):
        coins = c["coins"]
        if len(coins) >= 2 or c["wallet"] in size_band:
            return 0
        if any(v["net"] >= 1_000_000 for v in coins.values()):
            return 0
        return 1
    ordered = sorted(cands, key=lambda c: (tier(c),
                     -max(v["gross_buy"] for v in c["coins"].values())))
    done = {}
    tp = f"{OUT}/funding_trace.json"
    if os.path.exists(tp):
        done = {d["wallet"]: d for d in json.load(open(tp))}
        print(f"resume: {len(done)} already traced", flush=True)
    todo = [c["wallet"] for c in ordered if c["wallet"] not in done]
    print(f"tracing {len(todo)} wallets (total candidates {len(ordered)})", flush=True)
    n = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(trace_wallet, w): w for w in todo}
        for fut in as_completed(futs):
            w = futs[fut]
            try:
                done[w] = fut.result()
            except Exception as ex_:
                done[w] = {"wallet": w, "error": f"exception {ex_}"}
            n += 1
            if n % 20 == 0:
                json.dump(list(done.values()), open(tp, "w"), indent=1)
                print(f"  progress {n}/{len(todo)} (saved)", flush=True)
    json.dump(list(done.values()), open(tp, "w"), indent=1)
    ok = sum(1 for d in done.values() if d.get("funder"))
    print(f"TRACE DONE: {len(done)} wallets, {ok} with funder identified", flush=True)

if __name__ == "__main__":
    main()
