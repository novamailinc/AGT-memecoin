#!/usr/bin/env python3
"""INERT grad PARTIAL-EXIT window validation (equity/drawdown marks, cash spendable by other lanes, restart/crash recovery).
Harness copied from test_demo_shared_account_smoke.py (5767edc). Observed implementation: grad credits the shared account only at FULL close.
Run: python3 tests/test_grad_partial_exit_window.py
(original smoke docstring follows) INERT all-lane DEMO smoke: watcher, momentum, ceo, mirror and challenge-trader entries contend for ONE shared simulated
account (start $10) through the REAL DemoFiller + durable journal + real entry guards. Fake Jupiter client only.
TEST-FIXTURE inputs only (account start, max data age, price impact cap): NOT owner operating limits.
Containment is asserted: network blocked before any repo import, no subprocess/thread start, no live executor module imported.
Covered: entries on 6 lanes incl. grad, leg-A exits with exact per-lane ledger expectations, a slow quote on every lane. Not covered (disclosed): challenge-trader process_challenge TP/SL loop (only its _demo_exit helper is driven), full close of every lane except grad, the LIVE
positive entry (the entry hold stays on), real Jupiter/RPC. Run: python3 tests/test_demo_shared_account_smoke.py
"""
if __name__ != "__main__":
    import pytest
    pytest.skip("script-style test", allow_module_level=True)
import importlib, importlib.machinery, importlib.util, os, sys, tempfile, time, subprocess, threading
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import tests._guard_fixture as G
G.block_network()   # BEFORE any repo/lane import
sys.path.insert(0, os.path.join(ROOT, "daemon", "launch_scanner"))

if not hasattr(G, "stamped_quote"):   # helper from the c6e836 test delta; local equivalent so this file is self-contained
    def _sq(mod, fn):
        def q(chain, mint, spot_index=None):
            p = fn(chain, mint, spot_index)
            if isinstance(p, (int, float)) and p > 0:
                n = time.time(); mod._LAST_QUOTE_STAMP = {"fetched_at": n, "received_at": n}
            return p
        return q
    G.stamped_quote = _sq
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n)
    if not c: FAILS.append(n)

# containment: any process/thread start is a failure (patched to raise)
STARTS = []
def _no(*a, **k):
    STARTS.append(1); raise RuntimeError("process/thread start forbidden in smoke")
import multiprocessing
subprocess.Popen = _no; subprocess.run = _no; threading.Thread.start = _no; multiprocessing.Process.start = _no
for _f in ("fork", "forkpty", "system", "execv", "execve", "execvp", "execl", "execlp", "posix_spawn", "posix_spawnp", "spawnv", "spawnl", "popen"):
    if hasattr(os, _f): setattr(os, _f, _no)
MX_AT_START = any("mainnet_executor" in m for m in sys.modules)   # before any repo import here (G only)

from unittest.mock import patch
from daemon.launch_scanner import killswitch as KSM
_ks = tempfile.mkdtemp(prefix="smoke-ks-"); KSM.STATE_DIR = _ks
for _n, _f in (("STATE_FILE", "state.json"), ("SHADOW_FILE", "shadow.json"), ("LOCK_FILE", "state.lock")):
    setattr(KSM, _n, os.path.join(_ks, _f))
import broker.memecoin.execution.mainnet_executor as _MX   # constants only are imported by demo_fill; CONSTRUCTION is what is forbidden
LIVE_BUILT = []
for _nm in dir(_MX):
    _o = getattr(_MX, _nm)
    if isinstance(_o, type) and _o.__module__ == _MX.__name__ and "Executor" in _nm:
        def _mk(_c=_o):
            def _init(self, *a, **k): LIVE_BUILT.append(_c.__name__); raise RuntimeError("live executor construction forbidden")
            return _init
        _o.__init__ = _mk()
from daemon.launch_scanner import demo_fill as DF
from daemon.launch_scanner import watcher_lane as W, momentum_lane as MO, ceo as C, mirror as MI, signal_bus as BUS, grad as GR
from broker import BrokerError, BROKER_UNAVAILABLE
SOLM = "So11111111111111111111111111111111111111112"

class FakeJup:
    delay = 0.0
    mult = 1.0   # sell-side price multiplier (was previously set on the instance but never read: exits filled at 1x)
    def __init__(self): self.calls = 0
    def get_quote(self, inp, out, amount, slippage_bps=100):
        self.calls += 1; time.sleep(FakeJup.delay)
        if inp == SOLM: o = int(amount / 1e9 / 0.00001 * 1e6)
        else: o = int(amount / 1e6 * 0.00001 * 1e9 * FakeJup.mult)
        return {"outAmount": str(o), "priceImpactPct": 0.004, "routePlan": [{"swapInfo": {"label": "FakeAMM"}}]}
JUP = FakeJup()

class KS:
    def is_halted(self, lane): return False
    def gate(self, lane): return True
    def record_trade(self, lane, pnl, ts=None): pass
    def check_lane(self, lane): return {"lane": lane}
class Safe:
    def check_token(self, chain, mint, timeout=10): return {"passed": True, "score": 80, "flags": [], "fails": [], "unknown": False}
class Veto:
    def veto_check(self, mint, chain, symbol="", safety_report=None, run_bundle=False, lane="unknown"):
        return {"passed": True, "vetoes": [], "flags": [], "details": {}}

def tape():
    n = time.time()
    return {"fetched_at": n, "received_at": n, "dex": "pump", "usd_liquidity": 100000.0, "usd_market_cap": 500000.0,
            "chg_m5": 10.0, "buys_m5": 100, "sells_m5": 40, "vol_m5": 9000.0, "buys_h1": 400, "sells_h1": 150,
            "price_usd": 0.001, "pair_created_ms": (time.time() - 3600) * 1000, "tape_source": "dexscreener"}

def fresh(start="10", max_age="60", impact="2.0"):
    h = tempfile.mkdtemp(prefix="smoke-home-"); os.environ["FOREX_AGENT_HOME"] = h
    for k in list(os.environ):
        if k.startswith("PAPER_ACCOUNT"): os.environ.pop(k)
    os.environ["PAPER_ACCOUNT_ENABLED"] = "1"; os.environ["PAPER_ACCOUNT_START"] = start
    os.environ["PAPER_ACCOUNT_MAX_PRICE_IMPACT_PCT"] = impact   # TEST-FIXTURE
    os.environ["AGT_EXECUTION_MODE"] = "jupiter_quote_sim"
    import daemon.launch_scanner.paper_account as PA
    PA = importlib.reload(PA); G.install(PA)   # install() sets a wide fixture data-age cap; override AFTER it
    if max_age is not None: os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = max_age  # TEST-FIXTURE
    else: os.environ.pop("PAPER_ACCOUNT_MAX_DATA_AGE_S", None)
    F = DF.DemoFiller(JUP, sol_usd_fn=lambda: (100.0, time.time()), decimals_fn=lambda m: 6,
                      journal_path=os.path.join(tempfile.mkdtemp(prefix="smoke-j-"), "j.json"), seed=1, hourly_vol=0.0)
    led = tempfile.mkdtemp(prefix="smoke-led-")
    for mod, nm in ((W, "watcher"), (MO, "momentum"), (C, "ceo"), (MI, "mirror")):
        mod.LEDGER_PATH = os.path.join(led, nm + ".json")
        if hasattr(mod, "ensure_ledger"): mod.ensure_ledger()
        mod._demo_mode = lambda: True; mod._demo_filler = lambda F=F: F
        mod._ks = KS(); mod._safety = Safe(); mod._veto = Veto()
        mod._fresh_tape = lambda chain, mint: tape()
        mod.quote_price = G.stamped_quote(mod, lambda chain, mint, spot_index=None: 0.001)
        if hasattr(mod, "RUNNER_CONFIRM_GAP_S"): mod.RUNNER_CONFIRM_GAP_S = 0.0
        if hasattr(mod, "_ENTRY_BLOCKED"): mod._ENTRY_BLOCKED = False
        if hasattr(mod, "_RECOVERY_DONE"): mod._RECOVERY_DONE = False
    C.BUS_PATH = os.path.join(led, "bus.jsonl")
    GR.LEDGER_PATH = os.path.join(led, "grad.json"); GR.ensure_ledger()
    GR._demo_mode = lambda: True; GR._demo_filler = lambda F=F: F
    GR._ks = KS(); GR._veto = Veto(); GR.QUOTE_RETRY_GAP_S = 0.0; GR.MAX_OPEN = 50
    class GSafe:
        def check_token(self, chain, mint, timeout=10): return {"passed": True, "unknown": False, "score": 7, "flags": [], "fails": [], "source": "rugcheck"}
    GR._safety = GSafe()
    def _gt(m):
        n = time.time(); return {"usd_liquidity": 8000.0, "usd_market_cap": 69000.0, "chg_m5": 3.0, "price_usd": 0.00007,
                                 "tape_source": "dexscreener", "fetched_at": n, "received_at": n}
    GR._fresh_tape = _gt
    GR.quote_price = G.stamped_quote(GR, lambda chain, mint, spot_index=None: 0.00007)
    loader = importlib.machinery.SourceFileLoader("challenge_trader", os.path.join(ROOT, "scripts", "challenge-trader"))
    spec = importlib.util.spec_from_loader("challenge_trader", loader)
    CT = importlib.util.module_from_spec(spec); loader.exec_module(CT)
    if hasattr(CT, "_demo_filler"): CT._demo_filler = lambda: F
    return PA, F, CT

def cash(PA): return PA.snapshot()["cash"]

def wfire(mint):
    ev = {"smart_wallets": 3, "wallets": ["W0", "W1", "W2"], "wallet_groups": ["g0", "g1", "g2"], "safety": "ok",
          "components": {"convergence_groups": 3, "sightings": 3, "sol_spent_total": 4.2, "safety_score": 80}}
    return BUS.make_signal("solana", "smart-watcher", mint, symbol="WATCH", tier="converged", mcap=500000.0, liquidity=100000.0, evidence=ev)
def mrun(mint):
    return {"pool_address": "Pool" + mint[:6], "mint": mint, "symbol": "RUN", "name": "RUN", "chain": "solana",
            "source": "gecko-trending", "lane": "lowcap", "url": "", "chg_24h_pct": 55.0, "chg_1h_pct": 8.0,
            "vol_24h_usd": 250000.0, "liq_usd": 100000.0, "usd_market_cap": 500000.0}
def csig(mint):
    return BUS.make_signal(chain="solana", source="test-feed", mint=mint, symbol="TKN", score=70.0, tier="pick", mcap=500000.0, liquidity=50000.0)

LANES = []
def build(PA, CT):
    def e_w(i):
        r = W.consider_fire(wfire("WatchMint%037d" % i), time.time()); return bool(r[0]), r[1]
    def e_m(i):
        r = MO.consider_candidate(mrun("MomMint%038d" % i), time.time()); return bool(r[0]), r[1]
    def e_c(i):
        r = C.consider_card(csig("CeoMint%d" % i)); return bool(r[0]), r[1]
    def e_x(i):
        r = MI.mirror_card(chain="solana", source="test", mint="MirMint%d" % i, symbol="TKN", score=70.0, tier="pick", mcap=500000.0, liquidity=50000.0)
        return (bool(r[0]), r[1]) if isinstance(r, tuple) else (bool(r), r)
    def e_h(i):
        r = CT._demo_enter(PA, "L", "ChalMint%d" % i, 1.0); return bool(r[0]), r[1]
    def e_g(i):
        cand = {"mint": "GradMint%dpump" % i, "symbol": "TKN", "name": "TKN / SOL", "chain": "solana", "dex": "pumpswap",
                "created_ts": time.time() - 60.0, "pool_address": "POOL%d" % i, "reserve_usd": 6000.0, "fdv_usd": 69000.0,
                "price_usd": 0.00007, "source": "gecko-new-pool"}
        r = GR.consider_candidate(cand, time.time()); return bool(r[0]), r[1]
    return [("watcher", e_w), ("momentum", e_m), ("ceo", e_c), ("mirror", e_x), ("challenge", e_h), ("grad", e_g)]

def _cents(x): return int(round(float(x) * 100))
Q = 998.5; FEE = 15000 / 1e9 * 100.0; PX1 = 1e-5 * 100.0   # tokens at entry, fee USD, USD/token at 1x
def gpf(mult): return lambda chain, mint, spot_index=None: float((GR.open_positions().get(mint) or {}).get("entry_price") or 0) * mult
def leg(frac, m): return frac * Q * PX1 * m - FEE
A3, B3 = leg(0.5, 3), leg(0.25, 3)              # legs A and B sold at 3x
C2 = leg(0.25, 2)
TOTAL = A3 + B3 + C2
def marks_for(PA, mult):
    """Marks for EVERY open position: grad at `mult`, any other lane at 1x (test-only values)."""
    out = {}
    for lane, st in PA.snapshot()["lanes"].items():
        for p in st["positions"]:
            out.setdefault(lane, {})[p.get("mark_key", p["mint"])] = {
                "value": p["qty"] * PX1 * (mult if lane == "grad" else 1.0), "ts": time.time(), "source": "test-jupiter",
                "qty": p["qty"], "pos_id": p["pos_id"]}
    return out
def open_to_3x(start="10"):
    PA, F, CT = fresh(start=start); lanes = dict(build(PA, CT))
    ok, why = lanes["grad"](1); assert ok, why
    c0 = cash(PA); mint = list(GR.open_positions())[0]
    FakeJup.mult = 3.0; GR.poll_exits(price_fn=gpf(3.0)); FakeJup.mult = 1.0
    return PA, F, CT, lanes, c0, mint

# ---- W1. equity / drawdown marks inside the deferred-credit window
PA, F, CT, lanes, c0, gm = open_to_3x()
pos = GR.open_positions()[gm]
check("W1 setup: legs A+B sold, leg C open, shared cash credited the banked A+B net per leg (%.4f -> %.4f, model +%.4f)" % (c0, cash(PA), A3 + B3), _cents(cash(PA) - c0) == _cents(A3 + B3) and gm in GR.open_positions())
sn = PA.snapshot(); gl = sn["lanes"]["grad"]
check("W1 account qty/basis shrank with the sales: qty %.2f (model %.2f) basis %.4f (model 0.25)" % (gl["positions"][0]["qty"], 0.25 * Q, gl["open_basis"]),
      abs(gl["positions"][0]["qty"] - 0.25 * Q) < 0.01 and abs(gl["open_basis"] - 0.25) < 1e-6)
true_banked = A3 + B3
def true_equity(mult): return c0 + true_banked + 0.25 * Q * PX1 * mult
res = {}
for m in (3.0, 10.0, 0.5):
    s = PA.snapshot(marks=marks_for(PA, m))
    res[m] = (s["marks_status"], s["marked_equity_conservative"], true_equity(m))
check("W1 marks accepted at every price (status %s)" % {m: v[0] for m, v in res.items()}, all(v[0] == "ok" for v in res.values()))
rep3, tru3 = res[3.0][1], res[3.0][2]
check("W1 at the price the legs were sold (3x): reported equity %.4f ~= independent true equity %.4f (only the leg fees differ, <0.01)" % (rep3, tru3), abs(rep3 - tru3) < 0.01)
rep10, tru10 = res[10.0][1], res[10.0][2]
check("W1 REGRESSION GUARD: price rises to 10x - reported equity %.4f == true %.4f (gap %.4f < 0.01; was +5.2451 overstated before the per-leg credit)" % (res[10.0][1], res[10.0][2], res[10.0][1] - res[10.0][2]), abs(res[10.0][1] - res[10.0][2]) < 0.01)
check("W1 REGRESSION GUARD: price falls to 0.5x - reported equity %.4f == true %.4f (gap < 0.01; was understated before)" % (res[0.5][1], res[0.5][2]), abs(res[0.5][1] - res[0.5][2]) < 0.01)

# ---- W2. drawdown GATE consequence (limits below are TEST-FIXTURE values, NOT owner limits)
def gate_env():
    os.environ["PAPER_ACCOUNT_GATES_ENABLED"] = "1"
    os.environ["PAPER_ACCOUNT_MAX_DAILY_LOSS_USD"] = "1000"; os.environ["PAPER_ACCOUNT_MAX_DRAWDOWN_PCT"] = "20"; os.environ["PAPER_ACCOUNT_MAX_EXPOSURE_PCT"] = "100"
def other_entry(PA, tag, mult):
    return PA.try_debit("watcher", "OtherMint" + tag, 1.0, 1000.0, "w2-" + tag, marks=marks_for(PA, mult))
PA, F, CT, lanes, c0, gm = open_to_3x(); gate_env()
ctl = other_entry(PA, "ctl", 3.0)
check("W2 control: honest 3x mark, no spike - another lane's $1 entry is ADMITTED (%s)" % (ctl,), ctl[0] is True)
PA, F, CT, lanes, c0, gm = open_to_3x(); gate_env()
spike = other_entry(PA, "spike", 10.0)          # price spikes after A+B were banked: sold units marked as held -> inflated peak
back = other_entry(PA, "back", 3.0)             # price returns to the 3x level the legs were sold at; true equity is UNCHANGED vs the control
sn = PA.snapshot(marks=marks_for(PA, 3.0))
check("W2 true equity at 3x (%.4f) >= start balance 10.00 and equals the control scenario" % true_equity(3.0), true_equity(3.0) > 10.0)
check("W2 REGRESSION GUARD: after a 10x spike the same 3x-state entry is ADMITTED, no inflated peak (spike %s, back %s, peak %s, drawdown %.1f%%)" % (spike[0], back[0], (sn.get("peak_equity") or {}).get("value"), sn.get("drawdown_pct") or 0),
      spike[0] is True and back[0] is True and (sn.get("drawdown_pct") or 0) < 20)

# ---- W3. cash spendable by OTHER lanes during the window (start $1.50: only $0.50 left after grad's $1)
PA, F, CT, lanes, c0, gm = open_to_3x(start="1.5")
check("W3 setup: cash %.4f = 0.50 + grad banked %.4f (model)" % (cash(PA), A3 + B3), _cents(cash(PA)) == _cents(0.5 + A3 + B3) and gm in GR.open_positions())
okw, whyw = lanes["watcher"](1)
check("W3 REGRESSION GUARD: during the window another lane's $1 entry is ADMITTED on the banked cash (%s)" % (str(whyw)[:70],), okw is True)
cw = cash(PA)
FakeJup.mult = 2.0; GR.poll_exits(price_fn=gpf(2.0)); FakeJup.mult = 1.0
check("W3 after full close only the C remainder lands (%.4f vs model %.4f)" % (cash(PA) - cw, C2), _cents(cash(PA) - cw) == _cents(C2))

# ---- W4. restart between partial sale and full close
def restart(PA):
    importlib.reload(PA) if False else None
    GR._RECOVERY_DONE = False
    GR._ENTRY_BLOCKED = False
PA, F, CT, lanes, c0, gm = open_to_3x()
legs_before = {k: dict(v) for k, v in GR.open_positions()[gm]["legs"].items()}
nsell_before = len([f for f in F.journal_fills() if f["op_id"].startswith("grad:")]) if hasattr(F, "journal_fills") else None
restart(PA)
rec = GR._recover_close_intents()
check("W4 restart (recovery re-run) with an open partly-sold position: not entry-blocked (%s)" % (rec,), not rec)
check("W4 partial state survived the restart: legs A,B done, banked cash kept, account basis 0.25", gm in GR.open_positions() and _cents(cash(PA) - c0) == _cents(A3 + B3)
      and legs_before == {k: dict(v) for k, v in GR.open_positions()[gm]["legs"].items()} and abs(PA.snapshot()["lanes"]["grad"]["open_basis"] - 0.25) < 1e-6)
FakeJup.mult = 2.0; GR.poll_exits(price_fn=gpf(2.0)); FakeJup.mult = 1.0
check("W4 poll after restart closes once and credits the independent total %.4f exactly once (%.4f)" % (TOTAL, cash(PA) - c0), gm not in GR.open_positions() and _cents(cash(PA) - c0) == _cents(TOTAL))
d = GR._read()
check("W4 exactly one close record for the position (%d)" % len(d["closes"]), len([c for c in d["closes"] if c.get("pa_pos_id")]) == 1)

# ---- W4b. no close intent is left open after a full close whose every leg was credited per tranche
check("W4b no pending grad close intent after the full close (zero-remainder final credit is skipped, not refused) (%s)" % (GR._close_intents().pending("grad"),), not GR._close_intents().pending("grad"))

# ---- W4c. LEGACY position (no tranche marker, e.g. opened before this change): proceeds are NOT lost, credited once at the final close
PA, F, CT = fresh(start="10"); lanes = dict(build(PA, CT))
ok, why = lanes["grad"](1); assert ok, why
c0 = cash(PA); gm = list(GR.open_positions())[0]
_d = GR._read(); _d["positions"][gm].pop("tranche_credit", None); _d["positions"][gm].pop("tranche_credited", None); GR._write(_d)
FakeJup.mult = 3.0; GR.poll_exits(price_fn=gpf(3.0)); FakeJup.mult = 1.0
# CONTRACT CHANGE (legacy tranche migration, disclosed test edit): the legacy position is migrated in the SAME poll its legs
# sell: A+B net is credited once, the account keeps exactly the lane's unsold tokens. Was: "cash unchanged (deferred)".
_lp = GR.open_positions()[gm]
check("W4c legacy MIGRATED: legs A+B net credited once (%.4f -> %.4f, want +%.4f), marker set, account qty == lane unsold tokens" % (c0, cash(PA), A3 + B3),
      _cents(cash(PA) - c0) == _cents(A3 + B3) and gm in GR.open_positions() and _lp.get("tranche_credit") and not _lp.get("tranche_migrating")
      and abs(PA.snapshot()["lanes"]["grad"]["positions"][0]["qty"] - 0.25 * _lp["tokens"]) < 1e-6)
FakeJup.mult = 2.0; GR.poll_exits(price_fn=gpf(2.0)); FakeJup.mult = 1.0
check("W4c legacy: full close credits the independent total once (%.4f vs %.4f)" % (cash(PA) - c0, TOTAL), gm not in GR.open_positions() and _cents(cash(PA) - c0) == _cents(TOTAL))

# ---- W6. crash between a per-leg account credit and the lane-ledger write (leg replays from the journaled fill)
class Crash6(BaseException): pass
PA, F, CT = fresh(start="10"); lanes = dict(build(PA, CT))
ok, why = lanes["grad"](1); assert ok, why
c0 = cash(PA); gm = list(GR.open_positions())[0]
_rc, _rw = PA.credit, GR._write
_st = {"armed": True, "credited": False}
def _cr(*a, **k):
    r = _rc(*a, **k)
    if r[0] and _st["armed"] and str(a[3]).startswith("grad:leg:"): _st["credited"] = True
    return r
def _wr(d, *a, **k):
    if _st["armed"] and _st["credited"]: _st["armed"] = False; raise Crash6()
    return _rw(d, *a, **k)
PA.credit, GR._write = _cr, _wr
crashed6 = False
FakeJup.mult = 3.0
try: GR.poll_exits(price_fn=gpf(3.0))
except Crash6: crashed6 = True
finally: PA.credit, GR._write = _rc, _rw; FakeJup.mult = 1.0
check("W6 crash injected after a per-leg credit, before the ledger write (%s); mid cash +%.4f" % (crashed6, cash(PA) - c0), crashed6 and cash(PA) > c0)
restart(PA); GR._recover_close_intents()
FakeJup.mult = 3.0; GR.poll_exits(price_fn=gpf(3.0)); FakeJup.mult = 1.0
check("W6 after restart+replay: banked A+B credited exactly once (%.4f vs %.4f), leg C still open" % (cash(PA) - c0, A3 + B3), _cents(cash(PA) - c0) == _cents(A3 + B3) and gm in GR.open_positions())
FakeJup.mult = 2.0; GR.poll_exits(price_fn=gpf(2.0)); FakeJup.mult = 1.0
check("W6 final close after the replay: total credited once (%.4f vs %.4f), no pending intent" % (cash(PA) - c0, TOTAL), gm not in GR.open_positions() and _cents(cash(PA) - c0) == _cents(TOTAL) and not GR._close_intents().pending("grad"))

# ---- W7. TWAP sim slices and the TWAP remainder credit per tranche too
_old_tw = GR.TWAP_SLICES; GR.TWAP_SLICES = 2
try:
    PA, F, CT = fresh(start="10"); lanes = dict(build(PA, CT))
    ok, why = lanes["grad"](1); assert ok, why
    c0 = cash(PA); gm = list(GR.open_positions())[0]
    _d = GR._read(); _p = _d["positions"][gm]
    FakeJup.mult = 2.0
    r1 = GR._close_twap_slice(_p, _p["entry_price"] * 2.0)
    FakeJup.mult = 1.0
    gl = PA.snapshot()["lanes"]["grad"]
    check("W7 TWAP slice 1 credited at the sale: cash +%.4f == slice net %.4f; account basis %.4f (model 0.50)" % (cash(PA) - c0, r1, gl["open_basis"]), r1 > 0 and _cents(cash(PA) - c0) == _cents(r1) and abs(gl["open_basis"] - 0.5) < 1e-6)
    _p["twap"]["done"] = 0; _p["twap"]["sold_tokens"] = 0.0; _p["twap"]["sold_usd"] = 0.0; _p["realized_usd"] = 0.0
    c1 = cash(PA); FakeJup.mult = 2.0
    GR._close_twap_slice(_p, _p["entry_price"] * 2.0); FakeJup.mult = 1.0
    check("W7 TWAP slice replay (same op) does not double-credit (%.4f)" % (cash(PA) - c1), _cents(cash(PA) - c1) == 0)
    FakeJup.mult = 2.0
    rr = GR._close_remaining(_p, _p["entry_price"] * 2.0, "test_remainder"); FakeJup.mult = 1.0
    check("W7 TWAP remainder credited at the sale (+%.4f vs %.4f)" % (cash(PA) - c1, rr), rr > 0 and _cents(cash(PA) - c1) == _cents(rr))
finally:
    GR.TWAP_SLICES = _old_tw

# ---- W8. same-mint RE-ENTRY must not replay the previous position's journaled exit fill
PA, F, CT, lanes, c0, gm = open_to_3x()          # position 1: legs A+B sold at 3x (journaled)
FakeJup.mult = 2.0; GR.poll_exits(price_fn=gpf(2.0)); FakeJup.mult = 1.0
check("W8 setup: first position fully closed", gm not in GR.open_positions())
# The lane's own one-entry-per-mint-ever guard already refuses a same-mint re-entry; this checks the fill-journal key
# itself is position-scoped (defence in depth), by selling a second marked position instance of the same mint.
import copy
_d = GR._read(); _old = [c for c in _d["closes"] if c.get("pa_pos_id")][-1]
p_new = {"mint": gm, "tokens": Q, "tranche_credit": True, "pa_pos_id": "REENTRY-POS-2", "execution": {"source": "jupiter_quote_sim"}}
FakeJup.mult = 2.0
r = GR._demo_sell(p_new, 0.5 * Q, "a"); FakeJup.mult = 1.0
check("W8 same mint, new position instance: leg A sells FRESH at the 2x quote, not the old 3x journaled fill (gross %.4f vs 2x model %.4f, old-fill %.4f)" % ((r or [{}])[0].get("gross_usd", -1), 0.5 * Q * PX1 * 2, 0.5 * Q * PX1 * 3),
      r is not None and abs(r[0]["gross_usd"] - 0.5 * Q * PX1 * 2) < 1e-6)
check("W8 old and new positions have distinct journal ops", F.get_fill("grad:exit:%s:a:%s" % (gm, _old["pa_pos_id"])) is not None and F.get_fill("grad:exit:%s:a:REENTRY-POS-2" % gm) is not None)

# ---- W9. crash after leg A's credit, restart BELOW target (A does not re-trigger), then stop-out: no double sale
PA, F, CT = fresh(start="10"); lanes = dict(build(PA, CT))
ok, why = lanes["grad"](1); assert ok, why
c0 = cash(PA); gm = list(GR.open_positions())[0]
_rc, _rw = PA.credit, GR._write
_st = {"armed": True, "credited": False}
def _cr9(*a, **k):
    r = _rc(*a, **k)
    if r[0] and _st["armed"] and str(a[3]).startswith("grad:leg:a:"): _st["credited"] = True
    return r
def _wr9(d, *a, **k):
    if _st["armed"] and _st["credited"]: _st["armed"] = False; raise Crash6()
    return _rw(d, *a, **k)
PA.credit, GR._write = _cr9, _wr9
crashed9 = False
FakeJup.mult = 3.0
try: GR.poll_exits(price_fn=gpf(3.0))
except Crash6: crashed9 = True
finally: PA.credit, GR._write = _rc, _rw; FakeJup.mult = 1.0
acct_basis = PA.snapshot()["lanes"]["grad"]["open_basis"]
check("W9 crash after leg A account credit (%s): account basis already shrank (%.4f), lane leg A still pending (%s)" % (crashed9, acct_basis, not GR.open_positions()[gm]["legs"]["a"]["done"]),
      crashed9 and acct_basis < 0.99 and not GR.open_positions()[gm]["legs"]["a"]["done"])
restart(PA); GR._recover_close_intents()
GR.poll_exits(price_fn=gpf(1.0))   # restart BELOW every target: nothing triggers, reconciliation alone must finish leg A
check("W9 below-target poll after restart reconciled leg A from the account journal (legs A and B were both credited before the crash; lane legs done, credit not repeated: cash +%.4f vs A+B %.4f)" % (cash(PA) - c0, A3 + B3),
      GR.open_positions()[gm]["legs"]["a"]["done"] and GR.open_positions()[gm]["legs"]["b"]["done"] and _cents(cash(PA) - c0) == _cents(A3 + B3))
n_sells_a = len([1 for k in ("a",) if F.get_fill("grad:exit:%s:a:%s" % (gm, GR.open_positions()[gm]["pa_pos_id"]))])
FakeJup.mult = 0.4; GR.poll_exits(price_fn=gpf(0.4)); FakeJup.mult = 1.0   # hard stop: only leg C remains, sold once at the stop price
stop_net = (0.25 * Q * PX1 * 0.4 - FEE)    # leg C only (A+B already credited), one fee
check("W9 after the stop-out the position is closed and leg A was NOT sold again: total credited %.4f vs model %.4f (A+B at 3x + C at stop, one fee)" % (cash(PA) - c0, A3 + B3 + stop_net),
      gm not in GR.open_positions() and _cents(cash(PA) - c0) == _cents(A3 + B3 + stop_net) and not GR._close_intents().pending("grad"))
d = GR._read()
check("W9 exactly one close record", len([c for c in d["closes"] if c.get("pa_pos_id")]) == 1)

# ---- W10. mixed close: A+B credited per tranche, leg C a WRITE-DOWN (rugged / unquotable). Finding: a zero-qty credit
#      step used to be refused and break the plan before the write_down step.
PA, F, CT, lanes, c0, gm = open_to_3x()
with GR._ledger_lock():
    d = GR._read(); pos = d["positions"][gm]
    GR._close_remaining(pos, 0.0, "time_stop_24h_unquotable", pkind="write_down_zero")
    rec10 = GR._finalize(d, gm, pos, "time_stop_24h_unquotable")
    GR._write(d)
sn10 = PA.snapshot()
check("W10 write-down C after credited A+B: close recorded, no pending intent, cash == banked A+B only (%.4f vs %.4f)" % (cash(PA) - c0, A3 + B3),
      bool(rec10) and not GR._close_intents().pending("grad") and _cents(cash(PA) - c0) == _cents(A3 + B3))
check("W10 the unresolved units moved to the register, not left as an open account position (open positions %s, unresolved %s)" % (len(sn10["lanes"]["grad"]["positions"]), bool(PA._load_led().get("unresolved")) if hasattr(PA, "_load_led") else "n/a"),
      len(sn10["lanes"]["grad"]["positions"]) == 0)

# ---- W11. reconciliation uses the ORIGINAL kind from the account receipt; it never upgrades stale/unknown to current
def crash_after_leg_credit():
    PA, F, CT = fresh(start="10"); lanes = dict(build(PA, CT))
    ok, why = lanes["grad"](1); assert ok, why
    gm = list(GR.open_positions())[0]
    _rc, _rw = PA.credit, GR._write
    st = {"armed": True, "credited": False}
    def cr(*a, **k):
        r = _rc(*a, **k)
        if r[0] and st["armed"] and str(a[3]).startswith("grad:leg:a:"): st["credited"] = True
        return r
    def wr(d, *a, **k):
        if st["armed"] and st["credited"]: st["armed"] = False; raise Crash6()
        return _rw(d, *a, **k)
    PA.credit, GR._write = cr, wr
    FakeJup.mult = 3.0
    try: GR.poll_exits(price_fn=gpf(3.0))
    except Crash6: pass
    finally: PA.credit, GR._write = _rc, _rw; FakeJup.mult = 1.0
    restart(PA); GR._recover_close_intents()
    return PA, F, gm
PA, F, gm = crash_after_leg_credit()
_ro = PA.op_receipts
PA.op_receipts = lambda ops: {o: dict(r or {}, price_kind="stale_price_estimate") if r else r for o, r in _ro(ops).items()}
GR.poll_exits(price_fn=gpf(1.0)); PA.op_receipts = _ro
la = GR.open_positions()[gm]["legs"]["a"]
check("W11 receipt says STALE: reconciled leg A keeps the stale kind, not upgraded to current (%s)" % la.get("exit_price_kind"), la["done"] and la.get("exit_price_kind") == "stale_price_estimate")
PA, F, gm = crash_after_leg_credit()
PA.op_receipts = lambda ops: dict({o: None for o in ops}, _unreadable=True)
GR.poll_exits(price_fn=gpf(1.0)); PA.op_receipts = _ro
check("W11 journal unreadable: fail closed, leg A stays pending and is not marked done/current", not GR.open_positions()[gm]["legs"]["a"]["done"])
PA, F, gm = crash_after_leg_credit()
PA.op_receipts = lambda ops: {o: dict(r, price_kind=None) if r else r for o, r in _ro(ops).items()}
GR.poll_exits(price_fn=gpf(1.0)); PA.op_receipts = _ro
check("W11 receipt without a kind: reconciled as UNKNOWN, never current (%s)" % GR.open_positions()[gm]["legs"]["a"].get("exit_price_kind"), GR.open_positions()[gm]["legs"]["a"].get("exit_price_kind") == "unknown")
PA, F, gm = crash_after_leg_credit()
GR.poll_exits(price_fn=gpf(1.0))
check("W11 control: real receipt carries the original current kind (%s)" % GR.open_positions()[gm]["legs"]["a"].get("exit_price_kind"), GR.open_positions()[gm]["legs"]["a"].get("exit_price_kind") == "current_price_simulated_fill")

# ---- W12. TWAP: crash after a slice's account credit (before the lane write), then stop-out must not oversell
_old_tw = GR.TWAP_SLICES; GR.TWAP_SLICES = 2
try:
    PA, F, CT = fresh(start="10"); lanes = dict(build(PA, CT))
    ok, why = lanes["grad"](1); assert ok, why
    c0 = cash(PA); gm = list(GR.open_positions())[0]
    _d = GR._read(); _p = _d["positions"][gm]
    _p.setdefault("twap", {"slices": 2, "done": 0, "sold_tokens": 0.0, "sold_usd": 0.0})
    FakeJup.mult = 2.0
    r1 = GR._close_twap_slice(_p, _p["entry_price"] * 2.0)       # account credited; the ledger write "crashes" (never persisted)
    FakeJup.mult = 1.0
    sl1 = cash(PA) - c0
    pf = copy.deepcopy(GR._read()["positions"][gm]); pf.setdefault("twap", {"slices": 2, "done": 0, "sold_tokens": 0.0, "sold_usd": 0.0})
    check("W12 after the crash the lane has no slice but the account already holds the credit (done %s, cash +%.4f)" % (pf["twap"]["done"], sl1), pf["twap"]["done"] == 0 and sl1 > 0)
    GR._reconcile_credited_legs(pf)
    check("W12 reconcile finished slice 1 from the receipt (done %s, sold_tokens %.2f, kind %s)" % (pf["twap"]["done"], pf["twap"]["sold_tokens"], pf["twap"].get("price_kinds")),
          pf["twap"]["done"] == 1 and abs(pf["twap"]["sold_tokens"] - Q / 2) < 0.01)
    FakeJup.mult = 0.4
    rr = GR._close_remaining(pf, pf["entry_price"] * 0.4, "hard_stop"); FakeJup.mult = 1.0
    model_rem = 0.5 * Q * PX1 * 0.4 - FEE
    check("W12 stop-out sold ONLY the unsold half once (net %.4f vs model %.4f), account credited slice 1 once + remainder (cash +%.4f vs %.4f)" % (rr, model_rem, cash(PA) - c0, sl1 + model_rem),
          abs(rr - model_rem) < 1e-6 and _cents(cash(PA) - c0) == _cents(sl1 + model_rem))
finally:
    GR.TWAP_SLICES = _old_tw

# ---- W13. an applied credit that cannot be reconciled structurally REFUSES the combined remainder sale (no re-sell)
PA, F, gm = crash_after_leg_credit()
c13 = cash(PA); pos13 = copy.deepcopy(GR.open_positions()[gm])
nfills0 = len(F._locked(lambda j: list(j["fills"]))); _fk13_pre = set(F._locked(lambda j: list(j["fills"])))
PA.op_receipts = lambda ops: dict({o: None for o in ops}, _unreadable=True)
FakeJup.mult = 0.4
r13 = GR._close_remaining(pos13, pos13["entry_price"] * 0.4, "hard_stop"); FakeJup.mult = 1.0
PA.op_receipts = _ro
nfills1 = len(F._locked(lambda j: list(j["fills"])))
# TEST EDIT (disclosed, legacy-fix6 leg-scoped stop): the stop no longer refuses the WHOLE position. It sells ONLY the legs the fill journal proves were
# never sold (b,c: one combined fill), never touches leg a (which has its own journaled fill, credit unknown), and keeps a visibly pending.
_fk13 = F._locked(lambda j: sorted(j["fills"]))
check("W13 unreadable journal + credited-but-unreconciled leg a: stop sells ONLY the proven-unsold leg(s) (one new combined fill %d->%d, no new a fill %s), leg a untouched and visibly pending (%s legs %s), cash unchanged by the stop" % (nfills0, nfills1, not any(":exit:" in k and ":a:" in k and k not in _fk13_pre for k in _fk13), (pos13.get("pending_exit") or {}).get("why"), (pos13.get("pending_exit") or {}).get("legs")),
      nfills1 == nfills0 + 1 and not pos13["legs"]["a"]["done"] and (pos13.get("pending_exit") or {}).get("why") == "applied_credit_unreconciled" and (pos13.get("pending_exit") or {}).get("legs") == sorted(l for l in "abc" if any(":exit:%s:%s:" % (pos13["mint"], l) in k for k in _fk13_pre)) and pos13["legs"]["c"]["done"] and _cents(cash(PA)) == _cents(c13))
PA, F, gm = crash_after_leg_credit()
pos13 = copy.deepcopy(GR.open_positions()[gm]); pa_id = pos13["pa_pos_id"]
PA.op_receipts = lambda ops: {o: dict((_ro([o])[o] or {}), proceeds=123.0) if _ro([o])[o] else None for o in ops}
FakeJup.mult = 0.4
r13b = GR._close_remaining(pos13, pos13["entry_price"] * 0.4, "hard_stop"); FakeJup.mult = 1.0
PA.op_receipts = _ro
check("W13 receipt proceeds disagree with the journaled fill: combined stop sale REFUSED (ret %.4f)" % r13b, r13b == 0.0)

# ---- W14. TWAP: slice 1 done, then the stop-out REMAINDER credit is durable but its lane write is lost; also a stale persisted flag
_old_tw = GR.TWAP_SLICES; GR.TWAP_SLICES = 2
try:
    PA, F, CT = fresh(start="10"); lanes = dict(build(PA, CT))
    ok, why = lanes["grad"](1); assert ok, why
    c0 = cash(PA); gm = list(GR.open_positions())[0]
    _d = GR._read(); _p = _d["positions"][gm]
    _p.setdefault("twap", {"slices": 2, "done": 0, "sold_tokens": 0.0, "sold_usd": 0.0})
    FakeJup.mult = 2.0; GR._close_twap_slice(_p, _p["entry_price"] * 2.0); FakeJup.mult = 1.0
    slice1_state = copy.deepcopy(_p)                       # slice 1 persisted (lane done 1)
    FakeJup.mult = 0.4; rrem = GR._close_remaining(_p, _p["entry_price"] * 0.4, "hard_stop"); FakeJup.mult = 1.0   # remainder credited; lane write "lost"
    cash_after_rem = cash(PA)
    pf = copy.deepcopy(slice1_state); pf["_reconciling"] = True    # also: a stale persisted flag must NOT bypass the guard
    GR._reconcile_credited_legs(pf)
    check("W14 remainder credited but lane write lost: reconcile completes the TWAP remainder from the receipt (remainder %s, no extra cash %.4f)" % (bool(pf["twap"].get("remainder")), cash(PA) - cash_after_rem),
          bool(pf["twap"].get("remainder")) and _cents(cash(PA) - cash_after_rem) == 0)
    check("W14 stale persisted _reconciling flag does not bypass reconcile (remainder recorded above)", bool(pf["twap"].get("remainder")))
    ps2 = copy.deepcopy(slice1_state)
    nf0 = len(F._locked(lambda j: list(j["fills"]))); PA_ro = PA.op_receipts
    PA.op_receipts = lambda ops: dict({o: None for o in ops}, _unreadable=True)
    FakeJup.mult = 0.4; r14 = GR._close_remaining(ps2, ps2["entry_price"] * 0.4, "hard_stop"); FakeJup.mult = 1.0
    PA.op_receipts = PA_ro
    check("W14 unreadable journal with TWAP state: stop REFUSED, no new fill", r14 == 0.0 and len(F._locked(lambda j: list(j["fills"]))) == nf0)
finally:
    GR.TWAP_SLICES = _old_tw

# ---- W15. receipt QTY is checked as well as gross
PA, F, gm = crash_after_leg_credit()
_ro15 = PA.op_receipts
PA.op_receipts = lambda ops: {o: dict(r, qty=r["qty"] * 1.5) if r else r for o, r in _ro15(ops).items()}
pos15 = copy.deepcopy(GR.open_positions()[gm]); ok15 = GR._reconcile_credited_legs(pos15)
PA.op_receipts = _ro15
check("W15 receipt qty disagrees with the leg tokens while gross matches: not reconciled (ret %s, leg a done %s)" % (ok15, pos15["legs"]["a"]["done"]), ok15 is False and not pos15["legs"]["a"]["done"])

# ---- W16. account n_closes increments exactly once across A, B, C and the de-facto leg-C account close
PA, F, CT, lanes, c0, gm = open_to_3x()
FakeJup.mult = 2.0; GR.poll_exits(price_fn=gpf(2.0)); FakeJup.mult = 1.0
gl16 = PA.snapshot()["lanes"]["grad"]
check("W16 position closed; account lane n_closes == 1 (%s), no open account position (%d), basis %.4f" % (gl16.get("n_closes"), len(gl16["positions"]), gl16["open_basis"]),
      gm not in GR.open_positions() and gl16.get("n_closes") == 1 and len(gl16["positions"]) == 0 and abs(gl16["open_basis"]) < 1e-9)

# ---- W17. per-tranche kind split: stale A credited, then current B+C each credited at its own kind (the remainder-kind routing is now vestigial for marked positions: every sold leg is credited at its sale)
PA, F, CT = fresh(start="10"); lanes = dict(build(PA, CT))
ok, why = lanes["grad"](1); assert ok, why
c0 = cash(PA); gm = list(GR.open_positions())[0]
with GR._ledger_lock():
    _d = GR._read(); _p = _d["positions"][gm]
    FakeJup.mult = 3.0
    netA = GR._close_leg(_p, "a", _p["entry_price"] * 3.0, "leg_a_2x", pkind="stale_price_estimate")
    FakeJup.mult = 1.0
    _p["execution"] = dict(_p.get("execution") or {}, source="paper_model")   # B and C go through the NON-sim paper path: since ce0d they ALSO credit per tranche, each at its own kind
    GR._write(_d)
FakeJup.mult = 3.0; GR.poll_exits(price_fn=gpf(3.0)); GR.poll_exits(price_fn=gpf(3.0)); FakeJup.mult = 1.0
FakeJup.mult = 2.0; GR.poll_exits(price_fn=gpf(2.0)); FakeJup.mult = 1.0
rec17 = [c for c in GR._read()["closes"] if c.get("pa_pos_id")]
sn = PA.snapshot()
B17 = leg(0.25, 3)
pend17 = gm in GR.open_positions()
check("W17 (A stale-credited per tranche; B+C non-sim CURRENT credited per tranche, each at its own kind): closed %s; cash_estimated holds only A (%.4f vs %.4f); the B+C remainder stayed spendable (spendable +%.4f)" % (not pend17, sn["cash_estimated"], netA, sn["cash_spendable"] - 9.0),
      not pend17 and _cents(sn["cash_estimated"]) == _cents(netA) and sn["cash_spendable"] - 9.0 > 0.3)
check("W17 close kind label mixes the legs (%s)" % (rec17[-1].get("exit_price_kind") if rec17 else None), bool(rec17) and str(rec17[-1].get("exit_price_kind")).startswith("mixed:"))

# ---- W18. refusal classes: transient refusal keeps the leg pending WITH a diagnostic and recovers; a pre-account orphan converges
PA, F, CT = fresh(start="10"); lanes = dict(build(PA, CT))
ok, why = lanes["grad"](1); assert ok, why
c0 = cash(PA); gm = list(GR.open_positions())[0]
_rc = PA.credit
PA.credit = lambda *a, **k: (False, "account busy (test)") if str(a[3]).startswith("grad:leg:") else _rc(*a, **k)
FakeJup.mult = 3.0; GR.poll_exits(price_fn=gpf(3.0)); FakeJup.mult = 1.0
PA.credit = _rc
p18 = GR.open_positions()[gm]
check("W18 transient refusal: legs stay pending with a persisted diagnostic (%s), cash unchanged" % ((p18.get("pending_exit") or {}).get("why"),),
      not p18["legs"]["a"]["done"] and (p18.get("pending_exit") or {}).get("why") == "tranche_credit_refused" and _cents(cash(PA)) == _cents(c0))
FakeJup.mult = 3.0; GR.poll_exits(price_fn=gpf(3.0)); FakeJup.mult = 1.0
p18 = GR.open_positions()[gm]
check("W18 recovers once the account accepts: legs A+B done, banked once (+%.4f vs %.4f), diagnostic cleared (%s)" % (cash(PA) - c0, A3 + B3, p18.get("pending_exit")),
      p18["legs"]["a"]["done"] and p18["legs"]["b"]["done"] and _cents(cash(PA) - c0) == _cents(A3 + B3) and not p18.get("pending_exit"))
PA, F, CT = fresh(start="10"); lanes = dict(build(PA, CT))
ok, why = lanes["grad"](1); assert ok, why
c0 = cash(PA); gm = list(GR.open_positions())[0]
with GR._ledger_lock():
    _d = GR._read(); _d["positions"][gm]["pa_pos_id"] = "NEVER-IN-ACCOUNT"; GR._write(_d)
FakeJup.mult = 3.0; GR.poll_exits(price_fn=gpf(3.0)); FakeJup.mult = 1.0
p18b = GR.open_positions()[gm]
check("W18 account identity mismatch (stale pos_id, a REAL inconsistency, not a pre-account orphan): refused loudly, leg pending, cash unchanged (%s)" % ((p18b.get("pending_exit") or {}).get("detail", "")[:40],),
      not p18b["legs"]["a"]["done"] and (p18b.get("pending_exit") or {}).get("why") == "tranche_credit_refused" and _cents(cash(PA)) == _cents(c0))

# ---- W19. TWAP with 3 slices: slice 1 persisted, slice 2 credited but its lane write lost (2-of-3 crash), NON-round quantities
_old_tw = GR.TWAP_SLICES; GR.TWAP_SLICES = 3
try:
    PA, F, CT = fresh(start="10"); lanes = dict(build(PA, CT))
    ok, why = lanes["grad"](1); assert ok, why
    c0 = cash(PA); gm = list(GR.open_positions())[0]
    _d = GR._read(); _p = _d["positions"][gm]
    _p.setdefault("twap", {"slices": 3, "done": 0, "sold_tokens": 0.0, "sold_usd": 0.0})
    FakeJup.mult = 2.0
    GR._close_twap_slice(_p, _p["entry_price"] * 2.0)
    persisted = copy.deepcopy(_p)                                 # slice 1 durable in the lane
    n2 = GR._close_twap_slice(_p, _p["entry_price"] * 2.0)       # slice 2: account credited, lane write lost
    FakeJup.mult = 1.0
    cash_s2 = cash(PA)
    pf = copy.deepcopy(persisted)
    okr = GR._reconcile_credited_legs(pf)
    check("W19 2-of-3 crash: reconcile finishes slice 2 (done %s, sold_tokens %.4f vs 2/3 Q %.4f), no extra cash (%.4f), ok %s" % (pf["twap"]["done"], pf["twap"]["sold_tokens"], 2 * Q / 3, cash(PA) - cash_s2, okr),
          okr is True and pf["twap"]["done"] == 2 and abs(pf["twap"]["sold_tokens"] - 2 * Q / 3) < 1e-3 and _cents(cash(PA) - cash_s2) == 0)
    FakeJup.mult = 0.4
    rr = GR._close_remaining(pf, pf["entry_price"] * 0.4, "hard_stop"); FakeJup.mult = 1.0
    model_rem = (Q / 3) * PX1 * 0.4 - FEE
    check("W19 stop-out sells only the last third once (net %.4f vs model %.4f); slices 1+2 credited once each" % (rr, model_rem),
          abs(rr - model_rem) < 1e-6 and _cents(cash(PA) - cash_s2) == _cents(model_rem))
finally:
    GR.TWAP_SLICES = _old_tw

# ---- W5. crash INSIDE the final close, both windows
class Crash(BaseException): pass
def close_with_crash(where):
    PA, F, CT, lanes, c0, gm = open_to_3x()
    real_credit, real_write = PA.credit, GR._write
    state = {"armed": True}
    if where == "before_account_credit":
        def bad(*a, **k):
            if state["armed"]: state["armed"] = False; raise Crash()
            return real_credit(*a, **k)
        PA.credit = bad
    else:   # account credit durable, lane ledger write never lands
        def credit_then_flag(*a, **k):
            r = real_credit(*a, **k)
            if r[0] and state["armed"]: state["credited"] = True
            return r
        def bad(d, *a, **k):
            if state["armed"] and state.get("credited"):
                state["armed"] = False; raise Crash()
            return real_write(d, *a, **k)
        PA.credit = credit_then_flag; GR._write = bad
    crashed = False
    FakeJup.mult = 2.0
    try: GR.poll_exits(price_fn=gpf(2.0))
    except Crash: crashed = True
    finally:
        PA.credit, GR._write = real_credit, real_write; FakeJup.mult = 1.0
    cash_mid = cash(PA)
    restart(PA)
    GR._recover_close_intents()
    FakeJup.mult = 2.0; GR.poll_exits(price_fn=gpf(2.0)); FakeJup.mult = 1.0
    GR.poll_exits(price_fn=gpf(2.0))
    d = GR._read()
    closes = [c for c in d["closes"] if c.get("pa_pos_id")]
    return crashed, c0, cash_mid, cash(PA), gm in GR.open_positions(), len(closes), PA
for where in ("before_account_credit", "after_account_credit_before_lane_write"):
    crashed, c0, cmid, cend, still_open, ncl, PA = close_with_crash(where)
    check("W5[%s] crash injected inside the final close (%s); mid cash %.4f" % (where, crashed, cmid), crashed)
    check("W5[%s] after restart+re-poll: shared cash credited the independent total exactly once (%.4f vs %.4f), position closed, %d close record(s)" % (where, cend - c0, TOTAL, ncl),
          _cents(cend - c0) == _cents(TOTAL) and not still_open and ncl == 1)

print("FAILS: %d" % len(FAILS))
sys.exit(1 if FAILS else 0)
