#!/usr/bin/env python3
"""Shared-guard gating checks for ArmedWatcher (Item 4, 2026-11-16).

Bug: while the Helius quota was dead, every trader's armed_watch
retried the WSS handshake every 60s forever (13,303 handshake-429
failures on 2026-11-16) — the local backoff caps at 60s and the
handshake 429 was never reported to the shared guard. The fix routes
the handshake through the fleet-wide guard: a hot guard skips the
attempt entirely, and a 429 handshake engages the guard for all
processes. Exit pricing is unaffected (exempt polled fallback).

Plain script: run with the repo venv python. No network, no live
state, no real keys (fake key via env, temp FOREX_AGENT_HOME).

2026-11-22: the handshake also takes a pacer token and a 429
handshake engages the pacer (before that, 1,026 handshake 429s
in 48h were invisible to the spike monitor and the auto-quiet).
Cases reset the pacer for isolation — the pacer is fleet-shared
state in the temp home, and a cooldown left by an earlier case
is real behaviour, not test pollution.
"""
import os
import sys
import tempfile
import time

_TMP_HOME = tempfile.mkdtemp(prefix="armed-watch-guard-test-")
os.environ["FOREX_AGENT_HOME"] = _TMP_HOME
os.environ["HELIUS_API_KEY_DISCOVERY"] = "test-wss-key-123"
for _v in ("HELIUS_API_KEY",):
    os.environ.pop(_v, None)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))

from daemon.launch_scanner import armed_watch as aw  # noqa: E402
from daemon.launch_scanner import helius_ratelimit  # noqa: E402
from daemon.launch_scanner.logstream import WSError  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name
          + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


if not hasattr(aw, "_shared_guard") or not hasattr(aw, "_wss_bucket"):
    # The guard hook itself is absent (pre-fix code): report it as a
    # check failure instead of crashing on the missing attribute.
    print("FAIL armed_watch guard hook missing "
          "(_shared_guard/_wss_bucket not defined)")
    print("FAILED: 1 check(s): guard hook missing")
    sys.exit(1)


class FakeGuard:
    """Stand-in for SharedGuard with a controllable cooldown."""

    def __init__(self, cooldown_s=30.0):
        self.cooldown_s = cooldown_s
        self.until = 0.0
        self.report_calls = []
        self.check_calls = []

    def cooldown_remaining(self, bucket):
        self.check_calls.append(bucket)
        return max(0.0, self.until - time.time())

    def report_ratelimited(self, bucket):
        self.report_calls.append(bucket)
        self.until = time.time() + self.cooldown_s
        return self.cooldown_s


class FakeWS:
    """WSClient stand-in: connect() fails with the configured error."""

    attempts = 0
    error = "websocket handshake rejected: HTTP/1.1 429 Too Many Requests"

    def __init__(self, url, timeout=5.0):
        self.url = url

    def connect(self):
        FakeWS.attempts += 1
        raise WSError(FakeWS.error)

    def close(self):
        pass


def _run_watcher(seconds):
    w = aw.ArmedWatcher(on_price=lambda *a: None, ws_url="wss://mainnet.helius-rpc.com/?api-key=fixture")
    w.start()
    time.sleep(seconds)
    w.stop()
    w._thread.join(timeout=5)
    alive = w._thread.is_alive()
    return w, alive


def reset_account_test_state():
    # Isolated test home only. Production success must never erase this hold.
    import sqlite3
    from pathlib import Path
    from daemon.intelligence.acquisition import routine
    hold=Path(_TMP_HOME)/'helius_account_hold.sqlite3'
    if hold.exists():
        with sqlite3.connect(hold) as db:db.execute('DELETE FROM hold')
    helius_ratelimit.reset_pacer_for_tests()


_ORIG_GUARD_FN = aw._shared_guard
_ORIG_WS = aw.WSClient
try:
    # --- 0. bucket helper -------------------------------------------
    expect_bucket = helius_ratelimit.bucket_id_for_key("test-wss-key-123")
    check("bucket: uses the WSS key", aw._wss_bucket() == expect_bucket,
          repr(aw._wss_bucket()))
    os.environ.pop("HELIUS_API_KEY_DISCOVERY", None)
    check("bucket: no key -> empty (public fallback, unguarded)",
          aw._wss_bucket() == "", repr(aw._wss_bucket()))
    os.environ["HELIUS_API_KEY_DISCOVERY"] = "test-wss-key-123"

    aw.WSClient = FakeWS

    # --- 1. a 429 handshake engages the guard; attempts stop --------
    guard = FakeGuard(cooldown_s=30.0)
    aw._shared_guard = lambda: guard
    FakeWS.attempts = 0
    FakeWS.error = ("websocket handshake rejected: "
                    "HTTP/1.1 429 Too Many Requests")
    _w, alive = _run_watcher(7.0)
    check("429: thread exits on stop", not alive)
    check("429: exactly 1 handshake attempt in 7s (guard holds)",
          FakeWS.attempts == 1, "attempts=%d" % FakeWS.attempts)
    check("429: guard engaged exactly once",
          len(guard.report_calls) == 1, str(guard.report_calls))
    check("429: guard engaged on the WSS key bucket",
          guard.report_calls == [expect_bucket], str(guard.report_calls))
    # 2026-11-22: the handshake 429 also engages the pacer — the
    # event is counted (spike monitor / auto-quiet can see it) and
    # the bucket is in pacer cooldown (no token until it clears).
    check("429: pacer recorded the handshake 429",
          helius_ratelimit.recent_429_count(expect_bucket) == 1,
          "count=%d" % helius_ratelimit.recent_429_count(expect_bucket))
    check("429: pacer refuses a token while in cooldown",
          helius_ratelimit.pacer_acquire(expect_bucket, wait_s=0)
          is False)
    reset_account_test_state()

    # --- 2. guard hot at start -> zero attempts; resumes on expiry --
    guard2 = FakeGuard(cooldown_s=30.0)
    guard2.until = time.time() + 30.0
    aw._shared_guard = lambda: guard2
    FakeWS.attempts = 0
    w2 = aw.ArmedWatcher(on_price=lambda *a: None, ws_url="wss://mainnet.helius-rpc.com/?api-key=fixture")
    w2.start()
    time.sleep(3.0)
    check("hot-at-start: 0 attempts while guard hot",
          FakeWS.attempts == 0, "attempts=%d" % FakeWS.attempts)
    guard2.until = 0.0  # cooldown expires
    time.sleep(6.0)
    check("hot-at-start: 1 attempt after guard clears",
          FakeWS.attempts == 1, "attempts=%d" % FakeWS.attempts)
    w2.stop()
    w2._thread.join(timeout=5)
    check("hot-at-start: thread exits on stop", not w2._thread.is_alive())

    # --- 3. non-429 failure: local backoff retries, guard untouched --
    guard3 = FakeGuard(cooldown_s=30.0)
    aw._shared_guard = lambda: guard3
    FakeWS.attempts = 0
    FakeWS.error = "connection refused"
    reset_account_test_state()
    _w, alive = _run_watcher(5.5)
    check("non-429: thread exits on stop", not alive)
    check("non-429: retries via local backoff (>=2 attempts)",
          FakeWS.attempts >= 2, "attempts=%d" % FakeWS.attempts)
    check("non-429: guard never engaged",
          guard3.report_calls == [], str(guard3.report_calls))
    check("non-429: pacer never engaged (no 429 reported)",
          helius_ratelimit.recent_429_count(expect_bucket) == 0,
          "count=%d" % helius_ratelimit.recent_429_count(expect_bucket))

    # --- 4. pacer cooldown blocks the handshake; reset restores it --
    # The guard is cool here, so the pacer is the only gate: a
    # pacer backoff (set by an earlier 429) must hold the handshake
    # at zero attempts, and a fresh pacer must let it through.
    guard4 = FakeGuard(cooldown_s=30.0)
    aw._shared_guard = lambda: guard4
    FakeWS.attempts = 0
    FakeWS.error = ("websocket handshake rejected: "
                    "HTTP/1.1 429 Too Many Requests")
    reset_account_test_state()
    helius_ratelimit.pacer().report(expect_bucket, False)
    _w, alive = _run_watcher(3.0)
    check("pacer: 0 attempts while pacer in cooldown",
          FakeWS.attempts == 0, "attempts=%d" % FakeWS.attempts)
    check("pacer: thread exits on stop", not alive)
    reset_account_test_state()
    FakeWS.attempts = 0
    _w, alive = _run_watcher(3.0)
    check("pacer: 1 attempt after pacer reset",
          FakeWS.attempts == 1, "attempts=%d" % FakeWS.attempts)
    check("pacer: thread exits on stop", not alive)
finally:
    aw._shared_guard = _ORIG_GUARD_FN
    aw.WSClient = _ORIG_WS

print()
if FAILURES:
    print("FAILED: %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("ALL ARMED WATCH GUARD TESTS PASSED")
