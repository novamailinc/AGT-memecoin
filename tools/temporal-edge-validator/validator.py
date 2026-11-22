#!/usr/bin/env python3
"""Temporal-edge backtest engine (stdlib only).

Tests whether specific time windows (hour-of-day, day-of-week, long/short)
show a real directional edge on a symbol's hourly returns.

Honesty machinery (this is the whole point of the tool):
  * Chronological train/test split (70/30). Discovery happens ONLY on train.
  * Round-trip trading costs applied to every hypothetical trade.
  * Bonferroni correction over all candidates tested (the multiple-comparisons
    trap: test 62 windows and one will look like 65% by pure luck).
  * Permutation test: shuffles returns to show what the "best" window looks
    like under pure noise, so a lucky train result can't fool us.
  * For non-BTC symbols: BTC-beta stripped (residual returns), so we don't
    double-count BTC's shadow as a SOL/ETH edge.
  * Optional volatility filter: trade only calm regimes.
"""
import datetime
import json
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_data

ROUND_TRIP_COST = 0.002   # 0.1% per side, realistic retail spot
TRAIN_FRAC = 0.70
N_PERMUTATIONS = 500
MIN_N = 50                # minimum trades per candidate on train


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def t_pvalue(t):
    """Two-sided p-value, normal approximation (n is large here)."""
    return 2.0 * (1.0 - _phi(abs(t)))


def hourly_returns(rows):
    """rows: (open_ms, open, high, low, close, volume) -> list of dicts."""
    out = []
    for open_ms, o, h, l, c, v in rows:
        dt = datetime.datetime.fromtimestamp(open_ms / 1000.0,
                                             tz=datetime.timezone.utc)
        r = (c - o) / o if o else 0.0
        out.append({"ms": open_ms, "dt": dt, "ret": r,
                    "hour": dt.hour, "weekday": dt.weekday()})
    return out


def aggregate_daily(candles):
    """Group hourly candles into UTC days -> list of (date, weekday, ret)."""
    days = {}
    for c in candles:
        d = c["dt"].date()
        days.setdefault(d, []).append(c)
    out = []
    for d in sorted(days):
        cs = days[d]
        # exact enough: product of (1+r) - 1 over the day's hourly candles
        gross = 1.0
        for c in cs:
            gross *= (1.0 + c["ret"])
        wd = datetime.datetime(d.year, d.month, d.day,
                               tzinfo=datetime.timezone.utc).weekday()
        out.append({"date": d, "weekday": wd, "ret": gross - 1.0})
    return out


def split_train_test(items):
    k = int(len(items) * TRAIN_FRAC)
    return items[:k], items[k:]


def candidate_stats(returns):
    """returns: list of net returns. -> dict of stats (None if too few)."""
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns) / n
    var = sum((x - mean) ** 2 for x in returns) / (n - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    t = mean / (std / math.sqrt(n)) if std > 0 else 0.0
    wins = [x for x in returns if x > 0]
    losses = [-x for x in returns if x <= 0]
    return {
        "n": n,
        "win_rate": len(wins) / n,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "expectancy": mean,
        "t_stat": t,
        "p_value": t_pvalue(t),
    }


def build_candidates_hourly(candles):
    """48 candidates: 24 hours x {long, short}. Trade = that hour's return."""
    cands = []
    for h in range(24):
        for direction in ("long", "short"):
            cands.append({
                "name": f"hour_{h:02d}_{direction}",
                "kind": "hourly",
                "key": h,
                "direction": direction,
            })
    return cands


def build_candidates_weekday(days):
    cands = []
    for wd in range(7):
        for direction in ("long", "short"):
            cands.append({
                "name": f"weekday_{wd}_{direction}",
                "kind": "daily",
                "key": wd,
                "direction": direction,
            })
    return cands


def candidate_returns(cand, candles, days, cost=ROUND_TRIP_COST):
    if cand["kind"] == "hourly":
        seq = [c for c in candles if c["hour"] == cand["key"]]
        rets = [c["ret"] for c in seq]
    else:
        seq = [d for d in days if d["weekday"] == cand["key"]]
        rets = [d["ret"] for d in seq]
    if cand["direction"] == "short":
        rets = [-r for r in rets]
    return [r - cost for r in rets]


def permutation_max_t(all_rets_by_cand, n_perm=N_PERMUTATIONS, seed=7):
    """Null distribution of max |t| : shuffle returns, recompute best t."""
    rng = random.Random(seed)
    # flatten: keep per-candidate index lists into one returns array
    idx_lists = []
    flat = []
    for rets in all_rets_by_cand:
        idx_lists.append(list(range(len(flat), len(flat) + len(rets))))
        flat.extend(rets)
    max_ts = []
    for _ in range(n_perm):
        rng.shuffle(flat)
        best = 0.0
        for idxs in idx_lists:
            vals = [flat[i] for i in idxs]
            st = candidate_stats(vals)
            if st and abs(st["t_stat"]) > best:
                best = abs(st["t_stat"])
        max_ts.append(best)
    max_ts.sort()
    return max_ts


def strip_btc_beta(candles, btc_candles):
    """Residual SOL/ETH returns after removing BTC beta (train-fit OLS)."""
    btc = {c["ms"]: c["ret"] for c in btc_candles}
    paired = [(c["ret"], btc[c["ms"]]) for c in candles if c["ms"] in btc]
    if len(paired) < 100:
        return candles, 0.0
    n = len(paired)
    mx = sum(p[0] for p in paired) / n
    my = sum(p[1] for p in paired) / n
    cov = sum((p[0] - mx) * (p[1] - my) for p in paired) / n
    var = sum((p[1] - my) ** 2 for p in paired) / n
    beta = cov / var if var > 0 else 0.0
    out = []
    for c in candles:
        if c["ms"] in btc:
            c2 = dict(c)
            c2["ret"] = c["ret"] - beta * btc[c["ms"]]
            out.append(c2)
    return out, beta


def trailing_vol_filter(candles, lookback=24, median_days=30):
    """True for candle i if trailing 24h vol is below its 30d median."""
    vols = []
    rets = [c["ret"] for c in candles]
    for i in range(len(rets)):
        window = rets[max(0, i - lookback):i]
        if len(window) < 2:
            vols.append(None)
            continue
        m = sum(window) / len(window)
        v = math.sqrt(sum((x - m) ** 2 for x in window) / (len(window) - 1))
        vols.append(v)
    ok = []
    for i, v in enumerate(vols):
        hist = [x for x in vols[max(0, i - median_days * 24):i] if x is not None]
        if v is None or not hist:
            ok.append(False)
            continue
        hist_sorted = sorted(hist)
        median = hist_sorted[len(hist_sorted) // 2]
        ok.append(v <= median)
    return ok


def kelly_fraction(p, avg_win, avg_loss):
    """f* = (p*b - q) / b ; returns None when no edge."""
    if avg_loss <= 0 or avg_win <= 0:
        return None
    b = avg_win / avg_loss
    q = 1.0 - p
    f = (p * b - q) / b
    return f if f > 0 else None


def backtest(symbol, use_vol_filter=False, cost=ROUND_TRIP_COST):
    rows = fetch_data.load_series(symbol, "1h")
    if len(rows) < 1000:
        raise RuntimeError(f"not enough data for {symbol}: {len(rows)} candles")
    candles = hourly_returns(rows)

    beta = 0.0
    if symbol != "BTCUSDT":
        btc_rows = fetch_data.load_series("BTCUSDT", "1h")
        btc_candles = hourly_returns(btc_rows)
        candles, beta = strip_btc_beta(candles, btc_candles)

    days = aggregate_daily(candles)
    train_c, test_c = split_train_test(candles)
    train_d, test_d = split_train_test(days)

    vol_ok_train = trailing_vol_filter(train_c) if use_vol_filter else None

    candidates = build_candidates_hourly(train_c) + build_candidates_weekday(train_d)
    n_cand = len(candidates)
    bonf_alpha = 0.05 / n_cand

    # --- discovery on TRAIN ---
    train_rets = []
    discovered = []
    for cand in candidates:
        rets = candidate_returns(cand, train_c, train_d, cost)
        if use_vol_filter and cand["kind"] == "hourly":
            mask = [c["hour"] == cand["key"] for c in train_c]
            flags = [f for f, m in zip(vol_ok_train, mask) if m]
            seq_rets = candidate_returns(cand, train_c, train_d, cost=0)
            rets = [r - cost for r, f in zip(seq_rets, flags) if f]
        train_rets.append(rets)
        st = candidate_stats(rets)
        if st is None or st["n"] < MIN_N:
            continue
        if st["expectancy"] > 0 and st["p_value"] < bonf_alpha:
            st["candidate"] = cand["name"]
            st["direction"] = cand["direction"]
            st["kind"] = cand["kind"]
            st["key"] = cand["key"]
            discovered.append(st)

    # --- permutation null on GROSS returns: is there any temporal pattern
    #     at all, before costs? Costs drag every window equally and would
    #     pollute the null, so we shuffle gross returns.
    gross_rets = [candidate_returns(cand, train_c, train_d, cost=0)
                  for cand in candidates]
    null_max_t = permutation_max_t(gross_rets, n_perm=N_PERMUTATIONS)
    best_gross_t = max((abs(candidate_stats(r)["t_stat"])
                        for r in gross_rets if candidate_stats(r)), default=0.0)
    pct = sum(1 for x in null_max_t if x < best_gross_t) / len(null_max_t)

    # --- the cost drag: what the average hourly trade nets after costs ---
    all_net = [c["ret"] - cost for c in train_c]
    drag = candidate_stats(all_net)

    # --- validation on TEST (frozen discoveries only) ---
    validated = []
    for d in discovered:
        cand = {"name": d["candidate"], "kind": d["kind"],
                "key": d["key"], "direction": d["direction"]}
        rets = candidate_returns(cand, test_c, test_d, cost)
        st = candidate_stats(rets)
        if st is None:
            continue
        st["candidate"] = cand["name"]
        st["confirmed"] = st["expectancy"] > 0
        validated.append(st)

    # --- Kelly sizing implied by the best discovery (half-Kelly, sane) ---
    kelly = None
    if discovered:
        best = max(discovered, key=lambda s: s["expectancy"])
        f = kelly_fraction(best["win_rate"], best["avg_win"], best["avg_loss"])
        kelly = {"candidate": best["candidate"], "full_kelly": f,
                 "half_kelly": f / 2 if f else None}

    return {
        "symbol": symbol,
        "btc_beta": beta,
        "n_candles": len(candles),
        "n_candidates": n_cand,
        "bonferroni_alpha": bonf_alpha,
        "vol_filter": use_vol_filter,
        "round_trip_cost": cost,
        "discovered_on_train": sorted(discovered, key=lambda s: -s["expectancy"]),
        "validated_on_test": validated,
        "permutation": {
            "n": N_PERMUTATIONS,
            "best_gross_t": best_gross_t,
            "null_percentile": pct,
            "null_p95": null_max_t[int(0.95 * len(null_max_t))],
        },
        "cost_drag": {"mean_net_per_trade": drag["expectancy"],
                      "t_stat": drag["t_stat"], "n": drag["n"]},
        "kelly": kelly,
    }


def main(argv):
    symbol = "BTCUSDT"
    vol = False
    for a in argv:
        if a.startswith("--symbol="):
            symbol = a.split("=", 1)[1].upper()
        if a == "--vol-filter":
            vol = True
    res = backtest(symbol, use_vol_filter=vol)
    out = os.path.join(HERE, f"results_{symbol}.json")
    with open(out, "w") as fh:
        json.dump(res, fh, indent=2, default=str)
    print(f"wrote {out}")
    print(f"discovered on train: {len(res['discovered_on_train'])}")
    for v in res["validated_on_test"]:
        print(f"  {v['candidate']}: test expectancy {v['expectancy']:.4%} "
              f"({'CONFIRMED' if v['confirmed'] else 'FAILED'})")


if __name__ == "__main__":
    main(sys.argv[1:])
