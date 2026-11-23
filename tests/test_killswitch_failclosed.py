#!/usr/bin/env python3
"""Kill-switch fail-closed tests (HIGH-2 + LOW-5, 2026-11-21).

HIGH-2: is_halted() used to convert only _StateError (corrupt /
unreadable JSON) to "halted". A malformed-but-parseable state file
(valid JSON, wrong shape — missing "lanes" key, non-dict state)
escaped as a KeyError/TypeError that the entry gates'
except: pass swallowed, so entry proceeded with no halt
enforcement. Now is_halted() catches ALL exceptions -> True
(never raises), and the entry/scan/buyback gates call gate()
instead of is_halted() inside try/except pass.

LOW-5: the status helpers (status, recent_events, halt_lane,
reenable, check_lane, record_trade) used to crash on corrupt or
malformed state instead of reporting it. Now each reports a
state_error result (or [] / no-op) and never raises.

Temp state dir only; the live killswitch state is untouched.
The module reads FOREX_AGENT_HOME at import, so the env is set
and the module reloaded before anything touches state (same
pattern as tests/test_killswitch.py) — that re-points STATE_FILE,
SHADOW_FILE and LOCK_FILE at the temp dir together.
Plain script — run directly with the venv python, not pytest.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import importlib
import json
import os
import sys
import tempfile

REPO = _REPO_ROOT
TMPD = tempfile.mkdtemp(prefix="ks-failclosed-")
os.environ["FOREX_AGENT_HOME"] = TMPD
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

import killswitch
importlib.reload(killswitch)
KS = killswitch
KS._ensure_state_dir()  # dir creation is lazy (first use), not import

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def _write_raw(text):
    with open(KS.STATE_FILE, "w") as f:
        f.write(text)


def _write_json(obj):
    _write_raw(json.dumps(obj))


def _healthy():
    # A healthy state is what _load returns for a missing file.
    _write_json({"lanes": {}, "events": []})


# ---------------- healthy state: no false positive ----------------
_healthy()
check("healthy: unhalted lane is not halted",
      not KS.is_halted("grad_select") and KS.gate("grad_select"))
check("healthy: status() reports lanes, no state_error",
      "state_error" not in KS.status()
      and "grad_select" in KS.status())
check("healthy: recent_events() returns a list",
      isinstance(KS.recent_events(), list))

# ---------------- HIGH-2: malformed-but-parseable state -----------
# Valid JSON, missing the "lanes" key: _lane_state raises KeyError.
_write_json({"events": [], "version": 1})
check("HIGH-2: missing 'lanes' key -> is_halted True (fail-closed)",
      KS.is_halted("grad_select") is True)
check("HIGH-2: missing 'lanes' key -> gate False (do not trade)",
      KS.gate("grad_select") is False)
check("HIGH-2: missing 'lanes' key -> check_lane state_error, no raise",
      KS.check_lane("grad_select").get("action") == "state_error")
check("HIGH-2: missing 'lanes' key -> status reports state_error",
      KS.status().get("state_error") is True)
check("HIGH-2: missing 'lanes' key -> recent_events [] not raise",
      KS.recent_events() == [])
check("HIGH-2: missing 'lanes' key -> halt_lane state_error, no raise",
      KS.halt_lane("grad_select", "test").get("action") == "state_error")
check("HIGH-2: missing 'lanes' key -> reenable state_error, no raise",
      KS.reenable("grad_select", "test").get("action") == "state_error")
try:
    KS.record_trade("grad_select", -5.0)
    ok_rt = True
except Exception:
    ok_rt = False
check("HIGH-2: missing 'lanes' key -> record_trade never raises",
      ok_rt)

# Non-dict state (a JSON list): _lane_state raises TypeError.
_write_json([{"lane": "grad_select"}])
check("HIGH-2: non-dict state -> is_halted True",
      KS.is_halted("grad_select") is True)
check("HIGH-2: non-dict state -> gate False",
      KS.gate("grad_select") is False)
check("HIGH-2: non-dict state -> status reports state_error",
      KS.status().get("state_error") is True)
check("HIGH-2: non-dict state -> check_lane state_error",
      KS.check_lane("grad_select").get("action") == "state_error")

# Lanes present but a lane entry is not a dict.
_write_json({"lanes": {"grad_select": "halted"}, "events": []})
check("HIGH-2: non-dict lane entry -> is_halted True",
      KS.is_halted("grad_select") is True)
check("HIGH-2: non-dict lane entry -> gate False",
      KS.gate("grad_select") is False)

# ---------------- LOW-5: corrupt JSON ------------------------------
_write_raw("{not json")
check("LOW-5: corrupt JSON -> is_halted True",
      KS.is_halted("grad_select") is True)
check("LOW-5: corrupt JSON -> gate False",
      KS.gate("grad_select") is False)
check("LOW-5: corrupt JSON -> check_lane state_error",
      KS.check_lane("grad_select").get("action") == "state_error")
check("LOW-5: corrupt JSON -> status reports state_error",
      KS.status().get("state_error") is True)
check("LOW-5: corrupt JSON -> recent_events []",
      KS.recent_events() == [])
check("LOW-5: corrupt JSON -> halt_lane state_error",
      KS.halt_lane("grad_select", "test").get("action") == "state_error")
check("LOW-5: corrupt JSON -> reenable state_error",
      KS.reenable("grad_select", "test").get("action") == "state_error")

# ---------------- missing file: first boot, by design -------------
# A MISSING file is a genuine first boot (no halt history to
# lose) — _load returns a blank state, so the lane is NOT halted.
# That is deliberately different from a CORRUPT file, which
# refuses every load so a torn write can never silently become
# a blank state. Do not "fix" this into fail-closed: the
# fail-closed rule lives on the corrupt path above.
os.remove(KS.STATE_FILE)
check("missing file -> first boot, not halted (by design)",
      KS.is_halted("grad_select") is False)
check("missing file -> gate True (by design)",
      KS.gate("grad_select") is True)

# ---------------- recovery: a healthy state works again ------------
_healthy()
check("recovery: healthy state unhalts after repair",
      not KS.is_halted("grad_select") and KS.gate("grad_select"))
check("recovery: risk_report still works on healthy state",
      KS.risk_report("grad_select").get("risk") == "low")

print()
if fails:
    print("FAILURES: %d" % len(fails))
    for f in fails:
        print(" - " + f)
    sys.exit(1)
print("ALL KILLSWITCH-FAILCLOSED TESTS PASSED")
