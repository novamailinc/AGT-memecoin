#!/usr/bin/env python3
"""Generic recipient analysis: chronological mint trades + SOL flows
for one wallet history file. Usage: analyze_recip.py <txfile>
<wallet> <mint>. Prints trades (mint delta + SOL delta), top-ups,
first tx, and other-mint nets. Never raises per tx."""
import datetime
import json
import os
import sys

W = "/home/hatch/workspace/wallet-hunt"


def wat(bt):
    try:
        return datetime.datetime.fromtimestamp(
            bt, datetime.timezone(datetime.timedelta(hours=1))
        ).strftime("%m-%d %H:%M:%S")
    except Exception:
        return "?"


def main():
    fn, wallet, mint = sys.argv[1], sys.argv[2], sys.argv[3]
    path = fn if os.path.isabs(fn) else os.path.join(W, fn)
    data = json.load(open(path))
    nulls = [s for s, t in data.items() if not isinstance(t, dict)]
    if nulls:
        print(f"  note: {len(nulls)} null tx entries skipped: "
              f"{[s[:12] for s in nulls]}")
    txs = sorted(((t.get("blockTime") or 0, s, t)
                  for s, t in data.items() if isinstance(t, dict)),
                 key=lambda x: x[0])
    print(f"{fn} txs={len(txs)} wallet={wallet[:12]} mint={mint[:12]}")
    recv = sent = sol_in = sol_out = 0.0
    others = {}
    n_failed = 0
    for bt, sig, tx in txs:
        try:
            meta = tx.get("meta") or {}
            if meta.get("err") is not None:
                n_failed += 1
                continue
            msg = (tx.get("transaction") or {}).get("message") or {}
            keys = [k.get("pubkey") if isinstance(k, dict) else k
                    for k in (msg.get("accountKeys") or [])]
            try:
                wi = keys.index(wallet)
            except ValueError:
                wi = -1
            pre, post = meta.get("preBalances") or [], \
                meta.get("postBalances") or []
            sol = None
            if 0 <= wi < len(pre) and wi < len(post):
                sol = (post[wi] - pre[wi]) / 1e9
            pre_tok = {b.get("accountIndex"): b
                       for b in meta.get("preTokenBalances") or []
                       if b.get("owner") == wallet}
            post_tok = {b.get("accountIndex"): b
                        for b in meta.get("postTokenBalances") or []
                        if b.get("owner") == wallet}
            deltas = {}
            for idx in set(list(pre_tok) + list(post_tok)):
                pb, qb = pre_tok.get(idx) or {}, post_tok.get(idx) or {}
                m = (qb or pb).get("mint")
                if not m:
                    continue
                dec = ((qb or pb).get("uiTokenAmount") or {}).get(
                    "decimals", 0)
                pa = float(((pb.get("uiTokenAmount") or {}).get(
                    "amount") or 0))
                qa = float(((qb.get("uiTokenAmount") or {}).get(
                    "amount") or 0))
                d = (qa - pa) / (10 ** dec)
                if d:
                    deltas[m] = deltas.get(m, 0.0) + d
            d = deltas.get(mint, 0.0)
            if d != 0:
                if d > 0:
                    recv += d
                    tag = "RECV"
                else:
                    sent += -d
                    tag = "SEND"
                    if sol is not None and sol > 0:
                        sol_out += sol
                if d > 0 and sol is not None and sol < 0:
                    sol_in += -sol
                print(f"  {tag} {wat(bt)} {abs(d):,.0f} tok sol={sol}")
            elif sol is not None and sol > 1.0:
                print(f"  TOPUP {wat(bt)} +{sol:.2f} SOL "
                      f"fee_payer={keys[0][:12] if keys else '?'}")
            for m, dd in deltas.items():
                if m != mint:
                    o = others.setdefault(m, {"tok": 0.0, "n": 0})
                    o["tok"] += dd
                    o["n"] += 1
        except Exception:
            continue
    print(f"  SUMMARY recv={recv:,.0f} sent={sent:,.0f} "
          f"net={recv - sent:,.0f} sol_spent_on_mint={sol_in:.2f} "
          f"sol_received_on_sends={sol_out:.2f} failed={n_failed}")
    print(f"  other mints: {len(others)}")
    for m, o in sorted(others.items(), key=lambda kv: -abs(kv[1]["tok"]))[:10]:
        print(f"    {m[:12]} net={o['tok']:,.0f} n={o['n']}")


if __name__ == "__main__":
    main()
