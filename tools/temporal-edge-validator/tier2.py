#!/usr/bin/env python3
"""Tier 2 driver: scope extensions for the temporal edge validator.

  1. more_symbols  : XRP/DOGE/ADA/BNB 2y hourly — standard backtest + DSR
  2. longer_history: BTC 4y hourly — walk-forward + DSR + block bootstrap
  3. regime        : BTC full history — 124 regime-conditioned candidates
                     (24h x long/short x calm/volatile + 7d x long/short x calm/volatile)
  4. intraday_15m  : BTC 2y 15-minute — 192 quarter-hour candidates + DSR
  5. cross_venue   : Kraken XBTUSD 2y hourly — 62-candidate backtest, no beta
                     strip (it IS bitcoin); Binance-vs-Kraken correlation sanity

Writes results_tier2.json. Paper only; reads the existing klines DB.
"""
import datetime
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

COST = validator.ROUND_TRIP_COST


def _train_candidates(symbol, interval="1h", strip_beta=True):
    """Shared prep: candles, days, 70/30 split, 62 candidates, train net rets."""
    rows = fetch_data.load_series(symbol, interval)
    candles = validator.hourly_returns(rows)
    beta = 0.0
    if strip_beta and symbol not in ("BTCUSDT", "KRAKEN_XBTUSD"):
        btc_candles = validator.hourly_returns(
            fetch_data.load_series("BTCUSDT", "1h"))
        candles, beta = validator.strip_btc_beta(candles, btc_candles)
    days = validator.aggregate_daily(candles)
    train_c, test_c = validator.split_train_test(candles)
    train_d, test_d = validator.split_train_test(days)
    cands = (validator.build_candidates_hourly(train_c)
             + validator.build_candidates_weekday(train_d))
    train_rets = [validator.candidate_returns(c, train_c, train_d, COST)
                  for c in cands]
    return {
        "candles": candles, "days": days, "beta": beta,
        "train_c": train_c, "test_c": test_c, "train_d": train_d,
        "test_d": test_d, "candidates": cands, "train_rets": train_rets,
    }


def _discover_validate(prep, bonf_extra=""):
    cands, tr, te = (prep["candidates"], prep["train_c"], prep["test_c"])
    trd, ted = prep["train_d"], prep["test_d"]
    n_cand = len(cands)
    bonf = 0.05 / n_cand
    discovered, validated = [], []
    for cand, r in zip(cands, prep["train_rets"]):
        st = validator.candidate_stats(r)
        if st is None or st["n"] < validator.MIN_N:
            continue
        if st["expectancy"] > 0 and st["p_value"] < bonf:
            v = validator.candidate_stats(
                validator.candidate_returns(cand, te, ted, COST))
            row = {"candidate": cand["name"],
                   "train_expectancy": st["expectancy"],
                   "train_t": st["t_stat"], "train_n": st["n"]}
            if v is not None:
                row["test_expectancy"] = v["expectancy"]
                row["test_confirmed"] = v["expectancy"] > 0
            discovered.append(row)
            validated.append(row["test_confirmed"] if v else False)
    dsr = deflated_sharpe.deflated_sharpe(prep["train_rets"])
    return {
        "n_candidates": n_cand, "bonferroni_alpha": bonf,
        "discovered": discovered, "n_discovered": len(discovered),
        "n_validated": sum(1 for x in validated if x),
        "dsr": dsr["dsr"], "dsr_rejects_null": dsr["rejects_null_5pct"],
        "best_train_sr": dsr["best_sr"], "dsr_null_sr": dsr["sr0_null"],
    }


def more_symbols():
    out = {}
    for sym in ["XRPUSDT", "DOGEUSDT", "ADAUSDT", "BNBUSDT"]:
        print(f"  {sym} ...", flush=True)
        prep = _train_candidates(sym)
        bt = validator.backtest(sym)  # independent cross-check incl. permutation
        dv = _discover_validate(prep)
        out[sym] = {
            "n_candles": prep and len(prep["candles"]),
            "btc_beta": prep["beta"],
            "discovery": dv,
            "base_permutation_pct": bt["permutation"]["null_percentile"],
            "base_cost_drag_t": bt["cost_drag"]["t_stat"],
        }
    return out


def longer_history():
    print("  BTC 4y walk-forward ...", flush=True)
    wf = walkforward.walkforward("BTCUSDT")
    prep = _train_candidates("BTCUSDT", strip_beta=False)
    dsr = deflated_sharpe.deflated_sharpe(prep["train_rets"])
    print("  BTC 4y block bootstrap (300 perms) ...", flush=True)
    null = blockbootstrap.block_null_max_t(prep["train_c"], prep["candidates"],
                                           n_perm=300, seed=7)
    obs = blockbootstrap.observed_best_t(prep["train_c"], prep["candidates"])
    return {
        "n_candles": len(prep["candles"]),
        "walkforward_qualified": len(wf["qualified"]),
        "walkforward_n_folds": wf["n_folds"],
        "dsr": dsr["dsr"], "dsr_rejects_null": dsr["rejects_null_5pct"],
        "block_obs_t": obs,
        "block_null_p95": null[int(0.95 * len(null))],
        "block_percentile": blockbootstrap.percentile(null, obs),
    }


def regime():
    print("  regime-conditioned candidates ...", flush=True)
    rows = fetch_data.load_series("BTCUSDT", "1h")
    candles = validator.hourly_returns(rows)
    calm = validator.trailing_vol_filter(candles)  # True = calm
    n = len(candles)
    k = int(n * validator.TRAIN_FRAC)
    train_c, test_c = candles[:k], candles[k:]
    calm_tr, calm_te = calm[:k], calm[k:]

    def day_regime(dc, dcalm):
        by_date = {}
        for c, f in zip(dc, dcalm):
            by_date.setdefault(c["dt"].date(), []).append(f)
        return {d: sum(v) / len(v) >= 0.5 for d, v in by_date.items()}

    reg_tr = day_regime(train_c, calm_tr)
    reg_te = day_regime(test_c, calm_te)
    train_d = validator.aggregate_daily(train_c)
    test_d = validator.aggregate_daily(test_c)

    cands = []
    for h in range(24):
        for direction in ("long", "short"):
            for regime in ("calm", "volatile"):
                cands.append({"name": f"hour_{h:02d}_{direction}_{regime}",
                              "kind": "hourly", "key": h,
                              "direction": direction, "regime": regime})
    for wd in range(7):
        for direction in ("long", "short"):
            for regime in ("calm", "volatile"):
                cands.append({"name": f"weekday_{wd}_{direction}_{regime}",
                              "kind": "daily", "key": wd,
                              "direction": direction, "regime": regime})

    def rets(cand, dc, dcalm, dd, dreg):
        s = 1.0 if cand["direction"] == "long" else -1.0
        want_calm = cand["regime"] == "calm"
        out = []
        if cand["kind"] == "hourly":
            for c, f in zip(dc, dcalm):
                if c["hour"] == cand["key"] and f == want_calm:
                    out.append(s * c["ret"] - COST)
        else:
            for d in dd:
                if d["weekday"] == cand["key"] and dreg.get(d["date"]) == want_calm:
                    out.append(s * d["ret"] - COST)
        return out

    train_rets = [rets(c, train_c, calm_tr, train_d, reg_tr) for c in cands]
    bonf = 0.05 / len(cands)
    discovered = []
    for cand, r in zip(cands, train_rets):
        st = validator.candidate_stats(r)
        if st is None or st["n"] < validator.MIN_N:
            continue
        if st["expectancy"] > 0 and st["p_value"] < bonf:
            v = validator.candidate_stats(
                rets(cand, test_c, calm_te, test_d, reg_te))
            discovered.append({
                "candidate": cand["name"],
                "train_expectancy": st["expectancy"], "train_t": st["t_stat"],
                "train_n": st["n"],
                "test_expectancy": v["expectancy"] if v else None,
                "test_confirmed": bool(v and v["expectancy"] > 0),
            })
    dsr = deflated_sharpe.deflated_sharpe(train_rets)
    return {
        "n_candles": n, "n_candidates": len(cands), "bonferroni_alpha": bonf,
        "discovered": discovered, "n_discovered": len(discovered),
        "dsr": dsr["dsr"], "dsr_rejects_null": dsr["rejects_null_5pct"],
    }


def intraday_15m():
    print("  15m quarter-hour search ...", flush=True)
    rows = fetch_data.load_series("BTCUSDT", "15m")
    if len(rows) < 10000:
        return {"skipped": f"only {len(rows)} 15m candles"}
    candles = []
    for ms, o, _h, _l, c, _v in rows:
        dt = datetime.datetime.fromtimestamp(ms / 1000.0,
                                             tz=datetime.timezone.utc)
        r = (c - o) / o if o else 0.0
        candles.append({"ms": ms, "dt": dt, "ret": r,
                        "q": dt.hour * 4 + dt.minute // 15})
    k = int(len(candles) * validator.TRAIN_FRAC)
    train_c, test_c = candles[:k], candles[k:]
    cands = [{"name": f"q{q:02d}_{d}", "q": q, "direction": d}
             for q in range(96) for d in ("long", "short")]

    def rets(cand, dc):
        s = 1.0 if cand["direction"] == "long" else -1.0
        return [s * c["ret"] - COST for c in dc if c["q"] == cand["q"]]

    train_rets = [rets(c, train_c) for c in cands]
    bonf = 0.05 / len(cands)
    discovered = []
    for cand, r in zip(cands, train_rets):
        st = validator.candidate_stats(r)
        if st is None or st["n"] < validator.MIN_N:
            continue
        if st["expectancy"] > 0 and st["p_value"] < bonf:
            v = validator.candidate_stats(rets(cand, test_c))
            discovered.append({
                "candidate": cand["name"],
                "train_expectancy": st["expectancy"], "train_t": st["t_stat"],
                "train_n": st["n"],
                "test_expectancy": v["expectancy"] if v else None,
                "test_confirmed": bool(v and v["expectancy"] > 0),
            })
    dsr = deflated_sharpe.deflated_sharpe(train_rets)
    return {
        "n_candles": len(candles), "n_candidates": len(cands),
        "bonferroni_alpha": bonf,
        "discovered": discovered, "n_discovered": len(discovered),
        "dsr": dsr["dsr"], "dsr_rejects_null": dsr["rejects_null_5pct"],
        "best_train_sr": dsr["best_sr"], "dsr_null_sr": dsr["sr0_null"],
    }


def cross_venue():
    print("  Coinbase cross-venue ...", flush=True)
    rows = fetch_data.load_series("COINBASE_BTCUSD", "1h")
    if len(rows) < 1000:
        return {"skipped": f"only {len(rows)} Coinbase candles"}
    candles = validator.hourly_returns(rows)  # no beta strip: it IS bitcoin
    days = validator.aggregate_daily(candles)
    train_c, test_c = validator.split_train_test(candles)
    train_d, test_d = validator.split_train_test(days)
    cands = (validator.build_candidates_hourly(train_c)
             + validator.build_candidates_weekday(train_d))
    gross = [validator.candidate_returns(c, train_c, train_d, cost=0)
             for c in cands]
    null = validator.permutation_max_t(gross, n_perm=500, seed=7)
    best = max((abs(validator.candidate_stats(r)["t_stat"])
                for r in gross if validator.candidate_stats(r)), default=0.0)
    pct = sum(1 for x in null if x < best) / len(null)
    # discovery with costs, for the record
    bonf = 0.05 / len(cands)
    discovered = []
    for cand in cands:
        st = validator.candidate_stats(
            validator.candidate_returns(cand, train_c, train_d, COST))
        if st and st["n"] >= validator.MIN_N and st["expectancy"] > 0 \
                and st["p_value"] < bonf:
            discovered.append(cand["name"])
    # sanity: correlation of Kraken vs Binance hourly returns
    b_rows = fetch_data.load_series("BTCUSDT", "1h")
    b_ret = {r[0]: (r[4] - r[1]) / r[1] for r in b_rows if r[1]}
    paired = [(c["ret"], b_ret[c["ms"]]) for c in candles if c["ms"] in b_ret]
    n = len(paired)
    mx = sum(p[0] for p in paired) / n
    my = sum(p[1] for p in paired) / n
    cov = sum((p[0] - mx) * (p[1] - my) for p in paired) / n
    vx = sum((p[0] - mx) ** 2 for p in paired) / n
    vy = sum((p[1] - my) ** 2 for p in paired) / n
    corr = cov / (vx * vy) ** 0.5 if vx and vy else 0.0
    return {
        "n_candles": len(candles), "n_paired": n,
        "venue_correlation": corr,
        "best_gross_t": best, "null_p95": null[int(0.95 * len(null))],
        "null_percentile": pct,
        "discovered_with_costs": discovered,
    }


def main():
    results = {}
    print("== Tier 2: more symbols ==", flush=True)
    results["more_symbols"] = more_symbols()
    print("== Tier 2: longer history ==", flush=True)
    results["longer_history"] = longer_history()
    print("== Tier 2: regime conditioning ==", flush=True)
    results["regime"] = regime()
    print("== Tier 2: 15m intraday ==", flush=True)
    results["intraday_15m"] = intraday_15m()
    print("== Tier 2: cross-venue ==", flush=True)
    results["cross_venue"] = cross_venue()
    out = os.path.join(HERE, "results_tier2.json")
    with open(out, "w") as fh:
        json.dump(results, fh, indent=2, default=str)
    print(f"\nwrote {out}", flush=True)
    print("\n===== TIER 2 SUMMARY =====")
    for sym, r in results["more_symbols"].items():
        print(f"{sym}: discovered={r['discovery']['n_discovered']}, "
              f"DSR={r['discovery']['dsr']:.3f}")
    lh = results["longer_history"]
    print(f"BTC 4y: walk-forward qualified={lh['walkforward_qualified']}, "
          f"DSR={lh['dsr']:.3f}, block pct={lh['block_percentile']:.3f}")
    rg = results["regime"]
    print(f"regime (124 cands): discovered={rg['n_discovered']}, "
          f"DSR={rg['dsr']:.3f}")
    m15 = results["intraday_15m"]
    if "skipped" in m15:
        print(f"15m: SKIPPED ({m15['skipped']})")
    else:
        print(f"15m (192 cands): discovered={m15['n_discovered']}, "
              f"DSR={m15['dsr']:.3f}")
    cv = results["cross_venue"]
    if "skipped" in cv:
        print(f"Coinbase: SKIPPED ({cv['skipped']})")
    else:
        print(f"Coinbase: venue corr={cv['venue_correlation']:.4f}, "
              f"best|t|={cv['best_gross_t']:.2f} vs p95={cv['null_p95']:.2f}, "
              f"discovered={cv['discovered_with_costs']}")


if __name__ == "__main__":
    main()
