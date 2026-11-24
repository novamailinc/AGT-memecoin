#!/usr/bin/env python3
"""Rolling walk-forward validation for temporal edges (stdlib only).

A single 70/30 train/test split can get lucky: one favorable regime in the
test window (or an unfavorable one) decides everything. Walk-forward rolls
the train/test window forward through history, so a candidate must be
discovered on train AND confirm out-of-sample repeatedly — not once.

Folds (n=18000 candles): 5 rolling folds, each with a 9000-candle train
(~1y) and the immediately following 1800-candle test (~75d), stepped by
1800 candles. Test windows never overlap; train windows roll.

Qualification rule (stated, fixed before running):
  discovered on train in >= 3 of 5 folds
  AND pooled out-of-sample expectancy > 0
  AND pooled OOS t-stat passes the Bonferroni bar over all 62 candidates.

For non-BTC symbols the BTC beta is re-fit per fold on that fold's train
data (no future information leaks into the hedge).
"""
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_data
import validator

N_FOLDS = 5
MIN_DISCOVERED_FOLDS = 3


def _apply_beta(candles, btc_by_ms, beta):
    """Residualize with an already-fitted beta (no refit -> no leakage)."""
    out = []
    for c in candles:
        b = btc_by_ms.get(c["ms"])
        if b is None:
            continue
        c2 = dict(c)
        c2["ret"] = c["ret"] - beta * b
        out.append(c2)
    return out


def _fold_bounds(n, n_folds=N_FOLDS):
    train_size = n // 2
    test_size = n // 10
    step = test_size
    bounds = []
    for f in range(n_folds):
        s = f * step
        te = s + train_size + test_size
        if te > n:
            break
        bounds.append((s, s + train_size, te))
    return bounds


def walkforward(symbol, n_folds=N_FOLDS, cost=validator.ROUND_TRIP_COST):
    rows = fetch_data.load_series(symbol, "1h")
    if len(rows) < 4000:
        raise RuntimeError(f"not enough data for walk-forward: {len(rows)}")
    candles = validator.hourly_returns(rows)

    btc_by_ms = None
    if symbol != "BTCUSDT":
        btc_rows = fetch_data.load_series("BTCUSDT", "1h")
        btc_by_ms = {r[0]: (r[4] - r[1]) / r[1] if r[1] else 0.0
                     for r in btc_rows}

    n = len(candles)
    bounds = _fold_bounds(n, n_folds)
    n_cand = 62
    bonf_alpha = 0.05 / n_cand

    # per-candidate accumulator across folds
    acc = {}
    fold_reports = []
    for f, (s, m, e) in enumerate(bounds):
        train_c = candles[s:m]
        test_c = candles[m:e]
        test_start_date = test_c[0]["dt"].date()

        if btc_by_ms is not None:
            # fit beta on this fold's TRAIN only; apply it to test (no leakage)
            btc_train = [{"ms": c["ms"],
                          "ret": btc_by_ms.get(c["ms"], 0.0)}
                         for c in train_c]
            train_c, beta = validator.strip_btc_beta(train_c, btc_train)
            test_c = _apply_beta(test_c, btc_by_ms, beta)
        else:
            beta = 0.0

        # day aggregates must follow the (possibly stripped) candles
        all_d = validator.aggregate_daily(train_c + test_c)
        train_d = [d for d in all_d if d["date"] < test_start_date]
        test_d = [d for d in all_d if d["date"] >= test_start_date]

        candidates = (validator.build_candidates_hourly(train_c)
                      + validator.build_candidates_weekday(train_d))
        fold_disc = 0
        for cand in candidates:
            name = cand["name"]
            tr = validator.candidate_returns(cand, train_c, train_d, cost)
            te = validator.candidate_returns(cand, test_c, test_d, cost)
            st_tr = validator.candidate_stats(tr)
            st_te = validator.candidate_stats(te)
            entry = acc.setdefault(name, {
                "candidate": name, "kind": cand["kind"], "key": cand["key"],
                "direction": cand["direction"],
                "folds_discovered": 0, "folds_tested": 0,
                "folds_test_positive": 0, "test_rets_pooled": [],
                "train_t": [], "beta": beta,
            })
            entry["folds_tested"] += 1
            entry["test_rets_pooled"].extend(te)
            if st_tr and st_tr["n"] >= validator.MIN_N:
                entry["train_t"].append(st_tr["t_stat"])
                if st_tr["expectancy"] > 0 and st_tr["p_value"] < bonf_alpha:
                    entry["folds_discovered"] += 1
                    fold_disc += 1
            if st_te and st_te["expectancy"] > 0:
                entry["folds_test_positive"] += 1
        fold_reports.append({
            "fold": f,
            "train_candles": len(train_c),
            "test_candles": len(test_c),
            "train_from": train_c[0]["dt"].isoformat(),
            "test_from": test_c[0]["dt"].isoformat(),
            "test_to": test_c[-1]["dt"].isoformat(),
            "btc_beta": beta,
            "discovered_on_train": fold_disc,
        })

    qualified = []
    summary = []
    for name, a in acc.items():
        pooled = validator.candidate_stats(a["test_rets_pooled"])
        row = {
            "candidate": name,
            "kind": a["kind"],
            "key": a["key"],
            "direction": a["direction"],
            "folds_discovered": a["folds_discovered"],
            "folds_tested": a["folds_tested"],
            "folds_test_positive": a["folds_test_positive"],
            "pooled_oos_expectancy": pooled["expectancy"] if pooled else None,
            "pooled_oos_t": pooled["t_stat"] if pooled else None,
            "pooled_oos_p": pooled["p_value"] if pooled else None,
            "pooled_oos_n": pooled["n"] if pooled else 0,
            "mean_train_t": (sum(a["train_t"]) / len(a["train_t"])
                             if a["train_t"] else None),
        }
        ok = (a["folds_discovered"] >= MIN_DISCOVERED_FOLDS
              and pooled is not None
              and pooled["expectancy"] > 0
              and pooled["p_value"] < bonf_alpha)
        row["qualifies"] = ok
        if ok:
            qualified.append(row)
        summary.append(row)

    summary.sort(key=lambda r: r["folds_discovered"], reverse=True)
    return {
        "symbol": symbol,
        "n_candles": n,
        "n_folds": len(bounds),
        "min_discovered_folds": MIN_DISCOVERED_FOLDS,
        "bonferroni_alpha": bonf_alpha,
        "round_trip_cost": cost,
        "folds": fold_reports,
        "candidates": summary,
        "qualified": qualified,
    }
