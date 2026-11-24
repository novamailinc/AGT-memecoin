#!/usr/bin/env python3
"""Phase 2 acquisition tests — event_backfill.py cursors +
health_monitor.py.

Plain script, no network. Covers (§11): smart-money tracking
(cursor behaviour), deployer tracking (cache + incremental
cursor), gap detection, public-fallback-only alert, per-provider
latency/error surfacing.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import json
import os
import sys
import tempfile

REPO = _REPO_ROOT
sys.path.insert(0, REPO)

from daemon.intelligence.acquisition import event_backfill as EB
from daemon.intelligence.acquisition import health_monitor as HM

fails = []
total = [0]


def check(name, cond):
    total[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


HOME = tempfile.mkdtemp(prefix="acq-cursor-")

# ---------------- deployer / smart-money cursors --------------------
store = EB.CursorStore(home=HOME)
check("no cursor on first poll (plain window)",
      store.get("deployer", "mintA") is None)
params = store.page_params("deployer", "mintA", "mintA", limit=100)
check("first poll has no until cursor",
      params == ["mintA", {"limit": 100}])
store.set("deployer", "mintA", "sig_oldest_1")
check("cursor stored", store.get("deployer", "mintA") == "sig_oldest_1")
params = store.page_params("deployer", "mintA", "mintA", limit=100)
check("second poll pages forward with until=cursor",
      params[1].get("until") == "sig_oldest_1")
# cursor survives a reload (new store object, same file)
store2 = EB.CursorStore(home=HOME)
check("cursor persists across processes",
      store2.get("deployer", "mintA") == "sig_oldest_1")
# smart-money cursor is independent per wallet
store.set("smartmoney", "wallet1", "sigW1")
store.set("smartmoney", "wallet2", "sigW2")
check("per-wallet cursors independent",
      store.get("smartmoney", "wallet1") == "sigW1"
      and store.get("smartmoney", "wallet2") == "sigW2"
      and store.get("deployer", "mintA") == "sig_oldest_1")
# cursor file holds signatures only — no provider state, no keys
raw = open(os.path.join(HOME, EB.CURSOR_FILE)).read()
check("cursor file contains only signatures",
      "sig_oldest_1" in raw and "api" not in raw.lower())
# advancing the cursor moves the window forward, never backward
store.set("deployer", "mintA", "sig_newer_9")
check("cursor advances",
      store.page_params("deployer", "mintA", "mintA")[1]["until"]
      == "sig_newer_9")

# ---------------- gap detection --------------------------------------
gd = EB.GapDetector(tolerated_jump=10)
check("gap_for_disconnect: short outage, no gap",
      gd.gap_for_disconnect("k", disconnected_s=1.0) is None)
g = gd.gap_for_disconnect("k", disconnected_s=60.0, last_slot=5000)
check("gap_for_disconnect: 60s outage covers ~150 slots",
      g is not None and g["missed_slots"] >= 150
      and g["from_slot"] == 5000)
slots, slack = EB.slot_range_for_gap(10.0)
check("slot range math (10s -> 25 slots + slack)",
      slots == 25 + slack)

# ---------------- health monitor --------------------------------------
mon = HM.HealthMonitor(home=HOME)
mon.set_configured("helius", True)
mon.set_configured("alchemy", False)
for i in range(10):
    mon.record_rpc({"provider": "helius", "kind": "ok",
                    "elapsed": 0.2 + 0.01 * i})
snap = mon.snapshot()["helius"]
check("health ok with 10/10 successes",
      snap["status"] == "ok" and snap["calls"] == 10)
check("latency median/p95 reported",
      snap["median_s"] is not None and snap["p95_s"] is not None
      and snap["p95_s"] >= snap["median_s"])
check("unconfigured provider reports unconfigured",
      mon.snapshot()["alchemy"]["status"] == "unconfigured")

mon2 = HM.HealthMonitor(home=HOME)
mon2.set_configured("alchemy", True)
for _ in range(4):
    mon2.record_rpc({"provider": "alchemy", "kind": "error",
                     "elapsed": 1.0})
mon2.record_rpc({"provider": "alchemy", "kind": "ok", "elapsed": 0.3})
snap2 = mon2.snapshot()["alchemy"]
check("error rate 0.8 degrades status",
      snap2["status"] == "degraded" and abs(snap2["error_rate"] - 0.8) < 1e-9)

mon3 = HM.HealthMonitor(home=HOME)
mon3.set_configured("public", True)
for _ in range(5):
    mon3.record_rpc({"provider": "public", "kind": "error",
                     "elapsed": 0.5})
check("5/5 errors -> down",
      mon3.snapshot()["public"]["status"] == "down")

mon4 = HM.HealthMonitor(home=HOME)
mon4.set_configured("chainstack", True)
mon4.record_rpc({"provider": "chainstack", "kind": "rate_limited",
                 "elapsed": 0.1, "rate_limit_kind": "monthly_quota"})
check("monthly-quota 429 -> down + flag",
      mon4.snapshot()["chainstack"]["status"] == "down"
      and mon4.snapshot()["chainstack"]["monthly_quota_down"] is True)

# ---------------- public-fallback-only alert ------------------------
mon5 = HM.HealthMonitor(home=HOME)
check("public-only config triggers the alert",
      mon5.public_fallback_only(["public"]) is True)
check("helius+public does not trigger",
      mon5.public_fallback_only(["helius", "public"]) is False)
check("empty config does not trigger",
      mon5.public_fallback_only([]) is False)
alert = mon5.public_fallback_only_alert(["public"])
check("alert dict returned with severity + message",
      alert is not None and alert["severity"] == "warning"
      and "public" in alert["message"].lower())
alert_path = os.path.join(HOME, HM.ALERT_FILE)
check("alert file written",
      os.path.exists(alert_path)
      and json.load(open(alert_path))["alert"] == "public_fallback_only")
cleared = mon5.public_fallback_only_alert(["helius", "public"])
check("alert clears (file removed) when a keyed provider returns",
      cleared is None and not os.path.exists(alert_path))

print("\n%d checks, %d failed" % (total[0], len(fails)))
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("ALL PASS")
