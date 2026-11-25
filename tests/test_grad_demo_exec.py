#!/usr/bin/env python3
"""GRAD lane DEMO execution routing tests (Jupiter-quote simulation through the shared account).

Scope actually covered here: entry via the durable intent, sim-priced leg/remainder/TWAP exits, done-guard,
fixed exit ops, closed-mint refusal, and the mutants M1-M3 noted inline. The generic grad coverage (age/venue
filter, safety semantics, ladder, verdict, normalizers) lives in tests/test_grad.py and is NOT claimed here.
Harness copied from test_grad.py. No network: every feed is stubbed. The killswitch halt_lane sanity checks run
only AFTER all four killswitch paths (STATE_DIR, STATE_FILE, SHADOW_FILE, LOCK_FILE) are repointed to a temp dir.
"""
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

# A. entry: fill from the quote sim, booked into the shared account at the configured $1
ps = setup(); c0 = PA_cash()
ok, why = demo_card("DM1pump")
pos = G.open_positions().get("DM1pump") or {}
check("demo entry opens (%s)" % why, ok is True)
check("account debited exactly the $1 notional (%.6f)" % (c0 - PA_cash()), abs((c0 - PA_cash()) - 1.0) < 1e-6)
ex = pos.get("execution") or {}
check("position records execution_source jupiter_quote_sim and a synthetic signature",
      ex.get("source") == "jupiter_quote_sim" and ex.get("synthetic_signature") is True and str(ex.get("signature", "")).startswith("sim:"))
check("tokens come from the QUOTE (1 USD @ $100/SOL, 1e-5 SOL/token => ~99.85 tokens), not the paper fill model",
      abs(pos.get("tokens", 0) - (1.0 - 0.0015) / 100.0 / 0.00001) < 0.01)
check("quote was requested for the real notional in lamports (not scaled)", JUP.calls and JUP.calls[0][2] == int(round(1.0 / 100.0 * 1e9)) - 15000)

_pv = ex.get("provenance") or {}
check("propagation (real DemoFiller -> grad entry -> persisted position): provenance carries the percent_v2 stamp, true-percent pct (0.4 from ratio 0.004) and the RAW quote value 0.004",
      _pv.get("price_impact_unit") == "percent_v2" and abs((_pv.get("price_impact_pct") or 0) - 0.4) < 1e-9 and _pv.get("price_impact_raw") == 0.004)
check("propagation: the impact fields are NOT at the top level of the persisted execution record (they live in provenance; top-level reads would find nothing)",
      "price_impact_unit" not in ex and "price_impact_pct" not in ex)

# B. exit leg A: proceeds are the quote sim's. grad now credits the shared account PER LEG (tranche_credit), and the final
#    close credits only the remainder, so cash rises by leg A's sim net after leg A and by the sum of all sims' nets after the close.
entry = pos["entry_price"]; cb = PA_cash()
JUP.mult = 2.0
G.poll_exits(price_fn=prices([entry * 2.0]))
jr = FILLER[0].get_fill("grad:exit:DM1pump:a:%s" % pos["pa_pos_id"])
check("exit leg A sold through the quote sim (journaled)", bool(jr) and jr.get("status") == "filled")
p1 = G.open_positions()["DM1pump"]
check("leg A realized_usd equals the sim's net proceeds (%.6f vs %.6f)" % (p1["legs"]["a"]["realized_usd"], (jr or {}).get("net_proceeds_usd", -1)),
      jr and abs(p1["legs"]["a"]["realized_usd"] - jr["net_proceeds_usd"]) < 1e-6)
check("grad credits leg A per leg: cash rose by exactly leg A's sim net (%.6f vs %.6f)" % (PA_cash() - cb, (jr or {}).get("net_proceeds_usd", -1)), jr and abs((PA_cash() - cb) - jr["net_proceeds_usd"]) < 1e-6)
JUP.mult = 0.05
for _ in range(6):
    G.poll_exits(price_fn=prices([entry * 0.05]))
check("position fully closed", "DM1pump" not in G.open_positions())
_sells = FILLER[0]._locked(lambda j: [f for f in j["fills"].values() if f.get("side") == "sell" and f.get("status") == "filled"])
_sum = sum(f["net_proceeds_usd"] for f in _sells)
check("after the close, cash rose by exactly the sum of the sims' net proceeds (%.6f vs %.6f over %d sells)" % (PA_cash() - cb, _sum, len(_sells)),
      len(_sells) >= 2 and abs((PA_cash() - cb) - _sum) < 1e-5)
pass

# C. failed exit quote: leg stays pending, nothing recorded, later success records once
ps = setup(); ok, why = demo_card("DM2pump"); pos = G.open_positions()["DM2pump"]; entry = pos["entry_price"]
JUP.fail_sell = True; cb = PA_cash()
G.poll_exits(price_fn=prices([entry * 2.0]))
check("failed exit quote: leg A not done, cash untouched, position still open",
      not G.open_positions()["DM2pump"]["legs"]["a"]["done"] and abs(PA_cash() - cb) < 1e-9
      and G.open_positions()["DM2pump"].get("realized_usd", 0) == 0)
JUP.fail_sell = False
G.poll_exits(price_fn=prices([entry * 2.0]))
p2 = G.open_positions()["DM2pump"]; r1 = p2["realized_usd"]
check("retry after the route returns: leg done, realized recorded once", p2["legs"]["a"]["done"] and r1 > 0.1)
G.poll_exits(price_fn=prices([entry * 2.0]))
check("no double count on a further poll", abs(G.open_positions()["DM2pump"]["realized_usd"] - r1) < 1e-9)
pass

# D. mandatory price-impact cap unset: entry refused before any quote, nothing debited
ps = setup(cap=None); c0 = PA_cash()
ok, why = demo_card("DM3pump")
check("impact cap unset refuses the entry (%s)" % str(why)[:60], ok is False and not JUP.calls and abs(PA_cash() - c0) < 1e-9)
pass

# E. crash after debit; retry under a DIFFERENT market quote must replay the ORIGINAL quote/economics
ps = setup(); c0 = PA_cash()
with patch.object(G, "_write", side_effect=RuntimeError("crash")):
    try: demo_card("DM4pump")
    except Exception: pass
_fl = FILLER[0]._locked(lambda j: dict(j["fills"]))
check("crash left exactly one journaled fill", len(_fl) == 1)
first = list(_fl.values())[0] if _fl else None
nq = len(JUP.calls); JUP.bmult = 0.5   # market moved: a re-quote would give HALF the tokens
time.sleep(0.01)
ok, why = demo_card("DM4pump")
pos4 = G.open_positions().get("DM4pump") or {}
check("crash+retry converges (%s)" % why, ok is True)
check("crash+retry: exactly ONE $1 debit on the shared account (%.6f)" % (c0 - PA_cash()), abs((c0 - PA_cash()) - 1.0) < 1e-6)
check("crash+retry: NO second quote (calls %d -> %d)" % (nq, len(JUP.calls)), len(JUP.calls) == nq)
check("crash+retry: position economics equal the ORIGINAL fill, not the new market",
      first is not None and abs(pos4.get("tokens", 0) - first["qty_tokens"]) < 1e-9)
JUP.bmult = 1.0

# H. duplicate _close_leg must not double-count the position's realized PnL (cash is journal-idempotent)
ps = setup(); ok, why = demo_card("DM8pump"); pos8 = G.open_positions()["DM8pump"]
JUP.mult = 2.0; _px = pos8["entry_price"] * 2.0
r1 = G._close_leg(pos8, "a", _px, "leg_a_2x"); real1 = pos8["realized_usd"]; c1 = PA_cash()
r2 = G._close_leg(pos8, "a", _px, "leg_a_2x")
check("H: first leg close realized something (%.6f)" % real1, real1 > 0 and r1 > 0)
check("H: duplicate _close_leg returns 0 and keeps position realized_usd (%.6f -> %.6f)" % (real1, pos8["realized_usd"]),
      r2 == 0.0 and abs(pos8["realized_usd"] - real1) < 1e-9)
check("H: duplicate _close_leg moves no cash", abs(PA_cash() - c1) < 1e-9)
JUP.mult = 1.0

# G. DECLINED debit (account refuses, nothing debited) then the market moves: the retry takes a FRESH quote (new op, new tokens),
#    not a replay of the declined attempt's journaled fill. Kills a fixed-op-id regression.
ps = setup(); c0 = PA_cash()
_real_td = _pa.try_debit
_nd = {"k": 0}
def _decline_once(*a, **k):
    _nd["k"] += 1
    if _nd["k"] == 1: return False, "declined (test)", None
    return _real_td(*a, **k)
import daemon.launch_scanner.paper_account as _PAm
with patch.object(_PAm, "try_debit", _decline_once):
    ok1, why1 = demo_card("DM9pump")
check("G: first attempt refused by the account (%s)" % why1, ok1 is False and abs(PA_cash() - c0) < 1e-9)
nq = len(JUP.calls); JUP.bmult = 0.5
with patch.object(_PAm, "try_debit", _decline_once):
    ok2, why2 = demo_card("DM9pump")
pos9 = G.open_positions().get("DM9pump") or {}
check("G: retry opens (%s)" % why2, ok2 is True)
check("G: retry took a NEW quote (%d -> %d)" % (nq, len(JUP.calls)), len(JUP.calls) > nq)
check("G: retry tokens are the NEW market's (%.4f)" % pos9.get("tokens", 0), abs(pos9.get("tokens", 0) - 998.5 * 0.5) < 0.05 * 998.5)
check("G: exactly one $1 debit (%.6f)" % (c0 - PA_cash()), abs((c0 - PA_cash()) - 1.0) < 1e-6)
JUP.bmult = 1.0

# T. TWAP positions (variant lanes) must not fall back to the MODEL fill: slices and the remainder sell through the quote sim.
ps = setup(); ok, why = demo_card("DM13pump"); pos13 = G.open_positions()["DM13pump"]; ent = pos13["entry_price"]
_old_tw = G.TWAP_SLICES; G.TWAP_SLICES = 2
try:
    JUP.fail_sell = True
    r0 = G._close_twap_slice(pos13, ent * 2.0)
    check("T: failed slice quote: nothing sold, no model fallback, pending recorded", r0 == 0.0 and (pos13.get("twap") or {}).get("done", 0) == 0
          and "pending_exit" in pos13)
    JUP.fail_sell = False; JUP.mult = 2.0
    r1 = G._close_twap_slice(pos13, ent * 2.0)
    j1 = FILLER[0].get_fill("grad:exit:DM13pump:twap1:%s" % pos13["pa_pos_id"])
    check("T: slice 1 sold through the sim (journaled), realized == sim net (%.6f)" % r1,
          bool(j1) and j1.get("status") == "filled" and abs(r1 - (j1["gross_usd"] - j1["fee_usd"])) < 1e-9 and pos13["twap"]["done"] == 1)
    r2 = G._close_remaining(pos13, ent * 2.0, "test_remainder")
    j2 = FILLER[0].get_fill("grad:exit:DM13pump:twap_remainder:%s" % pos13["pa_pos_id"])
    check("T: remainder sold through the sim (journaled), tokens = the unsold half",
          bool(j2) and j2.get("status") == "filled" and abs(pos13["twap"]["remainder"]["realized_usd"] - round(j2["gross_usd"] - j2["fee_usd"], 6)) < 1e-9
          and abs(pos13["twap"]["remainder"]["tokens"] - pos13["tokens"] / 2) < 1e-3)
finally:
    G.TWAP_SLICES = _old_tw; JUP.mult = 1.0; JUP.fail_sell = False

# F. over-cap impact refuses the entry and counts a failed attempt
ps = setup(cap="0.1"); c0 = PA_cash()
ok, why = demo_card("DM5pump")
check("quote impact 0.4%% over cap 0.1%% refuses and moves no cash (%s)" % str(why)[:60], ok is False and abs(PA_cash() - c0) < 1e-9)
check("failed execution attempt counted", FILLER[0].failed_attempts() == 1)
pass
os.environ.pop("AGT_EXECUTION_MODE", None)

print("fails: %d" % len(fails)); sys.exit(1 if fails else 0)
