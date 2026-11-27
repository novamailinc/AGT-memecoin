#!/usr/bin/env python3
"""Wiring tests: grad lane <-> shared paper account (Stage 1).

Proves the actual runtime wiring, not just the account module:
with PAPER_ACCOUNT_ENABLED=1 the shared account is the
authoritative capital gate — an entry that the shared cash cannot
cover is REFUSED (fail-closed, transient), a covered entry debits
the shared account, and the close credits the exact realized
proceeds. With the flag off the lane never touches the account.

Harness mirrors tests/test_grad.py (same fakes, same drive path).
Run: python3 tests/test_paper_account_grad.py
"""
import importlib
import os
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests._guard_fixture as _gfb; _gfb.block_network()  # no network in tests

TMPD = tempfile.mkdtemp(prefix="paper-grad-test-")
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(
    prefix="paper-grad-home-")

import daemon.launch_scanner.grad as G
import daemon.launch_scanner.paper_account as PA
_gfb.install_price_feed(at_cost=True)  # mandatory-guard migration (disclosed): mechanics-only marks via real pipeline

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
    # TRUE section isolation (a fresh process): a new account home +
    # reloaded paper_account (fresh account, fresh close-intent
    # journal, fresh lane registry), a fresh grad ledger, and the grad
    # module globals a restart clears. The old _fresh() only deleted
    # the grad ledger, so a frozen close intent or an open account lot
    # from an earlier section blocked every later entry (fail-closed
    # admission: frozen intent / orphan lot), and the file aborted at
    # the first section after a freeze. Sections are independent (each
    # captures its own cash baseline), so a full reset is compatible.
    os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="paper-grad-home-")
    importlib.reload(PA)
    if os.path.exists(G.LEDGER_PATH):
        os.remove(G.LEDGER_PATH)
    FAKE_KS.halted = set()
    FAKE_KS.recorded = []
    ps, m_veto, m_safety = _patch_all(price=price)
    G.ensure_ledger()
    G._ENTRY_BLOCKED = False; G._RECOVERY_DONE = False
    G._PENDING_CLOSE_INTENTS.clear()
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
    """Drive a full close of the open position at `mult` x entry,
    the same path the lane's monitor uses."""
    with G._ledger_lock():
        d = G._read()
        pos = d["positions"][mint]
        exit_price = pos["entry_price"] * mult
        G._close_leg(pos, "a", exit_price, "test")
        G._close_leg(pos, "b", exit_price, "test")
        G._close_leg(pos, "c", exit_price, "test")
        G._finalize(d, mint, pos, "test")
        G._write(d)


# ---- flag OFF: lane never touches the account ---------------------
os.environ.pop("PAPER_ACCOUNT_ENABLED", None)
ps, _mv, _ms = _fresh()
opened, why, final = G.consider_candidate(_cand(mint="OFF111pump"))
# CONTRACT CHANGE (owner directive: one shared simulated $10, no private
# capital): flag off now REFUSES entry instead of using a private bankroll.
check("flag off: entry REFUSED by the mandatory guard, no private position",
      not opened and "mandatory entry guard" in str(why)
      and "OFF111pump" not in G._load()["positions"])
check("flag off: shared account untouched (no ledger dir)",
      not os.path.exists(PA.LEDGER_PATH))
_unpatch(ps)

# ---- flag ON: shared cash gates entries ----------------------------
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
os.environ["PAPER_ACCOUNT_GATES_ENABLED"] = "1"  # TEST FIXTURE (mandatory-guard edit): not owner values
os.environ["PAPER_ACCOUNT_MAX_DAILY_LOSS_USD"] = "1000"
os.environ["PAPER_ACCOUNT_MAX_DRAWDOWN_PCT"] = "90"
os.environ["PAPER_ACCOUNT_MAX_EXPOSURE_PCT"] = "100"
os.environ["PAPER_ACCOUNT_MAX_SIMULTANEOUS"] = "1000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_POSITIONS"] = "1000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = "100000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_TOTAL_EXPOSURE_USD"] = "1000000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_TOKEN_EXPOSURE_USD"] = "1000000"  # TEST-FIXTURE value, not owner
importlib.reload(PA)  # picks up FOREX_AGENT_HOME paths as-is
ps, _mv, _ms = _fresh()
# Drain the shared account to $1.50 via a direct debit so the next
# $1 grad entry fits but a second one cannot.
ok, _, _pid = PA.try_debit("other-lane", "DRAIN1", 8.50, 8.50,
                           "drain-1")
check("fixture: shared account drained to $1.50",
      ok and PA.snapshot()["cash"] == 1.50)

opened1, why1, _f = G.consider_candidate(_cand(mint="PAY111pump"))
snap = PA.snapshot()
check("flag on: first entry enters and debits the shared account",
      opened1 and snap["cash"] == 0.50
      and snap["lanes"]["grad"]["open_count"] == 1)

opened2, why2, final2 = G.consider_candidate(_cand(mint="POOR11pump"))
check("flag on: entry refused when shared cash is short "
      "(fail-closed, transient, not final)",
      not opened2 and not final2 and "paper account" in why2)
check("flag on: refused entry never opened a lane position",
      "POOR11pump" not in G._load()["positions"])
check("flag on: private bankroll unchanged by the refusal",
      G._load()["bankroll"] == 100.00 - 1.00 or
      abs(G._load()["bankroll"] - (100.00 - 1.0)) < 1e-6)

# ---- close settles into the shared account -------------------------
before = PA.snapshot()["cash"]
_close_open("PAY111pump", mult=2.0)
snap = PA.snapshot()
check("flag on: close credits realized proceeds to the account",
      snap["cash"] > before
      and snap["lanes"]["grad"]["open_count"] == 0
      and snap["lanes"]["grad"]["n_closes"] == 1)
check("account/lane agree on the close direction (profitable)",
      snap["lanes"]["grad"]["realized_pnl"] > 0)
_unpatch(ps)

# ---- NON-SIM per-tranche parity (disclosed ADDITION): a partial exit moves the shared account NOW ----
ps, _mv, _ms = _fresh()
okp, _w, _f = G.consider_candidate(_cand(mint="PART11pump"))
check("fixture: partial-exit entry opened (non-sim model fill, shared account)", okp)
_pt = G._load()["positions"]["PART11pump"]
_toks = _pt["tokens"]; _frac_a = _pt["legs"]["a"]["frac"]
_acct0 = [p for p in PA.snapshot()["lanes"]["grad"]["positions"] if p["mint"] == "PART11pump"][0]
check("account units equal the lane's token units at entry (net of entry gas)", abs(_acct0["qty"] - _toks) < 1e-6 * _toks)
_c0 = PA.snapshot()["cash"]
with G._ledger_lock():
    d = G._read(); pos = d["positions"]["PART11pump"]
    net_a = G._close_leg(pos, "a", pos["entry_price"] * 2.0, "test")
    G._write(d)
_sn = PA.snapshot()
_acct1 = [p for p in _sn["lanes"]["grad"]["positions"] if p["mint"] == "PART11pump"][0]
check("leg a only: shared cash rose by exactly leg a's net proceeds (%.6f vs %.6f)" % (_sn["cash"] - _c0, net_a),
      net_a > 0 and abs((_sn["cash"] - _c0) - net_a) < 1e-5)
check("leg a only: account units/basis shrank to the remaining fraction (equity marks see the true units)",
      abs(_acct1["qty"] - _toks * (1 - _frac_a)) < 1e-6 * _toks
      and abs(_acct1["basis"] - _acct0["basis"] * (1 - _frac_a)) < 1e-4)
_c1 = _sn["cash"]
_close_open("PART11pump", mult=2.0)   # finish: legs b, c credit their own tranches, the final close adds nothing
_sf = PA.snapshot()
check("finished: account closed once, no residual units, lane position gone",
      _sf["lanes"]["grad"]["open_count"] == 0 and "PART11pump" not in G._load()["positions"]
      and _sf["cash"] > _c1)
_unpatch(ps)

# ---- NON-SIM ladder crash replay via reconcile (exit_price 0), receipt qty binding, freeze journal failure (disclosed ADDITIONS) ----
import copy as _copy2
ps, _mv, _ms = _fresh()
okl, _w, _f = G.consider_candidate(_cand(mint="REPL11pump"))
check("fixture: replay entry opened (non-sim)", okl)
with G._ledger_lock():
    d = G._read(); pos = d["positions"]["REPL11pump"]
    G._write(d)                                         # disk: position open, no legs done
    pos_mem = _copy2.deepcopy(pos)
    netA = G._close_leg(pos_mem, "a", pos_mem["entry_price"] * 2.0, "test")   # account credited; lane write LOST
_cA = PA.snapshot()["cash"]
with G._ledger_lock():
    d = G._read(); pos = d["positions"]["REPL11pump"]
    okr = G._reconcile_credited_legs(pos)                # reaches _close_leg with exit_price 0.0 (no guard before the receipt branch)
    la = pos["legs"]["a"]
    check("non-sim crash replay (reconcile, price 0): leg a finished from the receipt: proceeds %.6f vs %.6f, cash unchanged" % (la.get("realized_usd") or 0, netA),
          okr is True and la.get("done") is True and abs((la.get("realized_usd") or 0) - netA) < 1e-5 and abs(PA.snapshot()["cash"] - _cA) < 1e-9)
    G._write(d)
# receipt qty must bind through a NORMAL trigger path too (not only reconcile): corrupt the lane leg fraction so the
# receipt qty (original) no longer matches this leg's tokens
with G._ledger_lock():
    d = G._read(); pos = d["positions"]["REPL11pump"]
    pos["legs"]["b"]["frac"] = pos["legs"]["b"]["frac"]   # b not credited yet: credit it, then fake a lost lane write
    pos_b = _copy2.deepcopy(pos)
    G._close_leg(pos_b, "b", pos_b["entry_price"] * 2.0, "test")
    pos["legs"]["b"]["frac"] = pos["legs"]["b"]["frac"] * 1.5   # disagrees with the receipt qty
    _cb = PA.snapshot()["cash"]
    rb = G._close_leg(pos, "b", pos["entry_price"] * 2.0, "test")
    check("receipt qty mismatch through a normal trigger: leg stays pending, loud, no cash, lane not booked",
          rb == 0.0 and pos["legs"]["b"].get("done") is not True
          and (pos.get("pending_exit") or {}).get("why") == "receipt_qty_mismatch"
          and abs(PA.snapshot()["cash"] - _cb) < 1e-9)
    pos["legs"]["b"]["frac"] = pos["legs"]["b"]["frac"] / 1.5
    # receipt present and qty agrees, but the account refuses the (idempotent) credit: the lane must NOT book the leg
    _orig_credit = PA.credit
    PA.credit = lambda *a_, **k_: (False, "identity mismatch (test)")
    try:
        rb2 = G._close_leg(pos, "b", pos["entry_price"] * 2.0, "test")
    finally:
        PA.credit = _orig_credit
    check("receipt replay with a refused credit: leg stays pending, lane not booked (never books what the account did not accept)",
          rb2 == 0.0 and pos["legs"]["b"].get("done") is not True)
    G._close_leg(pos, "b", pos["entry_price"] * 2.0, "test")
    G._close_leg(pos, "c", pos["entry_price"] * 2.0, "test")
    G._finalize(d, "REPL11pump", pos, "test"); G._write(d)
_unpatch(ps)
# freeze journal write failure must not look frozen: next poll retries the intent
ps, _mv, _ms = _fresh()
okg, _w, _f = G.consider_candidate(_cand(mint="GHOST2pump"))
with PA._lock():
    led, healthy, _deg = PA._load_locked()
    led["lanes"]["grad"]["open"].pop("GHOST2pump", None)
    PA._save_locked(led)
_ci_mod = G._close_intents()
_orig_begin = _ci_mod.begin
def _boom(*a, **k): raise OSError("journal write failed (test)")
_ci_mod.begin = _boom
with G._ledger_lock():
    d = G._read(); pos = d["positions"]["GHOST2pump"]
    G._close_leg(pos, "a", pos["entry_price"] * 2.0, "test")
check("freeze journal failure: entries blocked, but the leg is NOT marked frozen (next poll retries)",
      G._ENTRY_BLOCKED is True and (pos.get("pending_exit") or {}).get("why") == "tranche_credit_refused")
_ci_mod.begin = _orig_begin
with G._ledger_lock():
    G._close_leg(pos, "a", pos["entry_price"] * 2.0, "test")
check("freeze retry after the journal recovers: frozen exactly once",
      (pos.get("pending_exit") or {}).get("why") == "tranche_credit_divergence"
      and len([e for e in __import__("json").loads("[" + ",".join(l for l in open(_ci_mod.journal_path("grad")).read().splitlines() if l.strip()) + "]") if e.get("ev") == "frozen" and "GHOST2pump" in e.get("intent_id", "")]) == 1)
G._ENTRY_BLOCKED = False
_unpatch(ps)

# ---- LEGACY mid-upgrade (disclosed ADDITION): an UNMARKED position (opened before per-tranche credit) with leg A already
# sold lane-side keeps the deferred contract: the account sees ONE final close with the total, conserved, no tranche ops ----
ps, _mv, _ms = _fresh()
okm, _w, _f = G.consider_candidate(_cand(mint="MIGR11pump"))
_cm0 = PA.snapshot()["cash"]
with G._ledger_lock():
    d = G._read(); pos = d["positions"]["MIGR11pump"]
    pos.pop("tranche_credit", None)                       # opened by pre-upgrade code
    netA = G._close_leg(pos, "a", pos["entry_price"] * 2.0, "test")   # pre-upgrade partial exit: lane-side only
    G._write(d)
_cm1 = PA.snapshot()["cash"]
check("legacy mid-upgrade: leg A sold lane-side moved NO account cash (deferred contract)", abs(_cm1 - _cm0) < 1e-9)
with G._ledger_lock():
    d = G._read(); pos = d["positions"]["MIGR11pump"]
    nb = G._close_leg(pos, "b", pos["entry_price"] * 2.0, "test")
    nc = G._close_leg(pos, "c", pos["entry_price"] * 2.0, "test")
    G._finalize(d, "MIGR11pump", pos, "test"); G._write(d)
_sm = PA.snapshot()
check("legacy mid-upgrade: the final close credits the TOTAL once (cash +%.6f vs %.6f), account closed once" % (_sm["cash"] - _cm0, netA + nb + nc),
      abs((_sm["cash"] - _cm0) - (netA + nb + nc)) < 1e-5
      and _sm["lanes"]["grad"]["open_count"] == 0 and "MIGR11pump" not in G._load()["positions"])
_unpatch(ps)

# ---- reconcile must not report success when a replay helper refused (disclosed ADDITION) ----
ps, _mv, _ms = _fresh()
okq, _w, _f = G.consider_candidate(_cand(mint="RECO11pump"))
with G._ledger_lock():
    d = G._read(); pos = d["positions"]["RECO11pump"]; G._write(d)
    pm = _copy2.deepcopy(pos); G._close_leg(pm, "a", pm["entry_price"] * 2.0, "test")   # A credited, lane write lost
_cq = PA.snapshot()["cash"]
_orig_credit = PA.credit
PA.credit = lambda *a_, **k_: (False, "identity mismatch (test)")
try:
    with G._ledger_lock():
        d = G._read(); pos = d["positions"]["RECO11pump"]
        okc = G._reconcile_credited_legs(pos)
        rr = G._close_remaining(pos, pos["entry_price"] * 0.5, "hard_stop")
finally:
    PA.credit = _orig_credit
check("reconcile with a refused replay credit returns False (leg not done), and the stop-out makes NO combined sale",
      okc is False and rr == 0.0 and not any(l.get("done") for l in pos["legs"].values())
      and (pos.get("pending_exit") or {}).get("why") == "applied_credit_unreconciled"
      and abs(PA.snapshot()["cash"] - _cq) < 1e-9)
with G._ledger_lock():            # finish the position so later fixtures start from an empty account
    d = G._read(); pos = d["positions"]["RECO11pump"]
    for _k in ("a", "b", "c"):
        G._close_leg(pos, _k, pos["entry_price"] * 2.0, "test")
    G._finalize(d, "RECO11pump", pos, "test"); G._write(d)
_unpatch(ps)

# ---- freeze-only journal failure after begin succeeded: the retry freezes the SAME intent (no duplicate) ----
ps, _mv, _ms = _fresh()
okg, _w, _f = G.consider_candidate(_cand(mint="GHOST3pump"))
with PA._lock():
    led, healthy, _deg = PA._load_locked()
    led["lanes"]["grad"]["open"].pop("GHOST3pump", None)
    PA._save_locked(led)
_ci_mod = G._close_intents()
_orig_freeze = _ci_mod.freeze
def _boom2(*a_, **k_): raise OSError("freeze write failed (test)")
_ci_mod.freeze = _boom2
with G._ledger_lock():
    d = G._read(); pos = d["positions"]["GHOST3pump"]
    G._close_leg(pos, "a", pos["entry_price"] * 2.0, "test")
_ci_mod.freeze = _orig_freeze
time.sleep(1.1)   # a fresh time-derived id would differ now (ids are second-granular)
pos.pop("pending_exit", None)     # as if the process died before the lane persisted pending_exit: the intent id must still be stable
with G._ledger_lock():
    G._close_leg(pos, "a", pos["entry_price"] * 2.0, "test")
_evs = [__import__("json").loads(l) for l in open(_ci_mod.journal_path("grad")).read().splitlines() if l.strip()]
_ints = [e for e in _evs if e.get("ev") == "intent" and "GHOST3pump" in e.get("intent_id", "")]
_frz = [e for e in _evs if e.get("ev") == "frozen" and "GHOST3pump" in e.get("intent_id", "")]
check("freeze-only failure then retry (even with pending_exit lost): ONE distinct intent id, frozen once",
      len({e["intent_id"] for e in _ints}) == 1 and len(_frz) == 1 and (pos.get("pending_exit") or {}).get("why") == "tranche_credit_divergence")
G._ENTRY_BLOCKED = False
_unpatch(ps)

# ---- rate-limited operator diagnostics (disclosed ADDITION): a pinned refusal warns once per window, not every poll ----
import logging as _lg
class _Cap(_lg.Handler):
    def __init__(s): super().__init__(); s.n = 0
    def emit(s, r):
        if "tranche credit" in r.getMessage() and "refused" in r.getMessage(): s.n += 1
_cap = _Cap(); G.logger.addHandler(_cap)
G._WARN_LAST.clear()
ps, _mv, _ms = _fresh()
okw, _w, _f = G.consider_candidate(_cand(mint="WARN11pump"))
_orig_credit = PA.credit
PA.credit = lambda *a_, **k_: (False, "account busy (test)")
try:
    with G._ledger_lock():
        d = G._read(); pos = d["positions"]["WARN11pump"]
        for _i in range(5):
            G._close_leg(pos, "a", pos["entry_price"] * 2.0, "test")
        _w1 = _cap.n
        G._WARN_LAST.clear()
        G._close_leg(pos, "a", pos["entry_price"] * 2.0, "test")
finally:
    PA.credit = _orig_credit
check("pinned refusal: 5 polls log exactly ONE warning; the state is still persisted every poll",
      _w1 == 1 and (pos.get("pending_exit") or {}).get("why") == "tranche_credit_refused")
check("after the window the warning repeats (not silenced forever)", _cap.n == 2)
G.logger.removeHandler(_cap)
with G._ledger_lock():
    for _k in ("a", "b", "c"):
        G._close_leg(pos, _k, pos["entry_price"] * 2.0, "test")
    G._finalize(d, "WARN11pump", pos, "test"); G._write(d)
_unpatch(ps)

# ---- NON-SIM TWAP per-tranche parity + 2-of-3 crash replay (disclosed ADDITION) ----
import copy as _copy
_old_tw = G.TWAP_SLICES; G.TWAP_SLICES = 3
try:
    ps, _mv, _ms = _fresh()
    okt, _w, _f = G.consider_candidate(_cand(mint="TWAP11pump"))
    check("fixture: TWAP entry opened (non-sim)", okt)
    _T = G._load()["positions"]["TWAP11pump"]["tokens"]
    def _acct(m):
        r = [p for p in PA.snapshot()["lanes"]["grad"]["positions"] if p["mint"] == m]
        return r[0] if r else None
    _a0 = _acct("TWAP11pump"); _cs = PA.snapshot()["cash"]
    with G._ledger_lock():
        d = G._read(); pos = d["positions"]["TWAP11pump"]
        n1 = G._close_twap_slice(pos, pos["entry_price"] * 2.0)
        G._write(d)
        persisted = _copy.deepcopy(pos)                         # slice 1 durable in the lane
        n2 = G._close_twap_slice(pos, pos["entry_price"] * 2.0) # slice 2: account credited, lane write LOST
    _a2 = _acct("TWAP11pump"); _c2 = PA.snapshot()["cash"]
    check("non-sim TWAP: each slice moved shared cash by exactly its net (%.6f vs %.6f)" % (_c2 - _cs, n1 + n2),
          n1 > 0 and n2 > 0 and abs((_c2 - _cs) - (n1 + n2)) < 1e-5)
    check("non-sim TWAP: account units shrank by 2/3 after two slices", abs(_a2["qty"] - _T / 3) < 1e-6 * _T)
    with G._ledger_lock():
        d = G._read(); pos = d["positions"]["TWAP11pump"]       # the on-disk (slice 1 only) state
        okr = G._reconcile_credited_legs(pos)
        check("crash replay: reconcile finishes slice 2 from the receipt, no extra cash (done %s)" % pos["twap"]["done"],
              okr is True and pos["twap"]["done"] == 2 and abs(PA.snapshot()["cash"] - _c2) < 1e-9)
        # stop-out at a different price sells only the last third, once; slices 1+2 not re-credited
        nr = G._close_remaining(pos, pos["entry_price"] * 0.4, "hard_stop")
        _pre_fin = (PA.snapshot()["cash"] - _c2, _acct("TWAP11pump"))   # BEFORE the final close runs
        G._finalize(d, "TWAP11pump", pos, "hard_stop"); G._write(d)
    check("non-sim TWAP: the remainder moved the account AT ITS SALE (before the final close): cash +%.6f, position closed" % _pre_fin[0],
          abs(_pre_fin[0] - nr) < 1e-5 and _pre_fin[1] is None)
    check("non-sim TWAP: remainder credited once for the last third only (net %.6f), account closed, no residue" % nr,
          nr > 0 and abs((PA.snapshot()["cash"] - _c2) - nr) < 1e-5
          and _acct("TWAP11pump") is None and "TWAP11pump" not in G._load()["positions"])
    _unpatch(ps)
finally:
    G.TWAP_SLICES = _old_tw

# ---- orphan close: position predating the flag ----------------------
ps, _mv, _ms = _fresh()
opened3, _w, _f = G.consider_candidate(_cand(mint="LEGACY1pump"))
check("fixture: legacy entry opened with flag on", opened3)
# Simulate a position opened before the flag existed by removing
# its shared-account record, then closing.
with PA._lock():
    led, healthy, _degraded = PA._load_locked()
    led["lanes"]["grad"]["open"].pop("LEGACY1pump", None)
    PA._save_locked(led)
_close_open("LEGACY1pump", mult=1.5)
journal = open(PA.JOURNAL_PATH).read()
check("orphan close: lane settles anyway, account journals it",
      "credit_orphan" in journal)
# TEST EDIT (disclosed): this fixture removes the account record of a DEBITED position, which the account's own
# journal rule (pos_ever_existed) classes as a divergence, not a pre-account orphan. A refused per-tranche credit
# for it now freezes immediately and blocks entries (stricter than before, which only froze at the next restart).
check("debited-but-absent position: freezes loud and blocks entries in-process (fail closed)",
      G._ENTRY_BLOCKED is True)
G._ENTRY_BLOCKED = False   # section isolation
_unpatch(ps)



# ---- grad write-down: unquotable close settles via the register --
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
os.environ["PAPER_ACCOUNT_GATES_ENABLED"] = "1"  # TEST FIXTURE (mandatory-guard edit): not owner values
os.environ["PAPER_ACCOUNT_MAX_DAILY_LOSS_USD"] = "1000"
os.environ["PAPER_ACCOUNT_MAX_DRAWDOWN_PCT"] = "90"
os.environ["PAPER_ACCOUNT_MAX_EXPOSURE_PCT"] = "100"
os.environ["PAPER_ACCOUNT_MAX_SIMULTANEOUS"] = "1000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_POSITIONS"] = "1000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = "100000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_TOTAL_EXPOSURE_USD"] = "1000000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_TOKEN_EXPOSURE_USD"] = "1000000"  # TEST-FIXTURE value, not owner
importlib.reload(PA)
ps, _mv, _ms = _fresh()
opened, why, _f = G.consider_candidate(_cand(mint="WD111pump"))
check("wd fixture: entry opened", opened)
snap0 = PA.snapshot()
cash0, pnl0 = snap0["cash"], snap0["realized_pnl"]
# Drive the lane's real close path the way poll_time_exits does
# for a 24h-unquotable position (directly, to keep the test
# deterministic).
with G._ledger_lock():
    d = G._read()
    pos = d["positions"]["WD111pump"]
    G._close_remaining(pos, 0.0, "time_stop_24h_unquotable",
                       pkind="write_down_zero")
    rec_wd = G._finalize(d, "WD111pump", pos,
                         "time_stop_24h_unquotable")
    G._write(d)
wd = [rec_wd] if rec_wd else []
check("grad unquotable close fires", len(wd) == 1)
if wd:
    check("grad close reason is the unquotable variant",
          wd[0]["closed_reason"].endswith("unquotable"))
    check("grad write-down close labeled write_down_zero",
          wd[0].get("exit_price_kind") == "write_down_zero")
    check("grad write-down books no fee on a non-sale",
          float(wd[0]["realized_usd"]) == 0.0)
snap1 = PA.snapshot()
check("grad write-down moved no cash",
      abs(snap1["cash"] - cash0) < 1e-9)
check("grad write-down booked no realized loss in the account",
      abs(snap1["realized_pnl"] - pnl0) < 1e-9)
un = [u for u in snap1.get("unresolved") or []
      if u["mint"] == "WD111pump"]
check("grad written-down units held in the unresolved register "
      "with original cost",
      len(un) == 1 and float(un[0]["basis"]) > 0
      and un[0]["status"] == "held_unresolved")
check("grad write-down left no open account position",
      not snap1["lanes"].get("grad", {}).get("positions"))
_unpatch(ps)


# ---- grad MIXED close: priced tranche + unquotable remainder ----
# The sold units settle as a sale; the written-down units + their
# original cost move to the unresolved register (parent 2026-11-22).
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
os.environ["PAPER_ACCOUNT_GATES_ENABLED"] = "1"  # TEST FIXTURE (mandatory-guard edit): not owner values
os.environ["PAPER_ACCOUNT_MAX_DAILY_LOSS_USD"] = "1000"
os.environ["PAPER_ACCOUNT_MAX_DRAWDOWN_PCT"] = "90"
os.environ["PAPER_ACCOUNT_MAX_EXPOSURE_PCT"] = "100"
os.environ["PAPER_ACCOUNT_MAX_SIMULTANEOUS"] = "1000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_POSITIONS"] = "1000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = "100000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_TOTAL_EXPOSURE_USD"] = "1000000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_TOKEN_EXPOSURE_USD"] = "1000000"  # TEST-FIXTURE value, not owner
importlib.reload(PA)
for _f2 in (PA.LEDGER_PATH, PA.LEDGER_PATH + ".bak",
            PA.JOURNAL_PATH):
    if os.path.exists(_f2):
        os.remove(_f2)
importlib.reload(PA)
ps, _mv, _ms = _fresh()
opened, why, _f = G.consider_candidate(_cand(mint="MIX111pump"))
check("mix fixture: entry opened (%s)" % (why or ""), opened)
snap0 = PA.snapshot()
cash0 = snap0["cash"]
with G._ledger_lock():
    d = G._read()
    pos = d["positions"]["MIX111pump"]
    toks = pos["tokens"]
    frac_a = pos["legs"]["a"]["frac"]
    G._close_leg(pos, "a", pos["entry_price"] * 2.0, "test")
    G._close_remaining(pos, 0.0, "time_stop_24h_unquotable",
                       pkind="write_down_zero")
    rec_mix = G._finalize(d, "MIX111pump", pos,
                          "time_stop_24h_unquotable")
    G._write(d)
check("mixed close labeled mixed", rec_mix
      and str(rec_mix.get("exit_price_kind") or "").startswith(
          "mixed:"))
snap1 = PA.snapshot()
un = [u for u in snap1.get("unresolved") or []
      if u["mint"] == "MIX111pump"]
check("mixed close: written-down units in the register",
      len(un) == 1 and abs(float(un[0]["qty"])
                           - toks * (1 - frac_a)) / toks < 0.02)
check("mixed close: register keeps the written-down cost",
      len(un) == 1
      and abs(float(un[0]["basis"]) - (1 - frac_a)) < 0.02)
check("mixed close: priced tranche credited",
      snap1["cash"] > cash0)
if snap1["lanes"].get("grad", {}).get("positions"):
    print("DEBUG positions:", snap1["lanes"]["grad"]["positions"])
    print("DEBUG journal:", open(PA.JOURNAL_PATH).read()[-800:])
check("mixed close: nothing left open in the account",
      not snap1["lanes"].get("grad", {}).get("positions"))
check("mixed close: credited proceeds quarantine-checked by kind",
      abs(snap1["cash_spendable"] - snap1["cash"]) < 1e-9
      or snap1["cash_estimated"] > 0)
_unpatch(ps)

# ---- TRUE pre-account orphan on the tranche path (disclosed ADDITION): the account journal holds NO history for the
# position (never debited), so a refused tranche credit converges (leg proceeds lane-side), it does NOT freeze ----
ps, _mv, _ms = _fresh()
G._ENTRY_BLOCKED = False
oko, _w, _f = G.consider_candidate(_cand(mint="ORPH11pump"))
_pid_o = G._load()["positions"]["ORPH11pump"]["pa_pos_id"]
with PA._lock():
    led, healthy, _deg = PA._load_locked()
    led["lanes"]["grad"]["open"].pop("ORPH11pump", None)
    PA._save_locked(led)
_lines = [l for l in open(PA.JOURNAL_PATH).read().splitlines() if l.strip()]
open(PA.JOURNAL_PATH, "w").write("\n".join(l for l in _lines if _pid_o not in l) + "\n")   # erase the position's account history
check("fixture: the account journal holds no history for the orphan position", not PA.pos_ever_existed(pos_id=_pid_o, lane="grad", mint="ORPH11pump"))
with G._ledger_lock():
    d = G._read(); pos = d["positions"]["ORPH11pump"]
    ro = G._close_leg(pos, "a", pos["entry_price"] * 2.0, "test")
check("true orphan: the tranche converges (leg done lane-side, not frozen, entries not blocked)",
      ro > 0 and pos["legs"]["a"].get("done") is True and G._ENTRY_BLOCKED is False
      and (pos.get("pending_exit") or {}).get("why") not in ("tranche_credit_divergence", "tranche_credit_refused"))
G._ENTRY_BLOCKED = False
_unpatch(ps)

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL PAPER-ACCOUNT WIRING TESTS PASSED")
