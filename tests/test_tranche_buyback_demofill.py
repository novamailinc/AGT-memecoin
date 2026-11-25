#!/usr/bin/env python3
"""TRANCHE BUYBACK through the REAL quote-simulation fill path (DemoFiller) with the REAL shared paper account.

ENGINEERING FIXTURE TEST, not real-provider acceptance: the quote source is a canned-ratio FakeJup (documented V1 ratio form),
the account/gates run with TEST-FIXTURE limits (tests/_guard_fixture), and NONE of these values are owner operating limits.
Engine = the grad module instance (maybe_buyback only uses the engine API; the tranche engine is this same code exec'd with lane
overrides, LANE here is 'grad'). The real DemoFiller, real sim executor, real paper_account, real maybe_buyback and the real
exit/close lifecycle run; named fault injections (labelled FAULT) are applied to the real account/ledger writer.
Harness copied from tests/test_grad_demo_exec.py (disclosed). No network."""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import json
import os
import sys
import tempfile
import time
from unittest.mock import patch

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import grad as G
from daemon.launch_scanner import killswitch as KS
from daemon.launch_scanner import sources as SRC

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---------------- killswitch registration ----------------
check("killswitch: grad lane registered (window/min 100, halt)",
      "grad" in KS.CFG and KS.CFG["grad"]["window"] == 100
      and KS.CFG["grad"]["min_trades"] == 100
      and KS.CFG["grad"]["halt"] is True)
check("grad: verdict sample constant is 100",
      G.VERDICT_SAMPLE_CLOSES == 100)

# halt_lane API on an isolated killswitch state file
_ks_tmp = tempfile.mkdtemp(prefix="grad-ks-")
KS.STATE_DIR = _ks_tmp
KS.STATE_FILE = os.path.join(_ks_tmp, "state.json")
KS.SHADOW_FILE = os.path.join(_ks_tmp, "shadow.json")
KS.LOCK_FILE = os.path.join(_ks_tmp, "state.lock")
r = KS.halt_lane("grad", "unit test halt")
check("killswitch: halt_lane halts + records reason",
      r["action"] == "HALTED" and KS.is_halted("grad")
      and KS._load()["lanes"]["grad"]["halt_reason"] == "unit test halt")
r2 = KS.halt_lane("grad", "second halt ignored")
check("killswitch: halt_lane idempotent",
      r2["action"] == "halted"
      and KS._load()["lanes"]["grad"]["halt_reason"] == "unit test halt")
KS.reenable("grad", "unit test reset")
check("killswitch: grad not halted after human reenable",
      not KS.is_halted("grad"))


# ---------------- harness ----------------
TMPD = tempfile.mkdtemp(prefix="grad-test-")
G.LEDGER_PATH = os.path.join(TMPD, "grad_ledger.json")


class FakeKS:
    """Killswitch semantics for tests: halt flags + verdict halts."""

    def __init__(self):
        self.halted = set()
        self.recorded = []
        self.halt_reasons = {}

    def is_halted(self, lane):
        return lane in self.halted

    def gate(self, lane):
        return not self.is_halted(lane)

    def record_trade(self, lane, pnl, ts=None):
        self.recorded.append(pnl)

    def check_lane(self, lane):
        return {"lane": lane}

    def halt_lane(self, lane, reason=""):
        if lane not in self.halted:
            self.halted.add(lane)
            self.halt_reasons[lane] = reason
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
    FAKE_KS.halted = set()
    FAKE_KS.recorded = []
    FAKE_KS.halt_reasons = {}
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


_ks_iso = tempfile.mkdtemp(prefix="grad-ks-iso-")
KS.STATE_DIR = _ks_iso
KS.STATE_FILE = os.path.join(_ks_iso, "state.json")
KS.SHADOW_FILE = os.path.join(_ks_iso, "shadow.json")
KS.LOCK_FILE = os.path.join(_ks_iso, "state.lock")
check("killswitch isolation: all four KS paths under the temp dir",
      all(str(getattr(KS, n, "")).startswith(_ks_iso) for n in ("STATE_DIR", "STATE_FILE", "LOCK_FILE", "SHADOW_FILE")))
# DISCLOSED: guard fixture (TEST-FIXTURE limits, not owner values), same pattern as test_watcher_demo_exec.
import importlib as _il, tempfile as _tf
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
import tests._guard_fixture as _GF
os.environ["FOREX_AGENT_HOME"] = _tf.mkdtemp(prefix="shared-acct-home-")
os.environ["PAPER_ACCOUNT_START"] = "1000"
os.environ.pop("PAPER_ACCOUNT_LANE_CAP", None)
os.environ.pop("PAPER_ACCOUNT_TRADE_SIZE_USD", None)
import daemon.launch_scanner.paper_account as _pa
_pa = _il.reload(_pa)
_GF.install(_pa)
os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = "100000"
_ps, _mv, _ms = _patch_all()
TAPES = {}
class _Fk: pass
import importlib as _il2
from unittest.mock import patch
def PA_cash():
    return _il2.import_module("daemon.launch_scanner.paper_account").snapshot()["cash"]
def _reset_account():
    os.environ["FOREX_AGENT_HOME"] = _tf.mkdtemp(prefix="shared-acct-home-")
    os.environ["PAPER_ACCOUNT_START"] = "100"
    os.environ.pop("PAPER_ACCOUNT_TRADE_SIZE_USD", None)
    global _pa
    _pa = _il.reload(_pa)
    _GF.install(_pa)
    G._RECOVERY_DONE = False
    G._ENTRY_BLOCKED = False
    if os.path.exists(G.LEDGER_PATH):
        os.remove(G.LEDGER_PATH)
    G.ensure_ledger()
def demo_card(mint):
    pass
    opened, why, final = G.consider_candidate(_cand(mint=mint), time.time())
    return bool(opened), why
def prices(seq):
    it = iter(seq)
    def fn(chain, mint, spot_index=None):
        try: return next(it)
        except StopIteration: return seq[-1]
    return fn
os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = "100000"
from daemon.launch_scanner import demo_fill as DF
from broker import BrokerError, BROKER_UNAVAILABLE
SOLM = "So11111111111111111111111111111111111111112"
class FakeJup:
    def __init__(self): self.calls = []; self.fail_sell = False; self.mult = 1.0; self.bmult = 1.0
    def get_quote(self, inp, out, amount, slippage_bps=100):
        self.calls.append((inp, out, amount))
        if inp != SOLM and self.fail_sell:
            raise BrokerError(BROKER_UNAVAILABLE, "no route", detail={"http_status": 400})
        # buy: price 0.00001 SOL/token (dec 6): tokens = amount_lamports/1e9/1e-5 ; sell mirrors with self.mult
        if inp == SOLM: o = int(amount / 1e9 / 0.00001 * 1e6 * self.bmult)
        else: o = int(amount / 1e6 * 0.00001 * self.mult * 1e9)
        return {"outAmount": str(o), "priceImpactPct": 0.004, "routePlan": [{"swapInfo": {"label": "FakeAMM"}}]}

JUP = FakeJup()
def new_filler():
    return DF.DemoFiller(JUP, sol_usd_fn=lambda: (100.0, time.time()), decimals_fn=lambda m: 6,
                         journal_path=os.path.join(tempfile.mkdtemp(prefix="dfj-"), "j.json"), seed=1, hourly_vol=0.0)
FILLER = [None]
def setup(cap="2.0"):
    _reset_account()
    os.environ["AGT_EXECUTION_MODE"] = "jupiter_quote_sim"
    if cap is None: os.environ.pop("PAPER_ACCOUNT_MAX_PRICE_IMPACT_PCT", None)
    else: os.environ["PAPER_ACCOUNT_MAX_PRICE_IMPACT_PCT"] = cap
    FILLER[0] = new_filler(); JUP.calls.clear(); JUP.fail_sell = False; JUP.mult = 1.0; JUP.bmult = 1.0
    G._demo_filler = lambda: FILLER[0]
    return ()

import time as _tm
from daemon.launch_scanner import tranche as T
import daemon.launch_scanner.paper_account as _PAm

def PAm():
    return _il2.import_module("daemon.launch_scanner.paper_account")

def lane_pos(mint):
    return (G._read().get("positions") or {}).get(mint) or {}

def ready(mint):
    """Real entry through the real DemoFiller, then leg A banked at 2x through the quote sim (proceeds >> a buyback cost)."""
    setup()
    ok, why = demo_card(mint)
    pos = G.open_positions()[mint]; entry = pos["entry_price"]
    JUP.mult = 2.0
    G.poll_exits(price_fn=prices([entry * 2.0]))
    JUP.mult = 1.0
    p = G.open_positions()[mint]
    return p, entry, ok

def run_bb(mint, price, p_is_sim=True):
    T._LAST_BUYBACK_CHECK = 0.0
    with patch.object(G, "quote_price", return_value=price):
        return T.maybe_buyback(G, _tm.time())

def bop(mint, pid):
    return "%s:buyback:%s:%s" % (G.LANE, mint, pid)

def acct_lot_qty(pid):
    snap = PAm().snapshot()
    return sum(float(q.get("qty") or 0.0) for q in ((snap.get("lanes") or {}).get(G.LANE) or {}).get("positions") or []
               if str(q.get("pos_id")) == str(pid))

def fills_for(pid, mint):
    pre = bop(mint, pid) + ":fill:"
    return FILLER[0]._locked(lambda j: {k: v for k, v in j["fills"].items() if k.startswith(pre)})

# ---- S1: success + COMPLETE post-buyback lifecycle (account add, lane legs, provenance, exits, close, conservation) ----
mint = "BB1pump"
p, entry, ok = ready(mint)
pid = p["pa_pos_id"]; q0 = acct_lot_qty(pid); tokens0 = p["tokens"]
check("S1 setup: sim entry opened and leg A banked (a done, realized>0)", ok and p["legs"]["a"]["done"] and p["realized_usd"] > 0)
c_pre = PA_cash(); realized_pre = p["realized_usd"]; nq = len(JUP.calls)
out = run_bb(mint, entry * 1.3)
lp = lane_pos(mint); ef = (lp.get("entry_features") or {}).get("buyback") or {}
fl = fills_for(pid, mint)
check("S1 buyback executed once (%d) with ONE journaled fill under a fresh op id" % len(out), len(out) == 1 and len(fl) == 1)
f = list(fl.values())[0] if fl else {}
check("S1 a real quote was requested for the buyback (calls %d -> %d)" % (nq, len(JUP.calls)), len(JUP.calls) == nq + 1 and JUP.calls[-1][0] == SOLM)
ad = PAm().applied_add(bop(mint, pid))
check("S1 account add applied with the FILL's own debit and qty (stake %.6f vs %.6f)" % ((ad or {}).get("stake", -1), f.get("total_debit_usd", -2)),
      ad is not None and abs(ad["stake"] - f["total_debit_usd"]) < 1e-9 and abs(ad["qty"] - f["qty_tokens"]) < 1e-9)
check("S1 cash fell by exactly the fill debit (%.9f)" % (c_pre - PA_cash()), abs((c_pre - PA_cash()) - f["total_debit_usd"]) < 1e-9)
check("S1 account lot qty rose by the fill qty", abs(acct_lot_qty(pid) - (q0 + f["qty_tokens"])) < 1e-9)
_lgs = lp["legs"]; _bbq = sum(float(v.get("buyback_qty") or 0.0) for v in _lgs.values())
check("S1 lane buyback_qty over open legs == fill qty == buyback_qty_total (%.6f)" % _bbq,
      abs(_bbq - f["qty_tokens"]) < 1e-9 and abs(lp["buyback_qty_total"] - f["qty_tokens"]) < 1e-9 and not float(_lgs["a"].get("buyback_qty") or 0.0))
check("S1 lane realized_usd fell by the fill debit and buyback_used set", abs((realized_pre - lp["realized_usd"]) - f["total_debit_usd"]) < 1e-6 and lp.get("buyback_used") is True)
check("S1 lane original tokens unchanged (original entry quantity semantics)", abs(lp["tokens"] - tokens0) < 1e-12)
_pv = ef.get("provenance") or {}
check("S1 durable evidence on the lane: fill op, source, signature, fee, provenance with percent_v2 stamp and RAW 0.004",
      ef.get("fill_op") == list(fl.keys())[0] and ef.get("execution_source") == "jupiter_quote_sim" and ef.get("synthetic_signature") is True
      and _pv.get("price_impact_unit") == "percent_v2" and _pv.get("price_impact_raw") == 0.004 and abs(_pv.get("price_impact_pct") - 0.4) < 1e-9
      and ef.get("fee_usd") == f.get("fee_usd") and not ef.get("adopted_from_account"))
check("S1 intent record: state applied, fill_op equals the journaled op", (lp.get("buyback_intent") or {}).get("state") == "applied" and lp["buyback_intent"]["fill_op"] == ef.get("fill_op"))
nq = len(JUP.calls); c1 = PA_cash()
out2 = run_bb(mint, entry * 1.3)
check("S1 replay: a second cycle does nothing (no buyback, no quote, no cash move)", out2 == [] and len(JUP.calls) == nq and abs(PA_cash() - c1) < 1e-12)
# lifecycle: sell the remaining legs (legs b and c include their buyback share) until closed
JUP.mult = 0.05
for _ in range(8):
    G.poll_exits(price_fn=prices([entry * 0.05]))
closed = "BB1pump" not in G.open_positions()
rec = (G._read().get("closes") or [])[-1] if (G._read().get("closes") or []) else {}
sells = FILLER[0]._locked(lambda j: [v for k, v in j["fills"].items() if v.get("side") == "sell" and v.get("status") == "filled" and (":%s:" % mint) in k])
check("S1 lifecycle: position fully closed through the quote sim (%d sells)" % len(sells), closed and len(sells) >= 3)
check("S1 lifecycle: no stuck state (no pending_exit / settlement_pending on the close record)", closed and not rec.get("pending_exit") and not rec.get("settlement_pending"))
_sold_qty = sum(float(s_["qty_tokens"]) for s_ in sells)
check("S1 lifecycle: units sold == original + bought (%.6f vs %.6f)" % (_sold_qty, tokens0 + f["qty_tokens"]), abs(_sold_qty - (tokens0 + f["qty_tokens"])) < 1e-6)
check("S1 lifecycle: account lot fully consumed (flat)", acct_lot_qty(pid) < 1e-6)
c_end = PA_cash(); _nets = sum(s_["net_proceeds_usd"] for s_ in sells)
check("S1 conservation: account cash delta == sum(sim sell nets) - entry stake - buyback stake (%.6f)" % (c_end - 100.0),
      abs((c_end - 100.0) - (_nets - 1.0 - f["total_debit_usd"])) < 1e-5)
check("S1 conservation: lane pnl_usd == the account's cash delta (%.6f vs %.6f)" % (rec.get("pnl_usd", 0), c_end - 100.0),
      abs(rec.get("pnl_usd", 1e9) - (c_end - 100.0)) < 1e-4)
JUP.mult = 1.0

# ---- S2: cap breach on the QUOTE refuses; nothing applied; intent explains the (absent) fill ----
mint = "BB2pump"
p, entry, ok = ready(mint); pid = p["pa_pos_id"]; c0 = PA_cash(); nq = len(JUP.calls)
os.environ["PAPER_ACCOUNT_MAX_PRICE_IMPACT_PCT"] = "0.1"   # quote impact is 0.4 percent
out = run_bb(mint, entry * 1.3); lp = lane_pos(mint)
check("S2 refused (no buyback), a quote WAS asked (%d -> %d), cash/account untouched, no fill journaled" % (nq, len(JUP.calls)),
      out == [] and len(JUP.calls) == nq + 1 and abs(PA_cash() - c0) < 1e-12 and PAm().applied_add(bop(mint, pid)) is None and not fills_for(pid, mint))
check("S2 lane: buyback_used False, intent names the fill op with state refused:no_fill", lp.get("buyback_used") is not True
      and (lp.get("buyback_intent") or {}).get("state") == "refused:no_fill" and ":fill:" in (lp["buyback_intent"].get("fill_op") or ""))
check("S2 failed attempt counted by the filler", FILLER[0].failed_attempts() >= 1)

# ---- S3: owner cap unset / NaN / inf / zero / negative: refused with no quote ----
for label, val in (("unset", None), ("NaN", "nan"), ("inf", "inf"), ("zero", "0"), ("negative", "-1")):
    mint = "BB3%spump" % label[:2]
    p, entry, ok = ready(mint); pid = p["pa_pos_id"]; c0 = PA_cash(); nq = len(JUP.calls)
    if val is None: os.environ.pop("PAPER_ACCOUNT_MAX_PRICE_IMPACT_PCT", None)
    else: os.environ["PAPER_ACCOUNT_MAX_PRICE_IMPACT_PCT"] = val
    out = run_bb(mint, entry * 1.3)
    check("S3 cap %s: refused, no quote for the buyback, no add, no cash move" % label,
          out == [] and len(JUP.calls) == nq and PAm().applied_add(bop(mint, pid)) is None and abs(PA_cash() - c0) < 1e-12)

# ---- S4: account refuses AFTER a real fill: orphan fill is explained; retry uses a NEW fill op; one debit ----
mint = "BB4pump"
p, entry, ok = ready(mint); pid = p["pa_pos_id"]; c0 = PA_cash()
_real_add = _PAm.add_to_position
with patch.object(_PAm, "add_to_position", lambda *a, **k: (False, "declined (test)")):
    out = run_bb(mint, entry * 1.3)
lp = lane_pos(mint); fl1 = fills_for(pid, mint); fop1 = (lp.get("buyback_intent") or {}).get("fill_op")
check("S4 FAULT account refuses after the fill: no buyback, cash untouched, ONE journaled fill (orphan, no account effect)",
      out == [] and abs(PA_cash() - c0) < 1e-12 and len(fl1) == 1 and fop1 in fl1 and PAm().applied_add(bop(mint, pid)) is None)
check("S4 intent explains the orphan: state refused:account with the reason", (lp.get("buyback_intent") or {}).get("state") == "refused:account"
      and "declined" in (lp["buyback_intent"].get("why") or "") and lp.get("buyback_used") is not True)
out = run_bb(mint, entry * 1.3); lp = lane_pos(mint); fl2 = fills_for(pid, mint)
fop2 = (lp.get("buyback_intent") or {}).get("fill_op")
check("S4 retry takes a NEW fill op (%s != %s), two journaled fills, history keeps the first intent" % (fop1[-8:], str(fop2)[-8:]),
      len(out) == 1 and fop2 != fop1 and len(fl2) == 2 and (lp.get("buyback_attempts") or [{}])[-1].get("fill_op") == fop1)
_f2 = fl2[fop2]
check("S4 exactly ONE account debit (the retry's fill), equal to its own debit", abs((c0 - PA_cash()) - _f2["total_debit_usd"]) < 1e-9
      and abs(PAm().applied_add(bop(mint, pid))["qty"] - _f2["qty_tokens"]) < 1e-9)

# ---- S5: crash AFTER the account add, BEFORE the lane write; recovery adopts the account's numbers AND recovers the fill evidence ----
mint = "BB5pump"
p, entry, ok = ready(mint); pid = p["pa_pos_id"]; c0 = PA_cash()
_real_w = G._write
def _w_boom(dd, *a, **k):
    pp = (dd.get("positions") or {}).get(mint) or {}
    if pp.get("buyback_used") is True:
        raise OSError("injected lane write failure AFTER the account add")
    return _real_w(dd, *a, **k)
with patch.object(G, "_write", _w_boom):
    out = run_bb(mint, entry * 1.3)
lp = lane_pos(mint); ad = PAm().applied_add(bop(mint, pid)); fl = fills_for(pid, mint)
check("S5 FAULT: account add applied, lane NOT booked (buyback_used False), intent state filled, one fill journaled",
      out == [] and ad is not None and lp.get("buyback_used") is not True and (lp.get("buyback_intent") or {}).get("state") == "filled" and len(fl) == 1)
nq = len(JUP.calls); c1 = PA_cash()
out = run_bb(mint, entry * 1.3); lp = lane_pos(mint); ef = (lp.get("entry_features") or {}).get("buyback") or {}
f = list(fl.values())[0]
check("S5 recovery adopts: buyback booked once, NO new quote, NO second debit", len(out) == 1 and len(JUP.calls) == nq and abs(PA_cash() - c1) < 1e-12)
check("S5 recovery uses the ACCOUNT's applied stake/qty (== the journaled fill's)", abs(ef.get("cost", -1) - ad["stake"]) < 1e-6 and abs(ef.get("tokens", -1) - ad["qty"]) < 1e-9
      and abs(ad["stake"] - f["total_debit_usd"]) < 1e-9)
check("S5 evidence RECOVERED from the journal via the lane intent (fill_evidence recovered, provenance with percent_v2 stamp, fill op)",
      ef.get("adopted_from_account") is True and ef.get("fill_evidence") == "recovered" and ef.get("fill_op") == list(fl.keys())[0]
      and (ef.get("provenance") or {}).get("price_impact_unit") == "percent_v2")
_bbq = sum(float(v.get("buyback_qty") or 0.0) for v in lp["legs"].values())
check("S5 lane units equal the account add (legs %.6f)" % _bbq, abs(_bbq - ad["qty"]) < 1e-9 and lp.get("buyback_used") is True)

# ---- S6: crash after the add AND the fill evidence is gone: adoption states the loss explicitly, account stays authoritative ----
mint = "BB6pump"
p, entry, ok = ready(mint); pid = p["pa_pos_id"]
_real_w = G._write
def _w_boom6(dd, *a, **k):
    pp = (dd.get("positions") or {}).get(mint) or {}
    if pp.get("buyback_used") is True:
        raise OSError("injected lane write failure AFTER the account add")
    return _real_w(dd, *a, **k)
with patch.object(G, "_write", _w_boom6):
    run_bb(mint, entry * 1.3)
dd = G._read(); dd["positions"][mint]["buyback_intent"]["fill_op"] = "FAULT:evidence-lost"; _real_w(dd)   # FAULT: the intent no longer names a journaled fill
out = run_bb(mint, entry * 1.3); ef = (lane_pos(mint).get("entry_features") or {}).get("buyback") or {}
check("S6 adoption without evidence: booked from the account's numbers, fill_evidence says fill_missing, NO provenance claimed",
      len(out) == 1 and ef.get("adopted_from_account") is True and ef.get("fill_evidence") == "fill_missing" and "provenance" not in ef)

# ---- S7: position that is not a quote-sim position stays refused (the interim always-refuse gate) ----
mint = "BB7pump"
p, entry, ok = ready(mint); pid = p["pa_pos_id"]; c0 = PA_cash(); nq = len(JUP.calls)
with patch.object(G, "_is_sim", return_value=False):
    out = run_bb(mint, entry * 1.3)
check("S7 non-sim position: refused, no quote, no add, no cash move", out == [] and len(JUP.calls) == nq and PAm().applied_add(bop(mint, pid)) is None and abs(PA_cash() - c0) < 1e-12)
os.environ.pop("AGT_EXECUTION_MODE", None)
print("fails: %d" % len(fails)); sys.exit(1 if fails else 0)
