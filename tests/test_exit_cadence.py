#!/usr/bin/env python3
"""Exit-cadence tests (2026-11-20, faster-exits item 1b).

Covers daemon/launch_scanner/exit_cadence.py: the fast/idle
decision (pure, no network), the quote-volume bound, and the
ExitPoller thread (calls, no overlap, survives poll errors,
stops cleanly). Plus per-lane wiring proof: each of the five live
trader scripts starts an ExitPoller, and each live lane's engine
poll_exits is the module the fast poller actually calls.

No network, no live ledger, no Helius. Run directly:
  python3 tests/test_exit_cadence.py
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os
import sys
import tempfile
import threading
import time

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import exit_cadence as EC

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


NOW = 1795000000.0


def _pos(age_s, entry_ts=None):
    return {"entry_ts": NOW - age_s if entry_ts is None else entry_ts}


# ---------------- constants: the task's numbers ----------------
check("cadence: fast interval is ~10s", EC.FAST_INTERVAL_S == 10.0)
check("cadence: fast window is the first 15 min",
      EC.FAST_WINDOW_S == 900.0)
check("cadence: idle re-check is 30s", EC.IDLE_CHECK_S == 30.0)

# ---------------- pure age decision ----------------
check("age: newborn is fast", EC.is_fast_age(0))
check("age: 899s is fast", EC.is_fast_age(899))
check("age: 900s is back to normal", not EC.is_fast_age(900))
check("age: old is normal", not EC.is_fast_age(3600))
check("age: future-dated (clock skew) counts as fast, never slow",
      EC.is_fast_age(-5))
check("age: unparseable counts as fast",
      EC.is_fast_age("not-a-number"))

# ---------------- young-position detection ----------------
check("young: empty book is idle",
      not EC.has_young_position({}, NOW))
check("young: unknown book polls fast, never assumes old",
      EC.has_young_position(None, NOW))
check("young: one young position -> fast",
      EC.has_young_position({"A": _pos(60)}, NOW))
check("young: all old -> idle",
      not EC.has_young_position({"A": _pos(3600), "B": _pos(900)}, NOW))
check("young: exactly 900s old is idle",
      not EC.has_young_position({"A": _pos(900)}, NOW))
check("young: missing entry_ts counts as young",
      EC.has_young_position({"A": {}}, NOW))
check("young: future-dated entry counts as young",
      EC.has_young_position({"A": _pos(-30)}, NOW))
check("young: list of positions also works",
      EC.has_young_position([_pos(3600), _pos(30)], NOW))
check("interval: young -> 10s, old -> 30s",
      EC.poll_interval_s({"A": _pos(30)}, NOW) == 10.0
      and EC.poll_interval_s({"A": _pos(3600)}, NOW) == 30.0)

# ---------------- quote-volume bound ----------------
# One fast poll costs at most one quote per open position
# (grad._quote_detail: one DexScreener call per position, no cache).
check("cost: 20 open @10s = 120 quotes/min/lane",
      EC.quote_cost_estimate(20, 10.0) == 120.0)
check("cost: momentum MAX_OPEN 3 @10s = 18/min",
      EC.quote_cost_estimate(3, 10.0) == 18.0)
check("cost: fleet worst case 4x20+3=83 open @10s = 498/min",
      EC.quote_cost_estimate(83, 10.0) == 498.0)
check("cost: same fleet at the measured ~33s effective cadence "
      "would be ~151/min (fast polling adds ~347/min worst case)",
      abs(EC.quote_cost_estimate(83, 33.0) - 83 * 60.0 / 33.0) < 0.01)
check("cost: zero open costs zero",
      EC.quote_cost_estimate(0, 10.0) == 0.0)
check("cost: bad interval costs zero, never raises",
      EC.quote_cost_estimate(20, 0) == 0.0)

# ---------------- ExitPoller thread ----------------
_calls = []


def _young_positions():
    return {"A": {"entry_ts": time.time() - 30}}


def _old_positions():
    return {"A": {"entry_ts": time.time() - 3600}}


poller = EC.ExitPoller(poll_fn=lambda: _calls.append(time.time()),
                       positions_fn=_young_positions,
                       fast_s=0.05, idle_s=0.05)
poller.start()
time.sleep(0.35)
poller.stop(timeout=2.0)
check("thread: polls repeatedly while a position is young",
      len(_calls) >= 3)
check("thread: stopped cleanly",
      not (poller._thread is not None and poller._thread.is_alive()))

_calls2 = []
poller2 = EC.ExitPoller(poll_fn=lambda: _calls2.append(time.time()),
                        positions_fn=_old_positions,
                        fast_s=0.05, idle_s=0.2)
poller2.start()
time.sleep(0.3)
poller2.stop(timeout=2.0)
check("thread: no polls while every position is old (idle)",
      len(_calls2) == 0)

# Errors never kill the thread; polls resume after the failure.
_state = {"fail": True, "calls": 0, "errors": []}


def _flaky_poll():
    if _state["fail"]:
        raise RuntimeError("boom")
    _state["calls"] += 1


poller3 = EC.ExitPoller(poll_fn=_flaky_poll,
                        positions_fn=_young_positions,
                        fast_s=0.05, idle_s=0.05,
                        on_error=lambda e: _state["errors"].append(e))
poller3.start()
time.sleep(0.2)
check("thread: poll errors are counted, thread survives",
      poller3.errors >= 1 and poller3._thread.is_alive())
_state["fail"] = False
time.sleep(0.2)
poller3.stop(timeout=2.0)
check("thread: polls resume after the failure clears",
      _state["calls"] >= 1 and len(_state["errors"]) >= 1)

# A slow poll never overlaps the next one (no pile-up).
_conc = {"cur": 0, "max": 0, "lock": threading.Lock()}


def _slow_poll():
    with _conc["lock"]:
        _conc["cur"] += 1
        _conc["max"] = max(_conc["max"], _conc["cur"])
    time.sleep(0.12)
    with _conc["lock"]:
        _conc["cur"] -= 1


poller4 = EC.ExitPoller(poll_fn=_slow_poll,
                        positions_fn=_young_positions,
                        fast_s=0.05, idle_s=0.05)
poller4.start()
time.sleep(0.4)
poller4.stop(timeout=2.0)
check("thread: slow polls never overlap (max concurrency 1)",
      _conc["max"] == 1)

# ---------------- per-lane wiring: all five traders ----------------
TRADERS = {
    "grad_select": ("scripts/grad-select-trader",
                    "engine.poll_exits(_armed_price_fn)"),
    "grad_raw": ("scripts/grad-raw-trader",
                 "engine.poll_exits(_armed_price_fn)"),
    "tranche": ("scripts/tranche-trader",
                "engine.poll_exits(_armed_price_fn)"),
    "tranche_protect": ("scripts/tranche-protect-trader",
                        "engine.poll_exits(_armed_price_fn)"),
    "momentum": ("scripts/momentum-trader", "_mom.poll_exits()"),
}
for lane, (rel, poll_call) in TRADERS.items():
    src = open(os.path.join(REPO, rel)).read()
    check("wiring %s: trader starts an ExitPoller" % lane,
          "ExitPoller(" in src and "_exit_poller.start()" in src)
    check("wiring %s: poller calls the lane's own poll_exits (%s)"
          % (lane, poll_call), poll_call in src)
    check("wiring %s: main-loop poll kept as backstop" % lane,
          "poll_exits" in src and "time.sleep(POLL_SECS)" in src)

# ---------------- per-lane engines: who serves each lane ----------------
from daemon.launch_scanner import grad_select as SEL
from daemon.launch_scanner import grad_raw as RAW
from daemon.launch_scanner import tranche as TR
from daemon.launch_scanner import tranche_protect as TP
from daemon.launch_scanner import momentum_lane as MOM

for lane, mod in [("grad_select", SEL), ("grad_raw", RAW),
                  ("tranche", TR), ("tranche_protect", TP)]:
    eng = mod.make_engine()
    check("engine %s: exits run in grad.py (shared poller code)"
          % lane,
          eng.poll_exits.__code__.co_filename.endswith(
              "daemon/launch_scanner/grad.py")
          and eng.LANE == lane)
check("engine momentum: exits run in momentum_lane.py",
      MOM.poll_exits.__code__.co_filename.endswith(
          "daemon/launch_scanner/momentum_lane.py")
      and MOM.LANE == "momentum")
check("engine grad: MAX_OPEN 20 bounds the per-lane quote cost",
      SEL.make_engine().MAX_OPEN == 20)
check("engine momentum: MAX_OPEN 3 bounds the per-lane quote cost",
      MOM.MAX_OPEN == 3)

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("all exit-cadence tests passed")
