#!/usr/bin/env python3
"""$10 paper-trading demo: what happens if you trade the validator's BEST
train window (per symbol) with $10, fully compounded, on frozen TEST data.

Paper only. No real money. This is a demonstration of the "no edge" verdict:
the best window is picked on train (with hindsight), then traded honestly
out-of-sample on test. Costs: 0.2% round-trip per trade.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_data
import validator

START_EQUITY = 10.0
COST = validator.ROUND_TRIP_COST
SYMBOLS = ["BTCUSDT", "SOLUSDT", "ETHUSDT"]


def simulate(symbol):
    rows = fetch_data.load_series(symbol, "1h")
    candles = validator.hourly_returns(rows)
    stripped = False
    if symbol != "BTCUSDT":
        btc_candles = validator.hourly_returns(
            fetch_data.load_series("BTCUSDT", "1h"))
        candles, _beta = validator.strip_btc_beta(candles, btc_candles)
        stripped = True

    days = validator.aggregate_daily(candles)
    train_c, test_c = validator.split_train_test(candles)
    train_d, test_d = validator.split_train_test(days)

    candidates = (validator.build_candidates_hourly(train_c)
                  + validator.build_candidates_weekday(train_d))

    # best window on TRAIN by net expectancy (the "least-bad" discovery)
    scored = []
    for cand in candidates:
        rets = validator.candidate_returns(cand, train_c, train_d, COST)
        st = validator.candidate_stats(rets)
        if st and st["n"] >= validator.MIN_N:
            scored.append((st["expectancy"], cand, st))
    scored.sort(key=lambda x: -x[0])
    best_exp, best_cand, best_st = scored[0]

    # trade it on TEST, $10 fully compounded
    test_rets = validator.candidate_returns(best_cand, test_c, test_d, COST)
    eq = START_EQUITY
    peak = eq
    max_dd = 0.0
    wins = 0
    for r in test_rets:
        eq *= (1.0 + r)
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
        if r > 0:
            wins += 1

    n = len(test_rets)
    return {
        "symbol": symbol,
        "stripped": stripped,
        "window": best_cand["name"],
        "train_expectancy": best_exp,
        "train_t": best_st["t_stat"],
        "train_n": best_st["n"],
        "test_trades": n,
        "test_wins": wins,
        "test_win_rate": wins / n if n else 0.0,
        "final_equity": eq,
        "return_pct": (eq / START_EQUITY - 1.0) * 100.0,
        "max_drawdown_pct": max_dd * 100.0,
        "fees_paid": None,  # filled below
    }


def main():
    total_start = 0.0
    total_end = 0.0
    print(f"Paper-trading ${START_EQUITY:.2f} per symbol on each symbol's BEST "
          f"train window, traded on frozen test data.\n")
    for sym in SYMBOLS:
        r = simulate(sym)
        total_start += START_EQUITY
        total_end += r["final_equity"]
        tag = " (BTC-beta stripped)" if r["stripped"] else ""
        print(f"{sym}{tag}")
        print(f"  best train window : {r['window']} "
              f"(train expectancy {r['train_expectancy']:+.4%}/trade, "
              f"t={r['train_t']:+.2f}, n={r['train_n']})")
        print(f"  test trades       : {r['test_trades']} "
              f"({r['test_wins']} wins, {r['test_win_rate']:.1%})")
        print(f"  final equity      : ${r['final_equity']:.2f} "
              f"({r['return_pct']:+.1f}%), max drawdown {r['max_drawdown_pct']:.1f}%")
        print()
    print(f"TOTAL: ${total_start:.2f} -> ${total_end:.2f} "
          f"({(total_end / total_start - 1.0) * 100.0:+.1f}%)")


if __name__ == "__main__":
    main()
