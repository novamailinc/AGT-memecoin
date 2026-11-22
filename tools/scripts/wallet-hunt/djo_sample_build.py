#!/usr/bin/env python3
"""Build the CORRECTED djo_sample.json by merging three saved sources:
1. Wallet-trace events (djo_*_events.json): sells, funding SOL events, dust.
2. Pool-side 3SVJ buys (3SVJ_events.json): the buy txs are too large for
   the truncated public-RPC responses, so they are missing from the wallet
   trace; the pool parse already captured them exactly.
3. Deduped failed-sig lists (djo_trace_progress.json) — NOTE: the repair
   script's failed-list bookkeeping duplicated entries; dedup by sig here.
No RPC calls. Writes djo_sample.json and prints a digest."""
import json, os, glob
from collections import defaultdict
from datetime import datetime, timezone

OUT = os.path.expanduser("~/workspace/wallet-hunt")
MINT = "3SVjZ7BfT9FxiR49nZc7UdBmLgzRLJY7XBZDKhEFpump"
MIGRATION_TS = 1792871066  # 2026-10-24 19:44:26 UTC (3SVJ_report.md)
WHALES = ["BbQ23QTpbrvVJCYh14CcBnht2KrCqiCvGP54wxibyBLV",
          "6nU9ttFgM37QzbGD8qR1mZpCFwZLPnxT31dBTScmiThN",
          "4TbwARHGhNuUw3e8e694szG58of8rpeeW14QmbAsmXFf",
          "EwPdGA5qVWPmqRk9SQ36kySyADb9LSYckh6KTznUQHac",
          "EbF3tccfZg8okEbiwDziDtJ59JVPKJtDKsJxikRm4Hv5"]
LOOSE = ["9H27QM8fo4UJa3J4aCgtEdRN424eB6KvGNNYsuUsbn14",
         "By26Nt715oZUL7pzi1Q2UECPhYPMURtGaTBhvfT2wadd",
         "94sHTFK5NkqwohmzoRDVPv5eKfxoV1iwfjPK2YfCJsMM",
         "GygoVKEzwJWw2xDmrPEuEwUCFZkmcWu9Q81NUan3TPUP",
         "H4emhJAeNjFB6mYN9K4R37mT5tZDrEq4omwkdtdpaUrY"]

def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None

def med(vals):
    v = sorted(vals)
    return v[len(v) // 2] if v else None

# ---- pool-side buys (exact, from 3SVJ window parse) ----
pool = json.load(open(f"{OUT}/3SVJ_events.json"))
pool_buys = {}
for e in pool:
    if e.get("owner") in WHALES and e.get("token_delta", 0) > 0:
        pool_buys.setdefault(e["owner"], []).append(e)

# ---- hour-file loose buys (partial parse — flagged) ----
hour = json.load(open(f"{OUT}/3SVJ_events_hour.json"))
loose_hour = {}
for w in LOOSE:
    ws = [e for e in hour if e.get("owner") == w]
    loose_hour[w] = {
        "buys": [{"ts": e.get("ts"), "iso": iso(e.get("ts")),
                  "tokens": e.get("token_delta"), "sol_spent": -e["sol_delta"]
                  if e.get("sol_delta") else None} for e in ws
                 if e.get("token_delta", 0) > 0],
        "sells_observed": sum(1 for e in ws if e.get("token_delta", 0) < 0),
    }

prog = json.load(open(f"{OUT}/djo_trace_progress.json"))
wallets_out = {}
for w in WHALES:
    tag = w[:6]
    evs = json.load(open(f"{OUT}/djo_{tag}_events.json"))
    evs.sort(key=lambda e: (e.get("ts") or 0))
    sigs = json.load(open(f"{OUT}/djo_{tag}_sigs.json"))
    bt = {s["signature"]: s.get("blockTime") for s in sigs}
    # dedup failed (repair-script bug duplicated entries)
    failed = {}
    for f in prog.get(w, {}).get("failed", []):
        failed[f["sig"]] = {"sig": f["sig"], "blockTime": bt.get(f["sig"])}
    sells = [(e["ts"], -e["tokens"][MINT], e["sol_delta"], e["sig"])
             for e in evs if MINT in (e.get("tokens") or {})
             and e["tokens"][MINT] < 0]
    sells.sort()
    buys_pool = pool_buys.get(w, [])
    buy_sol = sum(-e["sol_delta"] for e in buys_pool if e.get("sol_delta"))
    buy_tokens = sum(e["token_delta"] for e in buys_pool)
    sell_sol = sum(s[2] for s in sells if s[2])
    sell_tokens = sum(s[1] for s in sells)
    intervals = [sells[i][0] - sells[i - 1][0] for i in range(1, len(sells))]
    slices = [s[2] for s in sells if s[2]]
    early = [s[2] for s in sells[:8] if s[2]]
    late = [s[2] for s in sells[8:] if s[2]]
    # funding: large SOL inflow with no tokens near migration (probable top-up)
    funding = [{"ts": e["ts"], "iso": iso(e["ts"]), "sol_delta": e["sol_delta"],
                "slot": e.get("slot")}
               for e in evs if e.get("sol_delta") and e["sol_delta"] > 20
               and not e.get("tokens")]
    dust = [t for e in evs for t in (e.get("transfers") or [])
            if t["destination"] == w]
    wallets_out[w] = {
        "tag": tag, "walked_sigs": len(sigs), "events": len(evs),
        "failed_unique": list(failed.values()),
        "mints_touched_in_trace": sorted({m for e in evs for m in (e.get("tokens") or {})}),
        "coin_3SVJ": {
            "buy": {"sig": [e["sig"] for e in buys_pool],
                    "ts": buys_pool[0]["ts"] if buys_pool else None,
                    "iso": iso(buys_pool[0]["ts"]) if buys_pool else None,
                    "tokens": buy_tokens, "sol_spent": round(buy_sol, 4),
                    "entry_price_sol_per_token": (buy_sol / buy_tokens) if buy_tokens else None,
                    "source": "pool-side 3SVJ_events.json (buy txs unrecovered in wallet trace)"},
            "sells": {"n": len(sells),
                      "tokens_sold": round(sell_tokens, 2),
                      "sol_received": round(sell_sol, 4),
                      "first_sell_lag_s": (sells[0][0] - MIGRATION_TS) if sells else None,
                      "last_sell_lag_s": (sells[-1][0] - MIGRATION_TS) if sells else None,
                      "median_interval_s": med(intervals),
                      "slice_sol_median": round(med(slices), 4) if slices else None,
                      "slice_sol_early_median": round(med(early), 4) if early else None,
                      "slice_sol_late_median": round(med(late), 4) if late else None,
                      "slice_sol_min": round(min(slices), 4) if slices else None,
                      "slice_sol_max": round(max(slices), 4) if slices else None},
            "remaining_tokens": round(buy_tokens - sell_tokens, 2),
            "fully_exited_in_trace": abs(buy_tokens - sell_tokens) <= 0.005 * buy_tokens,
            "realized_mult_on_sold": round((sell_sol / sell_tokens) / (buy_sol / buy_tokens), 4)
                                      if buy_tokens and sell_tokens and buy_sol else None,
            "proceeds_over_capital": round(sell_sol / buy_sol, 4) if buy_sol else None,
        },
        "funding_events": funding,
        "dust_transfers": dust,
    }

tot_buy = sum(v["coin_3SVJ"]["buy"]["sol_spent"] for v in wallets_out.values())
tot_sell = sum(v["coin_3SVJ"]["sells"]["sol_received"] for v in wallets_out.values())
out = {
    "method": "Wallet trace (public RPC, curl transport, 2s pace) merged with pool-side "
              "3SVJ buys (3SVJ_events.json). Buy txs are large multi-account txs that "
              "the egress proxy truncates, so they are missing from the wallet trace "
              "and sourced from the pool parse instead. SOL in/out = wallet SOL balance "
              "delta on the tx (includes small tx fees).",
    "coverage": {
        "whale_wallets_traced": 5, "whale_sigs_walked": sum(v["walked_sigs"] for v in wallets_out.values()),
        "whale_events": sum(v["events"] for v in wallets_out.values()),
        "whale_failed_unique": sum(len(v["failed_unique"]) for v in wallets_out.values()),
        "loose_wallets_traced": 0,
        "loose_reason": "public RPC returned HTTP 429 during the repair pass; per rate discipline, "
                        "all public-RPC work stopped immediately. Loose set NOT attempted.",
        "rate_limit_event": "HTTP 429 from api.mainnet-beta.solana.com during djo_repair.py (wallet BbQ23Q, "
                           "after 11 of 17 failures recovered). Helius discovery key also 429 on probe. "
                           "No further RPC calls made after the 429.",
        "mints_per_whale_wallet": {v["tag"]: v["mints_touched_in_trace"] for v in wallets_out.values()},
    },
    "wallets": wallets_out,
    "coin_3SVJ_totals": {
        "buy_sol": round(tot_buy, 4), "sell_sol_captured": round(tot_sell, 4),
        "blended_proceeds_over_capital": round(tot_sell / tot_buy, 4) if tot_buy else None,
        "note": "BbQ23Q tail (7,574,013 tokens, 14.7%) unsold in trace; its final 2 txs "
                "are unrecovered, so the blended figure excludes that tail.",
    },
    "loose_3SVJ_partial_hour": loose_hour,
    "loose_note": "Loose-set figures come ONLY from saved 3SVJ window/hour files. The hour file "
                  "is a PARTIAL parse (stopped at ~17% in prior work), so absence of loose "
                  "sells there is NOT evidence of holding.",
}
json.dump(out, open(f"{OUT}/djo_sample.json", "w"), indent=1)
print(f"wrote {OUT}/djo_sample.json")
for w, v in wallets_out.items():
    c = v["coin_3SVJ"]
    print(f"[{v['tag']}] buy {c['buy']['sol_spent']} SOL -> sold {c['sells']['sol_received']} SOL "
          f"({c['sells']['n']} sells, mult_on_sold {c['realized_mult_on_sold']}, "
          f"proceeds/capital {c['proceeds_over_capital']}, "
          f"first_sell +{c['sells']['first_sell_lag_s']}s, last +{c['sells']['last_sell_lag_s']}s, "
          f"remaining {c['remaining_tokens']:,.0f}, failed {len(v['failed_unique'])})")
print(f"TOTALS buy {tot_buy:.4f} sell {tot_sell:.4f} blended {tot_sell/tot_buy:.4f}x")
