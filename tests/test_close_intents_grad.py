#!/usr/bin/env python3
"""End-to-end close-intent lifecycle through the real grad engine
(2026-11-22, Stage C).

The crash suite (test_close_intents_crash.py) proves the journal's
recovery semantics against scripted kills; this suite proves the
WIRING: a close driven through grad's real entry/exit path opens
an intent, settles the account, and reaches terminal `applied`
when the lane ledger persists - with no intent left pending.

Also covered:
  - recovery after a converged close is a no-op (no double-settle)
  - an orphan close (position predating the flag) resolves as
    converged, never frozen
  - a write-down close (unquotable) completes the intent too

Run: python3 tests/test_close_intents_grad.py
"""
import importlib
import json
import os
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TMPD = tempfile.mkdtemp(prefix="ci-grad-test-")
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(
    prefix="ci-grad-home-")
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"

import daemon.launch_scanner.grad as G
import daemon.launch_scanner.paper_account as PA
import daemon.launch_scanner.close_intents as CI

G.LEDGER_PATH = os.path.join(TMPD, "grad_ledger.json")

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


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
    return (p1, p2, p3, p4, p5, p6), m_veto, m_safety


def _unpatch(ps):
    for p in ps:
        p.stop()


def _fresh(price=0.00007):
    if os.path.exists(G.LEDGER_PATH):
        os.remove(G.LEDGER_PATH)
    FAKE_KS.halted = set()
    FAKE_KS.recorded = []
    ps, m_veto, m_safety = _patch_all(price=price)
    G.ensure_ledger()
    return ps, m_veto, m_safety


def _cand(mint="AAA111pump", age_s=60.0, dex="pumpswap", **kw):
    c = {"mint": mint, "symbol": "TKN", "name": "TKN / SOL",
         "chain": "solana", "dex": dex,
         "created_ts": time.time() - age_s,
         "pool_address": "POOL1", "reserve_usd": 6_000.0,
         "fdv_usd": 69_000.0, "price_usd": 0.00007,
         "source": "gecko-new-pool"}
    c.update(kw)
    return c


def _close_open(mint, mult=2.0):
    with G._ledger_lock():
        d = G._read()
        pos = d["positions"][mint]
        exit_price = pos["entry_price"] * mult
        G._close_leg(pos, "a", exit_price, "test")
        G._close_leg(pos, "b", exit_price, "test")
        G._close_leg(pos, "c", exit_price, "test")
        G._finalize(d, mint, pos, "test")
        G._write(d)


def _events():
    path = CI.journal_path("grad")
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path) if l.strip()]


importlib.reload(PA)

# ---- full lifecycle: intent reaches terminal applied --------------
ps, _mv, _ms = _fresh()
opened, why, _f = G.consider_candidate(_cand(mint="LIFE111pump"))
check("fixture: entry opened", opened)
_close_open("LIFE111pump", mult=2.0)
evs = _events()
kinds = [e["ev"] for e in evs]
check("lifecycle: intent/account_applied/lane_applied/applied "
      "in order",
      kinds == ["intent", "account_applied", "lane_applied",
                "applied"])
check("lifecycle: nothing pending", CI.pending("grad") == [])
iid = evs[0]["intent_id"]
check("lifecycle: intent payload carries the settle op",
      evs[0]["settle_ops"][0]["kind"] == "credit"
      and evs[0]["settle_ops"][0]["amount"] > 0
      and evs[0]["close_info"]["realized_usd"] > 0)
_unpatch(ps)

# ---- recovery after convergence is a no-op -------------------------
cash_before = PA.snapshot()["cash"]
G._RECOVERY_DONE = False
G._recover_close_intents()
check("recovery: no new events after convergence",
      [e["ev"] for e in _events()] == kinds)
check("recovery: cash unchanged", PA.snapshot()["cash"]
      == cash_before)

# ---- orphan close: converged, never frozen -------------------------
# A TRUE pre-account-epoch position: entered with the flag OFF (never
# debited), closed with the flag ON. The account's journal holds no
# debit for it, so the orphan outcome is provably converged.
ps, _mv, _ms = _fresh()
os.environ.pop("PAPER_ACCOUNT_ENABLED", None)
opened, _w, _f = G.consider_candidate(_cand(mint="ORPH11pump"))
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
check("fixture: orphan-candidate entry opened (pre-account epoch)",
      opened)
_close_open("ORPH11pump", mult=1.5)
evs2 = [e for e in _events() if e.get("mint") == "ORPH11pump"
        or e.get("intent_id", "").startswith("grad:ORPH11pump")]
kinds2 = [e["ev"] for e in evs2]
check("orphan: intent reaches applied via the orphan path",
      kinds2 == ["intent", "account_applied", "lane_applied",
                 "applied"])
check("orphan: account journaled the orphan",
      PA.op_outcome(evs2[0]["settle_ops"][0]["op_id"]) == "orphan")
check("orphan: nothing pending, nothing frozen",
      CI.pending("grad") == []
      and not any(e["ev"] == "frozen" for e in _events()))
_unpatch(ps)

# ---- write-down close completes the intent -------------------------
ps, _mv, _ms = _fresh()
opened, _w, _f = G.consider_candidate(_cand(mint="WD1111pump"))
check("fixture: write-down entry opened", opened)
with G._ledger_lock():
    d = G._read()
    pos = d["positions"]["WD1111pump"]
    for leg in ("a", "b", "c"):
        G._close_leg(pos, leg, 0.0, "unquotable",
                     pkind="write_down_zero")
    G._finalize(d, "WD1111pump", pos, "time_stop_24h_unquotable")
    G._write(d)
evs3 = [e for e in _events() if e.get("mint") == "WD1111pump"
        or e.get("intent_id", "").startswith("grad:WD1111pump")]
kinds3 = [e["ev"] for e in evs3]
check("write-down: intent reaches applied",
      kinds3 == ["intent", "account_applied", "lane_applied",
                 "applied"])
check("write-down: settle op is a write_down",
      evs3[0]["settle_ops"][0]["kind"] == "write_down")
check("write-down: unresolved register holds the units",
      PA.snapshot()["unresolved_cost"] > 0)
_unpatch(ps)

# ---- orphan tolerance requires epoch proof ------------------------
# A DEBITED position that vanishes from the account without a settle
# (stale-restore shape) must NOT converge: the journal holds its
# debit, so the close settles nowhere and recovery freezes loud.
ps, _mv, _ms = _fresh()
opened, _w, _f = G.consider_candidate(_cand(mint="GHOST1pump"))
check("fixture: ghost entry opened and debited", opened)
with PA._lock():
    led, healthy, _deg = PA._load_locked()
    ghost_pid = led["lanes"]["grad"]["open"]["GHOST1pump"]["pos_id"]
    led["lanes"]["grad"]["open"].pop("GHOST1pump", None)
    PA._save_locked(led)
cash_before_ghost = PA.snapshot()["cash"]
_close_open("GHOST1pump", mult=1.5)
evs4 = [e for e in _events()
        if e.get("intent_id", "").startswith("grad:GHOST1pump")]
check("ghost: intent opened but account side NEVER applied",
      [e["ev"] for e in evs4] == ["intent"])
check("ghost: no cash invented by the refused settle",
      PA.snapshot()["cash"] == cash_before_ghost)
G._RECOVERY_DONE = False
G._recover_close_intents()
evs4b = [e for e in _events()
         if e.get("intent_id", "").startswith("grad:GHOST1pump")]
check("ghost: recovery freezes the divergence (debit in history)",
      evs4b[-1]["ev"] == "frozen")
check("ghost: still no double settlement",
      PA.snapshot()["cash"] == cash_before_ghost)
_unpatch(ps)

# ---- recovery runs BEFORE a post-restart entry ---------------------
# Simulate a kill after account_applied but before the lane write:
# _finalize with a FAILING _write (the kill lands at the persist).
# The next process's FIRST ENTRY must reconcile the pending intent
# before debiting.
ps, _mv, _ms = _fresh()
opened, _w, _f = G.consider_candidate(_cand(mint="PEND11pump"))
check("fixture: pending-close entry opened", opened)
_orig_write = G._write
G._write = lambda d: False   # the persist never lands
with G._ledger_lock():
    d = G._read()
    pos = d["positions"]["PEND11pump"]
    px = pos["entry_price"] * 2.0
    for leg in ("a", "b", "c"):
        G._close_leg(pos, leg, px, "test")
    G._finalize(d, "PEND11pump", pos, "test")
G._write = _orig_write
# On disk the position is still open and no close is recorded.
disk = G._load()
check("fixture: kill left the position open on disk",
      "PEND11pump" in disk["positions"]
      and not disk["closes"])
pend = CI.pending("grad")
check("fixture: intent pending with account side applied",
      len(pend) == 1 and pend[0]["account_applied"]
      and not pend[0]["lane_applied"])
cash_after_close = PA.snapshot()["cash"]
G._RECOVERY_DONE = False   # the restart
opened_b, why_b, _f = G.consider_candidate(_cand(mint="NEXT11pump"))
check("entry: new entry permitted after reconciliation", opened_b)
check("entry: recovery left the open position to its natural "
      "re-close (still pending, both ledgers consistent)",
      len(CI.pending("grad")) == 1)
check("entry: account not re-credited during reconciliation",
      abs(PA.snapshot()["cash"] - (cash_after_close - 1.0)) < 1e-9)
# The natural re-close settles the intent with the SAME op_id: no
# double settlement across the restart.
with G._ledger_lock():
    d = G._read()
    pos = d["positions"]["PEND11pump"]
    px = pos["entry_price"] * 2.0
    for leg in ("a", "b", "c"):
        G._close_leg(pos, leg, px, "test")
    G._finalize(d, "PEND11pump", pos, "test")
check("re-close: intent converged, no pending", [p for p in
      CI.pending("grad")
      if p["intent_id"].startswith("grad:PEND11pump")] == [])
check("re-close: stable op_id deduped (no double credit)",
      abs(PA.snapshot()["cash"] - (cash_after_close - 1.0)) < 1e-9)
_unpatch(ps)

# ---- incomplete split + write-down leg: conservation ---------------
# Legacy position WITHOUT per-leg fracs, one priced leg and one
# write-down leg: units cannot be attributed, so the WHOLE position
# must go to the unresolved register - no sale invented for the
# unquotable units, no cash claimed while the split is unknown.
ps, _mv, _ms = _fresh()
opened, _w, _f = G.consider_candidate(_cand(mint="LEGWD1pump"))
check("fixture: legacy-mixed entry opened", opened)
with PA._lock():
    _led, _h, _dg = PA._load_locked()
    acct_qty = _led["lanes"]["grad"]["open"]["LEGWD1pump"]["qty"]
with G._ledger_lock():
    d = G._read()
    pos = d["positions"]["LEGWD1pump"]
    for k, v in pos["legs"].items():
        v.pop("frac", None)   # pre-frac legacy shape
    pos["legs"]["a"].update({"done": True, "exit_price": 0.00014,
                            "realized_usd": 1.0,
                            "exit_reason": "t",
                            "exit_price_kind":
                            "current_price_simulated_fill"})
    pos["legs"]["b"].update({"done": True, "exit_price": 0.0,
                            "realized_usd": 0.0,
                            "exit_reason": "unquotable",
                            "exit_price_kind": "write_down_zero"})
    pos["legs"]["c"].update({"done": True, "exit_price": 0.0,
                            "realized_usd": 0.0,
                            "exit_reason": "unquotable",
                            "exit_price_kind": "write_down_zero"})
    pos["realized_usd"] = 1.0
    cash_pre = PA.snapshot()["cash"]
    G._finalize(d, "LEGWD1pump", pos, "legacy_mixed")
    G._write(d)
snap = PA.snapshot()
check("legacy-mixed: whole position to the unresolved register",
      snap["unresolved_cost"] > 0 and snap["unresolved"])
reg_entry = [v for v in snap["unresolved"]
             if v.get("mint") == "LEGWD1pump"]
check("legacy-mixed: register conserves ALL account units "
      "(none sold)",
      reg_entry and abs(float(reg_entry[0].get("entry_qty") or 0)
                        - acct_qty) < 1e-9)
check("legacy-mixed: register conserves the FULL original cost",
      reg_entry and abs(float(reg_entry[0].get("basis") or 0)
                        - 1.0) < 1e-9
      and abs(float(reg_entry[0].get("entry_cost") or 0)
              - 1.0) < 1e-9)
check("legacy-mixed: no sale cash claimed while split unknown",
      PA.snapshot()["cash"] == cash_pre)
_unpatch(ps)

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL CLOSE-INTENT GRAD WIRING TESTS PASSED")
