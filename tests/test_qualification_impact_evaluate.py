"""evaluate()/admission behaviour for price-impact evidence (SOURCE ONLY, WRITTEN NOT RUN). run: python3 tests/test_qualification_impact_evaluate.py
Hand-built engineering trades (labelled fixtures, NOT market data, NOT real-provider acceptance). The pre-existing ablation 'unsupported' entry always forces
INSUFFICIENT_EVIDENCE, so a generic non-pass proves nothing: these checks assert the EXACT price_impact entry and the counts."""
import os, sys, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import qualification_evaluator as q
from core import qualification_admission as adm

FAILS = 0
def check(n, ok, d=""):
    global FAILS
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else " | " + str(d)), flush=True); FAILS += (not ok)

def trade(i, pct=0.4, unit="percent_v2"):
    d = i * 86400.0
    t = {"id": "t%d" % i, "decision_ts": d, "entry_ts": d + 10, "exit_ts": d + 20, "data_as_of_ts": d - 1, "net_pnl_usd": 1.0 + (i % 3) * 0.1,
         "cost_usd": 0.1, "size_usd": 10.0, "quote_entry_price": 1.0, "fill_entry_price": 1.001, "quote_exit_price": 1.0, "fill_exit_price": 0.999,
         "price_impact_pct": pct, "market_regime": "bull" if i % 2 else "sideways", "signal_type": "s%d" % (i % 3), "liquidity_range": "mid",
         "token_category": "c", "mint": "M%d" % i, "lane": "L%d" % (i % 2), "pnl_basis": "realized_fill"}
    if unit is not None: t["price_impact_unit"] = unit
    return t

GATES = dict(q.TEST_ONLY_THRESHOLDS, min_trades=5, min_days=1)
EV = {"critical_risk_bypasses": 0, "unresolved_integrity_defects": 0, "unclassified_losses": 0, "execution_systemic_failures_unresolved": 0,
      "exit_reliability_tested": True, "strategy_version_frozen": True}
ATT = [{"kind": "entry", "ok": True}, {"kind": "exit", "ok": True}]

def rep(trades):
    return q.evaluate(trades, GATES, EV, 1000.0, 7, attempts=ATT)

def imp(r): return [u for u in r["unsupported_metrics"] if u.startswith("price_impact:")]

def main():
    ok = [trade(i) for i in range(1, 31)]
    r = rep(ok)
    check("report was actually evaluated (metrics present, not an input-validation refusal)", r["metrics"] is not None, r["reasons"][:3])
    check("all stamped+valid: NO price_impact unsupported entry", imp(r) == [] and r["metrics"] is not None, imp(r))
    cases = {
        "one legacy (unstamped)": ([trade(i) for i in range(1, 30)] + [trade(30, unit=None)], (29, 1, 0)),
        "all legacy": ([trade(i, unit=None) for i in range(1, 31)], (0, 30, 0)),
        "unknown unit": ([trade(i) for i in range(1, 30)] + [trade(30, unit="ratio_v1")], (29, 1, 0)),
    }
    for lab, v in (("inf", float("inf")), ("negative", -0.2)):
        cases["stamped but %s" % lab] = ([trade(i) for i in range(1, 30)] + [trade(30, pct=v)], (29, 0, 1))
    for lab, (tr, (ev, lg, inv)) in cases.items():
        r = rep(tr)
        want = "price_impact:not_evaluable:evaluable=%d_of_30_legacy_or_unstamped=%d_stamped_invalid=%d" % (ev, lg, inv)
        check("%s: exactly one price_impact entry, exact text" % lab, imp(r) == [want], imp(r))
        check("%s: counts partition the trades (evaluable+legacy+stamped_invalid == 30)" % lab, ev + lg + inv == 30 and r["metrics"] is not None)
        check("%s: verdict is not PASSED" % lab, r["verdict"] != q.PASSED, r["verdict"])
        check("%s: reasons carry the unsupported entry" % lab, any(x.startswith("unsupported:price_impact:") for x in r["reasons"]))
    # trade-level validation: bool / NaN price_impact_pct are refused by validate_trades before evaluation
    for lab, v in (("bool", True), ("NaN", float("nan")), ("None", None)):
        r = rep([trade(i) for i in range(1, 30)] + [trade(30, pct=v)])
        check("input with price_impact_pct %s is refused at validation (no metrics)" % lab, r["metrics"] is None and any("price_impact_pct" in x for x in r["reasons"]), r["reasons"][:2])
    # admission: a report whose unsupported list holds the impact entry is refused with report_has_unsupported_metrics
    entry = "price_impact:not_evaluable:evaluable=0_of_30_legacy_or_unstamped=30_stamped_invalid=0"
    B = {"strategy_version": "v1", "config_hash": "h1", "data_period": "p1"}
    RUN = dict(B, wallet="W", network="mainnet-beta", trade_amount_usd=5.0, risk_limits={"max_dd": 0.1, "max_day": 0.03}, mode="canary")
    NOW = 10000.0
    POL = {"allowed_states": ["MAINNET_READY_PENDING_AUTHORIZATION"], "max_report_age_s": 3600}
    REG = dict(availability="ok", state="MAINNET_READY_PENDING_AUTHORIZATION", binding=dict(B), missing_historical=[], missing_demo=[])
    def adm_run(unsup):
        r = {"verdict": q.PASSED, "unsupported_metrics": unsup, "gate_results": {"a": True}, "context": dict(B, evaluated_at=NOW - 60), "reasons": [], "metrics": {"profit_factor": 1.0}}
        t = q.dumps_report(r)
        a = dict(B, report_sha256=adm.report_sha256(t), wallet="W", network="mainnet-beta", trade_amount_usd=5.0, risk_limits={"max_dd": 0.1, "max_day": 0.03},
                 mode="canary", issued_at=NOW - 10, expires_at=NOW + 100)
        return adm.admit(REG, t, RUN, POL, a, lambda x: True, NOW, True)
    r0, r1 = adm_run([]), adm_run([entry])
    check("admission control: same report with no unsupported entry allows (synthetic admission only)", r0["allow"] is True, r0["reasons"])
    check("admission: the impact entry alone makes it refuse with report_has_unsupported_metrics", r1["allow"] is False and "report_has_unsupported_metrics" in r1["reasons"], r1["reasons"])
    print("FAILS: %d" % FAILS); return 1 if FAILS else 0

if __name__ == "__main__":
    sys.exit(main())
