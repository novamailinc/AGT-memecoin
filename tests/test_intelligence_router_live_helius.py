#!/usr/bin/env python3
"""Live-gated Helius integration check (Phases 3-4).

Helius is the ONLY provider verifiable live from this VM
(Alchemy/Chainstack live verification is DEFERRED to AGT — the
url_path_segment surrogate swap is UNPROVEN, corrected 2026-11-22
~02:02 PST). This script makes AT MOST ONE live call, and only
when Helius auto-quiet is INACTIVE:

1. Read ~/.forex-agent/helius_quiet.json. If quiet is ACTIVE, the
   live check is SKIPPED (reported, not failed) — a probe inside
   the quiet window measures the quiet mechanism, not Helius.
2. Otherwise ONE getSlot via the Helius adapter
   (providers/helius.py -> reserves.rpc, pacer-governed), routed
   through the central router. Asserts kind ok + an int slot, and
   that usage recorded the call against helius_main.

OPT-IN ONLY (D-08, Phase 2): this suite spends live Helius quota
and can trip rate limits, so it NEVER runs in the default suite
(scripts/run-test-suites skips it unless --include-live or
RUN_LIVE_TESTS=1 is set). It ALSO requires its own explicit
opt-in: set RUN_LIVE_HELIUS_TEST=1 to run it at all. Without the
env var it exits 0 having made zero provider calls (LIVE-SKIP).

Run: RUN_LIVE_HELIUS_TEST=1 venv python \\
    tests/test_intelligence_router_live_helius.py
"""
import json
import os
import sys
import tempfile
import time

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, REPO)

from daemon.intelligence.acquisition import provider_router as PR
from daemon.intelligence.acquisition.request_scheduler import (
    PRIORITY_CRITICAL)

fails = []
total = [0]
HOME = tempfile.mkdtemp(prefix="acq_live_helius_")


def check(name, cond):
    total[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def skipped(name, reason):
    total[0] += 1
    print("SKIP " + name + " — " + reason)


QUIET_FILE = os.path.expanduser("~/.forex-agent/helius_quiet.json")
quiet = {}
try:
    with open(QUIET_FILE) as fh:
        quiet = json.load(fh)
except (OSError, ValueError):
    quiet = {}

# D-08: opt-in gate — without RUN_LIVE_HELIUS_TEST=1 this suite
# makes ZERO provider calls and exits 0 (LIVE-SKIP). The default
# test runner also excludes this file; the env var is the second,
# independent gate so a direct invocation can never spend live
# quota by accident.
if os.environ.get("RUN_LIVE_HELIUS_TEST") != "1":
    print("LIVE-SKIP live Helius getSlot not attempted "
          "(RUN_LIVE_HELIUS_TEST unset — opt-in only; "
          "zero provider calls made)")
    print("\n%d checks, %d failed, 1 skipped (opt-in not set)"
          % (total[0], len(fails)))
    sys.exit(0)

active = bool(quiet.get("active"))
until = quiet.get("until") or 0
if active and time.time() < until:
    skipped("live Helius getSlot (auto-quiet ACTIVE until %s, reason: "
            "%s)" % (time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(until)),
                     quiet.get("reason", "?")),
            "a probe inside the quiet window measures the quiet "
            "mechanism, not Helius; live check DEFERRED, not failed")
    print("LIVE-SKIP live Helius getSlot deferred (auto-quiet active)")
    print("\n%d checks, %d failed, 1 skipped (quiet active)"
          % (total[0], len(fails)))
    sys.exit(0)

router = PR.ProviderRouter(home=HOME, helius_enabled=True)
res = router.call("getSlot", [], priority=PRIORITY_CRITICAL)
check("live Helius getSlot via router -> ok, int slot",
      res["kind"] == "ok" and isinstance(res["result"], int))
check("live call served by helius, account helius_main",
      res.get("provider") == "helius"
      and res.get("account") == "helius_main")
summary = router.usage_summary()
check("usage recorded the live call against helius_main",
      summary.get("helius_main", {}).get("calls", 0) >= 1)
print("live slot: %s (elapsed %.2fs)"
      % (res.get("result"), res.get("elapsed", 0.0)))

print("\n%d checks, %d failed" % (total[0], len(fails)))
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("ALL PASS")
