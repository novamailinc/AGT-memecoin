#!/usr/bin/env python3
"""Tier 1 driver: runs the four methodology upgrades on real data.

For each symbol:
  1. Walk-forward validation (5 rolling folds) -> qualified windows
  2. Deflated Sharpe Ratio over the 62 train candidates -> DSR verdict
  3. Block-bootstrap null (500 perms, 24h blocks) -> best |t| vs null
  4. Corwin-Schultz per-hour costs + cost-sensitivity re-check

Writes results_tier1_<SYM>.json. Paper only; reads the existing klines DB.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_data
import validator
import walkforward
import deflated_sharpe
import blockbootstrap
import costs

SYMBOLS = ["BTCUSDT", "SOLUSDT", "ETHUSDT"]
N_PERM = 500


def run_symbol(symbol):
    print(f"=== {symbol}: walk-forward ===", flush=True)
    wf = walkforward.walkforward(symbol)

    print(f"=== {symbol}: DSR + block bootstrap + costs ===", flush=True)
    rows = fetch_data.load_series(symbol, "1h")
    candles = validator.hourly_returns(rows)
    if symbol != "BTCUSDT":
        btc_candles = validator.hourly_returns(
            fetch_data.load_series("BTCUSDT", "1h"))
        candles, beta = validator.strip_btc_beta(candles, btc_candles)
    days = validator.aggregate_daily(candles)
    train_c, _ = validator.split_train_test(candles)
    train_d, _ = validator.split_train_test(days)
    candidates = (validator.build_candidates_hourly(train_c)
                  + validator.build_candidates_weekday(train_d))
    cost = validator.ROUND_TRIP_COST

    train_rets = [validator.candidate_returns(c, train_c, train_d, cost)
                  for c in candidates]
    dsr = deflated_sharpe.deflated_sharpe(train_rets)

    null = blockbootstrap.block_null_max_t(train_c, candidates,
                                           n_perm=N_PERM, seed=7)
    obs_t = blockbootstrap.observed_best_t(train_c, candidates)
    p95 = null[int(0.95 * len(null))]
    pct = blockbootstrap.percentile(null, obs_t)

    cs = costs.cost_summary(symbol)
    ch = cs["hourly_cost"]
    # cost sensitivity: hourly candidates re-scored with measured per-hour
    # costs. Only PROFITABLE windows count — the cost drag makes losers look
    # "significant" too, which is not an edge.
    sens = []
    for cand in candidates:
        if cand["kind"] != "hourly":
            continue
        gross = validator.candidate_returns(cand, train_c, train_d, cost=0)
        net = [r - ch[cand["key"]] for r in gross]
        st = validator.candidate_stats(net)
        if st and st["n"] >= validator.MIN_N and st["expectancy"] > 0:
            sens.append((abs(st["t_stat"]), cand["name"], st))
    sens.sort(key=lambda x: -x[0])
    bonf_t = 3.35  # approx two-sided |t| for p=0.05/62 at large n
    sens_best = sens[0] if sens else (0.0, None, None)

    result = {
        "symbol": symbol,
        "walkforward": {
            "n_folds": wf["n_folds"],
            "qualified": wf["qualified"],
            "n_qualified": len(wf["qualified"]),
            "folds": wf["folds"],
        },
        "deflated_sharpe": dsr,
        "block_bootstrap": {
            "n_perm": N_PERM,
            "block": blockbootstrap.BLOCK,
            "observed_best_t": obs_t,
            "null_p95": p95,
            "null_percentile": pct,
            "beats_null": pct > 0.95,
        },
        "costs": {
            "avg_round_trip": cs["avg_round_trip"],
            "min_cost": cs["min_cost"],
            "max_cost": cs["max_cost"],
            "min_hour": cs["min_hour"],
            "max_hour": cs["max_hour"],
            "flat_assumption": cs["flat_assumption"],
        },
        "cost_sensitivity": {
            "best_candidate": sens_best[1],
            "best_abs_t": sens_best[0],
            "passes_bonferroni": sens_best[0] > bonf_t,
            "n_hourly_candidates": len(sens),
        },
    }
    out = os.path.join(HERE, f"results_tier1_{symbol}.json")
    with open(out, "w") as fh:
        json.dump(result, fh, indent=2, default=str)
    print(f"wrote {out}", flush=True)
    return result


def main(argv):
    syms = SYMBOLS
    for a in argv:
        if a.startswith("--symbols="):
            syms = a.split("=", 1)[1].split(",")
    results = [run_symbol(s) for s in syms]
    print("\n===== TIER 1 SUMMARY =====")
    for r in results:
        bb = r["block_bootstrap"]
        print(f"{r['symbol']}: walk-forward qualified={r['walkforward']['n_qualified']}, "
              f"DSR={r['deflated_sharpe']['dsr']:.3f} "
              f"({'REJECTS null' if r['deflated_sharpe']['rejects_null_5pct'] else 'no edge'}), "
              f"block-null best|t|={bb['observed_best_t']:.2f} vs p95={bb['null_p95']:.2f}, "
              f"avg cost={r['costs']['avg_round_trip']:.4f}, "
              f"cost-sens best|t|={r['cost_sensitivity']['best_abs_t']:.2f}")


if __name__ == "__main__":
    main(sys.argv[1:])
