#!/usr/bin/env python3
"""Phases 3-4 tests — provider usage store + usage tracker (§7).

Plain script, no network. Covers: per-account usage recording
(calls/cost, per-feature, per-method), monthly-window rollover,
budgets from provider_capabilities (enforced vs unenforced),
warn-before-exhaustion (once per month), stop-nonessential at the
threshold (essential never stopped), account-level disablement
(operator action + monthly-quota 429; throughput 429s never
disable), the request journal (which account serviced each
request, no key material), and per-feature consumption mapping
(Helius parsed_streams / parsed_events vs standard_rpc).
"""
import json
import os
import sys
import tempfile
import time

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, REPO)

from daemon.intelligence.acquisition import provider_capabilities as CAPS
from daemon.intelligence.acquisition.provider_usage import (
    UsageStore,
    budget_for,
    feature_for,
)
from daemon.intelligence.acquisition.request_scheduler import (
    PRIORITY_BACKGROUND, PRIORITY_CARD, PRIORITY_CRITICAL)
from daemon.intelligence.acquisition.usage_tracker import UsageTracker

fails = []
total = [0]
HOME = tempfile.mkdtemp(prefix="acq_usage_")
os.environ["ALCHEMY_API_KEY"] = "testkey-not-real"


def check(name, cond):
    total[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


store = UsageStore(home=HOME)
trk = UsageTracker(home=HOME, store=store)

# ---------------- budgets from capabilities ----------------
b_al = budget_for("alchemy")
check("alchemy budget: 30M CU, enforced, warn 80% / stop 95%",
      b_al["amount"] == 30_000_000 and b_al["unit"] == "compute units"
      and b_al["enforced"] is True
      and b_al["warn_at"] == 24_000_000
      and b_al["stop_at"] == 28_500_000)
b_cs = budget_for("chainstack")
check("chainstack budget: 3M RU, enforced",
      b_cs["amount"] == 3_000_000 and b_cs["unit"] == "request units")
b_pub = budget_for("public")
check("public budget: unenforced (no metered quota)",
      b_pub["amount"] is None and b_pub["enforced"] is False)
b_h = budget_for("helius_main")
check("helius_main budget: 1M credits PROVISIONAL (plan "
      "UNVERIFIED)", b_h["amount"] == 1_000_000
      and "UNVERIFIED" in (b_h["note"] or ""))

# ---------------- recording: per-account, per-feature, per-method ----------------
store.record("alchemy", "alchemy", "getTransaction", "routine_tx",
             PRIORITY_CARD, cost=40, kind="ok", elapsed=0.5)
store.record("alchemy", "alchemy", "getAccountInfo", "routine_account",
             PRIORITY_CARD, cost=10, kind="ok", elapsed=0.2)
store.record("alchemy", "alchemy", "getSlot", "routine_account",
             PRIORITY_CARD, cost=20, kind="rate_limited", elapsed=0.1)
store.record("chainstack", "chainstack", "getTransaction",
             "independent_verification", PRIORITY_CARD, cost=1,
             kind="ok", elapsed=0.3)
rec = store.get("alchemy")
check("per-account counters accumulate (calls/ok/rate_limited)",
      rec["calls"] == 3 and rec["ok"] == 2
      and rec["rate_limited"] == 1)
check("per-account cost accumulates in the account's unit",
      rec["cost_spent"] == 70)
check("per-method split recorded",
      rec["by_method"]["getTransaction"]["calls"] == 1
      and rec["by_method"]["getTransaction"]["cost"] == 40)
check("per-feature split recorded (workload as feature)",
      rec["by_feature"]["routine_tx"]["calls"] == 1
      and rec["by_feature"]["routine_account"]["calls"] == 2)
check("accounts tracked separately (no cross-account merge)",
      store.get("chainstack")["calls"] == 1
      and store.get("chainstack")["cost_spent"] == 1)

# ---------------- cached calls cost nothing ----------------
store.record("alchemy", "alchemy", "getTransaction", "routine_tx",
             PRIORITY_CARD, cost=40, kind="ok", elapsed=0.0,
             cached=True)
rec = store.get("alchemy")
check("cached call counted but costs nothing",
      rec["calls"] == 4 and rec["cached_calls"] == 1
      and rec["cost_spent"] == 70)

# ---------------- feature mapping (Helius specialised products) ----------------
check("helius parsed stream -> parsed_streams feature",
      feature_for("helius", "parsed_tx_stream") == "parsed_streams")
check("helius complex parse -> parsed_events feature",
      feature_for("helius", "complex_tx_parse") == "parsed_events")
check("helius routine -> standard_rpc feature",
      feature_for("helius", "routine_account") == "standard_rpc")
check("alchemy workload is its own feature",
      feature_for("alchemy", "routine_tx") == "routine_tx")
store.record("helius_main", "helius", "getTransaction",
             "complex_tx_parse", PRIORITY_CARD, cost=10, kind="ok")
check("helius per-feature consumption split (parsed_events vs "
      "standard_rpc)",
      store.get("helius_main")["by_feature"]["parsed_events"]["calls"]
      == 1)

# ---------------- journal: which account serviced each request ----------------
lines = [json.loads(x) for x in
         open(os.path.join(HOME,
                           "acquisition_provider_usage.jsonl"))
         if x.strip()]
check("journal has one line per serviced request",
      len(lines) == 6)
check("journal lines carry account/method/workload/cost/kind",
      all(set(("account", "method", "workload", "cost", "kind"))
          <= set(l) for l in lines))
check("journal lines carry the Helius rotation caveat for "
      "helius_main",
      any(l.get("account_note", "").startswith("reserves.rpc rotates")
          for l in lines if l["account"] == "helius_main"))
check("journal contains no key material",
      "testkey-not-real" not in open(os.path.join(
          HOME, "acquisition_provider_usage.jsonl")).read()
      and "api-key=" not in open(os.path.join(
          HOME, "acquisition_provider_usage.jsonl")).read())
usage_text = open(os.path.join(
    HOME, "acquisition_provider_usage.json")).read()
check("usage store file contains no key material",
      "testkey-not-real" not in usage_text
      and "api-key=" not in usage_text)

# ---------------- warn before exhaustion (once per month) ----------------
home_w = tempfile.mkdtemp(prefix="acq_usage_warn_")
trk_w = UsageTracker(home=home_w)
trk_w.record("alchemy", "alchemy", "getTransaction", "routine_tx",
             PRIORITY_CARD, cost=24_000_000, kind="ok")
st = trk_w.budget_status("alchemy")
check("at 80% the warning fires and is recorded",
      st["warn"] is True and abs(st["pct"] - 0.8) < 0.001)
warned_at_1 = trk_w.store.get("alchemy")["warned_at"]
trk_w.record("alchemy", "alchemy", "getTransaction", "routine_tx",
             PRIORITY_CARD, cost=100, kind="ok")
check("warning fires once per month (warned_at unchanged)",
      trk_w.store.get("alchemy")["warned_at"] == warned_at_1)
check("warn_pending surfaces the warning for status output",
      trk_w.warn_pending("alchemy") is not None
      and trk_w.warn_pending("alchemy")["pct"] >= 0.8)
check("warn_pending is None below the threshold",
      UsageTracker(home=tempfile.mkdtemp()).warn_pending("alchemy")
      is None)

# ---------------- stop nonessential at 95% ----------------
home_s = tempfile.mkdtemp(prefix="acq_usage_stop_")
trk_s = UsageTracker(home=home_s)
trk_s.record("alchemy", "alchemy", "getTransaction", "routine_tx",
             PRIORITY_CARD, cost=28_500_000, kind="ok")
st = trk_s.budget_status("alchemy")
check("at 95% stop_nonessential is set",
      st["stop_nonessential"] is True)
allowed, reason = trk_s.nonessential_allowed(
    "alchemy", PRIORITY_BACKGROUND)
check("background refused at the stop threshold",
      allowed is False and reason == "budget_stop_nonessential")
allowed, _ = trk_s.nonessential_allowed("alchemy", PRIORITY_CARD)
check("card (essential) still allowed at the stop threshold",
      allowed is True)
allowed, _ = trk_s.nonessential_allowed("alchemy", PRIORITY_CRITICAL)
check("critical (essential) still allowed at the stop threshold",
      allowed is True)
check("unenforced budget never stops nonessential traffic",
      UsageTracker(home=tempfile.mkdtemp()).nonessential_allowed(
          "public", PRIORITY_BACKGROUND)[0] is True)

# ---------------- account-level disablement ----------------
home_d = tempfile.mkdtemp(prefix="acq_usage_disable_")
trk_d = UsageTracker(home=home_d)
check("account not disabled by default",
      not trk_d.account_disabled("chainstack"))
trk_d.disable_account("chainstack", "test disablement")
check("operator disablement disables the account",
      trk_d.account_disabled("chainstack"))
allowed, reason = trk_d.nonessential_allowed("chainstack",
                                             PRIORITY_CRITICAL)
check("disabled account refuses even essential traffic",
      allowed is False and reason == "account_disabled")
trk_d.enable_account("chainstack")
check("operator re-enable restores the account",
      not trk_d.account_disabled("chainstack"))
trk_d.record_monthly_quota("alchemy")
check("monthly-quota 429 disables the account",
      trk_d.account_disabled("alchemy")
      and "monthly-quota" in
      (trk_d.store.get("alchemy")["disabled_reason"] or ""))

# ---------------- monthly rollover ----------------
home_m = tempfile.mkdtemp(prefix="acq_usage_month_")
store_m = UsageStore(home=home_m)
past = time.time() - 40 * 86400  # ~40 days ago: a different month
store_m.record("alchemy", "alchemy", "getSlot", "routine_account",
               PRIORITY_CARD, cost=20, kind="ok", ts=past)
store_m.record("alchemy", "alchemy", "getSlot", "routine_account",
               PRIORITY_CARD, cost=20, kind="ok")
rec_m = store_m.get("alchemy")
check("month rollover resets counters and keeps the previous "
      "month", rec_m["calls"] == 1 and rec_m["cost_spent"] == 20
      and rec_m["previous_month"] is not None
      and rec_m["previous_month"]["calls"] == 1)

# ---------------- summary ----------------
summ = trk.summary()
check("summary covers every known account",
      set(summ) == set(CAPS.ACCOUNTS))
check("summary reports pct None for unenforced budgets",
      summ["public"]["budget_pct"] is None)
check("summary reports per-feature consumption",
      isinstance(summ["alchemy"]["by_feature"], dict))

del os.environ["ALCHEMY_API_KEY"]

print("\n%d checks, %d failed" % (total[0], len(fails)))
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("ALL PASS")
