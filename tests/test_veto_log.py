#!/usr/bin/env python3
"""Veto-log tests (2026-10-20): veto_check must append a JSONL record
to $FOREX_AGENT_HOME/veto_log.jsonl on every veto, attributed by lane.

Background: veto.py documented this log since 2026-10-16 but the file
never existed in production — the write was only reachable on a real
veto, and every live caller was halted, killed, or calling in a
degraded mode; the bare `except: pass` also hid any write failure.
These tests pin the fixed behavior: a synthetic veto lands a line at
the documented path, passing checks write nothing, lane attribution
is recorded, and a failing log write never breaks the veto itself.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import json
import os
import sys
import tempfile

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

TMPD = tempfile.mkdtemp(prefix="veto-log-test-")
os.environ["FOREX_AGENT_HOME"] = TMPD

from daemon.launch_scanner import veto as V  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


LOG = os.path.join(TMPD, "veto_log.jsonl")
MINT = "VetoLogProbeMint111111111111111111111111111"
# top-10 = 10 x 9% = 90% > TOP10_MAX_PCT (40%) -> concentration veto
RUG_REPORT = {"topHolders": [{"pct": 9.0}] * 10}


def _lines():
    if not os.path.exists(LOG):
        return []
    with open(LOG) as f:
        return [ln for ln in f.read().splitlines() if ln.strip()]


check("vetolog: path resolves to $FOREX_AGENT_HOME/veto_log.jsonl",
      str(V._veto_log_path()) == LOG)
check("vetolog: no file before any veto", not os.path.exists(LOG))

# 1. A synthetic veto lands exactly one line with all required fields.
res = V.veto_check(MINT, "solana", "PROBE", safety_report=RUG_REPORT,
                   run_bundle=False, lane="main")
check("vetolog: synthetic veto fails the check", res["passed"] is False)
lines = _lines()
check("vetolog: one line landed at the documented path", len(lines) == 1)
rec = json.loads(lines[0]) if lines else {}
check("vetolog: record has ts/mint/chain", 
      isinstance(rec.get("ts"), float) and rec.get("mint") == MINT
      and rec.get("chain") == "solana")
check("vetolog: record carries lane attribution",
      rec.get("lane") == "main")
check("vetolog: record carries veto kind + details",
      any("concentration:top10" in v for v in rec.get("vetoes", []))
      and "concentration" in rec.get("details", {}))

# 2. Lane attribution follows the caller; appends, never overwrites.
res = V.veto_check(MINT, "solana", "PROBE", safety_report=RUG_REPORT,
                   run_bundle=False, lane="steady")
lines = _lines()
check("vetolog: second veto appends (2 lines)", len(lines) == 2)
check("vetolog: second line lane = steady",
      len(lines) == 2 and json.loads(lines[1]).get("lane") == "steady")

# 3. A passing check writes nothing.
res = V.veto_check(MINT, "solana", "PROBE", safety_report=None,
                   run_bundle=False, lane="main")
check("vetolog: clean check passes", res["passed"] is True)
check("vetolog: pass writes no line", len(_lines()) == 2)

# 4. Fail-soft: an unwritable log path must not break the veto, and
#    the failure surfaces via the module logger (not silent pass).
import logging

records = []


class _Grab(logging.Handler):
    def emit(self, r):
        records.append(r.getMessage())


V.logger.addHandler(_Grab())
V.logger.setLevel(logging.WARNING)
_orig = V._veto_log_path
V._veto_log_path = lambda: os.path.join(TMPD, "no-such-dir", "x.jsonl")
try:
    res = V.veto_check(MINT, "solana", "PROBE",
                       safety_report=RUG_REPORT, run_bundle=False,
                       lane="mirror")
    check("vetolog: write failure does not break the veto",
          res["passed"] is False)
finally:
    V._veto_log_path = _orig
check("vetolog: write failure reported to daemon log",
      any("veto log write failed" in m for m in records))
check("vetolog: failed write added no line", len(_lines()) == 2)

# 5. Non-JSON-native detail values must not lose the record.
V._log_veto(MINT, "PROBE", "solana", ["test:synthetic"],
            {"weird": {"a", "set"}}, lane="ceo")
lines = _lines()
check("vetolog: set-valued details still serialize (default=str)",
      len(lines) == 3
      and json.loads(lines[2]).get("lane") == "ceo")

print()
if fails:
    print("FAILURES: %d" % len(fails))
    raise SystemExit(1)
print("ALL VETO LOG TESTS PASSED")
