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
# SOURCE-VERIFIED cash contract: paper_account._apply_add stores cash = round(cash - stake, 6) (paper_account.py:890), so a before/after cash
# delta equals the full-precision debit only to the 6 dp rounding (<= 5e-7 per rounded value). CASH_TOL applies ONLY to cash-vs-debit deltas;
# every qty, lane and journal oracle keeps its original tolerance.
CASH_TOL = 1e-6
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

def dump(label, mint, pid, *extra):
    """DIAGNOSTIC ONLY (no assertion): print the operands behind the checks, from the persisted account/lane/journal records."""
    snap = PAm().snapshot(); lp = lane_pos(mint) or {}
    led = (G._read().get("closes") or [])[-1:] or [{}]
    print("DIAG %s cash=%r lot_qty=%r account_ops=%r" % (label, snap.get("cash"), acct_lot_qty(pid), {k: (v.get("event"), v.get("stake"), v.get("qty"), v.get("realized")) for k, v in (PAm()._ledger_ops() if hasattr(PAm(), "_ledger_ops") else {}).items()}))
    print("DIAG %s lane intent=%r fill_ops=%r ef.buyback=%r" % (label, lp.get("buyback_intent"), lp.get("buyback_fill_ops"), {k: v for k, v in ((lp.get("entry_features") or {}).get("buyback") or {}).items() if k != "provenance"}))
    print("DIAG %s fills=%r" % (label, {k[-40:]: {x: v.get(x) for x in ("side", "status", "price_usd", "qty_tokens", "total_debit_usd", "net_proceeds_usd", "fee_usd")} for k, v in FILLER[0]._locked(lambda j: dict(j["fills"])).items()}))
    print("DIAG %s last_close=%r" % (label, {x: led[0].get(x) for x in ("pnl_usd", "realized_usd", "pending_exit", "settlement_pending", "closed_reason")}))
    for e in extra: print("DIAG %s %s" % (label, e))

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
check("S1 cash fell by exactly the fill debit (%.9f)" % (c_pre - PA_cash()), abs((c_pre - PA_cash()) - f["total_debit_usd"]) < CASH_TOL)
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
_pre_keys = set(FILLER[0]._locked(lambda j: list(j["fills"].keys()))); c_mid = PA_cash()
for _ in range(8):
    G.poll_exits(price_fn=prices([entry * 0.05]))
closed = "BB1pump" not in G.open_positions()
rec = (G._read().get("closes") or [])[-1] if (G._read().get("closes") or []) else {}
sells = FILLER[0]._locked(lambda j: [v for k, v in j["fills"].items() if v.get("side") == "sell" and v.get("status") == "filled" and (":%s:" % mint) in k])
# PLAN-AWARE lifecycle (replaces the unsupported ">=3 sells" count: the persisted journal shows legs b and c are sold in ONE remaining-fill):
_closes1 = [c for c in (G._read().get("closes") or []) if c.get("mint") == mint]
_new = FILLER[0]._locked(lambda j: {k: v for k, v in j["fills"].items() if k not in _pre_keys and v.get("side") == "sell" and v.get("status") == "filled" and (":%s:" % mint) in k})
check("S1 lifecycle: position closed EXACTLY ONCE (%d close records) and gone from open positions" % len(_closes1), closed and len(_closes1) == 1)
check("S1 lifecycle: no duplicate sell fill (distinct fill ops %d, sell qty rows %d)" % (len(sells), len(sells)), len({(round(float(x["qty_tokens"]), 9), x.get("price_usd")) for x in sells}) == len(sells) and len(sells) >= 2)
check("S1 lifecycle: POSITIVE actual remaining credit: the final sell(s) net %.7f > 0 and the account cash rose by it (%.7f), within the 6 dp cash contract" % (sum(v["net_proceeds_usd"] for v in _new.values()), PA_cash() - c_mid),
      bool(_new) and sum(v["net_proceeds_usd"] for v in _new.values()) > 0 and abs((PA_cash() - c_mid) - sum(v["net_proceeds_usd"] for v in _new.values())) < CASH_TOL)
check("S1 lifecycle: no stuck state (no pending_exit / settlement_pending on the close record)", closed and not rec.get("pending_exit") and not rec.get("settlement_pending"))
_sold_qty = sum(float(s_["qty_tokens"]) for s_ in sells)
check("S1 lifecycle: units sold == original + bought (%.6f vs %.6f)" % (_sold_qty, tokens0 + f["qty_tokens"]), abs(_sold_qty - (tokens0 + f["qty_tokens"])) < 1e-6)
check("S1 lifecycle: account lot fully consumed (flat)", acct_lot_qty(pid) < 1e-6)
c_end = PA_cash(); _nets = sum(s_["net_proceeds_usd"] for s_ in sells)
check("S1 conservation: account cash delta == sum(sim sell nets) - entry stake - buyback stake (%.6f)" % (c_end - 100.0),
      abs((c_end - 100.0) - (_nets - 1.0 - f["total_debit_usd"])) < 1e-5)
check("S1 conservation: lane pnl_usd == the account's cash delta (%.6f vs %.6f)" % (rec.get("pnl_usd", 0), c_end - 100.0),
      abs(rec.get("pnl_usd", 1e9) - (c_end - 100.0)) < 1e-4)
check("S1 balance (tight): cash delta == lane realized_usd(6dp) - entry stake 1.0 (%.6f vs %.6f), and == journal nets - entry - buyback stake, both within 2e-6 (the 4dp pnl_usd above is a rounded field)"
      % (c_end - 100.0, rec.get("realized_usd", 0) - 1.0),
      abs((c_end - 100.0) - (rec.get("realized_usd", 1e9) - 1.0)) < 2e-6 and abs((c_end - 100.0) - (_nets - 1.0 - f["total_debit_usd"])) < 2e-6)
check("S1 recorded buyback price is the FILL's price_usd (%r vs %r), source fill_price_usd" % (ef.get("price"), f["price_usd"]),
      abs(float(ef.get("price") or -1) - float(f["price_usd"])) < 1e-15 and ef.get("price_source") == "fill_price_usd")
dump("S1-end", mint, pid, "c_pre=%r c_end=%r nets=%r sell_qty=%r" % (c_pre, c_end, _nets, _sold_qty))
from daemon.launch_scanner import close_intents as _CI
check("S1 no close intent left open (the account settle applied)", _CI.pending("grad") == [])
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
# NEGATIVE CONTROL for the same-condition suppression: nothing relevant changed (lane proceeds, account cash) and no explicit retry
# was requested, so the next cycle must NOT quote and must NOT journal another orphan fill.
nq = len(JUP.calls)
out = run_bb(mint, entry * 1.3); lp = lane_pos(mint)
check("S4 suppression: unchanged state after a post-fill refusal => no quote, no new fill, no cash move",
      out == [] and len(JUP.calls) == nq and len(fills_for(pid, mint)) == 1 and abs(PA_cash() - c0) < 1e-12
      and (lp.get("buyback_intent") or {}).get("fill_op") == fop1)
dd = G._read(); dd["positions"][mint]["buyback_retry_requested"] = True; G._write(dd)   # explicit retry request on the lane record
out = run_bb(mint, entry * 1.3); lp = lane_pos(mint); fl2 = fills_for(pid, mint)
fop2 = (lp.get("buyback_intent") or {}).get("fill_op")
check("S4 explicit retry takes a NEW fill op (%s != %s), two journaled fills, history keeps the first intent" % (fop1[-8:], str(fop2)[-8:]),
      len(out) == 1 and fop2 != fop1 and len(fl2) == 2 and (lp.get("buyback_attempts") or [{}])[-1].get("fill_op") == fop1)
_f2 = fl2[fop2]
print("DIAG S4 conjunct_cash c0-cash=%r debit=%r delta=%r conj_qty=%r" % (c0 - PA_cash(), _f2["total_debit_usd"], abs((c0 - PA_cash()) - _f2["total_debit_usd"]), abs(PAm().applied_add(bop(mint, pid))["qty"] - _f2["qty_tokens"])))
check("S4 exactly ONE account debit (the retry's fill), equal to its own debit", abs((c0 - PA_cash()) - _f2["total_debit_usd"]) < CASH_TOL
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
dump("S5", mint, pid)
check("S5 evidence RECOVERED from the journal via the lane intent (fill_evidence recovered, provenance with percent_v2 stamp, fill op)",
      ef.get("adopted_from_account") is True and ef.get("fill_evidence") == "recovered" and ef.get("fill_op") == list(fl.keys())[0]
      and (ef.get("provenance") or {}).get("price_impact_unit") == "percent_v2")
check("S5 recovered adoption records the RECOVERED fill's price_usd, not the model price (%r vs fill %r)" % (ef.get("price"), f["price_usd"]),
      abs(float(ef.get("price") or -1) - float(f["price_usd"])) < 1e-15 and ef.get("price_source") == "fill_price_usd")
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
dump("S6", mint, pid)
check("S6 adoption without evidence: booked from the account's numbers, fill_evidence says fill_missing, NO provenance claimed",
      len(out) == 1 and ef.get("adopted_from_account") is True and ef.get("fill_evidence") == "fill_missing" and "provenance" not in ef)
check("S6 adoption without evidence records NO execution price (None, source named), never the model price (%r / %r)" % (ef.get("price"), ef.get("price_source")),
      ef.get("price") is None and ef.get("price_source") == "unavailable_adopted_without_fill_evidence")

# ---- S7: position that is not a quote-sim position stays refused (the interim always-refuse gate) ----
mint = "BB7pump"
p, entry, ok = ready(mint); pid = p["pa_pos_id"]; c0 = PA_cash(); nq = len(JUP.calls)
with patch.object(G, "_is_sim", return_value=False):
    out = run_bb(mint, entry * 1.3)
check("S7 non-sim position: refused, no quote, no add, no cash move", out == [] and len(JUP.calls) == nq and PAm().applied_add(bop(mint, pid)) is None and abs(PA_cash() - c0) < 1e-12)
# ---- S8: FAULT crash AFTER the fill, BEFORE the add (add raises): intent stays "filled", account untouched; retry re-quotes at a CHANGED price with a new fop ----
mint = "BB8pump"
p, entry, ok = ready(mint); pid = p["pa_pos_id"]; c0 = PA_cash()
def _boom_add(*a, **k): raise RuntimeError("injected crash before add")
with patch.object(_PAm, "add_to_position", _boom_add):
    try: run_bb(mint, entry * 1.3)
    except Exception: pass
lp = lane_pos(mint); fl1 = fills_for(pid, mint); fop1 = (lp.get("buyback_intent") or {}).get("fill_op")
check("S8 FAULT crash before add: orphan fill journaled, account untouched, intent state filled (evidence of the unbooked fill is durable)",
      len(fl1) == 1 and fop1 in fl1 and abs(PA_cash() - c0) < 1e-12 and PAm().applied_add(bop(mint, pid)) is None
      and (lp.get("buyback_intent") or {}).get("state") in ("filled",) and lp.get("buyback_used") is not True)
JUP.bmult = 0.5   # CHANGED requote: the retry gets half the tokens per SOL (canned-ratio fixture)
out = run_bb(mint, entry * 1.3); lp = lane_pos(mint); fl2 = fills_for(pid, mint); fop2 = (lp.get("buyback_intent") or {}).get("fill_op")
JUP.bmult = 1.0
print("DIAG S8 conj: len(out)=%r fop_new=%r len(fl2)=%r qty_match=%r qty_diff=%r debit_delta=%r" % (len(out), fop2 != fop1, len(fl2), abs(PAm().applied_add(bop(mint, pid))["qty"] - fl2[fop2]["qty_tokens"]), abs(fl2[fop2]["qty_tokens"] - fl1[fop1]["qty_tokens"]), abs((c0 - PA_cash()) - fl2[fop2]["total_debit_usd"])))
check("S8 retry after crash: NEW fop, the account add uses the NEW fill's qty (changed requote), the orphan fill is NOT booked",
      len(out) == 1 and fop2 != fop1 and len(fl2) == 2 and abs(PAm().applied_add(bop(mint, pid))["qty"] - fl2[fop2]["qty_tokens"]) < 1e-9
      and abs(fl2[fop2]["qty_tokens"] - fl1[fop1]["qty_tokens"]) > 1e-6 and abs((c0 - PA_cash()) - fl2[fop2]["total_debit_usd"]) < CASH_TOL)

# ---- S9: FAULT post-quote gate refusal AFTER a real fill (second call of the guard refuses): no account effect, intent says why ----
mint = "BB9pump"
p, entry, ok = ready(mint); pid = p["pa_pos_id"]; c0 = PA_cash()
_eg_mod = _il2.import_module("daemon.launch_scanner.entry_guard")
_real_pq = _eg_mod.post_quote_refusal; _n = [0]
def _pq_fault(*a, **k):
    _n[0] += 1
    return _real_pq(*a, **k) if _n[0] == 1 else "FAULT post-quote refusal (test)"
with patch.object(_eg_mod, "post_quote_refusal", _pq_fault):
    out = run_bb(mint, entry * 1.3)
lp = lane_pos(mint); fl = fills_for(pid, mint)
check("S9 FAULT post-quote refusal: no buyback, cash untouched, orphan fill journaled, intent state refused:post_quote_gate (guard called %d times)" % _n[0],
      out == [] and abs(PA_cash() - c0) < 1e-12 and len(fl) == 1 and PAm().applied_add(bop(mint, pid)) is None
      and (lp.get("buyback_intent") or {}).get("state") == "refused:post_quote_gate" and lp.get("buyback_used") is not True)

# ---- S10: CLOSE GUARD. The account REFUSES the final close credit: the executed sale stays recorded, NO clean close is booked,
# nothing is resold on retry, ONE close intent stays open (stable op), and the pending state clears only after a confirmed settle ----
mint = "BB10pump"
p, entry, ok = ready(mint); pid = p["pa_pos_id"]
run_bb(mint, entry * 1.3)
lp = lane_pos(mint); bq = lp.get("buyback_qty_total")
JUP.mult = 0.05
_real_credit = _PAm.credit
_ncred = [0]
_credit_ops = []
def _credit_refuse(*a, **k):
    if str(a[3] if len(a) > 3 else k.get("op_id")).startswith("grad:close:"):
        _ncred[0] += 1
        _credit_ops.append((str(a[3] if len(a) > 3 else k.get("op_id")), a[2] if len(a) > 2 else k.get("proceeds")))
        return (False, "declined (test)")
    return _real_credit(*a, **k)
def _sells(m):
    return FILLER[0]._locked(lambda j: [v for kk, v in j["fills"].items() if v.get("side") == "sell" and v.get("status") == "filled" and (":%s:" % m) in kk])
with patch.object(_PAm, "credit", _credit_refuse):
    for _ in range(4):
        G.poll_exits(price_fn=prices([entry * 0.05]))
    s_a = len(_sells(mint)); br_a = G._read()["bankroll"]; cash_a = PA_cash()
    for _ in range(3):
        G.poll_exits(price_fn=prices([entry * 0.05]))
    s_b = len(_sells(mint)); br_b = G._read()["bankroll"]
lp = lane_pos(mint); sp = lp.get("settlement_pending") or {}
check("S10 refused settle: position NOT booked closed (still open, settlement_pending why account_settle_refused)", bool(lp) and sp.get("why") == "account_settle_refused" and mint in G.open_positions())
check("S10 refused settle: no close record for it", not [c for c in (G._read().get("closes") or []) if c.get("mint") == mint])
check("S10 no resale on retry (sell fills %d -> %d, legs all done)" % (s_a, s_b), s_a == s_b and s_a >= 2 and all(v.get("done") for v in lp["legs"].values()))
check("S10 lane bankroll not advanced by the unsettled close (%r vs %r) and account cash unchanged (%r)" % (br_a, br_b, cash_a), br_a == br_b and abs(PA_cash() - cash_a) < 1e-12)
check("S10 exactly ONE open close intent despite repeated refused retries (tries %r)" % sp.get("tries"), len(_CI.pending("grad")) == 1 and int(sp.get("tries") or 0) >= 2)
_s10_ops = [o for o in _credit_ops if (":%s:" % mint) in o[0]]
check("S10 stable credit op: ACTUAL refused op ids collected (%d refusals, distinct ids %r) are identical, carry this mint and pos id, amounts equal (%r)"
      % (len(_s10_ops), sorted({o[0] for o in _s10_ops}), sorted({o[1] for o in _s10_ops})),
      len(_s10_ops) >= 2 and len({o[0] for o in _s10_ops}) == 1 and len({o[1] for o in _s10_ops}) == 1 and pid in _s10_ops[0][0])
dump("S10-pending", mint, pid)
for _ in range(3):
    G.poll_exits(price_fn=prices([entry * 0.05]))
rec = [c for c in (G._read().get("closes") or []) if c.get("mint") == mint]
check("S10 after the account accepts: closed once, pending cleared, no resale", len(rec) == 1 and mint not in G.open_positions() and len(_sells(mint)) == s_a)
check("S10 account flat after the confirmed settle", acct_lot_qty(pid) < 1e-6)
check("S10 conservation: lane pnl == account cash delta (%r vs %r)" % (rec[0].get("pnl_usd") if rec else None, PA_cash() - 100.0), bool(rec) and abs(rec[0]["pnl_usd"] - (PA_cash() - 100.0)) < 1e-4)
check("S10 close intent no longer pending", _CI.pending("grad") == [])
JUP.mult = 1.0

# ---- S11: FAULT crash AFTER the account add, lane never booked it, and the position then reaches its final close BEFORE adoption ran:
# the close must HOLD visibly (account holds units the lane did not sell), never book a clean close or a wrong credit ----
mint = "BB11pump"
p, entry, ok = ready(mint); pid = p["pa_pos_id"]
_rw = G._write
def _w_boom11(dd, *a, **k):
    pp = (dd.get("positions") or {}).get(mint) or {}
    if pp.get("buyback_used") is True:
        raise OSError("injected lane write failure AFTER the account add")
    return _rw(dd, *a, **k)
with patch.object(G, "_write", _w_boom11):
    run_bb(mint, entry * 1.3)
ad = PAm().applied_add(bop(mint, pid)); c_pre = PA_cash(); JUP.mult = 0.05
for _ in range(6):
    G.poll_exits(price_fn=prices([entry * 0.05]))
lp = lane_pos(mint); sp = lp.get("settlement_pending") or {}
s11 = [v for kk, v in FILLER[0]._locked(lambda j: dict(j["fills"])).items() if v.get("side") == "sell" and (":%s:" % mint) in kk and v.get("status") == "filled"]
check("S11 add applied, lane unbooked, then close attempted: position HELD with settlement_pending why buyback_add_applied_but_lane_unbooked (got %r)" % sp.get("why"),
      ad is not None and lp.get("buyback_used") is not True and sp.get("why") == "buyback_add_applied_but_lane_unbooked" and mint in G.open_positions())
check("S11 no close record, no close credit applied (cash %r -> %r), account still holds the units" % (c_pre, PA_cash()),
      not [c for c in (G._read().get("closes") or []) if c.get("mint") == mint] and abs(PA_cash() - c_pre) < 1e-12 and acct_lot_qty(pid) > 0)
check("S11 no resale across the retried polls (%d sells)" % len(s11), len(s11) <= 2)
JUP.mult = 1.0

# ---- S12: an OLD persisted close intent with a negative credit amount (the pre-fix shape): recovery must FREEZE it visibly, never rewrite
# it, never apply it, never mark it applied ----
setup()
_ci_rec = _CI.begin("grad", "OLDNEGpump", "grad/OLDNEGpump#1",
                    [{"kind": "credit", "op_id": "grad:close:OLDNEGpump:grad/OLDNEGpump#1", "mint": "OLDNEGpump", "amount": -0.286441, "qty": None,
                      "price_kind": "current_price_simulated_fill", "pos_id": "grad/OLDNEGpump#1"}],
                    {"realized_usd": 0.710559, "entry_notional_usd": 1.0, "closed_reason": "hard_stop_-50pct", "close_ts": _tm.time()})
_cash12 = PA_cash(); G._RECOVERY_DONE = False
G._recover_close_intents()
_ents, _st, _ = _CI._read(_CI.journal_path("grad"))
_evs = [e for e in _ents if e.get("intent_id") == _ci_rec]
_amt = [o.get("amount") for e in _evs if e.get("ev") == "intent" for o in e.get("settle_ops", [])]
check("S12 old negative intent: FROZEN visibly (not applied), amount untouched %r, no cash move" % _amt,
      any(e.get("ev") == "frozen" for e in _evs) and not any(e.get("ev") in ("applied", "account_applied") for e in _evs) and _amt == [-0.286441] and abs(PA_cash() - _cash12) < 1e-12)

def _jl(path):
    import json as _j
    try:
        return [_j.loads(x) for x in open(path).read().splitlines() if x.strip()]
    except Exception:
        return []
def _rec_exits(m):
    import glob as _g
    out = []
    for p_ in _g.glob(os.path.join(os.environ["FOREX_AGENT_HOME"], "recorder", "events-*.jsonl")):
        out += [e for e in _jl(p_) if e.get("kind") == "exit" and e.get("mint") == m]
    return out
def _verdict_closes(m):
    p_ = os.path.join(os.path.dirname(os.path.abspath(G.LEDGER_PATH)), "verdict_ledger", "events.jsonl")
    return [e for e in _jl(p_) if m in str(e)]

# ---- S13: RESTART while a refused settle is pending (real recovery re-run): the pending position stays open and visible, the open intent
# is re-driven against the account (refused again => FROZEN visibly by existing recover_lane, entries blocked), the old amount is never
# rewritten, bankroll/recorder/verdict are NOT booked, nothing is resold ----
mint = "BB13pump"
p, entry, ok = ready(mint); pid = p["pa_pos_id"]
run_bb(mint, entry * 1.3); JUP.mult = 0.05
br0 = G._read()["bankroll"]; ex0 = len(_rec_exits(mint)); vc0 = len(_verdict_closes(mint))
with patch.object(_PAm, "credit", _credit_refuse):
    for _ in range(3):
        G.poll_exits(price_fn=prices([entry * 0.05]))
    _iid13 = (lane_pos(mint).get("settlement_pending") or {}).get("intent_id")
    G._RECOVERY_DONE = False
    G._recover_close_intents()   # the restart path, account still refusing
    for _ in range(2):
        G.poll_exits(price_fn=prices([entry * 0.05]))
lp = lane_pos(mint); _e13 = [e for e in _CI._read(_CI.journal_path("grad"))[0] if e.get("intent_id") == _iid13]
check("S13 pending refusal survives a restart: still OPEN, settlement_pending, no close record, bankroll not booked (%r -> %r), no recorder exit (%d) / verdict close (%d -> %d)"
      % (br0, G._read()["bankroll"], len(_rec_exits(mint)) - ex0, vc0, len(_verdict_closes(mint))),
      mint in G.open_positions() and bool(lp.get("settlement_pending")) and not [c for c in (G._read().get("closes") or []) if c.get("mint") == mint]
      and G._read()["bankroll"] == br0 and len(_rec_exits(mint)) == ex0 and len(_verdict_closes(mint)) == vc0)
_ev13 = [e for e in _CI._read(_CI.journal_path("grad"))[0] if (e.get("mint") == mint and e.get("ev") == "intent") or e.get("ev") == "frozen"]
_seen_frozen = set(); _reuse = 0
for e in _CI._read(_CI.journal_path("grad"))[0]:
    if e.get("ev") == "frozen":
        _seen_frozen.add(e.get("intent_id"))
    elif e.get("ev") == "intent" and e.get("intent_id") in _seen_frozen:
        _reuse += 1
_i13 = [e.get("intent_id") for e in _CI._read(_CI.journal_path("grad"))[0] if e.get("ev") == "intent" and e.get("mint") == mint]
check("S13 no intent id is re-begun after it was frozen (re-used-after-frozen events: %d; intent events for this mint %d, distinct ids %d)" % (_reuse, len(_i13), len(set(_i13))),
      _reuse == 0 and len(_i13) == len(set(_i13)))
check("S13 restart re-drive of the refused op FROZE its intent visibly (frozen event present, never applied, amount untouched)",
      any(e.get("ev") == "frozen" for e in _e13) and not any(e.get("ev") in ("applied", "account_applied") for e in _e13))
JUP.mult = 1.0

# ---- S10b: the close that finally settles books bankroll/recorder/verdict EXACTLY once ----
mint = "BB14pump"
p, entry, ok = ready(mint); pid = p["pa_pos_id"]
run_bb(mint, entry * 1.3); JUP.mult = 0.05
br0 = G._read()["bankroll"]; ex0 = len(_rec_exits(mint))
with patch.object(_PAm, "credit", _credit_refuse):
    for _ in range(3):
        G.poll_exits(price_fn=prices([entry * 0.05]))
vc14 = len(_verdict_closes(mint))
check("S10b during the refusal: no bankroll, recorder exit or verdict effect", G._read()["bankroll"] == br0 and len(_rec_exits(mint)) == ex0 and vc14 == 0)
for _ in range(3):
    G.poll_exits(price_fn=prices([entry * 0.05]))
rec = [c for c in (G._read().get("closes") or []) if c.get("mint") == mint]
check("S10b after the settle: bankroll advanced by realized EXACTLY once (%r -> %r, realized %r), ONE recorder exit (%d)" % (br0, G._read()["bankroll"], rec[0].get("realized_usd") if rec else None, len(_rec_exits(mint)) - ex0),
      len(rec) == 1 and abs(G._read()["bankroll"] - (br0 + rec[0]["realized_usd"])) < 1e-6 and len(_rec_exits(mint)) - ex0 == 1)
check("S10b verdict POSITIVE control: the settled close wrote exactly ONE verdict close event for the mint (was %d, now %d); the refused phases wrote none (negative checks above are therefore sensitive)" % (vc14, len(_verdict_closes(mint))),
      len(_verdict_closes(mint)) - vc14 == 1)
JUP.mult = 1.0

# ---- S14: FAIL CLOSED when the close intent cannot be made durable (injected at the journal append, the real _finalize and real account):
# no account credit, no close, bankroll unbooked, position pending with why close_intent_not_durable, nothing resold; once the journal works the
# close settles exactly once ----
mint = "BB15pump"
p, entry, ok = ready(mint); pid = p["pa_pos_id"]
run_bb(mint, entry * 1.3); JUP.mult = 0.05
br0 = G._read()["bankroll"]; ex0 = len(_rec_exits(mint)); cash0 = PA_cash()
_real_append = _CI._append
def _append_no_intent(path, event):
    if event.get("ev") == "intent":
        return False   # INJECTED journal write failure for the intent record only (named fault)
    return _real_append(path, event)
_calls14 = []
def _credit_spy(*a, **k):
    _calls14.append(str(a[3] if len(a) > 3 else k.get("op_id")))
    return _real_credit(*a, **k)
with patch.object(_CI, "_append", _append_no_intent), patch.object(_PAm, "credit", _credit_spy):
    for _ in range(3):
        G.poll_exits(price_fn=prices([entry * 0.05]))
lp = lane_pos(mint); sp14 = lp.get("settlement_pending") or {}
check("S14 intent not durable: position still OPEN with settlement_pending why close_intent_not_durable (tries %r)" % sp14.get("tries"),
      mint in G.open_positions() and sp14.get("why") == "close_intent_not_durable" and int(sp14.get("tries") or 0) >= 2)
check("S14 no account credit was attempted for this close (credit calls with grad:close: for the mint: %d), cash unchanged (%r vs %r)"
      % (len([c for c in _calls14 if (":%s:" % mint) in c]), PA_cash(), cash0), not [c for c in _calls14 if (":%s:" % mint) in c] and abs(PA_cash() - cash0) < 1e-12)
check("S14 no close record, bankroll not booked (%r vs %r), no recorder exit, no resale (legs all done)" % (G._read()["bankroll"], br0),
      not [c for c in (G._read().get("closes") or []) if c.get("mint") == mint] and G._read()["bankroll"] == br0 and len(_rec_exits(mint)) == ex0
      and all(v.get("done") for v in lp["legs"].values()))
check("S14 no intent for this mint is on disk (nothing to replay, nothing silently applied)",
      not [e for e in _CI._read(_CI.journal_path("grad"))[0] if e.get("mint") == mint])
for _ in range(3):
    G.poll_exits(price_fn=prices([entry * 0.05]))
rec = [c for c in (G._read().get("closes") or []) if c.get("mint") == mint]
check("S14 journal healthy again: closes exactly once, pending cleared, bankroll +realized once (%r -> %r), flat" % (br0, G._read()["bankroll"]),
      len(rec) == 1 and mint not in G.open_positions() and abs(G._read()["bankroll"] - (br0 + rec[0]["realized_usd"])) < 1e-6 and acct_lot_qty(pid) < 1e-6)
JUP.mult = 1.0


# ---- S16: SETTLE-PLAN SUPERSEDE LIVENESS (real grad poll, real journal, real account). The account refuses the close credit, so a settle intent I0 stays open
# with its plan signature stored. NAMED STATE INJECTION: the stored plan_sig is replaced by a different value, which is exactly what a changed settle plan looks
# like to the retry. NAMED FAULT INJECTION: the first begin() after the freeze raises IntentNotDurable. Expected: the freeze of I0 lands, begin fails (position stays
# pending, close_intent_not_durable, still pointing at I0, no second intent); the NEXT poll recognises I0 as exactly frozen and opens its successor through
# begin(supersedes=I0); further polls reuse that successor (no third intent); once the account accepts, the close books exactly once and I0 stays frozen. ----
mint = "BB16pump"
p, entry, ok = ready(mint); pid = p["pa_pos_id"]
run_bb(mint, entry * 1.3); JUP.mult = 0.05
br0 = G._read()["bankroll"]; ex0 = len(_rec_exits(mint))
def _gi(m):
    return [e for e in _CI._read(_CI.journal_path("grad"))[0] if e.get("ev") == "intent" and e.get("mint") == m]
with patch.object(_PAm, "credit", _credit_refuse):
    for _ in range(3):
        G.poll_exits(price_fn=prices([entry * 0.05]))
    _i0 = (lane_pos(mint).get("settlement_pending") or {}).get("intent_id")
    check("S16 setup: the refused settle left ONE open intent I0 (%s) with a stored plan_sig" % _i0,
          bool(_i0) and len(_gi(mint)) == 1 and _CI.intent_status("grad", _i0) == "open" and bool((lane_pos(mint).get("settlement_pending") or {}).get("plan_sig")))
    _d16 = G._read(); _d16["positions"][mint]["settlement_pending"]["plan_sig"] = "INJECTED-previous-plan-signature"; G._write(_d16)
    _real_begin = _CI.begin; _bf = [0]
    def _begin_fail_once(*a, **k):
        if not _bf[0]:
            _bf[0] = 1
            raise _CI.IntentNotDurable("INJECTED: begin failed after the freeze landed")
        return _real_begin(*a, **k)
    with patch.object(_CI, "begin", _begin_fail_once):
        G.poll_exits(price_fn=prices([entry * 0.05]))
    _sp1 = lane_pos(mint).get("settlement_pending") or {}
    check("S16 freeze OK then begin fails: I0 is FROZEN, the position stays OPEN pending close_intent_not_durable still pointing at I0 (%r), no second intent (%d), nothing booked"
          % (_sp1.get("intent_id"), len(_gi(mint))),
          _CI.intent_status("grad", _i0) == "frozen" and mint in G.open_positions() and _sp1.get("why") == "close_intent_not_durable" and _sp1.get("intent_id") == _i0
          and len(_gi(mint)) == 1 and G._read()["bankroll"] == br0)
    G.poll_exits(price_fn=prices([entry * 0.05]))
    _gi1 = _gi(mint)
    _sp2 = lane_pos(mint).get("settlement_pending") or {}
    check("S16 NEXT poll recognises the exactly-frozen I0 and opens ONE successor with supersedes=I0 (intents %d, successor parent %r), position pending on the successor (%r)"
          % (len(_gi1), _gi1[-1].get("supersedes") if len(_gi1) > 1 else None, _sp2.get("intent_id")),
          len(_gi1) == 2 and _gi1[1].get("supersedes") == _i0 and _sp2.get("intent_id") == _gi1[1]["intent_id"] and _CI.intent_status("grad", _i0) == "frozen"
          and _CI.intent_status("grad", _gi1[1]["intent_id"]) == "open" and mint in G.open_positions())
    for _ in range(3):
        G.poll_exits(price_fn=prices([entry * 0.05]))
    check("S16 further refused polls reuse the successor: still exactly 2 intents (%d)" % len(_gi(mint)), len(_gi(mint)) == 2)
for _ in range(3):
    G.poll_exits(price_fn=prices([entry * 0.05]))
_rec16 = [c for c in (G._read().get("closes") or []) if c.get("mint") == mint]
_gi2 = _gi(mint)
check("S16 account accepts: closes exactly once, flat, bankroll +realized once (%r -> %r), successor APPLIED, I0 still FROZEN, still 2 intents (%d)" % (br0, G._read()["bankroll"], len(_gi2)),
      len(_rec16) == 1 and mint not in G.open_positions() and abs(G._read()["bankroll"] - (br0 + _rec16[0]["realized_usd"])) < 1e-6 and acct_lot_qty(pid) < 1e-6
      and _CI.intent_status("grad", _gi2[1]["intent_id"]) == "applied" and _CI.intent_status("grad", _i0) == "frozen" and len(_gi2) == 2)
JUP.mult = 1.0

# ---- S17/S18: DROPPED-OP SAFETY (N2) and CHAIN-TIP RECONCILIATION (N3). Real grad poll, real journal, real shared account. NAMED STATE INJECTIONS are
# labelled where used (stored plan_sig / intent pointer; a crafted two-op intent opened through the real begin; real account ops booked by the test). ----
def _gi2(m):
    return [e for e in _CI._read(_CI.journal_path("grad"))[0] if e.get("ev") == "intent" and e.get("mint") == m]
def _point(m, iid, sig="INJECTED-previous-plan-signature"):
    _d = G._read(); _sp = _d["positions"][m]["settlement_pending"]; _sp["intent_id"] = iid; _sp["plan_sig"] = sig; G._write(_d)
def _refused_setup(m):
    p_, entry_, ok_ = ready(m); pid_ = p_["pa_pos_id"]
    run_bb(m, entry_ * 1.3); JUP.mult = 0.05
    with patch.object(_PAm, "credit", _credit_refuse):
        for _ in range(3):
            G.poll_exits(price_fn=prices([entry_ * 0.05]))
    return pid_, entry_, (lane_pos(m).get("settlement_pending") or {}).get("intent_id")

# S17a: the old intent carries a write-down op (":wd") that the new plan does not; that op is BOOKED in the account -> the change is refused
mint = "BB17pump"; pid, entry, _ir = _refused_setup(mint)
_opc = "grad:close:%s:%s" % (mint, pid)
_CI.freeze("grad", _ir, "test: replaced by a crafted two-op intent")
_two = [{"kind": "credit", "op_id": _opc, "mint": mint, "amount": 1.0, "qty": None, "price_kind": "current", "pos_id": pid},
        {"kind": "write_down", "op_id": _opc + ":wd", "mint": mint, "pos_id": pid, "reason": "crafted"}]
_i2 = _CI.begin("grad", mint, pid, _two, {"realized_usd": 1.0, "close_ts": 1795600777.0, "closed_reason": "crafted"}, supersedes=_ir)
# bb93: write_down() requires the generation binding; the crafted op
# is booked against the position's own entry op (stored on the lane
# position at entry), so the generation proof passes.
_lp17 = lane_pos(mint)
_wd_ok, _wd_why = _PAm.write_down("grad", mint, _opc + ":wd", pos_id=pid, qty=None, reason="crafted booked write-down",
                                  expected_account_id=_lp17.get("pa_account_id"), entry_op_id=_lp17.get("pa_entry_op"))
_point(mint, _i2); _cash17 = PA_cash(); _br17 = G._read()["bankroll"]; _n17 = len(_gi2(mint))
G.poll_exits(price_fn=prices([entry * 0.05]))
_sp17 = lane_pos(mint).get("settlement_pending") or {}
check("S17a setup: the crafted write-down op is really BOOKED in the account (ok %r, outcome %r)" % (_wd_ok, _PAm.op_outcome(_opc + ":wd")), _wd_ok is True and _PAm.op_outcome(_opc + ":wd") == "applied")
check("S17a a changed plan that would DROP the booked write-down is REFUSED: no new intent (%d -> %d), crafted intent still open (%s), position open and pending (%r), nothing booked"
      % (_n17, len(_gi2(mint)), _CI.intent_status("grad", _i2), _sp17.get("why")),
      len(_gi2(mint)) == _n17 and _CI.intent_status("grad", _i2) == "open" and mint in G.open_positions() and _sp17.get("why") == "close_intent_not_durable"
      and G._read()["bankroll"] == _br17 and not [c for c in (G._read().get("closes") or []) if c.get("mint") == mint])
# S17b NEGATIVE CONTROL: identical setup but the ":wd" op was NOT booked -> the change proceeds (freeze, then a recorded successor)
mint = "BB18pump"; pid, entry, _ir = _refused_setup(mint)
_opc = "grad:close:%s:%s" % (mint, pid)
_CI.freeze("grad", _ir, "test: replaced by a crafted two-op intent")
_two = [{"kind": "credit", "op_id": _opc, "mint": mint, "amount": 1.0, "qty": None, "price_kind": "current", "pos_id": pid},
        {"kind": "write_down", "op_id": _opc + ":wd", "mint": mint, "pos_id": pid, "reason": "crafted"}]
_i2 = _CI.begin("grad", mint, pid, _two, {"realized_usd": 1.0, "close_ts": 1795600778.0, "closed_reason": "crafted"}, supersedes=_ir)
_point(mint, _i2)
with patch.object(_PAm, "credit", _credit_refuse):
    G.poll_exits(price_fn=prices([entry * 0.05]))
_g18 = _gi2(mint)
check("S17b control (wd NOT booked, outcome %r): the same change proceeds: crafted intent FROZEN, a successor recorded with supersedes (intents %d)" % (_PAm.op_outcome(_opc + ":wd"), len(_g18)),
      _PAm.op_outcome(_opc + ":wd") != "applied" and _CI.intent_status("grad", _i2) == "frozen" and len(_g18) == 3 and _g18[2].get("supersedes") == _i2)
for _ in range(3):
    G.poll_exits(price_fn=prices([entry * 0.05]))
check("S17b control: the account accepts, the close books once and the successor is applied", len([c for c in (G._read().get("closes") or []) if c.get("mint") == mint]) == 1 and mint not in G.open_positions()
      and _CI.intent_status("grad", _g18[2]["intent_id"]) == "applied")

# S17c: SAME op id, DIFFERENT kind (the crafted old intent holds a booked WRITE-DOWN under the close op id; the new plan is a credit under the same id) -> refused
mint = "BB21pump"; pid, entry, _ir = _refused_setup(mint)
_opc = "grad:close:%s:%s" % (mint, pid)
_CI.freeze("grad", _ir, "test: replaced by a crafted write-down intent")
_i2 = _CI.begin("grad", mint, pid, [{"kind": "write_down", "op_id": _opc, "mint": mint, "pos_id": pid, "reason": "crafted"}],
                {"realized_usd": 0.0, "close_ts": 1795600779.0, "closed_reason": "crafted"}, supersedes=_ir)
# bb93: same generation binding as S17a — the write-down under the
# close op id is booked against the position's own entry op.
_lp21 = lane_pos(mint)
_wdk_ok, _ = _PAm.write_down("grad", mint, _opc, pos_id=pid, qty=None, reason="crafted booked write-down under the close op id",
                             expected_account_id=_lp21.get("pa_account_id"), entry_op_id=_lp21.get("pa_entry_op"))
_point(mint, _i2); _n21 = len(_gi2(mint)); _br21 = G._read()["bankroll"]
G.poll_exits(price_fn=prices([entry * 0.05]))
check("S17c kind change on the SAME op id: the booked write-down (outcome %r) is not silently re-booked as a credit: refused, no new intent (%d -> %d), crafted intent open (%s), nothing booked"
      % (_PAm.op_outcome(_opc), _n21, len(_gi2(mint)), _CI.intent_status("grad", _i2)),
      _wdk_ok is True and _PAm.op_outcome(_opc) == "applied" and len(_gi2(mint)) == _n21 and _CI.intent_status("grad", _i2) == "open" and mint in G.open_positions()
      and G._read()["bankroll"] == _br21 and not [c for c in (G._read().get("closes") or []) if c.get("mint") == mint])
# S17d: an UNREADABLE account journal cannot prove a dropped op was not applied -> refused. NAMED FAULT INJECTION, SCOPED: only a call made DIRECTLY from
# grad._finalize (the dropped-op guard site) sees the torn status; every other reader (buyback applied-add, snapshot) passes through unchanged, so the guard
# under test is the one that fires. Reachability is asserted by a hit counter on that exact site, and a pass-through control with the SAME crafted state proceeds.
import sys as _sys17, logging as _lg17
class _Cap17(_lg17.Handler):
    def __init__(self): super().__init__(); self.msgs = []
    def emit(self, r): self.msgs.append(r.getMessage())
def _scoped_jr(fault, hits):
    # Intercepts _journal_read ONLY when the IMMEDIATE caller is grad._finalize (module grad, function _finalize); every other caller, and the real read when
    # not faulting, is delegated to the real paper_account function unchanged.
    _real = _PAm._journal_read
    def _jr(*a, **k):
        _f = _sys17._getframe(1)
        if _f.f_code.co_name == "_finalize" and _f.f_globals.get("__name__") == G.__name__:
            hits.append(1)
            if fault:
                return ([], "torn", None)
        return _real(*a, **k)
    return _jr
def _s17d_setup(mint):
    pid, entry, _ir = _refused_setup(mint)
    _opc = "grad:close:%s:%s" % (mint, pid)
    _CI.freeze("grad", _ir, "test: replaced by a crafted two-op intent")
    _i2 = _CI.begin("grad", mint, pid, [{"kind": "credit", "op_id": _opc, "mint": mint, "amount": 1.0, "qty": None, "price_kind": "current", "pos_id": pid},
                                          {"kind": "write_down", "op_id": _opc + ":wd", "mint": mint, "pos_id": pid, "reason": "crafted"}],
                    {"realized_usd": 1.0, "close_ts": 1795600780.0, "closed_reason": "crafted"}, supersedes=_ir)
    _point(mint, _i2)
    return pid, entry, _i2
def _s17d_run(mint, fault):
    pid, entry, _i2 = _s17d_setup(mint); _n = len(_gi2(mint)); _h = []; _cap = _Cap17(); _root = _lg17.getLogger(); _root.addHandler(_cap)
    try:
        with patch.object(_PAm, "_journal_read", _scoped_jr(fault, _h)):
            G.poll_exits(price_fn=prices([entry * 0.05]))
    finally:
        _root.removeHandler(_cap)
    return pid, _i2, _n, _h, _cap.msgs
mint = "BB22pump"; pid, _i2, _n22, _h, _msgs = _s17d_run(mint, True)
_why = (lane_pos(mint).get("settlement_pending") or {}).get("why")
_hit = [m for m in _msgs if "account journal unreadable: cannot prove the dropped op(s)" in m and "close_intent_not_durable" in m]
check("S17d unreadable account journal AT THE DROPPED-OP GUARD (exactly %d intercept from grad._finalize; guard log lines %d; pending why %r, not buyback_applied_add_unreadable; no new intent %d -> %d, crafted intent %s, position open)"
      % (len(_h), len(_hit), _why, _n22, len(_gi2(mint)), _CI.intent_status("grad", _i2)),
      len(_h) == 1 and len(_hit) == 1 and _why == "close_intent_not_durable" and _why != "buyback_applied_add_unreadable"
      and not [m for m in _msgs if "buyback_applied_add_unreadable" in m]
      and len(_gi2(mint)) == _n22 and _CI.intent_status("grad", _i2) == "open" and mint in G.open_positions())
mint = "BB23pump"; pid, _i2, _n23, _h, _msgs = _s17d_run(mint, False)
_ev = [e for e in _gi2(mint)]
check("S17d control: SAME crafted state with the IDENTICAL wrapper installed but delegating to the real read (exactly %d intercept, so the wrapper is neutral): crafted intent %s, successor recorded (intents %d -> %d), no guard log lines (%d)"
      % (len(_h), _CI.intent_status("grad", _i2), _n23, len(_ev), len([m for m in _msgs if "account journal unreadable" in m])),
      len(_h) == 1 and _CI.intent_status("grad", _i2) == "frozen" and len(_ev) > _n23 and not [m for m in _msgs if "account journal unreadable" in m])

def _to_successor(m):
    pid_, entry_, i0_ = _refused_setup(m)
    _d = G._read(); _d["positions"][m]["settlement_pending"]["plan_sig"] = "INJECTED-previous-plan-signature"; G._write(_d)
    with patch.object(_PAm, "credit", _credit_refuse):
        G.poll_exits(price_fn=prices([entry_ * 0.05]))
    g_ = _gi2(m)
    return pid_, entry_, i0_, g_[1]["intent_id"], g_

# S18a: successor B created and still OPEN, but the state write was lost (position still names the frozen parent I0): follow the chain to the tip, reuse B
mint = "BB19pump"; pid, entry, _i0, _b, _g = _to_successor(mint)
check("S18a setup: I0 frozen, successor B open (2 intents)", len(_g) == 2 and _CI.intent_status("grad", _i0) == "frozen" and _CI.intent_status("grad", _b) == "open" and _g[1].get("supersedes") == _i0)
_point(mint, _i0, sig=(lane_pos(mint).get("settlement_pending") or {}).get("plan_sig"))   # INJECTION: the lost state write
with patch.object(_PAm, "credit", _credit_refuse):
    G.poll_exits(price_fn=prices([entry * 0.05]))
check("S18a the stale pointer to the frozen parent follows the chain to the open tip: NO third intent (%d), position now points at B (%r)" % (len(_gi2(mint)), (lane_pos(mint).get("settlement_pending") or {}).get("intent_id") == _b),
      len(_gi2(mint)) == 2 and (lane_pos(mint).get("settlement_pending") or {}).get("intent_id") == _b and _CI.intent_status("grad", _b) == "open")
for _ in range(3):
    G.poll_exits(price_fn=prices([entry * 0.05]))
check("S18a the account accepts: one close, B applied, I0 frozen", len([c for c in (G._read().get("closes") or []) if c.get("mint") == mint]) == 1 and _CI.intent_status("grad", _b) == "applied" and _CI.intent_status("grad", _i0) == "frozen")

# S18b: B was driven to APPLIED in the journal (real account credit booked, both markers) but the LANE write was lost, the position still names I0, AND the
# retry sees a DIFFERENT quote and a different current lane figure. The reconcile must come from B's DURABLE record (settle ops and close_info), never from the
# current re-price. NAMED INJECTIONS: stored pointer; a different quote (JUP.mult and the poll price); the lane's current realized changed (what a re-price does).
mint = "BB20pump"; pid, entry, _i0, _b, _g = _to_successor(mint)
_bop = [o for o in _g[1]["settle_ops"] if o.get("kind") == "credit"][0]; _rec_real = float(_g[1]["close_info"]["realized_usd"])
_cash_pre = PA_cash()
# bb93: the injected real credit needs the generation binding from
# the position's own entry op, as the lane's own settle calls do.
_lp20 = lane_pos(mint)
_ok20, _w20 = _real_credit("grad", mint, _bop["amount"], _bop["op_id"], pos_id=pid, qty=_bop.get("qty"), price_kind=_bop.get("price_kind"),
                           expected_account_id=_lp20.get("pa_account_id"), entry_op_id=_lp20.get("pa_entry_op"))   # INJECTION: real account op booked
_m1 = _CI.mark_account_applied("grad", _b); _m2 = _CI.mark_lane_applied("grad", _b)
_cash20 = PA_cash(); _br20 = G._read()["bankroll"]
check("S18b setup: B credit booked in the account (%r, cash delta %.6f vs recorded amount %.6f), journal says B applied (%r), the lane still holds the position open"
      % (_ok20, _cash20 - _cash_pre, float(_bop["amount"]), _CI.intent_status("grad", _b)),
      _ok20 is True and _m1 and _m2 and _CI.intent_status("grad", _b) == "applied" and mint in G.open_positions() and abs((_cash20 - _cash_pre) - float(_bop["amount"])) < 1e-6)
_point(mint, _i0, sig=(lane_pos(mint).get("settlement_pending") or {}).get("plan_sig"))
_d20 = G._read(); _d20["positions"][mint]["realized_usd"] = round(float(_d20["positions"][mint]["realized_usd"]) + 0.5, 6); G._write(_d20)
JUP.mult = 0.03
_cur_real = float(lane_pos(mint)["realized_usd"])
check("S18b sensitivity: the CURRENT lane realized (%.6f) differs from B's recorded close_info realized (%.6f), the quote differs (JUP.mult 0.03 vs 0.05)" % (_cur_real, _rec_real), abs(_cur_real - _rec_real) > 0.4)
for _ in range(3):
    G.poll_exits(price_fn=prices([entry * 0.03]))
_rec20 = [c for c in (G._read().get("closes") or []) if c.get("mint") == mint]
check("S18b reconcile from the DURABLE tip record: no 3rd intent (%d), exactly ONE close, flat, close realized %r == B recorded %r, bankroll +recorded once (%r -> %r)"
      % (len(_gi2(mint)), _rec20[0].get("realized_usd") if _rec20 else None, _rec_real, _br20, G._read()["bankroll"]),
      len(_gi2(mint)) == 2 and len(_rec20) == 1 and mint not in G.open_positions() and abs(_rec20[0]["realized_usd"] - _rec_real) < 1e-9
      and abs(G._read()["bankroll"] - (_br20 + _rec_real)) < 1e-6 and acct_lot_qty(pid) < 1e-6)
check("S18b the account credit is NOT booked a second time and equals the recorded amount (cash %r vs %r after the injected credit, delta %.6f vs %.6f), no hold loop (no pending, no settlement_pending)"
      % (PA_cash(), _cash20, _cash20 - _cash_pre, float(_bop["amount"])),
      abs(PA_cash() - _cash20) < 1e-9 and abs((_cash20 - _cash_pre) - float(_bop["amount"])) < 1e-6 and not lane_pos(mint) and _CI.intent_status("grad", _b) == "applied")
JUP.mult = 1.0

# ---- S15: REAL DemoFiller sale idempotency and the execute-before-save window (real filler, real sim executor, FakeJup quotes = canned quote fixture, engineering only) ----
setup(); _F = FILLER[0]; _mint15 = "S15pump"; _op15 = "grad:exit:%s:a" % _mint15
_f1 = _F.sell(_op15, _mint15, 100.0, 2.0); _n1 = len(JUP.calls)
_f2 = _F.sell(_op15, _mint15, 100.0, 2.0)
check("S15a real sell: a second call with the SAME op id returns the SAME journaled fill and requests/executes nothing (calls %d -> %d)" % (_n1, len(JUP.calls)),
      _f2 == _f1 and len(JUP.calls) == _n1 and _f1.get("status") == "filled")
# crash/save-failure window: the swap executes, then the fill cannot be saved
setup(); _F = FILLER[0]; _op = "grad:exit:S15crash:a"; _real_save = _F._save
def _bad_save(op_id, fill): raise RuntimeError("INJECTED: fill journal save failed after the swap executed")
_F._save = _bad_save; _n0 = len(JUP.calls)
try:
    _F.sell(_op, "S15crash", 100.0, 2.0); _raised = False
except RuntimeError:
    _raised = True
_nx = len(JUP.calls); _rec = _F.get_fill(_op)
check("S15b INJECTED save failure after execute: sell raised (%r), the swap WAS requested (%d -> %d), journal holds an INFLIGHT marker (%r)" % (_raised, _n0, _nx, (_rec or {}).get("status")),
      _raised and _nx > _n0 and (_rec or {}).get("status") == "inflight")
_F._save = _real_save
try:
    _F.sell(_op, "S15crash", 100.0, 2.0); _why = None
except DF.DemoSkip as _e:
    _why = _e.reason
check("S15b retry after the crash is REFUSED fail-closed (unresolved_inflight, got %r) and does NOT execute the swap again (calls %d -> %d)" % (_why, _nx, len(JUP.calls)),
      _why == "unresolved_inflight" and len(JUP.calls) == _nx and (_F.get_fill(_op) or {}).get("status") == "inflight")
# clean executor refusal clears the marker: a later retry may execute exactly once
setup(); _F = FILLER[0]; _op = "grad:exit:S15refuse:a"; JUP.fail_sell = True
try:
    _F.sell(_op, "S15refuse", 100.0, 2.0); _w = None
except DF.DemoSkip as _e:
    _w = _e.reason
_after = _F.get_fill(_op)
JUP.fail_sell = False; _nr = len(JUP.calls)
_ok15 = _F.sell(_op, "S15refuse", 100.0, 2.0)
check("S15c clean refusal (%r) leaves NO inflight marker (%r) and a later retry executes once (calls +%d) to a filled record" % (_w, _after, len(JUP.calls) - _nr),
      _w is not None and _after is None and _ok15.get("status") == "filled" and len(JUP.calls) > _nr)
# marker unwritable: nothing executes
setup(); _F = FILLER[0]; _n0 = len(JUP.calls)
def _bad_mark(*a, **k): raise RuntimeError("INJECTED: inflight marker not writable")
_F._mark_inflight = _bad_mark
try:
    _F.sell("grad:exit:S15nomark:a", "S15nomark", 100.0, 2.0); _r = False
except RuntimeError:
    _r = True
check("S15d marker not writable: the sale raises and the executor was never reached (execute calls %d -> %d)" % (_n0, len(JUP.calls)), _r and len(JUP.calls) == _n0)


# ---- S15e-n: atomic claim, mint-level guard, timeout->unknown->reconcile, BUY path controls (real DemoFiller, canned FakeJup quotes) ----
import threading as _thr
# e. two concurrent callers of the SAME sell op id: exactly ONE executes (claim is a single locked check-and-claim)
setup(); _F = FILLER[0]; _n0 = len(JUP.calls); _res = []; _lk = _thr.Lock()
def _t():
    try:
        r = _F.sell("grad:exit:S15conc:a", "S15conc", 100.0, 2.0); o = ("fill", r.get("status"))
    except DF.DemoSkip as e:
        o = ("skip", e.reason)
    with _lk: _res.append(o)
_ts = [_thr.Thread(target=_t) for _ in range(6)]
[t.start() for t in _ts]; [t.join() for t in _ts]
_ex = len(JUP.calls) - _n0
check("S15e 6 concurrent sells of ONE op id: swap executed exactly %d time(s) (quote calls), outcomes %r, final record %r" % (_ex, sorted(set(_res)), (_F.get_fill("grad:exit:S15conc:a") or {}).get("status")),
      _ex == 1 and (_F.get_fill("grad:exit:S15conc:a") or {}).get("status") == "filled" and all(o[0] == "fill" or o[1] == "unresolved_inflight" for o in _res) and ("fill", "filled") in _res)
# f. MINT-LEVEL guard: leg a left inflight (save failed after execute), then the combined remainder under a DIFFERENT op id must NOT execute
setup(); _F = FILLER[0]; _real_save = _F._save
def _bad(op_id, fill): raise RuntimeError("INJECTED save failure")
_F._save = _bad
try: _F.sell("grad:exit:S15mint:a", "S15mint", 100.0, 2.0)
except RuntimeError: pass
_F._save = _real_save; _n1 = len(JUP.calls)
try: _F.sell("grad:exit:S15mint:remaining:abc", "S15mint", 300.0, 2.0); _w = None
except DF.DemoSkip as e: _w = e.reason
check("S15f leg a inflight, then a remainder under another op id: refused (%r), NO executor call (%d -> %d), no record created for the remainder (%r)" % (_w, _n1, len(JUP.calls), _F.get_fill("grad:exit:S15mint:remaining:abc")),
      _w == "unresolved_inflight" and len(JUP.calls) == _n1 and _F.get_fill("grad:exit:S15mint:remaining:abc") is None)
_n2 = len(JUP.calls)
_o = _F.sell("grad:exit:S15other:a", "S15other", 100.0, 2.0)
check("S15f control: a DIFFERENT mint is unaffected by that inflight claim (fills, calls +%d)" % (len(JUP.calls) - _n2), _o.get("status") == "filled" and len(JUP.calls) > _n2)
# g. confirm_timeout -> UNKNOWN replaces the claim -> retry refused (unresolved_unknown) -> reconcile settled adopts the ORIGINAL fill
setup(); _F = FILLER[0]; _F._inject_outage = "settled"
try: _F.sell("grad:exit:S15to:a", "S15to", 100.0, 2.0); _w = None
except DF.DemoSkip as e: _w = e.reason
_st = (_F.get_fill("grad:exit:S15to:a") or {}).get("status"); _n3 = len(JUP.calls)
try: _F.sell("grad:exit:S15to:a", "S15to", 100.0, 2.0); _w2 = None
except DF.DemoSkip as e: _w2 = e.reason
_fr = _F.reconcile("grad:exit:S15to:a")
check("S15g confirm_timeout (%r) leaves an UNKNOWN record (%r) not inflight; retry refused %r with no executor call (%d -> %d); reconcile(settled) adopts a filled record (%r)"
      % (_w, _st, _w2, _n3, len(JUP.calls), (_fr or {}).get("status")),
      _w == "confirm_timeout" and _st == "unknown" and _w2 == "unresolved_unknown" and len(JUP.calls) == _n3 and (_fr or {}).get("status") == "filled")
setup(); _F = FILLER[0]; _F._inject_outage = "unlanded"
try: _F.sell("grad:exit:S15to2:a", "S15to2", 100.0, 2.0)
except DF.DemoSkip: pass
_F.reconcile("grad:exit:S15to2:a")
check("S15g' reconcile(unlanded) clears the unknown (no fill, record %r) and a retry may execute again" % (_F.get_fill("grad:exit:S15to2:a"),),
      _F.get_fill("grad:exit:S15to2:a") is None and _F.sell("grad:exit:S15to2:a", "S15to2", 100.0, 2.0).get("status") == "filled")
# h. the claim survives a failure while recording the timeout: still INFLIGHT (fail closed), never cleared
setup(); _F = FILLER[0]; _F._inject_outage = "settled"; _real_ra = _F._record_attempt
def _bad_ra(*a, **k): raise RuntimeError("INJECTED: attempt journal not writable")
_F._record_attempt = _bad_ra
try: _F.sell("grad:exit:S15ra:a", "S15ra", 100.0, 2.0)
except RuntimeError: pass
_F._record_attempt = _real_ra
check("S15h a timeout whose attempt record cannot be written leaves the INFLIGHT claim (%r), not cleared" % ((_F.get_fill("grad:exit:S15ra:a") or {}).get("status"),),
      (_F.get_fill("grad:exit:S15ra:a") or {}).get("status") == "inflight")
# i. an unlisted/unknown refusal reason keeps the claim (only PROVEN pre-execution reasons clear)
setup(); _F = FILLER[0]
_orig = _F._executor
class _Ex:
    def __init__(s, ex): s.ex = ex
    def execute_swap(s, *a, **k): raise BrokerError(BROKER_UNAVAILABLE, "x", detail={"reason": "zero_output", "signature": "sig"})
_F._executor = lambda *a, **k: _Ex(_orig(*a, **k))
try: _F.sell("grad:exit:S15zero:a", "S15zero", 100.0, 2.0)
except DF.DemoSkip as e: _zr = e.reason
_F._executor = _orig
check("S15i zero_output (swap landed, no output measured) does NOT clear the claim (reason %r, record %r)" % (_zr, (_F.get_fill("grad:exit:S15zero:a") or {}).get("status")),
      _zr == "zero_output" and (_F.get_fill("grad:exit:S15zero:a") or {}).get("status") == "inflight")
# BUY path controls
setup(); _F = FILLER[0]
_b = _F.buy("grad:entry:S15b:1", "S15b", 10.0, 2.0)
check("S15j healthy BUY: filled record, NO inflight left (%r), a repeat returns the same fill with no new quote" % (_b.get("status"),),
      _b.get("status") == "filled" and (_F.get_fill("grad:entry:S15b:1") or {}).get("status") == "filled")
_nb = len(JUP.calls); _b2 = _F.buy("grad:entry:S15b:1", "S15b", 10.0, 2.0)
check("S15j repeat BUY same op id: same fill, calls unchanged (%d -> %d)" % (_nb, len(JUP.calls)), _b2 == _b and len(JUP.calls) == _nb)
setup(); _F = FILLER[0]; _real_save = _F._save; _F._save = _bad
try: _F.buy("grad:entry:S15bs:1", "S15bs", 10.0, 2.0)
except RuntimeError: pass
_F._save = _real_save; _nbs = len(JUP.calls)
try: _F.buy("grad:entry:S15bs:1", "S15bs", 10.0, 2.0); _w = None
except DF.DemoSkip as e: _w = e.reason
check("S15k BUY save failure after execute: inflight kept, retry refused (%r) with no second execute (%d -> %d)" % (_w, _nbs, len(JUP.calls)), _w == "unresolved_inflight" and len(JUP.calls) == _nbs)
setup(); _F = FILLER[0]; _real_get = JUP.get_quote
def _fail_buy(inp, out, amount, slippage_bps=100):
    raise BrokerError(BROKER_UNAVAILABLE, "no route", detail={"http_status": 400})
JUP.get_quote = _fail_buy
try: _F.buy("grad:entry:S15bn:1", "S15bn", 10.0, 2.0); _w = None
except DF.DemoSkip as e: _w = e.reason
_after = _F.get_fill("grad:entry:S15bn:1"); JUP.get_quote = _real_get
check("S15l BUY clean refusal (%r) clears the claim (%r) and a retry executes" % (_w, _after), _w is not None and _after is None and _F.buy("grad:entry:S15bn:1", "S15bn", 10.0, 2.0).get("status") == "filled")
setup(); _F = FILLER[0]; _n0 = len(JUP.calls); _F._mark_inflight = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("INJECTED"))
try: _F.buy("grad:entry:S15bw:1", "S15bw", 10.0, 2.0); _r = False
except RuntimeError: _r = True
check("S15m BUY claim not writable: raises, executor never reached (%d -> %d)" % (_n0, len(JUP.calls)), _r and len(JUP.calls) == _n0)

# n. the mint guard blocks on an UNKNOWN record too (not only inflight), and the unknown record keeps the sale's intent
setup(); _F = FILLER[0]; _F._inject_outage = "settled"
try: _F.sell("grad:exit:S15ug:a", "S15ug", 100.0, 2.0, meta={"reason": "hard_stop_-50pct", "pkind": "x", "key": "a"})
except DF.DemoSkip: pass
_ug = _F.get_fill("grad:exit:S15ug:a") or {}; _n4 = len(JUP.calls)
try: _F.sell("grad:exit:S15ug:remaining:abc", "S15ug", 300.0, 2.0); _w = None
except DF.DemoSkip as e: _w = e.reason
try: _F.buy("grad:entry:S15ug:1", "S15ug", 10.0, 2.0); _wb = None
except DF.DemoSkip as e: _wb = e.reason
check("S15n leg a UNKNOWN: a remainder under another op id is refused (%r), a buyback entry is refused (%r), NO executor call (%d -> %d), unknown record keeps intent reason (%r)"
      % (_w, _wb, _n4, len(JUP.calls), (_ug.get("intent") or {}).get("reason")),
      _ug.get("status") == "unknown" and _w == "unresolved_unknown" and _wb == "unresolved_unknown" and len(JUP.calls) == _n4
      and (_ug.get("intent") or {}).get("reason") == "hard_stop_-50pct")
# o. a landed-failed tx: the attempt AND its fee are durable BEFORE the claim is cleared; if recording raises, the claim stays
def _txfail_filler():
    setup(); _F = FILLER[0]; _booked = []
    _F._fee_sink = lambda usd, fid: (_booked.append((round(usd, 6), fid)) or (True, ""))
    _orig = _F._executor
    class _Ex:
        def __init__(s, ex): s.ex = ex
        def execute_swap(s, *a, **k): raise BrokerError(BROKER_UNAVAILABLE, "x", detail={"reason": "tx_failed", "fee_lamports": 5000, "signature": "sigfail"})
    _F._executor = lambda *a, **k: _Ex(_orig(*a, **k))
    return _F, _booked
_F, _booked = _txfail_filler()
try: _F.sell("grad:exit:S15tf:a", "S15tf", 100.0, 2.0); _w = None
except DF.DemoSkip as e: _w = e.reason
with open(_F._jpath()) as _fh: _j = json.load(_fh)
check("S15o tx_failed: fee booked once (%r), attempt journaled (%d) with fee_op_id, then the claim is cleared (%r)" % (_booked, len(_j["attempts"]), _F.get_fill("grad:exit:S15tf:a")),
      _w == "tx_failed" and len(_booked) == 1 and _booked[0][1] == "feeloss:grad:exit:S15tf:a:0" and len(_j["attempts"]) == 1
      and _j["attempts"][0]["detail"].get("fee_op_id") == "feeloss:grad:exit:S15tf:a:0" and _F.get_fill("grad:exit:S15tf:a") is None)
_F, _booked = _txfail_filler()
def _bad_ra2(*a, **k): raise RuntimeError("INJECTED: attempt journal not writable")
_F._record_attempt = _bad_ra2
try: _F.sell("grad:exit:S15tf2:a", "S15tf2", 100.0, 2.0)
except RuntimeError: pass
check("S15o' tx_failed whose attempt/fee record cannot be written: claim STAYS inflight (%r), nothing booked (%r): the fee evidence is not lost by an early clear"
      % ((_F.get_fill("grad:exit:S15tf2:a") or {}).get("status"), _booked), (_F.get_fill("grad:exit:S15tf2:a") or {}).get("status") == "inflight" and not _booked)
# p. resolve_inflight is disabled: no caller string clears an inflight claim
setup(); _F = FILLER[0]; _F._save = _bad
try: _F.sell("grad:exit:S15rs:a", "S15rs", 100.0, 2.0)
except RuntimeError: pass
try: _F.resolve_inflight("grad:exit:S15rs:a", "not_executed", "operator says so", "op"); _w = None
except DF.DemoSkip as e: _w = e.reason
check("S15p resolve_inflight refuses (%r) and the claim remains (%r)" % (_w, (_F.get_fill("grad:exit:S15rs:a") or {}).get("status")),
      _w == "resolution_disabled" and (_F.get_fill("grad:exit:S15rs:a") or {}).get("status") == "inflight")

os.environ.pop("AGT_EXECUTION_MODE", None)
print("fails: %d" % len(fails)); sys.exit(1 if fails else 0)
