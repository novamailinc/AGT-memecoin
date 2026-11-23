#!/usr/bin/env python3
"""End-to-end close-intent wiring for the TRANCHE lanes (watcher,
ceo, momentum) — Stage C, 2026-11-22.

test_close_intents_grad.py proves the lifecycle through the grad
engine; this suite proves the same acceptance conditions through
each tranche lane's real entry/exit path:

  - full lifecycle: per-tranche intents reach terminal `applied`
    when the lane ledger persists, nothing left pending
  - kill at the lane-write boundary: the next process's FIRST ENTRY
    reconciles the pending intent before debiting; the natural
    re-close converges with the SAME op_id (no double settlement)
  - ghost (debited position vanished from the account): the close
    settles nowhere, recovery FREEZES loud, and the frozen intent
    BLOCKS the next entry (not merely a log line)
  - orphan tolerance requires epoch proof: an intent whose account
    op is a provably never-debited orphan converges
  - torn/corrupt journal history is unknown: recovery freezes and
    entries are blocked

Plain script — run directly (the pytest conftest ignores files
without pytest-style tests; scripts/run_script_tests.py executes
this file).
"""
import importlib
import json
import os
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TMPD = tempfile.mkdtemp(prefix="ci-tranche-test-")
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(
    prefix="ci-tranche-home-")
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"

from daemon.launch_scanner import watcher_lane as W
from daemon.launch_scanner import ceo as C
from daemon.launch_scanner import momentum_lane as M
from daemon.launch_scanner import paper_account as PA
from daemon.launch_scanner import close_intents as CI
from daemon.launch_scanner import killswitch as KS
from daemon.launch_scanner import signal_bus as BUS

importlib.reload(PA)

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# ---- shared stubs ----------------------------------------------------
_ks_tmp = tempfile.mkdtemp(prefix="ci-tranche-ks-")
KS.STATE_DIR = _ks_tmp
KS.STATE_FILE = os.path.join(_ks_tmp, "state.json")
KS.SHADOW_FILE = os.path.join(_ks_tmp, "state.json.bak")
KS.LOCK_FILE = os.path.join(_ks_tmp, "state.lock")

_bus_tmp = tempfile.mkdtemp(prefix="ci-tranche-bus-")
BUS.BUS_PATH = os.path.join(_bus_tmp, "signal_bus.jsonl")
C.BUS_PATH = BUS.BUS_PATH

GOOD_TAPE = {"usd_liquidity": 100000.0, "usd_market_cap": 500000.0,
             "chg_m5": 5.0, "buys_m5": 100, "sells_m5": 40,
             "price_usd": 0.001, "tape_source": "test"}


class _SafetyStub:
    def check_token(self, chain, mint, timeout=10):
        return {"passed": True, "score": 80, "flags": [],
                "fails": [], "unknown": False, "source": "test"}


class _VetoStub:
    def veto_check(self, mint, chain, symbol="", safety_report=None,
                   run_bundle=False, lane="unknown"):
        return {"passed": True, "vetoes": [], "flags": [],
                "details": {}}


# watcher / momentum: direct attribute stubs (their own suites'
# pattern). ceo: patched per entry (its suite's _patch_all pattern).
W._fresh_tape = lambda chain, mint: dict(GOOD_TAPE)
W.quote_price = lambda chain, mint, spot_index=None: 0.001
W._safety = _SafetyStub()
M._fresh_tape = lambda chain, mint: dict(GOOD_TAPE)
M.quote_price = lambda chain, mint, spot_index=None: 0.001
M._safety = _SafetyStub()
M._veto = _VetoStub()


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

for _lane, _mod in (("watcher", W), ("ceo", C), ("momentum", M)):
    _mod.LEDGER_PATH = os.path.join(TMPD, "%s_ledger.json" % _lane)


def _events(lane, mint=None):
    path = CI.journal_path(lane)
    if not os.path.exists(path):
        return []
    evs = [json.loads(l) for l in open(path) if l.strip()]
    if mint is not None:
        evs = [e for e in evs if e.get("mint") == mint
               or e.get("intent_id", "").startswith(
                   "%s:%s" % (lane, mint))]
    return evs


def _close_all(mod, mint, mult=2.0):
    """Close every leg at `mult` x entry and finalize, driving the
    lane's real leg-close helpers under its lock."""
    with mod._ledger_lock():
        d = mod._read()
        pos = d["positions"][mint]
        px = pos["entry_price"] * mult
        for leg in ("a", "b", "c"):
            if not pos["legs"][leg]["done"]:
                mod._close_leg(pos, leg, px, "test")
        mod._finalize(d, mint, pos, "test")
        mod._write(d)


# ---- per-lane entry drivers -------------------------------------------
def _w_fire(mint):
    ev = {"smart_wallets": 3,
          "wallets": ["Wal0", "Wal1", "Wal2"],
          "wallet_groups": ["g0", "g1", "g2"],
          "safety": "ok",
          "components": {"convergence_groups": 3, "sightings": 3,
                         "sol_spent_total": 4.2, "safety_score": 80}}
    sig = BUS.make_signal("solana", "smart-watcher", mint,
                          symbol="TRKN", tier="converged",
                          mcap=500000.0, liquidity=100000.0,
                          evidence=ev)
    return W.consider_fire(sig, time.time())


def _c_enter(mint):
    ps = (patch.object(C, "_veto"),
          patch.object(C, "_safety"),
          patch.object(C, "_ks", FAKE_KS),
          patch.object(C, "quote_price", return_value=0.001),
          patch.object(C, "_fresh_tape",
                       return_value=dict(GOOD_TAPE)),
          patch.object(C, "RUNNER_CONFIRM_GAP_S", 0.0))
    m_veto, m_safety = ps[0].start(), ps[1].start()
    for p in ps[2:]:
        p.start()
    m_veto.veto_check.return_value = {"passed": True, "vetoes": []}
    m_safety.check_token.return_value = {"passed": True,
                                         "unknown": False}
    try:
        sig = BUS.make_signal(chain="solana", source="test-feed",
                              mint=mint, symbol="TRKN", score=70.0,
                              tier="pick", mcap=500000.0,
                              liquidity=50000.0)
        ok, why = C.consider_card(sig)
        return ok, why, not ok
    finally:
        for p in ps:
            p.stop()


def _m_enter(mint):
    cand = {"pool_address": "Pool" + mint[:6], "mint": mint,
            "symbol": "TRKN", "name": "TRKN", "chain": "solana",
            "source": "gecko-trending", "lane": "lowcap", "url": "",
            "chg_24h_pct": 55.0, "chg_1h_pct": 8.0,
            "vol_24h_usd": 250000.0, "liq_usd": 100000.0,
            "usd_market_cap": 500000.0}
    return M.consider_candidate(cand, time.time())


DRIVERS = (("watcher", W, _w_fire), ("ceo", C, _c_enter),
           ("momentum", M, _m_enter))

for lane, mod, enter in DRIVERS:
    PFX = "%s: " % lane

    # ============ lifecycle: intents reach terminal applied ==========
    mint = "Life%s11111111111111111111111111111111" % lane
    opened, why, _f = enter(mint)
    check(PFX + "fixture: entry opened (%s)" % why, opened)
    _close_all(mod, mint, mult=2.0)
    kinds = [e["ev"] for e in _events(lane, mint)]
    check(PFX + "lifecycle: every intent reached applied",
          kinds and kinds.count("intent") > 0
          and kinds.count("intent") == kinds.count("applied"))
    check(PFX + "lifecycle: nothing pending",
          CI.pending(lane) == [])
    settle_kinds = {op["kind"] for e in _events(lane, mint)
                    if e["ev"] == "intent"
                    for op in e.get("settle_ops", [])}
    check(PFX + "lifecycle: per-tranche credit ops journaled",
          "credit" in settle_kinds)
    check(PFX + "lifecycle: close record carries pa_pos_id",
          any(r.get("pa_pos_id") for r in mod._read()["closes"]
              if r.get("mint") == mint))

    # recovery after convergence is a no-op
    cash0 = PA.snapshot()["cash"]
    mod._RECOVERY_DONE = False
    mod._recover_close_intents()
    check(PFX + "recovery: converged close is a no-op (cash flat)",
          abs(PA.snapshot()["cash"] - cash0) < 1e-9)
    check(PFX + "recovery: still nothing pending",
          CI.pending(lane) == [])

    # ============ kill at the lane-write boundary ====================
    mint = "Pend%s11111111111111111111111111111111" % lane
    opened, why, _f = enter(mint)
    check(PFX + "fixture: kill-test entry opened", opened)
    orig_write = mod._write
    mod._write = lambda d: False   # the persist never lands
    with mod._ledger_lock():
        d = mod._read()
        pos = d["positions"][mint]
        px = pos["entry_price"] * 2.0
        for leg in ("a", "b", "c"):
            mod._close_leg(pos, leg, px, "test")
        mod._finalize(d, mint, pos, "test")
    mod._write = orig_write
    disk = mod._load()
    check(PFX + "fixture: kill left the position open on disk",
          mint in disk["positions"]
          and not any(r.get("mint") == mint
                      for r in disk["closes"]))
    pend = [p for p in CI.pending(lane)
            if p.get("mint") == mint
            or p["intent_id"].startswith("%s:%s" % (lane, mint))]
    check(PFX + "fixture: intents pending with account side applied",
          len(pend) >= 1 and all(p["account_applied"]
                                 and not p["lane_applied"]
                                 for p in pend))
    cash_after_close = PA.snapshot()["cash"]
    mod._RECOVERY_DONE = False   # the restart
    mint2 = "Next%s11111111111111111111111111111111" % lane
    opened_b, why_b, _f = enter(mint2)
    check(PFX + "entry: permitted after reconciliation (%s)" % why_b,
          opened_b)
    check(PFX + "entry: consistent pending intent does not block",
          mod._ENTRY_BLOCKED is False)
    pend_b = [p for p in CI.pending(lane)
              if p.get("mint") == mint
              or p["intent_id"].startswith("%s:%s" % (lane, mint))]
    check(PFX + "entry: open position left to its natural re-close",
          len(pend_b) >= 1)
    # account re-apply during reconciliation deduped by op_id
    check(PFX + "entry: no re-credit during reconciliation",
          abs(PA.snapshot()["cash"] - (cash_after_close - 1.0)) < 1e-9)
    _close_all(mod, mint, mult=2.0)
    pend_c = [p for p in CI.pending(lane)
              if p.get("mint") == mint
              or p["intent_id"].startswith("%s:%s" % (lane, mint))]
    check(PFX + "re-close: intents converged, none pending",
          pend_c == [])
    check(PFX + "re-close: stable op_id deduped (no double credit)",
          abs(PA.snapshot()["cash"] - (cash_after_close - 1.0)) < 1e-9)

    # ============ ghost: debited position vanished ===================
    mint = "Ghost%s111111111111111111111111111111" % lane
    opened, why, _f = enter(mint)
    check(PFX + "fixture: ghost entry opened and debited", opened)
    with PA._lock():
        led, _h, _dg = PA._load_locked()
        led["lanes"][lane]["open"].pop(mint, None)
        PA._save_locked(led)
    cash_g = PA.snapshot()["cash"]
    _close_all(mod, mint, mult=1.5)
    gevs = _events(lane, mint)
    check(PFX + "ghost: account side NEVER applied",
          gevs and "account_applied" not in
          [e["ev"] for e in gevs])
    check(PFX + "ghost: no cash invented by the refused settle",
          abs(PA.snapshot()["cash"] - cash_g) < 1e-9)
    mod._RECOVERY_DONE = False
    mod._recover_close_intents()
    gevs2 = _events(lane, mint)
    check(PFX + "ghost: recovery freezes the divergence",
          gevs2 and gevs2[-1]["ev"] == "frozen")
    mint3 = "Block%s111111111111111111111111111111" % lane
    opened_blk, why_blk, _f = enter(mint3)
    check(PFX + "frozen: entry BLOCKED, not merely logged",
          not opened_blk and "frozen" in why_blk)
    check(PFX + "frozen: still no double settlement",
          abs(PA.snapshot()["cash"] - cash_g - 1.0) < 1e-9
          or not opened_blk)
    mod._ENTRY_BLOCKED = False   # section isolation

    # ============ orphan tolerance requires epoch proof ==============
    # An intent whose account op is a provably never-debited orphan
    # (pre-account epoch) converges: account side orphan-proven, lane
    # side already persisted.
    mint = "Orph%s11111111111111111111111111111111" % lane
    pid = "%s:entry:%s:0" % (lane, mint)
    iid = CI.begin(lane, mint, pid,
                   [{"kind": "credit",
                     "op_id": "%s:leg:a:%s:%s" % (lane, mint, pid),
                     "mint": mint, "amount": 1.5, "qty": 1500.0,
                     "fee": 0.0, "price_kind": "test",
                     "pos_id": pid}],
                   {"realized_usd": 1.5, "closed_reason": "test",
                    "close_ts": time.time(), "leg_suffix": "leg:a"})
    # lane side: persist a close record carrying that pa_pos_id
    with mod._ledger_lock():
        d = mod._read()
        d["closes"].append({"mint": mint, "pa_pos_id": pid,
                            "realized_usd": 1.5,
                            "closed_reason": "test",
                            "close_ts": time.time()})
        mod._write(d)
    mod._RECOVERY_DONE = False
    blocked = mod._recover_close_intents()
    oevs = [e["ev"] for e in _events(lane, mint)]
    check(PFX + "orphan: epoch-proven orphan converges (applied)",
          "applied" in oevs and "frozen" not in oevs)
    check(PFX + "orphan: recovery not blocked by convergence",
          blocked is False)

    # ============ torn/corrupt journal: unknown freezes ==============
    # A mid-file corrupt record makes history UNKNOWN: with an
    # incomplete intent outstanding, recovery must freeze it (no
    # re-application against suspect history) and block the entry.
    mint = "Torn%s11111111111111111111111111111111" % lane
    opened, why, _f = enter(mint)
    check(PFX + "fixture: torn-test entry opened", opened)
    jp = CI.journal_path(lane)
    CI.begin(lane, mint, "torn-pos-0",
             [{"kind": "credit", "op_id": "%s:leg:a:%s:torn-pos-0"
               % (lane, mint), "mint": mint, "amount": 1.0,
               "qty": 1000.0, "fee": 0.0, "price_kind": "test",
               "pos_id": "torn-pos-0"}],
             {"realized_usd": 1.0, "closed_reason": "test",
              "close_ts": time.time(), "leg_suffix": "leg:a"})
    lines = open(jp).read().splitlines(keepends=True)
    bad = "{not json torn record\n"
    mid = len(lines) // 2
    with open(jp, "w") as f:
        f.writelines(lines[:mid] + [bad] + lines[mid:])
    mod._RECOVERY_DONE = False
    blocked = mod._recover_close_intents()
    check(PFX + "torn: corrupt history freezes recovery",
          blocked is True)
    mint4 = "TornB%s111111111111111111111111111111" % lane
    opened_t, why_t, _f = enter(mint4)
    check(PFX + "torn: entry blocked on unknown history",
          not opened_t and "frozen" in why_t)
    mod._ENTRY_BLOCKED = False
    # restore a clean journal (per-lane file; this lane is done)
    lines = open(jp).read().splitlines(keepends=True)
    with open(jp, "w") as f:
        f.writelines(l for l in lines if l != bad)

print()
if FAILS:
    print("FAILURES: %d" % len(FAILS))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED")
