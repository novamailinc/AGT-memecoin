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
import tests._guard_fixture as _gfb; _gfb.block_network()  # no network in tests (mandatory-guard migration, disclosed)

TMPD = tempfile.mkdtemp(prefix="ci-grad-test-")
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(
    prefix="ci-grad-home-")
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"

import daemon.launch_scanner.grad as G
import daemon.launch_scanner.paper_account as PA
_gfb.set_limits_env(); _gfb.install_price_feed(at_cost=True)  # mandatory-guard migration (disclosed): gates on + TEST-FIXTURE limits, mechanics-only marks
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
TAPE_OK = {"fetched_at": __import__("time").time(), "received_at": __import__("time").time(),  # freshness stamps (TEST-FIXTURE)
   "usd_liquidity": 8_000.0, "usd_market_cap": 69_000.0,
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
        import shutil as _shl; _shl.rmtree(PA.ACCOUNT_DIR, ignore_errors=True)  # DISCLOSED TEST EDIT: reset the account with the lane ledger (see test_regime)
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

# ---- full lifecycle, per-tranche (marked) position: every leg is credited to the account at its own
# sale (journaled op receipts), so the final close has NO remainder to credit and journals no close intent.
# TEST EDIT (disclosed): this block used to assert the deferred-credit lifecycle for a marked position.
ps, _mv, _ms = _fresh()
cash0 = PA.snapshot()["cash"]
opened, why, _f = G.consider_candidate(_cand(mint="TRNCH1pump"))
check("fixture: marked entry opened", opened and G._load()["positions"]["TRNCH1pump"].get("tranche_credit"))
_close_open("TRNCH1pump", mult=2.0)
_snap = PA.snapshot()
check("tranche lifecycle: no close intent is needed (every leg credited at its sale); nothing pending",
      _events() == [] and CI.pending("grad") == [])
check("tranche lifecycle: account position fully closed once and cash credited (n_closes 1, open 0, cash > start)",
      _snap["lanes"]["grad"]["open_count"] == 0 and _snap["lanes"]["grad"]["n_closes"] == 1
      and _snap["cash"] > cash0 and G._load()["positions"].get("TRNCH1pump") is None)
_unpatch(ps)

# ---- legacy (UNMARKED) position: the deferred final-close credit keeps the full intent lifecycle
ps, _mv, _ms = _fresh()
opened, why, _f = G.consider_candidate(_cand(mint="LIFE111pump"))
check("fixture: entry opened", opened)
with G._ledger_lock():
    _d = G._read(); _d["positions"]["LIFE111pump"].pop("tranche_credit", None); G._write(_d)
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
# MIGRATION (mandatory guard): the guard now REFUSES a flag-off entry, so the
# premise cannot be rebuilt through the entry path. The state is instead a
# HISTORICAL FIXTURE generated by the ACTUAL pre-guard grad lane (6b0aed8)
# inside the OS network-deny wrapper (see tests/fixtures/pre_guard_orphan/
# *.provenance.json and gen_pre_guard_orphan_grad.py). Loaded unmodified.
import hashlib, shutil
_FX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "fixtures", "pre_guard_orphan")
_prov = json.load(open(os.path.join(
    _FX, "pre_guard_orphan_grad_ledger.provenance.json")))
_fx_path = os.path.join(_FX, _prov["fixture"])
check("orphan fixture bytes match its recorded sha256 (unmodified)",
      hashlib.sha256(open(_fx_path, "rb").read()).hexdigest()
      == _prov["fixture_sha256"] and _prov["hand_edited"] is False
      and _prov["generated_against_commit"].startswith("6b0aed8"))
ps, _mv, _ms = _fresh()
shutil.copyfile(_fx_path, G.LEDGER_PATH)
_orph = G._load()["positions"].get("ORPH11pump")
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
opened = bool(_orph) and not _orph.get("pa_pos_id")
check("fixture: historical pre-account-epoch position loaded (no shared "
      "position id, never debited)", opened)
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
# TEST EDIT (disclosed): the divergence now freezes at the FIRST refused per-tranche credit (loud, entries
# blocked) instead of waiting for the final close intent; the account side is still NEVER applied.
check("ghost: intent opened, account side NEVER applied, frozen at the refusal (one intent, no duplicates)",
      [e["ev"] for e in evs4] == ["intent", "frozen"])
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
# A frozen intent BLOCKS entries, not merely logs.
opened_blk, why_blk, _f = G.consider_candidate(
    _cand(mint="BLOCKED1pump"))
check("frozen: entry refused while divergence is frozen",
      not opened_blk and "frozen" in why_blk)
_unpatch(ps)
G._ENTRY_BLOCKED = False   # section isolation

# ---- recovery runs BEFORE a post-restart entry ---------------------
# Simulate a kill after account_applied but before the lane write:
# _finalize with a FAILING _write (the kill lands at the persist).
# The next process's FIRST ENTRY must reconcile the pending intent
# before debiting.
ps, _mv, _ms = _fresh()
G._ENTRY_BLOCKED = False
_cash_pre_pend = PA.snapshot()["cash"]
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
# TEST EDIT (disclosed): for a per-tranche (marked) position the account side is already durable as journaled
# per-leg credit receipts at kill time; there is no final close intent. The restart must (1) permit entries,
# (2) never re-credit, (3) finish the lane close on the natural re-close from the RECEIPTS.
pend = CI.pending("grad")
_s0 = PA.snapshot()
check("fixture: kill left the account fully credited per tranche (position closed there, no intent)",
      pend == [] and _s0["lanes"]["grad"]["open_count"] == 0 and _s0["lanes"]["grad"]["n_closes"] >= 1)
cash_after_close = PA.snapshot()["cash"]
G._RECOVERY_DONE = False   # the restart
opened_b, why_b, _f = G.consider_candidate(_cand(mint="NEXT11pump"))
check("entry: new entry permitted after restart", opened_b)
check("entry: account not re-credited by the restart",
      abs(PA.snapshot()["cash"] - (cash_after_close - 1.0)) < 1e-9)
# The natural re-close finishes the LANE side from the journaled receipts: no double settlement.
with G._ledger_lock():
    d = G._read()
    pos = d["positions"]["PEND11pump"]
    px = pos["entry_price"] * 3.0    # a DIFFERENT price: the replay must use the receipt, not this price
    for leg in ("a", "b", "c"):
        G._close_leg(pos, leg, px, "test")
    G._finalize(d, "PEND11pump", pos, "test")
    G._write(d)
check("re-close: lane close booked exactly once, nothing pending",
      sum(1 for c in G._load()["closes"] if c.get("mint") == "PEND11pump") == 1
      and "PEND11pump" not in G._load()["positions"] and CI.pending("grad") == [])
_rec = [c for c in G._load()["closes"] if c.get("mint") == "PEND11pump"][0]
_acct_pnl = cash_after_close - (_cash_pre_pend - 1.0)   # net proceeds the account actually received (lane realized_usd is net proceeds)
check("re-close: lane realized equals the ACCOUNT's receipts, not the replay price (%.6f vs %.6f)" % (_rec["realized_usd"], _acct_pnl),
      abs(_rec["realized_usd"] - _acct_pnl) < 1e-4)
check("re-close: replay used the receipts (no double credit, price change ignored)",
      abs(PA.snapshot()["cash"] - (cash_after_close - 1.0)) < 1e-9)
_unpatch(ps)

# ---- incomplete split + write-down leg: conservation ---------------
# Legacy position WITHOUT per-leg fracs, one priced leg and one
# write-down leg: units cannot be attributed, so the WHOLE position
# must go to the unresolved register - no sale invented for the
# unquotable units, no cash claimed while the split is unknown.
ps, _mv, _ms = _fresh()
G._ENTRY_BLOCKED = False
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

# ---- torn intent journal: unknown history freezes ------------------
# A corrupt journal proves nothing: recovery cannot classify any
# incomplete intent, so every one of them freezes and entries block.
ps, _mv, _ms = _fresh()
G._ENTRY_BLOCKED = False
opened, _w, _f = G.consider_candidate(_cand(mint="TORN11pump"))
check("fixture: torn-journal entry opened", opened)
_close_open("TORN11pump", mult=1.5)
# Corrupt the journal MID-FILE (not the tail): no automatic repair
# can justify itself.
_jpath = CI.journal_path("grad")
_lines = open(_jpath).read().splitlines(True)
with open(_jpath, "w") as _f2:
    _f2.writelines(_lines[:1] + ["{not json\n"] + _lines[1:])
G._RECOVERY_DONE = False
G._ENTRY_BLOCKED = False
opened_t, why_t, _f = G.consider_candidate(_cand(mint="AFTERT1pump"))
check("torn: recovery cannot prove state -> entry BLOCKED",
      not opened_t and "frozen" in why_t)
check("torn: op_outcome unknown on truncated/corrupt history",
      PA.op_outcome("grad:close:TORN11pump:nonexistent") == "unknown")
_unpatch(ps)

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL CLOSE-INTENT GRAD WIRING TESTS PASSED")
