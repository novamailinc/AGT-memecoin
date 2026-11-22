#!/usr/bin/env python3
"""Deep dive on whale histories: for every traced-mint trade tx,
print the FULL delta map (all mints + SOL) and every post token
balance owner (reveals transfer destinations without extra RPC).
Also prints every large SOL inflow (top-ups) with fee payer.
Reads the same whale_*_txs.json files. Never raises per tx."""
import datetime
import json
import os

W = "/home/hatch/workspace/wallet-hunt"
WHALES = {
    "AYz4wvVy": ("whale_AYz4wvVy_txs.json",
                 "7hfxEzFMAhdZ747772R7bpc7yhhfNYegmvSkHHo8pump",
                 "AYz4wvVy1WDXhdGW5NGWBs2SayW76FiLpnJnMYeBStkN"),
    "9Vs2EsLS": ("whale_9Vs2EsLS_txs.json",
                 "7hfxEzFMAhdZ747772R7bpc7yhhfNYegmvSkHHo8pump",
                 "9Vs2EsLSfCqiEKV3U2Uumac2SNSAL326JuWsT7pF75jJ"),
    "7m4s1x72": ("whale_7m4s1x72_txs.json",
                 "7hfxEzFMAhdZ747772R7bpc7yhhfNYegmvSkHHo8pump",
                 "7m4s1x72wBK4JX5cv4VbwKdVBLXWjGTCsX73MD2CL4uF"),
    "35kggAbH": ("whale_35kggAbH_txs.json",
                 "7hfxEzFMAhdZ747772R7bpc7yhhfNYegmvSkHHo8pump",
                 "35kggAbH15DUcAQYYZxFKEAMY4jaRQ8F7LugDhT4y91H"),
    "4duCoSRh": ("whale_4duCoSRh_txs.json",
                 "9Ro2AeikFisoMqyE2HjcPhgUnXvZqRwZgRxtsgsLpump",
                 "4duCoSRhgucb5ejgWev1CQXtUEh5ooeHSoJfXCSPxbk1"),
    "FQbi7eht": ("whale_FQbi7eht_txs.json",
                 "9Ro2AeikFisoMqyE2HjcPhgUnXvZqRwZgRxtsgsLpump",
                 "FQbi7ehtiTSRyTNy8CZseoQFu8m1uQkLBm92uFnPzVcu"),
    "3KnJ5Xay": ("whale_3KnJ5Xay_txs.json",
                 "9Ro2AeikFisoMqyE2HjcPhgUnXvZqRwZgRxtsgsLpump",
                 "3KnJ5XayE8RXfCTo2KsXJQfA7kVtmtEJeT4kQ4YvcNhj"),
    "6cj5CfBZ": ("whale_6cj5CfBZ_txs.json",
                 "9Ro2AeikFisoMqyE2HjcPhgUnXvZqRwZgRxtsgsLpump",
                 "6cj5CfBZgK5XgHZ4n8ELeqBU6hoNUncavCuxL9mXrQyX"),
}


def wat(bt):
    try:
        return datetime.datetime.fromtimestamp(
            bt, datetime.timezone(datetime.timedelta(hours=1))
        ).strftime("%m-%d %H:%M:%S")
    except Exception:
        return "?"


def main():
    for short, (fn, mint, wallet) in WHALES.items():
        data = json.load(open(os.path.join(W, fn)))
        txs = []
        for sig, tx in (data.items() if isinstance(data, dict) else []):
            try:
                txs.append((tx.get("blockTime") or 0, sig, tx))
            except Exception:
                continue
        txs.sort(key=lambda x: x[0])
        print("=" * 70)
        print(short, "txs:", len(txs))
        for bt, sig, tx in txs:
            try:
                meta = tx.get("meta") or {}
                if meta.get("err") is not None:
                    continue
                msg = (tx.get("transaction") or {}).get("message") or {}
                keys = msg.get("accountKeys") or []
                key_strs = [k.get("pubkey") if isinstance(k, dict)
                            else k for k in keys]
                try:
                    wi = key_strs.index(wallet)
                except ValueError:
                    wi = -1
                pre = meta.get("preBalances") or []
                post = meta.get("postBalances") or []
                sol = None
                if 0 <= wi < len(pre) and wi < len(post):
                    sol = (post[wi] - pre[wi]) / 1e9
                # full delta map, all owners grouped by mint
                deltas = {}
                pre_by_acct = {}
                for b in meta.get("preTokenBalances") or []:
                    pre_by_acct[b.get("accountIndex")] = b
                post_by_acct = {}
                for b in meta.get("postTokenBalances") or []:
                    post_by_acct[b.get("accountIndex")] = b
                for idx in set(list(pre_by_acct) + list(post_by_acct)):
                    pb = pre_by_acct.get(idx) or {}
                    qb = post_by_acct.get(idx) or {}
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
                        owner = (qb or pb).get("owner")
                        deltas.setdefault(m, []).append((owner, d))
                whale_delta = 0.0
                for owner, d in deltas.get(mint, []):
                    if owner == wallet:
                        whale_delta += d
                is_trade = whale_delta != 0
                is_topup = (sol is not None and sol > 1.0
                            and not is_trade)
                if not (is_trade or is_topup):
                    continue
                tag = "TRADE" if is_trade else "TOPUP"
                print(f"  {tag} {wat(bt)} sig={sig[:12]} sol={sol}")
                for m, lst in deltas.items():
                    for owner, d in lst:
                        mark = " <- whale" if owner == wallet else ""
                        print(f"    {m[:12]} {d:,.0f} owner="
                              f"{str(owner)[:12]}{mark}")
                # parsed token-transfer destinations
                for ins in msg.get("instructions") or []:
                    p = ins.get("parsed") or {}
                    if isinstance(p, dict) and \
                            p.get("type") in ("transfer", "transferChecked"):
                        info = p.get("info") or {}
                        print(f"    token-xfer dest="
                              f"{str(info.get('destination'))[:16]} "
                              f"amt={info.get('amount') or info.get('tokenAmount', {}).get('uiAmount')} "
                              f"auth={str(info.get('authority'))[:12]}")
            except Exception:
                continue


if __name__ == "__main__":
    main()
