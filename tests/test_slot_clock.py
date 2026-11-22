#!/usr/bin/env python3
"""Tests for daemon/launch_scanner/slot_clock.py.

Mirrors the pumpfun-bonkfun-bot reference semantics
(tests/regression/verify_token_age_on_chain.py): age never below
0; a stream slot ahead moves it on, an older one doesn't; no
answer before a reading / unknown slot / stale readings; a failed
read keeps the last reading; and the clock spends at most one
getSlot per refresh interval (no background reader — a 2s
background getSlot would burn ~43k credits/day against a
1M/month free tier). No network: the slot source is injected,
and time.monotonic is patched to a controllable fake clock.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import os
import sys
import time
import unittest
from unittest.mock import patch

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import slot_clock as SC

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


class _FakeTime:
    """Controllable monotonic clock."""

    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


FT = _FakeTime()


def _run():
    # 1. No answer before a reading / unknown slot.
    clock = SC.SlotClock(slot_fn=lambda: None)
    check("age: None with no reading",
          clock.age(100) is None)
    check("age: None for unknown slot",
          clock.age(None) is None)
    check("age: None for zero slot",
          clock.age(0) is None)
    check("current_slot: None with no reading",
          clock.current_slot() is None)

    # 2. A reading answers; age is pure arithmetic at 0.4s/slot.
    calls = []

    def _slot_fn():
        calls.append(1)
        return 1000

    clock = SC.SlotClock(slot_fn=_slot_fn)
    check("age: 100 slots back = 40.0s",
          clock.age(900) == 40.0)
    check("age: current slot = 0.0s",
          clock.age(1000) == 0.0)
    check("age: never below 0 (future slot)",
          clock.age(1100) == 0.0)

    # 3. On-demand refresh is throttled: age() twice in quick
    # succession spends at most one getSlot.
    check("refresh: throttled to one call per interval",
          len(calls) == 1)
    clock.age(950)
    check("refresh: still one call inside the interval",
          len(calls) == 1)
    FT.advance(2.5)
    clock.age(950)
    check("refresh: second call after the interval",
          len(calls) == 2)

    # 4. Stream slot ahead moves it on; older doesn't.
    clock2 = SC.SlotClock(slot_fn=lambda: None)
    clock2.observe(500)
    check("observe: stream slot answers",
          clock2.age(400) == 40.0)
    clock2.observe(480)  # older: ignored
    check("observe: older stream slot ignored",
          clock2.age(400) == 40.0)
    clock2.observe(600)  # ahead: moves on
    check("observe: newer stream slot moves age on",
          clock2.age(400) == 80.0)
    clock2.observe(None)
    check("observe: None is a no-op",
          clock2.age(400) == 80.0)
    clock2.observe("not-a-slot")
    check("observe: garbage is a no-op",
          clock2.age(400) == 80.0)

    # 5. Stale readings -> None (fail-closed).
    FT.advance(SC.STALE_SECONDS + 1.0)
    check("age: None once readings are stale",
          clock2.age(400) is None)

    # 6. Failed read keeps the last reading.
    state = {"slot": 2000}

    def _flaky():
        if state["slot"] is None:
            raise RuntimeError("rpc down")
        return state["slot"]

    clock3 = SC.SlotClock(slot_fn=_flaky)
    check("flaky: first read answers",
          clock3.age(1900) == 40.0)
    state["slot"] = None
    FT.advance(2.5)
    check("flaky: failed read keeps last reading",
          clock3.age(1900) == 40.0 + 2.5)
    FT.advance(SC.STALE_SECONDS + 1.0)
    check("flaky: stale after the window even with failures",
          clock3.age(1900) is None)

    # 7. A slot_fn that raises never propagates out of age().
    clock4 = SC.SlotClock(
        slot_fn=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    check("raising slot_fn: age returns None, never raises",
          clock4.age(100) is None)

    # 8. Node + stream readings combine: the largest age wins.
    clock5 = SC.SlotClock(slot_fn=lambda: 1000)
    clock5.observe(1050)  # stream ahead of node
    check("combined: stream-ahead reading wins",
          clock5.age(900) == 60.0)


with patch.object(time, "monotonic", FT.monotonic):
    _run()

print()
if FAILURES:
    print("FAILURES: %d" % len(FAILURES))
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("ALL SLOT CLOCK TESTS PASSED")
