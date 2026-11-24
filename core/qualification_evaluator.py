"""Offline demo-qualification evaluator (directive sections 12A, 13, 13.1-13.3, 22.1).

Pure functions, stdlib only, no network, no files, no clock, no randomness
beyond a caller-supplied seed. It never changes qualification state and never
authorizes anything: the strongest verdict is GATES_PASSED_PENDING_REVIEW, which is currently
unreachable because signal-family ablation has no verifiable support (see evaluate).
Missing gate values, missing evidence or malformed trades fail closed.
There are no default qualification thresholds. DIRECTIVE_PROPOSED_VALUES holds only the five
numbers the directive calls "proposed initial" (not approved). Every other number in this file is
either an engineering resampling assumption (ENGINEERING_ASSUMPTIONS, recorded in each report) or a
test-only illustration (TEST_ONLY_THRESHOLDS). Any required metric that cannot be computed from the
supplied data blocks a positive verdict.
"""
import math
import random

NOT_QUALIFIED = "NOT_QUALIFIED"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
PASSED = "GATES_PASSED_PENDING_REVIEW"

# Qualification THRESHOLDS: caller-specified only, no defaults. Every key must be set.
GATE_KEYS = (
    "min_trades", "min_days", "min_profit_factor", "max_drawdown_frac",
    "max_daily_loss_frac", "min_oos_expectancy_usd", "max_single_period_profit_share",
    "min_expectancy_ci_low_usd", "min_trades_per_regime", "max_ruin_probability",
    "ruin_loss_frac", "max_token_exposure_share", "max_failed_execution_rate",
    "min_exit_completion_rate",
)
LIST_GATE_KEYS = ("required_regimes",)  # non-empty list of regime labels that must be covered
# Directive section 13.1/13.2 "proposed initial" values, and ONLY those. Not approved gates,
# not defaults. Usable only if a caller opts in explicitly (tests, owner-approved config).
DIRECTIVE_PROPOSED_VALUES = {
    "min_trades": 500, "min_days": 30, "min_profit_factor": 1.30,
    "max_drawdown_frac": 0.10, "max_daily_loss_frac": 0.03,
}
# Evaluator ENGINEERING ASSUMPTIONS: my own choices, not from the directive, not thresholds.
# Recorded in every report. Callers may override.
ENGINEERING_ASSUMPTIONS = {
    "holdout_fraction": 0.30, "n_periods": 4, "bootstrap_resamples": 1000,
    "bootstrap_block": 5, "confidence": 0.95,
}
# Illustrative values for tests only. Not directive values, not approved.
TEST_ONLY_THRESHOLDS = dict(
    DIRECTIVE_PROPOSED_VALUES, min_oos_expectancy_usd=0.0, max_single_period_profit_share=0.60,
    min_expectancy_ci_low_usd=0.0, min_trades_per_regime=10, max_ruin_probability=0.05,
    ruin_loss_frac=0.5, max_token_exposure_share=0.5, max_failed_execution_rate=0.2,
    min_exit_completion_rate=0.9, required_regimes=["bull", "sideways"],
)
REQUIRED_EVIDENCE = (
    "critical_risk_bypasses", "unresolved_integrity_defects",
    "unclassified_losses", "execution_systemic_failures_unresolved",
    "exit_reliability_tested", "strategy_version_frozen",
)
TRADE_FIELDS = ("id", "decision_ts", "entry_ts", "exit_ts", "data_as_of_ts", "net_pnl_usd",
                "cost_usd", "size_usd", "quote_entry_price", "fill_entry_price",
                "quote_exit_price", "fill_exit_price", "price_impact_pct")
PNL_BASES = ("realized_fill", "estimated", "write_down", "invalid")
LABEL_FIELDS = ("market_regime", "signal_type", "liquidity_range", "token_category", "mint", "lane")


def _num(v):
    return not (v is None or isinstance(v, bool) or not isinstance(v, (int, float))
                or v != v or v in (math.inf, -math.inf))


def validate_gates(gates, assumptions):
    p = []
    if not isinstance(gates, dict):
        return ["gates_not_a_mapping"]
    for k in GATE_KEYS:
        if not _num(gates.get(k)):
            p.append("gate_missing_or_invalid:" + k)
    rg = gates.get("required_regimes")
    if not (isinstance(rg, list) and rg and all(isinstance(x, str) and x for x in rg)):
        p.append("gate_missing_or_invalid:required_regimes")
    if not isinstance(assumptions, dict):
        return p + ["assumptions_not_a_mapping"]
    for k in ENGINEERING_ASSUMPTIONS:
        if not _num(assumptions.get(k)):
            p.append("assumption_missing_or_invalid:" + k)
    if p:
        return p
    for k in ("holdout_fraction", "confidence"):
        if not 0 < assumptions[k] < 1:
            p.append("assumption_out_of_range:" + k)
    for k in ("n_periods", "bootstrap_resamples", "bootstrap_block"):
        if assumptions[k] < 1:
            p.append("assumption_out_of_range:" + k)
    for k in ("min_trades", "min_trades_per_regime"):
        if gates[k] < 1 or gates[k] != int(gates[k]):
            p.append("gate_out_of_range:" + k)
    for k in ("min_days", "min_profit_factor"):
        if not gates[k] > 0:
            p.append("gate_out_of_range:" + k)
    for k in ("max_drawdown_frac", "max_daily_loss_frac", "ruin_loss_frac", "max_token_exposure_share",
              "max_single_period_profit_share"):
        if not 0 < gates[k] <= 1:
            p.append("gate_out_of_range:" + k)
    for k in ("max_ruin_probability", "max_failed_execution_rate", "min_exit_completion_rate"):
        if not 0 <= gates[k] <= 1:
            p.append("gate_out_of_range:" + k)
    # Expectancy floors must be >= 0: a negative floor would let a losing strategy pass.
    # Allowing one needs a separately reviewed explicit semantics, not a silent number.
    for k in ("min_oos_expectancy_usd", "min_expectancy_ci_low_usd"):
        if gates[k] < 0:
            p.append("gate_out_of_range:" + k + ":negative_floor_not_allowed")
    if len(set(gates["required_regimes"])) != len(gates["required_regimes"]):
        p.append("gate_invalid:required_regimes_duplicates")
    for k in ("n_periods", "bootstrap_resamples", "bootstrap_block"):
        if assumptions[k] != int(assumptions[k]):
            p.append("assumption_out_of_range:" + k)
    return p


def validate_trades(trades):
    p = []
    if not trades:
        return ["no_trades"]
    seen = set()
    for i, t in enumerate(trades):
        if not isinstance(t, dict):
            p.append("trade_%d_not_mapping" % i)
            continue
        for f in TRADE_FIELDS:
            v = t.get(f)
            if v is None or isinstance(v, bool) or (f != "id" and (not isinstance(v, (int, float)) or v != v)):
                p.append("trade_%d_missing_or_invalid:%s" % (i, f))
        if any(x.startswith("trade_%d_" % i) for x in p):
            continue
        if t["id"] in seen:
            p.append("duplicate_trade_id:%s" % t["id"])
        seen.add(t["id"])
        if not t["decision_ts"] <= t["entry_ts"] <= t["exit_ts"]:
            p.append("trade_%d_timestamps_not_causal" % i)
        for f in LABEL_FIELDS:
            v = t.get(f)
            if not isinstance(v, str) or not v or v.upper() == "UNKNOWN":
                p.append("trade_%d_missing_label:%s" % (i, f))
        if t.get("pnl_basis") not in PNL_BASES:
            p.append("trade_%d_pnl_basis_missing_or_unknown" % i)
        if t["data_as_of_ts"] > t["decision_ts"]:
            p.append("trade_%d_lookahead_data_after_decision" % i)
        if t["size_usd"] <= 0:
            p.append("trade_%d_nonpositive_size" % i)
        if min(t["quote_entry_price"], t["fill_entry_price"], t["quote_exit_price"], t["fill_exit_price"]) <= 0:
            p.append("trade_%d_nonpositive_price" % i)
    return p


def wilson_interval(wins, n, confidence):
    if n <= 0:
        return (None, None)
    z = _z(confidence)
    ph = wins / n
    d = 1 + z * z / n
    c = ph + z * z / (2 * n)
    m = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def _z(conf):
    lo, hi = 0.0, 10.0  # solve erf for two-sided normal quantile
    target = conf
    for _ in range(80):
        mid = (lo + hi) / 2
        if math.erf(mid / math.sqrt(2)) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def max_drawdown(pnls, start_capital):
    """Return (max_drawdown_frac, avg_drawdown_frac) of the closed-trade equity curve."""
    eq = peak = start_capital
    mx = 0.0
    dds = []
    for x in pnls:
        eq += x
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 1.0
        dds.append(dd)
        mx = max(mx, dd)
    return mx, (sum(dds) / len(dds) if dds else 0.0)


def basic_metrics(trades, start_capital):
    ts = sorted(trades, key=lambda t: (t["exit_ts"], str(t["id"])))
    pn = [t["net_pnl_usd"] for t in ts]
    n = len(pn)
    w = [x for x in pn if x > 0]
    l = [x for x in pn if x < 0]
    gp, gl = sum(w), -sum(l)
    mdd, add = max_drawdown(pn, start_capital)
    run = best = 0
    for x in pn:
        run = run + 1 if x < 0 else 0
        best = max(best, run)
    # Daily loss is measured against that UTC exit-day's START equity (start capital plus all
    # trades closed on earlier days), so it does not weaken as the account falls. The loss as a
    # share of the FIRST start capital is reported separately and is not used by the gate.
    days = {}
    for t in ts:
        d = int(t["exit_ts"] // 86400)
        days[d] = days.get(d, 0) + t["net_pnl_usd"]
    eq, worst_start, worst_first = start_capital, 0.0, 0.0
    for d in sorted(days):
        if days[d] < 0:
            worst_first = max(worst_first, -days[d] / start_capital)
            worst_start = max(worst_start, (-days[d] / eq) if eq > 0 else math.inf)
        eq += days[d]
    neg = [min(0.0, x) for x in pn]
    dv = math.sqrt(sum(x * x for x in neg) / n) if n else 0.0
    return {
        "trades": n, "win_rate": len(w) / n if n else None, "loss_rate": len(l) / n if n else None,
        "avg_win": gp / len(w) if w else 0.0, "avg_loss": -gl / len(l) if l else 0.0,
        "gross_profit": gp, "gross_loss": gl, "net_profit": sum(pn),
        "profit_factor": (gp / gl) if gl > 0 else (math.inf if gp > 0 else 0.0),
        "expectancy_usd": sum(pn) / n if n else None,
        "return_on_start_capital": sum(pn) / start_capital,
        "max_drawdown_frac": mdd, "avg_drawdown_frac": add,
        "largest_loss": min(pn) if pn else 0.0, "max_consecutive_losses": best,
        "downside_deviation": dv,
        "worst_day_loss_frac": worst_start, "worst_day_loss_frac_of_first_start_capital": worst_first,
        "total_cost_usd": sum(t["cost_usd"] for t in ts),
        "max_size_share_of_capital": max(t["size_usd"] for t in ts) / start_capital if ts else None,
        "span_days": ((ts[-1]["exit_ts"] - ts[0]["entry_ts"]) / 86400) if ts else 0.0,
    }


def block_bootstrap(trades, start_capital, resamples, block, confidence, seed):
    """Moving-block bootstrap (keeps short-range dependence) of expectancy and max drawdown."""
    pn = [t["net_pnl_usd"] for t in sorted(trades, key=lambda t: (t["exit_ts"], str(t["id"])))]
    n = len(pn)
    if n == 0:
        return None
    block = max(1, min(int(block), n))
    rng = random.Random(seed)
    exps, dds = [], []
    for _ in range(int(resamples)):
        s = []
        while len(s) < n:
            i = rng.randrange(0, n - block + 1)
            s.extend(pn[i:i + block])
        s = s[:n]
        exps.append(sum(s) / n)
        dds.append(max_drawdown(s, start_capital)[0])
    a = (1 - confidence) / 2
    q = lambda v, p: sorted(v)[min(len(v) - 1, max(0, int(p * len(v))))]
    return {"expectancy_ci": (q(exps, a), q(exps, 1 - a)), "max_drawdown_ci": (q(dds, a), q(dds, 1 - a)),
            "seed": seed, "resamples": int(resamples), "block": block}


def chronological_split(trades, holdout_fraction):
    """Split by DECISION time (the information boundary). Returns (dev, hold, purged).

    Holdout = trades whose decision_ts is at or after the boundary B. A development trade whose
    exit_ts is later than B had its outcome unknown at B but known to anyone tuning during the
    holdout, so it is PURGED from development. Purged trades are never dropped from the
    whole-sample metrics, and their count and P&L are reported. Ties in decision_ts are not split.
    """
    ts = sorted(trades, key=lambda t: (t["decision_ts"], str(t["id"])))
    k = int(round(len(ts) * (1 - holdout_fraction)))
    while 0 < k < len(ts) and ts[k - 1]["decision_ts"] == ts[k]["decision_ts"]:
        k += 1
    dev, hold = ts[:k], ts[k:]
    if not hold or not dev:
        return dev, hold, []
    B = hold[0]["decision_ts"]
    purged = [t for t in dev if t["exit_ts"] > B]
    keep = [t for t in dev if t["exit_ts"] <= B]
    return keep, hold, purged


def period_breakdown(trades, n_periods):
    ts = sorted(trades, key=lambda t: (t["exit_ts"], str(t["id"])))
    if not ts:
        return []
    lo, hi = ts[0]["exit_ts"], ts[-1]["exit_ts"]
    width = (hi - lo) / n_periods or 1.0
    out = [{"period": i, "trades": 0, "net_profit": 0.0} for i in range(int(n_periods))]
    for t in ts:
        i = min(int(n_periods) - 1, int((t["exit_ts"] - lo) / width))
        out[i]["trades"] += 1
        out[i]["net_profit"] += t["net_pnl_usd"]
    return out


def group_breakdown(trades, key):
    g = {}
    for t in trades:
        k = str(t.get(key, "UNKNOWN"))
        d = g.setdefault(k, {"trades": 0, "net_profit": 0.0, "wins": 0})
        d["trades"] += 1
        d["net_profit"] += t["net_pnl_usd"]
        d["wins"] += 1 if t["net_pnl_usd"] > 0 else 0
    return g


def bootstrap_ruin_probability(trades, start_capital, ruin_loss_frac, resamples, block, seed):
    """Fraction of resampled equity paths whose equity ever falls to start*(1-ruin_loss_frac).

    Explicit assumptions: future trades are drawn in blocks from the observed trade P&L
    distribution (stationary, short-range dependence only within a block), trade size
    unchanged. No analytic formula is offered: closed-form ruin needs i.i.d. trades and
    fixed payoffs, which this data does not establish. Empirical estimate only.
    """
    pn = [t["net_pnl_usd"] for t in sorted(trades, key=lambda t: (t["exit_ts"], str(t["id"])))]
    n = len(pn)
    if n == 0:
        return None
    block = max(1, min(int(block), n))
    rng = random.Random(seed + 1)
    floor = start_capital * (1 - ruin_loss_frac)
    hit = 0
    for _ in range(int(resamples)):
        s, eq, ruined = 0, start_capital, False
        while s < n and not ruined:
            i = rng.randrange(0, n - block + 1)
            for x in pn[i:i + block]:
                eq += x
                s += 1
                if eq <= floor:
                    ruined = True
                    break
                if s >= n:
                    break
        hit += 1 if ruined else 0
    return {"probability": hit / int(resamples), "resamples": int(resamples), "block": block,
            "assumptions": "block-resampled observed P&L, unchanged size, horizon equal to sample length; "
                           "empirical, no analytic formula",
            "analytic": "UNSUPPORTED:iid_and_fixed_payoff_not_established"}


def exposure_concentration(trades, start_capital):
    """Peak CONCURRENT exposure over realized (closed-trade) capital.

    Evaluated at EVERY entry and exit instant, because the share can peak right after an exit
    shrinks the denominator. Half-open activity: a trade is active at t when entry <= t < exit.
    Capital at t = start capital + P&L of every trade with exit <= t (post-settlement).
    Same-instant ordering: exits settle first, then entries activate. A zero-duration trade is
    active at its own instant with its P&L NOT yet in capital (it is realized after being active);
    a second sample right after it settles has it inactive and its P&L in capital.
    Capital <= 0 with any exposure gives infinity. Open positions are not marked to market.
    """
    out = {"mint": 0.0, "lane": 0.0, "total": 0.0, "max_simultaneous": 0}
    times = sorted({t["entry_ts"] for t in trades} | {t["exit_ts"] for t in trades})

    def sample(act, cap):
        out["max_simultaneous"] = max(out["max_simultaneous"], len(act))
        for key in ("mint", "lane"):
            d = {}
            for t in act:
                d[t[key]] = d.get(t[key], 0.0) + t["size_usd"]
            v = max(d.values()) if d else 0.0
            out[key] = max(out[key], (v / cap) if cap > 0 else (math.inf if v > 0 else 0.0))
        tot = sum(t["size_usd"] for t in act)
        out["total"] = max(out["total"], (tot / cap) if cap > 0 else (math.inf if tot > 0 else 0.0))

    for at in times:
        zero_now = [t for t in trades if t["entry_ts"] == t["exit_ts"] == at]
        zid = {id(t) for t in zero_now}
        live = [t for t in trades if t["entry_ts"] <= at < t["exit_ts"]]
        cap_pre = start_capital + sum(t["net_pnl_usd"] for t in trades if t["exit_ts"] <= at and id(t) not in zid)
        sample(live + zero_now, cap_pre)
        if zero_now:
            cap_post = start_capital + sum(t["net_pnl_usd"] for t in trades if t["exit_ts"] <= at)
            sample(live, cap_post)
    return out


def to_json_safe(obj):
    """Convert a report (or any value) to strict-JSON-safe data. Non-finite floats become
    {"__nonfinite__": "inf"|"-inf"|"nan"}; tuples become lists. Report internals keep real inf so
    numeric gates still fail closed; only this export changes representation."""
    if isinstance(obj, float):
        if obj != obj:
            return {"__nonfinite__": "nan"}
        if obj == math.inf:
            return {"__nonfinite__": "inf"}
        if obj == -math.inf:
            return {"__nonfinite__": "-inf"}
        return obj
    if isinstance(obj, dict):
        return {str(k): to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_json_safe(v) for v in obj]
    return obj


def from_json_safe(obj):
    if isinstance(obj, dict):
        if set(obj) == {"__nonfinite__"}:
            return {"inf": math.inf, "-inf": -math.inf, "nan": math.nan}[obj["__nonfinite__"]]
        return {k: from_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [from_json_safe(v) for v in obj]
    return obj


def dumps_report(report):
    """Strict JSON text (allow_nan=False) of a report, with sentinels for non-finite values."""
    import json
    return json.dumps(to_json_safe(report), allow_nan=False, sort_keys=True)


def _bps(a, b):
    return (a / b - 1.0) * 1e4


def execution_quality(trades, attempts):
    """Entry/exit slippage vs quote, price impact, failed-execution and exit-completion rates,
    time-to-enter/exit. attempts: list of {kind: entry|exit, ok: bool, reason: str}."""
    ent = [_bps(t["fill_entry_price"], t["quote_entry_price"]) for t in trades]   # buy: higher fill is worse
    ext = [-_bps(t["fill_exit_price"], t["quote_exit_price"]) for t in trades]    # sell: lower fill is worse
    def mean(v):
        return sum(v) / len(v) if v else None
    ea = [a for a in attempts if a["kind"] == "entry"]
    xa = [a for a in attempts if a["kind"] == "exit"]
    det = [t["deterioration_detected_ts"] - t["entry_ts"] for t in trades
           if _num(t.get("deterioration_detected_ts")) and t["deterioration_detected_ts"] >= t["entry_ts"]]
    return {
        "entry_slippage_bps_mean": mean(ent), "exit_slippage_bps_mean": mean(ext),
        "price_impact_pct_mean": mean([t["price_impact_pct"] for t in trades]),
        "price_impact_pct_max": max(t["price_impact_pct"] for t in trades),
        "failed_execution_rate": (sum(1 for a in attempts if not a["ok"]) / len(attempts)) if attempts else None,
        "exit_completion_rate": (sum(1 for a in xa if a["ok"]) / len(xa)) if xa else None,
        "entry_attempts": len(ea), "exit_attempts": len(xa),
        "time_to_enter_s_mean": mean([t["entry_ts"] - t["decision_ts"] for t in trades]),
        "time_to_exit_s_mean": mean([t["exit_ts"] - t["entry_ts"] for t in trades]),
        "time_to_recognize_deterioration_s_mean": mean(det),
        "deterioration_samples": len(det),
    }


def _attempts_problems(attempts):
    if not isinstance(attempts, list) or not attempts:
        return ["attempts_missing:failed_execution_log_required"]
    p = []
    for i, a in enumerate(attempts):
        if not (isinstance(a, dict) and a.get("kind") in ("entry", "exit") and isinstance(a.get("ok"), bool)):
            p.append("attempt_%d_invalid" % i)
    return p


def evaluate(trades, gates, evidence, start_capital, seed, attempts=None, ablation=None,
             assumptions=None, context=None):
    """Return a deterministic report. Fails closed; never claims profitability.

    gates: caller-specified qualification thresholds (no defaults).
    assumptions: engineering resampling knobs; ENGINEERING_ASSUMPTIONS if omitted, recorded in report.
    attempts: execution attempt log incl. failures. ablation: {signal_family: {"verdict": "PASS"|"FAIL"}}
    produced by a paired family-masked replay; absent means UNSUPPORTED and blocks a pass.
    """
    assumptions = dict(ENGINEERING_ASSUMPTIONS) if assumptions is None else assumptions
    gp = validate_gates(gates, assumptions)
    tp = validate_trades(trades) if isinstance(trades, list) else ["trades_not_a_list"]
    ep = []
    if not isinstance(evidence, dict):
        ep = ["evidence_not_a_mapping"]
    else:
        for k in REQUIRED_EVIDENCE:
            if evidence.get(k) is None:
                ep.append("evidence_missing:" + k)
    if not _num(start_capital) or not start_capital > 0:
        ep.append("start_capital_invalid")
    if not isinstance(seed, int) or isinstance(seed, bool):
        ep.append("seed_missing")
    ep += _attempts_problems(attempts)
    base = {"verdict": INSUFFICIENT_EVIDENCE, "reasons": gp + tp + ep, "gate_results": {},
            "unsupported_metrics": [], "metrics": None, "out_of_sample": None, "uncertainty": None,
            "context": context or {},
            "thresholds": dict(gates) if isinstance(gates, dict) else None,
            "engineering_assumptions": dict(assumptions) if isinstance(assumptions, dict) else None,
            "note": "Offline evaluation only. Not a profitability claim, not an authorization."}
    if base["reasons"]:
        return base

    A = assumptions
    m = basic_metrics(trades, start_capital)
    dev, hold, purged = chronological_split(trades, A["holdout_fraction"])
    om = basic_metrics(hold, start_capital) if hold else None
    ci = block_bootstrap(trades, start_capital, A["bootstrap_resamples"], A["bootstrap_block"],
                         A["confidence"], seed)
    wl, wh = wilson_interval(round(m["win_rate"] * m["trades"]), m["trades"], A["confidence"])
    periods = period_breakdown(trades, int(A["n_periods"]))
    pos = sum(p["net_profit"] for p in periods if p["net_profit"] > 0)
    top_share = (max(p["net_profit"] for p in periods) / pos) if pos > 0 else None
    ruin = bootstrap_ruin_probability(trades, start_capital, gates["ruin_loss_frac"],
                                      A["bootstrap_resamples"], A["bootstrap_block"], seed)
    expo = exposure_concentration(trades, start_capital)
    eq = execution_quality(trades, attempts)
    by_regime = group_breakdown(trades, "market_regime")

    unsupported = []
    if eq["time_to_recognize_deterioration_s_mean"] is None:
        unsupported.append("time_to_recognize_deterioration:no_deterioration_samples")
    if eq["exit_completion_rate"] is None:
        unsupported.append("exit_completion_rate:no_exit_attempts")
    # Ablation needs verifiable paired family-masked replay (same input digest, timestamps and
    # strategy version) that this module cannot re-derive. Caller-supplied verdicts are assertions,
    # not evidence: ignored here, so a positive verdict is unreachable until such a verifier exists.
    unsupported.append("signal_family_ablation:no_verifiable_paired_replay_support"
                       + (":caller_assertion_ignored" if ablation else ""))
    basis_counts = {b: 0 for b in PNL_BASES}
    for t in trades:
        basis_counts[t["pnl_basis"]] += 1
    unresolved_econ = len(trades) - basis_counts["realized_fill"]
    if unresolved_econ:
        unsupported.append("economic_evidence:%d_trades_not_realized_fill" % unresolved_econ)

    g = {}
    insufficient = []
    g["sample_size"] = m["trades"] >= gates["min_trades"]
    g["observation_days"] = m["span_days"] >= gates["min_days"]
    g["regime_coverage"] = all(by_regime.get(r, {"trades": 0})["trades"] >= gates["min_trades_per_regime"]
                               for r in gates["required_regimes"])
    g["net_expectancy_positive"] = m["expectancy_usd"] > 0
    g["expectancy_ci_low"] = ci["expectancy_ci"][0] > gates["min_expectancy_ci_low_usd"]
    g["profit_factor"] = m["profit_factor"] >= gates["min_profit_factor"]
    g["max_drawdown"] = m["max_drawdown_frac"] <= gates["max_drawdown_frac"]
    g["daily_loss"] = m["worst_day_loss_frac"] <= gates["max_daily_loss_frac"]
    g["out_of_sample"] = bool(om and om["trades"] > 0 and om["expectancy_usd"] > gates["min_oos_expectancy_usd"])
    if not (om and om["trades"] > 0):
        insufficient.append("empty_holdout")
    g["period_consistency"] = top_share is not None and top_share <= gates["max_single_period_profit_share"]
    g["risk_of_ruin"] = ruin["probability"] <= gates["max_ruin_probability"]
    g["exposure_concentration"] = max(expo["mint"], expo["lane"]) <= gates["max_token_exposure_share"]
    g["failed_execution_rate"] = eq["failed_execution_rate"] <= gates["max_failed_execution_rate"]
    g["exit_completion"] = eq["exit_completion_rate"] is not None and eq["exit_completion_rate"] >= gates["min_exit_completion_rate"]
    g["critical_risk_bypasses_zero"] = evidence["critical_risk_bypasses"] == 0
    g["integrity_defects_zero"] = evidence["unresolved_integrity_defects"] == 0
    g["losses_classified"] = evidence["unclassified_losses"] == 0
    g["execution_no_systemic_failure"] = evidence["execution_systemic_failures_unresolved"] == 0
    g["exit_reliability_tested"] = evidence["exit_reliability_tested"] is True
    g["strategy_version_frozen"] = evidence["strategy_version_frozen"] is True
    g["economic_evidence_complete"] = unresolved_econ == 0

    failed = [k for k, v in g.items() if not v]
    short = ("sample_size", "observation_days", "regime_coverage", "exit_completion",
             "economic_evidence_complete")
    hard_fail = [k for k in failed if k not in short]
    if hard_fail:
        verdict = NOT_QUALIFIED
    elif failed or unsupported:
        verdict = INSUFFICIENT_EVIDENCE
    else:
        verdict = PASSED
    base.update({
        "verdict": verdict,
        "reasons": ["gate_failed:" + k for k in failed] + ["unsupported:" + u for u in unsupported],
        "gate_results": g, "unsupported_metrics": unsupported, "metrics": m,
        "pnl_basis_counts": basis_counts,
        "out_of_sample": {"development_trades": len(dev), "holdout_trades": len(hold), "holdout_metrics": om,
                          "purged_trades": len(purged),
                          "purged_net_pnl_usd": sum(t["net_pnl_usd"] for t in purged),
                          "purged_ids": [t["id"] for t in purged],
                          "embargo": "none beyond purge (explicit assumption; no embargo duration is specified)",
                          "boundary": "decision_ts; development trades exiting after the first holdout decision are purged"},
        "uncertainty": {"win_rate_ci": (wl, wh), **ci},
        "risk_of_ruin": ruin, "exposure_concentration": expo,
        "exposure_assumptions": "peak concurrent size over realized capital, sampled at every entry and exit "
                                "instant, post-settlement; open positions not marked to market; no embargo "
                                "beyond the holdout purge; purge and capital interact only through the "
                                "whole-sample metrics", "execution_quality": eq,
        "periods": periods, "top_period_profit_share": top_share,
        "by_signal_type": group_breakdown(trades, "signal_type"), "by_regime": by_regime,
        "by_liquidity_range": group_breakdown(trades, "liquidity_range"),
        "by_token_category": group_breakdown(trades, "token_category"),
    })
    return base
