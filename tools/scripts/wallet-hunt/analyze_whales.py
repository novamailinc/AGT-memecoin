#!/usr/bin/env python3
"""Analyze fetched whale histories: funding source, traced-mint
buys/sells with multiples and timing, other mints traded, fails.
Reads ~/workspace/wallet-hunt/whale_*_txs.json (dict sig -> tx).
Prints a compact per-whale report and writes whale_analysis.json.
Never raises on a single bad tx (skips it, counts it)."""
import datetime
import json
import os
import sys

W = "/home/hatch/workspace/wallet-hunt"
WHALES = {
    # wallet short -> (file, traced mint, launch bt)
    "AYz4wvVy": ("whale_AYz4wvVy_txs.json",
                 "7hfxEzFMAhdZ747772R7bpc7yhhfNYegmvSkHHo8pump",
                 1791391398),
    "9Vs2EsLS": ("whale_9Vs2EsLS_txs.json",
                 "7hfxEzFMAhdZ747772R7bpc7yhhfNYegmvSkHHo8pump",
                 1791391398),
    "7m4s1x72": ("whale_7m4s1x72_txs.json",
                 "7hfxEzFMAhdZ747772R7bpc7yhhfNYegmvSkHHo8pump",
                 1791391398),
    "35kggAbH": ("whale_35kggAbH_txs.json",
                 "7hfxEzFMAhdZ747772R7bpc7yhhfNYegmvSkHHo8pump",
                 1791391398),
    "4duCoSRh": ("whale_4duCoSRh_txs.json",
                 "9Ro2AeikFisoMqyE2HjcPhgUnXvZqRwZgRxtsgsLpump",
                 1791711328),
    "FQbi7eht": ("whale_FQbi7eht_txs.json",
                 "9Ro2AeikFisoMqyE2HjcPhgUnXvZqRwZgRxtsgsLpump",
                 1791711328),
    "3KnJ5Xay": ("whale_3KnJ5Xay_txs.json",
                 "9Ro2AeikFisoMqyE2HjcPhgUnXvZqRwZgRxtsgsLpump",
                 1791711328),
    "6cj5CfBZ": ("whale_6cj5CfBZ_txs.json",
                 "9Ro2AeikFisoMqyE2HjcPhgUnXvZqRwZgRxtsgsLpump",
                 1791711328),
}
WALLETS = {
    "AYz4wvVy": "AYz4wvVy1WDXhdGW5NGWBs2SayW76FiLpnJnMYeBStkN",
    "9Vs2EsLS": "9Vs2EsLSfCqiEKV3U2Uumac2SNSAL326JuWsT7pF75jJ",
    "7m4s1x72": "7m4s1x72wBK4JX5cv4VbwKdVBLXWjGTCsX73MD2CL4uF",
    "35kggAbH": "35kggAbH15DUcAQYYZxFKEAMY4jaRQ8F7LugDhT4y91H",
    "4duCoSRh": "4duCoSRhgucb5ejgWev1CQXtUEh5ooeHSoJfXCSPxbk1",
    "FQbi7eht": "FQbi7ehtiTSRyTNy8CZseoQFu8m1uQkLBm92uFnPzVcu",
    "3KnJ5Xay": "3KnJ5XayE8RXfCTo2KsXJQfA7kVtmtEJeT4kQ4YvcNhj",
    "6cj5CfBZ": "6cj5CfBZgK5XgHZ4n8ELeqBU6hoNUncavCuxL9mXrQyX",
}


def wat(bt):
    try:
        return datetime.datetime.fromtimestamp(
            bt, datetime.timezone(datetime.timedelta(hours=1))
        ).strftime("%m-%d %H:%M:%S")
    except Exception:
        return "?"


def analyze(short):
    fn, mint, launch_bt = WHALES[short]
    wallet = WALLETS[short]
    path = os.path.join(W, fn)
    data = json.load(open(path))
    txs = []
    for sig, tx in (data.items() if isinstance(data, dict) else []):
        try:
            bt = tx.get("blockTime") or 0
            txs.append((bt, sig, tx))
        except Exception:
            continue
    txs.sort(key=lambda x: x[0])
    out = {"wallet": short, "file": fn, "fetched": len(txs),
           "failed": 0, "skipped": 0, "trades": [], "other_mints": {},
           "oldest": None, "buy_sol": 0.0, "buy_tok": 0.0,
           "sell_sol": 0.0, "sell_tok": 0.0}
    for bt, sig, tx in txs:
        try:
            meta = tx.get("meta") or {}
            if meta.get("err") is not None:
                out["failed"] += 1
                continue
            msg = (tx.get("transaction") or {}).get("message") or {}
            keys = msg.get("accountKeys") or []
            # keys may be dicts (parsed) or strings
            key_strs = [k.get("pubkey") if isinstance(k, dict) else k
                        for k in keys]
            try:
                wi = key_strs.index(wallet)
            except ValueError:
                # versioned: whale may sit in loaded addresses
                wi = -1
            sol_delta = None
            pre = meta.get("preBalances") or []
            post = meta.get("postBalances") or []
            if 0 <= wi < len(pre) and wi < len(post):
                sol_delta = (post[wi] - pre[wi]) / 1e9
            # token deltas for whale-owned accounts
            pre_tok = {}
            for b in meta.get("preTokenBalances") or []:
                if b.get("owner") == wallet:
                    pre_tok[b.get("accountIndex")] = b
            post_tok = {}
            for b in meta.get("postTokenBalances") or []:
                if b.get("owner") == wallet:
                    post_tok[b.get("accountIndex")] = b
            deltas = {}
            for idx in set(list(pre_tok) + list(post_tok)):
                pb = pre_tok.get(idx) or {}
                qb = post_tok.get(idx) or {}
                m = (qb or pb).get("mint")
                if not m:
                    continue
                dec = ((qb or pb).get("uiTokenAmount") or {}).get(
                    "decimals", 0)
                pa = float(((pb.get("uiTokenAmount") or {}).get(
                    "amount") or 0))
                qa = float(((qb.get("uiTokenAmount") or {}).get(
                    "amount") or 0))
                d = (qa - pa) / (10 ** dec) if dec is not None else 0.0
                if d:
                    deltas[m] = deltas.get(m, 0.0) + d
            rec = {"bt": bt, "sig": sig, "sol": sol_delta,
                   "deltas": deltas,
                   "fee_payer": key_strs[0] if key_strs else None}
            # parsed transfer source/destination when present
            for ins in msg.get("instructions") or []:
                p = ins.get("parsed") or {}
                if isinstance(p, dict) and p.get("type") == "transfer":
                    info = p.get("info") or {}
                    rec.setdefault("transfers", []).append(
                        (info.get("source"), info.get("destination"),
                         info.get("lamports")))
            if out["oldest"] is None:
                out["oldest"] = rec
            if mint in deltas:
                d = deltas[mint]
                if d > 0:
                    out["buy_tok"] += d
                    if sol_delta is not None and sol_delta < 0:
                        out["buy_sol"] += -sol_delta
                elif d < 0:
                    out["sell_tok"] += -d
                    if sol_delta is not None and sol_delta > 0:
                        out["sell_sol"] += sol_delta
                out["trades"].append(rec)
            for m, d in deltas.items():
                if m != mint:
                    o = out["other_mints"].setdefault(
                        m, {"tok": 0.0, "n": 0})
                    o["tok"] += d
                    o["n"] += 1
        except Exception:
            out["skipped"] += 1
            continue
    return out


def main():
    summary = {}
    for short in WHALES:
        try:
            r = analyze(short)
        except Exception as e:
            print(short, "ANALYZE FAILED:", e)
            continue
        summary[short] = r
        old = r["oldest"] or {}
        print("=" * 70)
        print(f"{short} fetched={r['fetched']} failed={r['failed']} "
              f"skipped={r['skipped']}")
        print(f"  oldest {wat(old.get('bt') or 0)} sol_delta="
              f"{old.get('sol')} fee_payer={old.get('fee_payer')}")
        for t in old.get("transfers") or []:
            print(f"    transfer {t[0]} -> {t[1]} lamports={t[2]}")
        print(f"  traced buys: {r['buy_tok']:,.0f} tok for "
              f"{r['buy_sol']:.2f} SOL; sells: {r['sell_tok']:,.0f} tok "
              f"for {r['sell_sol']:.2f} SOL")
        if r["buy_sol"] > 0 and r["sell_sol"] > 0:
            print(f"  realized multiple (sold SOL / bought SOL): "
                  f"{r['sell_sol'] / r['buy_sol']:.2f}x")
        for tr in r["trades"]:
            d = tr["deltas"].get(WHALES[short][1], 0.0)
            kind = "BUY " if d > 0 else "SELL"
            print(f"    {kind} {wat(tr['bt'])} {abs(d):,.0f} tok "
                  f"sol_delta={tr['sol']}")
        others = r["other_mints"]
        print(f"  other mints traded: {len(others)}")
        for m, o in list(others.items())[:10]:
            print(f"    {m[:12]} net_tok={o['tok']:,.0f} n={o['n']}")
    json.dump(summary, open(os.path.join(W, "whale_analysis.json"),
                            "w"), indent=1, default=str)
    print("wrote whale_analysis.json")


if __name__ == "__main__":
    main()
