#!/usr/bin/env python3
"""Phase 9 tests — cost management + usage report (spec §12).

Plain script, no network. Covers: per-method costs from
METHOD_COSTS only (no invented rates; UNVERIFIED -> None);
provider-specific counters; Helius credits / Chainstack RU /
Alchemy CU + bandwidth (measured bytes only, else unmeasured);
cache savings via the router's cache (accounted, not rebuilt);
dedup / batch / incremental / filtered-subscription savings;
budgets enforced per provider (warn 80% / stop 95% proven against
a seeded usage store); low-value suppression ordering
(nonessential shed before essential, essential never suppressed,
background suppressed at 80% incl. global pressure); alerts;
and the usage report reproducing exact counts from a seeded
journal (requests, usage, errors per provider x workload) with
its measurement window stated.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout

REPO = _REPO_ROOT
sys.path.insert(0, REPO)

from daemon.intelligence.acquisition import provider_capabilities as CAPS
from daemon.intelligence.acquisition.cost_management import (
    LOW_VALUE_DEFINITION,
    CostManager,
    cost_unit,
    method_cost,
)
from daemon.intelligence.acquisition.request_scheduler import (
    PRIORITY_BACKGROUND, PRIORITY_CARD, PRIORITY_CRITICAL,
    PRIORITY_DEPLOYER)
from daemon.intelligence.acquisition.provider_usage import UsageStore
from daemon.intelligence.acquisition.usage_report import (
    build_report,
    format_text,
    main as report_main,
)
from daemon.intelligence.acquisition.usage_tracker import UsageTracker

fails = []
total = [0]


def check(name, cond):
    total[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def fresh():
    home = tempfile.mkdtemp(prefix="acq_cost_")
    return home, CostManager(home=home)


# ---------------- METHOD_COSTS only, no invented rates ----------------
check("helius getTransaction costs 10 credits",
      method_cost("helius", "getTransaction") == 10)
check("alchemy getTransaction costs 40 CU",
      method_cost("alchemy", "getTransaction") == 40)
check("chainstack getTransaction costs 1 RU",
      method_cost("chainstack", "getTransaction") == 1)
check("alchemy getTokenLargestAccounts cost UNVERIFIED -> None",
      method_cost("alchemy", "getTokenLargestAccounts") is None)
check("chainstack getProgramAccounts cost UNVERIFIED -> None",
      method_cost("chainstack", "getProgramAccounts") is None)
check("unknown method -> None (never assumed 1)",
      method_cost("alchemy", "noSuchMethod") is None)
check("cost units: helius credits / alchemy CU / chainstack RU / "
      "public unmetered",
      cost_unit("helius") == "credits"
      and cost_unit("alchemy") == "compute units"
      and cost_unit("chainstack") == "request units"
      and cost_unit("public") is None)

home, cm = fresh()
est = cm.cost_estimate("alchemy", "getTransaction", calls=3)
check("cost_estimate traces to METHOD_COSTS (3 x 40 CU)",
      est["cost"] == 120 and est["unit"] == "compute units"
      and est["verified"] is True)
est = cm.cost_estimate("alchemy", "getTokenLargestAccounts")
check("cost_estimate for UNVERIFIED method is None, flagged",
      est["cost"] is None and est["verified"] is False)

# ---------------- low-value definition is explicit ----------------
check("low-value definition names background at 80% and all "
      "nonessential at 95%, essential never suppressed",
      "background" in LOW_VALUE_DEFINITION.lower()
      and "80%" in LOW_VALUE_DEFINITION
      and "95%" in LOW_VALUE_DEFINITION
      and "never" in LOW_VALUE_DEFINITION.lower())

# ---------------- budgets enforced: warn 80% ----------------
home_w, cm_w = fresh()
trk_w = cm_w.tracker
trk_w.record("alchemy", "alchemy", "getTransaction", "routine_tx",
             PRIORITY_CARD, cost=24_000_000, kind="ok")
check("seeded alchemy at exactly 80% of 30M CU budget",
      abs(trk_w.budget_status("alchemy")["pct"] - 0.8) < 0.001)
allowed, reason = cm_w.pre_call_check("alchemy", PRIORITY_BACKGROUND)
check("background suppressed at 80% (own account)",
      allowed is False and reason == "budget_warn_suppress_background")
allowed, _ = cm_w.pre_call_check("alchemy", PRIORITY_DEPLOYER)
check("deployer still allowed at 80% (only background shed)",
      allowed is True)
allowed, _ = cm_w.pre_call_check("alchemy", PRIORITY_CRITICAL)
check("critical (essential) allowed at 80%",
      allowed is True)
allowed, _ = cm_w.pre_call_check("alchemy", PRIORITY_CARD)
check("card (essential) allowed at 80%",
      allowed is True)
alerts = cm_w.alerts()
check("warn alert raised at 80%",
      any(a["account"] == "alchemy" and a["warn"] for a in alerts))
st = cm_w.suppression_status("alchemy")
check("suppression_status: background suppressed, nonessential "
      "not, essential never",
      st["background_suppressed"] is True
      and st["nonessential_suppressed"] is False
      and st["essential_suppressed"] is False)

# ---------------- global pressure: strained provider sheds background everywhere ----------------
allowed, reason = cm_w.pre_call_check("chainstack", PRIORITY_BACKGROUND)
check("background on a healthy account suppressed while ANY "
      "provider is at 80% (global pressure)",
      allowed is False and reason == "global_budget_pressure_background")
allowed, _ = cm_w.pre_call_check("chainstack", PRIORITY_CRITICAL)
check("critical on a healthy account NOT suppressed by global "
      "pressure", allowed is True)
allowed, _ = cm_w.pre_call_check("chainstack", PRIORITY_DEPLOYER)
check("deployer on a healthy account NOT suppressed by global "
      "pressure (background only)", allowed is True)

# ---------------- budgets enforced: stop 95% ----------------
home_s, cm_s = fresh()
trk_s = cm_s.tracker
trk_s.record("alchemy", "alchemy", "getTransaction", "routine_tx",
             PRIORITY_CARD, cost=28_500_000, kind="ok")
check("seeded alchemy at exactly 95% of budget",
      trk_s.budget_status("alchemy")["stop_nonessential"] is True)
# Suppression ordering: nonessential shed BEFORE essential.
allowed_bg, reason_bg = cm_s.pre_call_check("alchemy",
                                            PRIORITY_BACKGROUND)
allowed_dep, reason_dep = cm_s.pre_call_check("alchemy",
                                              PRIORITY_DEPLOYER)
allowed_crit, _ = cm_s.pre_call_check("alchemy", PRIORITY_CRITICAL)
allowed_card, _ = cm_s.pre_call_check("alchemy", PRIORITY_CARD)
check("ordering at 95%: background shed first",
      allowed_bg is False and reason_bg == "budget_stop_nonessential")
check("ordering at 95%: deployer shed too (all nonessential)",
      allowed_dep is False and reason_dep == "budget_stop_nonessential")
check("ordering at 95%: critical NEVER suppressed",
      allowed_crit is True)
check("ordering at 95%: card NEVER suppressed",
      allowed_card is True)
alerts = cm_s.alerts()
check("stop alert raised at 95%",
      any(a["account"] == "alchemy" and a["stop_nonessential"]
          for a in alerts))
st = cm_s.suppression_status("alchemy")
check("suppression_status at 95%: nonessential suppressed, "
      "essential never",
      st["nonessential_suppressed"] is True
      and st["essential_suppressed"] is False)

# ---------------- disabled account refuses everything ----------------
home_d, cm_d = fresh()
cm_d.tracker.disable_account("chainstack", "test disablement")
allowed, reason = cm_d.pre_call_check("chainstack", PRIORITY_CRITICAL)
check("disabled account refuses even essential traffic",
      allowed is False and reason == "account_disabled")
allowed, _ = cm_d.pre_call_check("chainstack", PRIORITY_BACKGROUND)
check("disabled account refuses background too", allowed is False)
check("disabled account appears in alerts",
      any(a["account"] == "chainstack" and a["disabled"]
          for a in cm_d.alerts()))

# ---------------- unenforced budget never suppresses ----------------
home_u, cm_u = fresh()
# Public has no metered cost: record a huge number of costless calls.
for _ in range(5):
    cm_u.tracker.record("public", "public", "getSlot",
                        "routine_account", PRIORITY_CARD, cost=None,
                        kind="ok")
allowed, _ = cm_u.pre_call_check("public", PRIORITY_BACKGROUND)
check("unenforced budget (public) never suppresses background",
      allowed is True)
check("no alerts on an empty/unenforced fleet",
      cm_u.alerts() == [])

# ---------------- suppression audit log ----------------
acct = cm_w.accounting()
check("suppressed pre-call checks are logged",
      len(acct.get("suppressions", [])) >= 2
      and all(s.get("reason") for s in acct["suppressions"]))

# ---------------- WebSocket bandwidth: measured vs unmeasured ----------------
home_b, cm_b = fresh()
cm_b.record_ws("alchemy", "alchemy", bytes_received=4096, events=4,
               subscription="accountSubscribe")
cm_b.record_ws("alchemy", "alchemy", bytes_received=None, events=3)
ws = cm_b.accounting()["ws"]["alchemy"]
check("WS measured bytes accumulate only when reported",
      ws["bytes_measured"] == 4096 and ws["events"] == 7)
check("WS unreported bytes recorded as unmeasured, never invented",
      ws["bytes_unmeasured_events"] == 3)

# ---------------- dedup / batch / incremental / filtered ----------------
home_a, cm_a = fresh()
cm_a.record_dedup("alchemy", "alchemy", "getTransaction",
                  "routine_tx", count=5)
dd = cm_a.accounting()["dedup"]["alchemy"]
check("dedup saved cost = 5 x 40 CU",
      dd["requests"] == 5 and dd["saved_cost"] == 200)
cm_a.record_dedup("alchemy", "alchemy", "getTokenLargestAccounts",
                  "routine_tx", count=2)
dd = cm_a.accounting()["dedup"]["alchemy"]
check("dedup with UNVERIFIED method cost -> saved_unknown, no "
      "invented saving",
      dd["saved_unknown"] == 2 and dd["saved_cost"] == 200)
cm_a.record_batch("chainstack", "chainstack", "getTransaction",
                  "independent_verification", batch_size=10,
                  batch_cost=4)
bt = cm_a.accounting()["batch"]["chainstack"]
check("batch saved cost = 10 x 1 RU - 4 RU billed",
      bt["batch_calls"] == 1 and bt["batch_items"] == 10
      and bt["saved_cost"] == 6)
cm_a.record_incremental("alchemy", "alchemy", "getSignaturesForAddress",
                        "routine_tx", full_cost=400,
                        incremental_cost=40)
inc = cm_a.accounting()["incremental"]["alchemy"]
check("incremental saved cost = full - incremental",
      inc["saved_cost"] == 360 and inc["full_cost"] == 400)
cm_a.record_incremental("alchemy", "alchemy", "getSignaturesForAddress",
                        "routine_tx", full_cost=40,
                        incremental_cost=400)
inc = cm_a.accounting()["incremental"]["alchemy"]
check("incremental that costs more than full saves 0 (floored)",
      inc["saved_cost"] == 360)
cm_a.record_filtered_subscription(
    "helius_main", "helius", "parsed_tx_stream", events_received=100,
    events_filtered=900, bytes_received=2048)
fs = cm_a.accounting()["filtered_subscriptions"]["helius_main"]
check("filtered subscription counts received + filtered",
      fs["events_received"] == 100 and fs["events_filtered"] == 900
      and fs["bytes_measured"] == 2048)
cm_a.record_filtered_subscription(
    "helius_main", "helius", "parsed_tx_stream", events_received=10,
    events_filtered=50, bytes_received=None)
fs = cm_a.accounting()["filtered_subscriptions"]["helius_main"]
check("filtered events with unreported bytes -> unmeasured",
      fs["bytes_unmeasured_events"] == 50)

# ---------------- provider counters ----------------
counters = cm_a.provider_counters()
check("provider counters aggregate per family (helius present)",
      "helius" in counters
      and counters["helius"]["unit"] == "credits")

# ---------------- cache savings from the journal ----------------
home_c, cm_c = fresh()
store_c = cm_c.store
store_c.record("alchemy", "alchemy", "getTransaction", "routine_tx",
               PRIORITY_CARD, cost=40, kind="ok", elapsed=0.1)
store_c.record("alchemy", "alchemy", "getTransaction", "routine_tx",
               PRIORITY_CARD, cost=40, kind="ok", elapsed=0.0,
               cached=True)
store_c.record("alchemy", "alchemy", "getTransaction", "routine_tx",
               PRIORITY_CARD, cost=40, kind="ok", elapsed=0.0,
               cached=True)
sav = cm_c.cache_savings()
check("cache hits counted as saved cost (2 x 40 CU), no second "
      "cache built",
      sav["alchemy"]["cached_requests"] == 2
      and sav["alchemy"]["saved_cost"] == 80)
tot = cm_c.total_saved()
check("total_saved rolls cache savings up",
      tot["alchemy"]["saved_cost"] == 80)

# ---------------- usage report reproduces exact journal counts ----------------
home_r = tempfile.mkdtemp(prefix="acq_report_")
store_r = UsageStore(home=home_r)
# alchemy x routine_tx: 2 ok getTransaction (40 CU, 0.1s), 1 cached
# (saved 40), 2 cost-None UNVERIFIED methods (unknown, not estimated).
store_r.record("alchemy", "alchemy", "getTransaction", "routine_tx",
               PRIORITY_CARD, cost=40, kind="ok", elapsed=0.1)
store_r.record("alchemy", "alchemy", "getTransaction", "routine_tx",
               PRIORITY_CARD, cost=40, kind="ok", elapsed=0.1)
store_r.record("alchemy", "alchemy", "getTransaction", "routine_tx",
               PRIORITY_CARD, cost=40, kind="ok", elapsed=0.0,
               cached=True)
store_r.record("alchemy", "alchemy", "getTokenLargestAccounts",
               "routine_tx", PRIORITY_CARD, cost=None, kind="ok",
               elapsed=0.1)
store_r.record("alchemy", "alchemy", "getTokenSupply", "routine_tx",
               PRIORITY_CARD, cost=None, kind="ok", elapsed=0.1)
# alchemy x routine_account: 1 ok getSlot (20 CU, 0.2s), 1
# rate_limited getAccountInfo (10 CU, 0.3s).
store_r.record("alchemy", "alchemy", "getSlot", "routine_account",
               PRIORITY_BACKGROUND, cost=20, kind="ok", elapsed=0.2)
store_r.record("alchemy", "alchemy", "getAccountInfo",
               "routine_account", PRIORITY_CARD, cost=10,
               kind="rate_limited", elapsed=0.3)
# chainstack x independent_verification: 1 ok (1 RU, 0.05s).
store_r.record("chainstack", "chainstack", "getTransaction",
               "independent_verification", PRIORITY_CARD, cost=1,
               kind="ok", elapsed=0.05)
# A line from last month: must NOT appear in this month's report.
past = time.time() - 40 * 86400
store_r.record("alchemy", "alchemy", "getSlot", "routine_account",
               PRIORITY_CARD, cost=20, kind="ok", ts=past)

report = build_report(home=home_r,
                      provenance="mocked-transport fixture run "
                                 "(test seed)")
rows = {(r["provider"], r["workload"]): r for r in report["rows"]}
check("report has one row per provider x workload (3 rows)",
      len(report["rows"]) == 3)
rtx = rows[("alchemy", "routine_tx")]
check("routine_tx row: exact request count from journal",
      rtx["requests"] == 5)
check("routine_tx row: exact usage (2 x 40 CU; cached 0; "
      "UNVERIFIED unknown, not estimated)",
      rtx["usage"] == 80 and rtx["cost_estimate"] == 80
      and rtx["cost_basis"] == "METHOD_COSTS")
check("routine_tx row: unknown-cost calls flagged, not invented",
      rtx["usage_unknown_calls"] == 2
      and rtx["cost_estimated"] is False)
check("routine_tx row: cached request counted + saved cost",
      rtx["cached_requests"] == 1 and rtx["saved_cost"] == 40)
check("routine_tx row: latency from measured timings "
      "(mean 80ms, p50 100ms)",
      rtx["latency_mean_ms"] == 80.0 and rtx["latency_p50_ms"] == 100.0
      and rtx["latency_source"].startswith("router elapsed_s"))
check("routine_tx row: no errors", rtx["errors"] == 0)
racc = rows[("alchemy", "routine_account")]
check("routine_account row: exact requests/usage/errors",
      racc["requests"] == 2 and racc["usage"] == 30
      and racc["errors"] == 1 and racc["rate_limited"] == 1)
check("routine_account row: latency mean/p50 250ms",
      racc["latency_mean_ms"] == 250.0
      and racc["latency_p50_ms"] == 250.0)
civ = rows[("chainstack", "independent_verification")]
check("chainstack row: exact requests/usage, RU unit",
      civ["requests"] == 1 and civ["usage"] == 1
      and civ["usage_unit"] == "request units"
      and civ["errors"] == 0)
check("report states its measurement window (month + journal "
      "line count)",
      report["measurement_window"]["journal_lines"] == 8
      and report["measurement_window"]["month"]
      == time.strftime("%Y-%m", time.gmtime()))
check("report carries its provenance label (mocked, not live)",
      "mocked-transport" in report["provenance"])
check("report defaults to unlabelled provenance (never presented "
      "as live)",
      "unlabelled" in build_report(home=home_r)["provenance"])
check("report budgets cover every known account",
      set(report["budgets"]) == set(CAPS.ACCOUNTS))

# ---------------- report text + CLI ----------------
text = format_text(report)
check("text table has the spec columns",
      "Provider" in text and "Workload" in text
      and "Requests" in text and "Usage" in text
      and "Latency" in text and "Errors" in text
      and "Cost estimate" in text)
check("text table carries the provenance label",
      "mocked-transport" in text)
buf = io.StringIO()
with redirect_stdout(buf):
    rc = report_main(["--home", home_r, "--month",
                      time.strftime("%Y-%m", time.gmtime()),
                      "--provenance", "mocked-transport fixture run"])
check("CLI text mode exits 0 and prints the table",
      rc == 0 and "routine_tx" in buf.getvalue())
buf = io.StringIO()
with redirect_stdout(buf):
    rc = report_main(["--home", home_r, "--json"])
check("CLI --json exits 0 and emits parseable JSON with rows",
      rc == 0
      and len(json.loads(buf.getvalue())["rows"]) == 3)
proc = subprocess.run(
    [sys.executable, "-m",
     "daemon.intelligence.acquisition.usage_report",
     "--home", home_r, "--month",
     time.strftime("%Y-%m", time.gmtime())],
    cwd=REPO, capture_output=True, text=True)
check("python -m usage_report runs as a module",
      proc.returncode == 0 and "routine_tx" in proc.stdout)

print("\n%d checks, %d failed" % (total[0], len(fails)))
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("ALL PASS")
