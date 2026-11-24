#!/usr/bin/env python3
"""Verdict + report provenance visibility (Stage C, owner-approved
2026-11-22).

The registered halt math is UNTOUCHED: expectancy, net_usd, action
and the halt/survive decision are computed exactly as registered.
The verdict RECORD and the downstream report/lessons consumers gain
additive visibility: per-kind sample breakdown, unverified/mixed/
unknown counts, and an explicit simulation-unvalidated /
not-qualified status, so a legacy expectancy number can never be
read as qualified performance. Fill-model readiness
(fill_model_validated: False) stays a separate axis from the
price-source labels.

Run: python3 tests/test_verdict_visibility.py  (script-style;
executed by scripts/run_script_tests.py, not collected by pytest -
see tests/conftest.py collect_ignore.)
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="verdict-vis-")
os.environ["FOREX_AGENT_HOME"] = _TMP

from daemon.launch_scanner import exit_provenance as PK
from daemon.launch_scanner import grad as G
from daemon.launch_scanner import watcher_lane as W
from daemon.launch_scanner import momentum_lane as M
from daemon.launch_scanner import lessons as L

for mod, name in ((G, "grad"), (W, "watcher"), (M, "momentum")):
    mod.LEDGER_PATH = os.path.join(_TMP, "%s_ledger.json" % name)

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# ---- sample_breakdown taxonomy ---------------------------------------
closes = [
    {"exit_price_kind": "current_price_simulated_fill"},
    {"exit_price_kind": "stale_price_estimate"},
    {"exit_price_kind": "write_down_zero"},
    {"exit_price_kind": "mixed:current_price_simulated_fill,"
                        "stale_price_estimate"},
    {"exit_price_kind": "unknown"},
    {"exit_price_kind": "mixed:current_price_simulated_fill,unknown"},
    {},  # legacy: no label -> UNKNOWN, never promoted
]
bd = PK.sample_breakdown(closes)
check("breakdown: total", bd["total"] == 7)
check("breakdown: only the one current-priced close is verified",
      bd["unverified"] == 6)
check("breakdown: mixed closes counted",
      bd["mixed"] == 2)
check("breakdown: unknown incl. legacy unlabeled + mixed-with-unknown",
      bd["unknown"] == 3)
check("breakdown: per-kind labels preserved verbatim",
      bd["per_kind"]["stale_price_estimate"] == 1
      and bd["per_kind"]["mixed:current_price_simulated_fill,unknown"]
      == 1)

vv = PK.verdict_visibility(closes)
check("visibility: explicit not-qualified status",
      vv["qualification_status"]
      == "simulation_unvalidated_not_qualified")
check("visibility: fill-model axis separate and False",
      vv["fill_model_validated"] is False)
check("visibility: expectancy presentation marked",
      vv["expectancy_presentation"]
      == "reproducibility_only_not_qualified_performance")


# ---- registered halt math preserved, record enriched ------------------
def _drive_verdict(mod, lane, pnls, kinds):
    d = {"closes": [{"pnl_usd": p,
                     "exit_price_kind": k}
                    for p, k in zip(pnls, kinds)],
         "positions": {}, "bankroll": 100.0}
    halt = mod._verdict_check(d)
    return halt, d.get("verdict")


N = G.VERDICT_SAMPLE_CLOSES
for mod, lane in ((G, "grad"), (W, "watcher"), (M, "momentum")):
    # SURVIVE case: positive expectancy, mixed provenance
    pnls = [0.5] * N
    kinds = (["current_price_simulated_fill"] * (N - 30)
             + ["stale_price_estimate"] * 20
             + ["write_down_zero"] * 10)
    halt, v = _drive_verdict(mod, lane, pnls, kinds)
    check("%s: registered math unchanged (survive, exp==0.5)"
          % lane,
          halt is False and v["expectancy"] == 0.5
          and v["n_closes"] == N
          and v["action"].startswith("SURVIVED"))
    check("%s: breakdown attached with exact counts" % lane,
          v["price_kind_breakdown"]["unverified"] == 30
          and v["price_kind_breakdown"]["per_kind"][
              "stale_price_estimate"] == 20)
    check("%s: verdict record is explicitly not qualified" % lane,
          v["qualification_status"]
          == "simulation_unvalidated_not_qualified"
          and v["fill_model_validated"] is False)
    # HALT case: negative expectancy, all current-priced
    pnls = [-0.5] * N
    kinds = ["current_price_simulated_fill"] * N
    halt, v = _drive_verdict(mod, lane, pnls, kinds)
    check("%s: registered math unchanged (halt, exp==-0.5)" % lane,
          halt is True and v["expectancy"] == -0.5
          and v["action"].startswith("HALT"))
    check("%s: all-current sample still not qualified" % lane,
          v["price_kind_breakdown"]["unverified"] == 0
          and v["qualification_status"]
          == "simulation_unvalidated_not_qualified")
    # a recorded verdict is never re-evaluated
    halt2 = mod._verdict_check({"verdict": v, "closes": []})
    check("%s: verdict still runs exactly once" % lane,
          halt2 is False)


# ---- lessons: settled decisions carry provenance ----------------------
home = tempfile.mkdtemp(prefix="verdict-vis-lessons-")
L.log_decision("grad_select", "VISMINT1", 1000.0,
            {"grad_age_s": 10.0, "deployer_launches_24h": 1,
             "curve_ttg_s": 30.0, "curve_trades": 50,
             "copycat_same_ticker_24h": 0},
            home=home)
ok = L.settle_from_close(
    "grad_select",
    {"mint": "VISMINT1", "entry_ts": 1000.0, "pnl_usd": 1.5,
     "realized_mult": 2.5, "closed_reason": "f2_last_real_price",
     "close_ts": 1060.0, "exit_price_kind": "stale_price_estimate"},
    home=home)
check("lessons: settle ok", ok)
recs = L.load_decisions("grad_select", home=home)
check("lessons: settled record carries the close's price kind",
      recs and recs[-1].get("exit_price_kind")
      == "stale_price_estimate")

payload = L.generate_lessons(
    "grad_select", L.merged_settled_decisions("grad_select",
                                              home=home))
vis = payload.get("visibility") or {}
check("lessons: payload carries the not-qualified visibility",
      vis.get("qualification_status")
      == "simulation_unvalidated_not_qualified"
      and vis.get("fill_model_validated") is False
      and (vis.get("price_kind_breakdown") or {}).get("unverified")
      >= 1)

# a close record WITHOUT a kind settles as unknown, never promoted
L.log_decision("grad_select", "VISMINT2", 2000.0,
            {"grad_age_s": 45.0}, home=home)
L.settle_from_close(
    "grad_select",
    {"mint": "VISMINT2", "entry_ts": 2000.0, "pnl_usd": -1.0,
     "realized_mult": 0.0, "closed_reason": "legacy",
     "close_ts": 2060.0},
    home=home)
recs = L.load_decisions("grad_select", home=home)
r2 = [r for r in recs if r.get("mint") == "VISMINT2"]
check("lessons: unlabeled close settles as explicit unknown",
      r2 and r2[0].get("exit_price_kind") == "unknown")

print()
if FAILS:
    print("FAILURES: %d" % len(FAILS))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("ALL VERDICT-VISIBILITY TESTS PASSED")
