#!/usr/bin/env python3
"""Close-denomination tests (2026-10-27).

REGRESSION: closes were denominated at the lane's CURRENT notional,
not at the stake the position actually opened with. When grad_raw /
grad_select switched stakes from $1 to $25 on 2026-10-24, every
pre-switch $1 entry that closed after the switch booked a phantom
loss of up to $24 (recorded pnl = realized - 25 instead of
realized - 1). The fix: _finalize denominates every close at the
position's stored entry notional (pos["notional_usd"]), falling back
to the current notional only for legacy positions that predate the
field.

Proves, on the shared grad.py engine:
  (a) a $1 entry closed after a switch to $25 records $1 pnl/mult;
  (a2) the sharpest case: a $1 entry rugged to $0 books -$1, not -$25;
  (b) a $25 entry records $25-denominated pnl/mult;
  (c) a mixed book reconciles: sum of close pnls == bankroll
      movement measured from pre-entry cash;
  (d) a position without notional_usd falls back to current notional;
  (e) the close record carries entry_notional_usd for future audits.

Plain script — run directly, not via pytest. No network, no real
ledgers: LEDGER_PATH points at a temp dir, _ks and _record_event
are stubbed.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os
import sys
import tempfile
import time

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "grad_close_denom_engine",
    os.path.join(REPO, "daemon", "launch_scanner", "grad.py"))
G = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(G)

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


TMPD = tempfile.mkdtemp(prefix="grad-close-denom-")
G.LEDGER_PATH = os.path.join(TMPD, "grad_ledger.json")


class FakeKS:
    def __init__(self):
        self.recorded = []

    def record_trade(self, lane, pnl, ts=None):
        self.recorded.append(pnl)

    def check_lane(self, lane):
        return {"lane": lane}

    def halt_lane(self, lane, reason=""):
        return {"lane": lane, "action": "HALTED"}


G._ks = FakeKS()
G._record_event = lambda kind, payload: None


def _pos(mint, entry_notional, realized, entry_ts=None):
    return {
        "mint": mint, "symbol": "TST", "chain": "solana",
        "source": "graduation", "lane": G.LANE,
        "entry_ts": entry_ts or time.time(),
        "entry_price": 0.001, "notional_usd": entry_notional,
        "peak_price": 0.001, "peak_ts": time.time(),
        "tokens": 1000.0,
        "legs": {
            "a": {"done": True, "exit_price": 0.001,
                  "realized_usd": realized, "exit_reason": "test"},
            "b": {"done": True, "exit_price": 0.001,
                  "realized_usd": 0.0, "exit_reason": "test"},
            "c": {"done": True, "exit_price": 0.001,
                  "realized_usd": 0.0, "exit_reason": "test"},
        },
        "realized_usd": realized, "status": "open",
        "entry_snapshot": {}, "entry_features": {},
    }


def _ledger(bankroll):
    return {"bankroll": bankroll, "positions": {}, "closes": [],
            "state": "active", "verdict": None,
            "watchlist": {}, "entries_day": {},
            "dead_mints": {}}


def _close(d, mint, pos, reason="test_close"):
    with G._ledger_lock():
        d["positions"][mint] = pos
        G._finalize(d, mint, pos, reason)
    return d["closes"][-1]


# ---- the lane has switched to $25 AFTER $1 entries opened ----
G.NOTIONAL_USD = 25.00

# (a) $1 entry, half value left, closed after the switch
d = _ledger(1000.00)
rec = _close(d, "AAA1pump", _pos("AAA1pump", 1.00, 0.50))
check("(a) $1 entry @ $25 lane: pnl is -$0.50, not -$24.50",
      rec["pnl_usd"] == -0.50)
check("(a) $1 entry @ $25 lane: mult is 0.50, not 0.02",
      rec["realized_mult"] == 0.50)
check("(a) close record carries entry_notional_usd=1.0",
      rec.get("entry_notional_usd") == 1.00)
check("(a) killswitch fed the corrected pnl",
      G._ks.recorded[-1] == -0.50)

# (a2) $1 entry rugged to zero after the switch — the 25x distortion
d = _ledger(1000.00)
rec = _close(d, "AAA2pump", _pos("AAA2pump", 1.00, 0.00))
check("(a2) $1 rug @ $25 lane: pnl is -$1.00, not -$25.00",
      rec["pnl_usd"] == -1.00)
check("(a2) $1 rug @ $25 lane: mult is 0.0",
      rec["realized_mult"] == 0.0)

# (b) $25 entry on the $25 lane is unchanged by the fix
d = _ledger(1000.00)
rec = _close(d, "BBB1pump", _pos("BBB1pump", 25.00, 50.00))
check("(b) $25 entry @ $25 lane: pnl is +$25.00",
      rec["pnl_usd"] == 25.00)
check("(b) $25 entry @ $25 lane: mult is 2.00",
      rec["realized_mult"] == 2.00)
check("(b) close record carries entry_notional_usd=25.0",
      rec.get("entry_notional_usd") == 25.00)

# (c) mixed book reconciles: pre-entry cash 1000, entries $1 + $25,
# closes realize $0.40 and $30.00 -> final cash 1004.40, and
# sum(pnls) == final - pre-entry == +$4.40
d = _ledger(1000.00 - 1.00 - 25.00)
_close(d, "CCC1pump", _pos("CCC1pump", 1.00, 0.40))
_close(d, "CCC2pump", _pos("CCC2pump", 25.00, 30.00))
pnls = [c["pnl_usd"] for c in d["closes"]]
check("(c) mixed book: sum(pnls) is +$4.40",
      round(sum(pnls), 4) == 4.40)
check("(c) mixed book: bankroll is $1004.40",
      round(d["bankroll"], 4) == 1004.40)
check("(c) mixed book: sum(pnls) == bankroll - pre-entry cash",
      round(sum(pnls), 4) == round(d["bankroll"] - 1000.00, 4))

# (d) legacy position without notional_usd falls back to current
d = _ledger(1000.00)
legacy = _pos("DDD1pump", 25.00, 12.50)
del legacy["notional_usd"]
rec = _close(d, "DDD1pump", legacy)
check("(d) legacy position w/o notional: falls back to $25 lane",
      rec["pnl_usd"] == -12.50 and rec["realized_mult"] == 0.50)

# (e) a $1 winner after the switch keeps its true multiple
d = _ledger(1000.00)
rec = _close(d, "EEE1pump", _pos("EEE1pump", 1.00, 3.00))
check("(e) $1 entry 3x @ $25 lane: pnl +$2.00, mult 3.00",
      rec["pnl_usd"] == 2.00 and rec["realized_mult"] == 3.00)

print()
if fails:
    print("FAILURES: %d (%s)" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("ALL CLOSE-DENOMINATION TESTS PASSED")
