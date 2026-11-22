#!/usr/bin/env python3
"""Tests for the cross-process shared Helius guard (2026-11-16,
RPC-burn fix, Lever 2).

The legacy in-process guard is unchanged (its tests live in
test_helius_ratelimit.py); this file covers the fleet-wide layer:
one cooldown per KEY, propagated across processes via a flocked
JSON state file that carries only sha256 bucket ids + timestamps —
never key material. No network, no live state file: every test uses
a temp home dir. Run: python3 tests/test_helius_shared_guard.py
"""
import fcntl
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.launch_scanner import helius_ratelimit as rl

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# 1. Legacy in-process API is untouched by the shared layer.
rl.reset_for_tests()
check("legacy: clear by default", rl.cooldown_remaining() == 0.0)
check("legacy: first 429 engages 60s", rl.report_ratelimited() == 60.0)
check("legacy: cooldown hot", rl.cooldown_remaining() > 50.0)
check("legacy: backoff doubles", rl.report_ratelimited() == 120.0)
rl.report_success()
check("legacy: success resets ladder",
      rl.report_ratelimited() == 60.0)
rl.reset_for_tests()
check("legacy: reset clears", rl.cooldown_remaining() == 0.0)

# 2. Bucket ids are stable, distinct, and carry no key material.
check("bucket: deterministic",
      rl.bucket_id_for_key("KEY_A") == rl.bucket_id_for_key("KEY_A"))
check("bucket: 16 hex chars",
      len(rl.bucket_id_for_key("KEY_A")) == 16)
check("bucket: distinct per key",
      rl.bucket_id_for_key("KEY_A") != rl.bucket_id_for_key("KEY_B"))
check("bucket: key itself never in the id",
      "KEY_A" not in rl.bucket_id_for_key("KEY_A"))

# 3. Two guards on one home share one cooldown (cross-process).
with tempfile.TemporaryDirectory() as home:
    g1 = rl.SharedGuard(home=home)
    g2 = rl.SharedGuard(home=home)
    check("shared: clear by default",
          g1.cooldown_remaining("bkt") == 0.0)
    check("shared: report engages 60s",
          g1.report_ratelimited("bkt") == 60.0)
    check("shared: own guard sees it",
          g1.cooldown_remaining("bkt") > 50.0)
    check("shared: second guard sees the fleet cooldown",
          g2.cooldown_remaining("bkt") > 50.0)
    check("shared: state file has no key material",
          "bkt" in open(os.path.join(home, "helius_guard.json")).read()
          and "KEY" not in open(
              os.path.join(home, "helius_guard.json")).read())
    g1.report_success("bkt")
    check("shared: success clears own guard",
          g1.cooldown_remaining("bkt") == 0.0)
    check("shared: success clears the other guard too",
          g2.cooldown_remaining("bkt") == 0.0)
    # Backoff ladder adopted upward via the file.
    check("shared: ladder 60", g1.report_ratelimited("bkt") == 60.0)
    check("shared: ladder 120", g1.report_ratelimited("bkt") == 120.0)
    check("shared: second guard adopts ladder",
          g2.report_ratelimited("bkt") == 240.0)
    check("shared: fleet cooldown long after adoption",
          g2.cooldown_remaining("bkt") > 200.0)
    g1.reset()
    check("shared: reset removes the state file",
          not os.path.exists(os.path.join(home, "helius_guard.json")))

# 4. Corrupt state file falls back to local-only, never raises.
with tempfile.TemporaryDirectory() as home:
    g = rl.SharedGuard(home=home)
    g.report_ratelimited("bkt")
    with open(os.path.join(home, "helius_guard.json"), "w") as fh:
        fh.write("{broken json")
    check("shared: corrupt file does not raise on read",
          g.cooldown_remaining("bkt") > 0.0)  # local guard still knows
    fresh = rl.SharedGuard(home=home)
    check("shared: fresh guard on corrupt file starts clear",
          fresh.cooldown_remaining("bkt") == 0.0)
    check("shared: fresh guard can still engage locally",
          fresh.report_ratelimited("bkt") == 60.0)

# 5. A stuck lock never blocks the caller: the shared write is
# skipped, the local guard still engages, nothing raises.
with tempfile.TemporaryDirectory() as home:
    g = rl.SharedGuard(home=home)
    lock_path = os.path.join(home, "helius_guard.lock")
    holder = open(lock_path, "a+")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        t0 = time.time()
        got = g.report_ratelimited("bkt")
        dt = time.time() - t0
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    check("shared: report under a stuck lock still returns",
          got == 60.0)
    check("shared: report under a stuck lock returns fast",
          dt < 2.0)
    check("shared: local guard engaged despite skipped write",
          g.cooldown_remaining("bkt") > 50.0)
    check("shared: no state file was written under the stuck lock",
          not os.path.exists(os.path.join(home, "helius_guard.json")))

# 6. Concurrent writers from many threads: no exception, file parses.
with tempfile.TemporaryDirectory() as home:
    g = rl.SharedGuard(home=home)
    errors = []

    def _hammer():
        try:
            for _ in range(20):
                g.report_ratelimited("bkt")
                g.cooldown_remaining("bkt")
        except Exception as exc:  # noqa: BLE001 - collected, asserted below
            errors.append(exc)

    threads = [threading.Thread(target=_hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("shared: 8-thread hammer raises nothing", errors == [])
    import json as _json
    with open(os.path.join(home, "helius_guard.json")) as fh:
        parsed = _json.load(fh)
    check("shared: state file parses after the hammer",
          isinstance(parsed.get("buckets"), dict)
          and "bkt" in parsed["buckets"])

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL SHARED GUARD TESTS PASSED")
