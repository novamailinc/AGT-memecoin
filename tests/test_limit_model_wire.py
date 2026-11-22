#!/usr/bin/env python3
"""Limit-order exit model + cover journal wiring tests.

Covers the grad.py wiring of pieces orig-3 + new-3 (advanced build,
2026-11-21): the journal-only LimitModel notes every exit-eval quote
on the live quote path, the cover layer journals TAKE_PROFIT /
CUT_COVER decisions, and both ride into the close record via
_finalize. Neither gates an exit: the live ladder is untouched —
a 2.05x quote still banks leg A live, and a 0.49x quote still
hard-stops. No network: every feed is stubbed. Temp ledger only.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os
import sys
import tempfile
import time
from unittest.mock import patch

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import grad as G

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


TMPD = tempfile.mkdtemp(prefix="limit-wire-")
G.LEDGER_PATH = os.path.join(TMPD, "grad_ledger.json")


class FakeKS:
    def __init__(self):
        self.halted = set()
        self.recorded = []

    def is_halted(self, lane):
        return lane in self.halted

    def gate(self, lane):
        return not self.is_halted(lane)

    def record_trade(self, lane, pnl, ts=None):
        self.recorded.append(pnl)

    def check_lane(self, lane):
        return {"lane": lane}

    def halt_lane(self, lane, reason=""):
        self.halted.add(lane)
        return {"lane": lane, "action": "HALTED"}


FAKE_KS = FakeKS()
SAFETY_OK = {"passed": True, "unknown": False, "score": 7,
             "flags": [], "fails": [], "source": "rugcheck"}
TAPE_OK = {"usd_liquidity": 8_000.0, "usd_market_cap": 69_000.0,
           "chg_m5": 3.0, "price_usd": 0.00007,
           "tape_source": "dexscreener"}


def _patch_all(price=0.00007):
    p1 = patch.object(G, "_veto")
    p2 = patch.object(G, "_safety")
    p3 = patch.object(G, "_ks", FAKE_KS)
    p4 = patch.object(G, "quote_price", return_value=price)
    p5 = patch.object(G, "_fresh_tape", return_value=dict(TAPE_OK))
    p6 = patch.object(G, "QUOTE_RETRY_GAP_S", 0.0)
    m_veto, m_safety = p1.start(), p2.start()
    for p in (p3, p4, p5, p6):
        p.start()
    m_veto.veto_check.return_value = {"passed": True, "vetoes": []}
    m_safety.check_token.return_value = dict(SAFETY_OK)
    return (p1, p2, p3, p4, p5, p6)


def _unpatch(ps):
    for p in ps:
        p.stop()


def _fresh(price=0.00007):
    if os.path.exists(G.LEDGER_PATH):
        os.remove(G.LEDGER_PATH)
    FAKE_KS.halted = set()
    FAKE_KS.recorded = []
    ps = _patch_all(price=price)
    G.ensure_ledger()
    return ps


def _cand(mint="AAA111pump", age_s=60.0, dex="pumpswap", **kw):
    c = {"mint": mint, "symbol": "TKN", "name": "TKN / SOL",
         "chain": "solana", "dex": dex,
         "created_ts": time.time() - age_s,
         "pool_address": "POOL1", "reserve_usd": 6_000.0,
         "fdv_usd": 69_000.0, "price_usd": 0.00007,
         "source": "gecko-new-pool"}
    c.update(kw)
    return c


# ---------------- modules import fail-soft ----------------
check("wire: limit_exits module imported", G._limit_exits is not None)
check("wire: cover module imported", G._cover is not None)

# ---------------- model notes quotes, ladder untouched ----------------
ps = _fresh()
opened, why, final = G.consider_candidate(_cand(mint="LMW111pump"))
check("wire: entry opens", opened)
entry = G._load()["positions"]["LMW111pump"]["entry_price"]
# 2.05x: live leg A fires AND the limit model banks leg A at the rung
closes = G.poll_exits(lambda c, m, s: entry * 2.05)
d = G._load()
pos = d["positions"].get("LMW111pump") or {}
check("wire: live leg A still fires at 2.05x",
      closes == [] and pos.get("legs", {}).get("a", {}).get("done"))
lm = pos.get("limit_model") or {}
fills = lm.get("fills") or []
check("wire: limit model banked leg A at exactly 2.0x",
      len(fills) == 1 and fills[0].get("leg") == "a"
      and abs(fills[0].get("fill_mult", 0) - 2.0) < 1e-6)
check("wire: cover_last journaled TAKE_PROFIT leg_a_rung",
      (pos.get("cover_last") or {}).get("action") == "TAKE_PROFIT"
      and (pos.get("cover_last") or {}).get("reason") == "leg_a_rung")
check("wire: cover_journal has the leg_a_rung decision",
      len(pos.get("cover_journal") or []) >= 1
      and pos["cover_journal"][-1].get("reason") == "leg_a_rung")
# 3.05x: live leg B fires, model banks B too
G.poll_exits(lambda c, m, s: entry * 3.05)
pos = G._load()["positions"].get("LMW111pump") or {}
check("wire: live leg B still fires at 3.05x",
      pos.get("legs", {}).get("b", {}).get("done"))
check("wire: limit model banked leg B at exactly 3.0x",
      len((pos.get("limit_model") or {}).get("fills") or []) == 2)
# trail completes the ladder live; close carries model + cover
closes = G.poll_exits(lambda c, m, s: entry * 3.05 * 0.65)
d = G._load()
rec = (d["closes"] or [{}])[-1]
check("wire: live ladder still completes at the trail",
      len(closes) == 1 and rec.get("closed_reason") == "ladder_complete")
check("wire: close carries limit_model with 3 fills",
      len((rec.get("limit_model") or {}).get("fills") or []) == 3)
check("wire: close carries cover_journal + cover_last",
      len(rec.get("cover_journal") or []) >= 1
      and (rec.get("cover_last") or {}).get("action") in
      ("TAKE_PROFIT", "CUT_COVER", "HOLD"))
_unpatch(ps)

# ---------------- hard stop: model fills remainder at 0.475x ----------------
ps = _fresh()
G.consider_candidate(_cand(mint="LMH111pump"))
entry = G._load()["positions"]["LMH111pump"]["entry_price"]
closes = G.poll_exits(lambda c, m, s: entry * 0.49)
rec = (G._load()["closes"] or [{}])[-1]
lm = rec.get("limit_model") or {}
fills = lm.get("fills") or []
check("wire: hard stop still closes live at -50%",
      len(closes) == 1 and rec.get("closed_reason") == "hard_stop_-50pct")
check("wire: model filled all 3 legs at 0.475x on the stop",
      len(fills) == 3
      and all(abs(f.get("fill_mult", 0) - 0.475) < 1e-6 for f in fills))
from daemon.launch_scanner import limit_exits as _LE
_restored = _LE.LimitModel.from_dict(lm)
check("wire: restored model realized_mult is 0.475 on a full stop",
      abs((_restored.realized_mult() or 0) - 0.475) < 1e-6)
_unpatch(ps)

# ---------------- fast loser: cover CUT_COVER journaled, position lives ----------------
ps = _fresh()
G.consider_candidate(_cand(mint="LMC111pump"))
entry = G._load()["positions"]["LMC111pump"]["entry_price"]
# 0.60x within 15 min of entry: fast loser, nothing banked
closes = G.poll_exits(lambda c, m, s: entry * 0.60)
pos = G._load()["positions"].get("LMC111pump") or {}
check("wire: cover CUT_COVER journaled on a fast loser, live untouched",
      closes == []
      and (pos.get("cover_last") or {}).get("action") == "CUT_COVER"
      and (pos.get("cover_last") or {}).get("reason") == "fast_loser"
      and "LMC111pump" in G._load()["positions"])
_unpatch(ps)

print()
if fails:
    print("FAILURES: %d" % len(fails))
    for f in fails:
        print(" - " + f)
    sys.exit(1)
print("ALL LIMIT-MODEL-WIRE TESTS PASSED")
