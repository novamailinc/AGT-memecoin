#!/usr/bin/env python3
"""SIGTERM graceful-stop checks for the launch scanner (Item 3, 2026-11-16).

Bug: the scanner logged "received signal 15, stopping" but kept scanning —
Daemon._stop was only honored BETWEEN run_once() cycles, while a cycle
(judge backlog + 240s scan budget) runs for minutes, so restarts needed
SIGKILL and the "stopped" line never appeared. The fix honors _stop
inside the long loops (drains, scan candidate loop, judge loop) and at
the scan-phase boundary in run_once().

Plain script: run with the repo venv python. No network, no live state.
"""
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("FOREX_AGENT_HOME", "/tmp/scanner-sigterm-test-home")

from daemon.launch_scanner import LaunchScanner  # noqa: E402
from daemon.launch_scanner import runner  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name
          + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# --- 1. _cycle_over_budget honors _stop -------------------------------
s = LaunchScanner()
s._cycle_deadline = time.time() + 3600.0  # far from over budget
s._stop = False
check("budget: no-stop + future deadline -> False",
      s._cycle_over_budget() is False)
s._stop = True
check("budget: stop + future deadline -> True",
      s._cycle_over_budget() is True)
s._stop = False
s._cycle_deadline = time.time() - 1.0  # past deadline
check("budget: no-stop + past deadline -> True",
      s._cycle_over_budget() is True)
s2 = LaunchScanner()  # no _cycle_deadline attribute at all
s2._stop = True
check("budget: stop + no deadline set -> True",
      s2._cycle_over_budget() is True)
s2._stop = False
check("budget: no-stop + no deadline set -> False (fail-open)",
      s2._cycle_over_budget() is False)


# --- 2. Drains break immediately on _stop, process normally otherwise --
class FakeFeed:
    def __init__(self, items):
        self._items = list(items)

    def drain(self):
        return list(self._items)


def _counting_consider(calls):
    def _consider(config, adapter, state, c):
        calls.append(c.get("mint"))
    return _consider


cfg = SimpleNamespace(degen=SimpleNamespace(degen_lane_enabled=False))

s3 = LaunchScanner()
s3._logstream = FakeFeed([{"mint": "M1", "ts": 1.0},
                          {"mint": "M2", "ts": 2.0},
                          {"mint": "M3", "ts": 3.0}])
calls = []
s3._consider = _counting_consider(calls)
s3._stop = True
s3._drain_logstream(cfg, None, {})
check("drain logstream: stop set -> 0 considers", calls == [], str(calls))
s3._stop = False
s3._drain_logstream(cfg, None, {})
check("drain logstream: no stop -> all 3 considered",
      calls == ["M1", "M2", "M3"], str(calls))

s4 = LaunchScanner()
s4._pumpportal = FakeFeed([{"mint": "P1", "source": "pumpportal-new"},
                           {"mint": "P2", "source": "pumpportal-new"}])
calls = []
s4._consider = _counting_consider(calls)
s4._stop = True
s4._drain_pumpportal(cfg, None, {})
check("drain pumpportal: stop set -> 0 considers", calls == [], str(calls))
s4._stop = False
s4._drain_pumpportal(cfg, None, {})
check("drain pumpportal: no stop -> all 2 considered",
      calls == ["P1", "P2"], str(calls))


# --- 3. _judge loop breaks on _stop, iterates fully otherwise ---------
def _make_judge_scanner():
    sc = LaunchScanner()
    sc._cycle_deadline = time.time() + 3600.0
    sc._batch_refresh = lambda cands, now: {}
    verdict_calls = []
    sc._verdict = lambda j, cand, now, batch: (
        verdict_calls.append(cand.get("mint")),
        (runner.WAIT, {}))[1]
    sc._fingerprint_track = lambda *a, **k: None
    sc._breakout_track = lambda *a, **k: None
    sc._sentiment_track = lambda *a, **k: None
    sc._narrative_attach = lambda *a, **k: None
    sc._lowcap_track = lambda *a, **k: None
    return sc, verdict_calls


def _judge_state(n=5):
    return {"candidates": {("mint%d" % i): {"mint": "mint%d" % i,
                                            "symbol": "S%d" % i}
                           for i in range(n)}}


jcfg = SimpleNamespace(degen=SimpleNamespace())
s5, vcalls = _make_judge_scanner()
s5._stop = True
s5._judge(jcfg, None, _judge_state())
check("judge: stop set -> 0 verdicts", vcalls == [], str(vcalls))
s6, vcalls = _make_judge_scanner()
s6._stop = False
st = _judge_state()
s6._judge(jcfg, None, st)
check("judge: no stop -> all 5 verdicts", len(vcalls) == 5, str(vcalls))
check("judge: no stop -> candidates still tracked (WAIT keeps them)",
      len(st["candidates"]) == 5, str(len(st["candidates"])))


print()
if FAILURES:
    print("FAILED: %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("ALL SCANNER SIGTERM TESTS PASSED")
