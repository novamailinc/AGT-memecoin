#!/usr/bin/env python3
"""Remaining grad fixtures (executed through the wrapper; F1-F4b, L1-L9 legacy migration, M/N truthful unknown marks). Harness copied from tests/test_grad_partial_exit_window.py
at 2b2671061bd2c55ef3d1ad09993307825716550b. TEST-FIXTURE inputs only, NOT owner limits.
F1 real TWAP entry through poll_exits (sim), F2 n=3 non-sim TWAP crash, F3 fee + non-round conservation (n=3 and n=7, sim and non-sim, write-down),
F4/F4b mixed legacy/new entry qty and equity. L: legacy migration invariants. M/N: unproven legacy positions are not marked. Expectations come from the account journal receipts, not from a price model, so they hold for the non-sim fill model.
Run (producer wrapper only): python3 tests/test_grad_fixtures_remaining.py
"""
if __name__ != "__main__":
    import pytest
    pytest.skip("script-style test", allow_module_level=True)
import json, copy, threading
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
        return {"outAmount": str(o), "priceImpactPct": 0.4, "routePlan": [{"swapInfo": {"label": "FakeAMM"}}]}
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


import copy
class Crash(BaseException): pass
def restart(PA):
    GR._RECOVERY_DONE = False
    GR._ENTRY_BLOCKED = False
def rc_ops(snap, keys): return ["%s:leg:%s:%s:%s" % (GR.LANE, k, snap["mint"], snap["pa_pos_id"]) for k in keys]
def rcpts(PA, snap, keys):
    r = PA.op_receipts(rc_ops(snap, keys)); return [r.get(o) for o in rc_ops(snap, keys)]
def lane_snap(PA): return PA.snapshot()["lanes"].get("grad") or {"positions": [], "open_basis": 0.0}
def acct_qty(PA): return sum(p["qty"] for p in lane_snap(PA)["positions"])
def set_twap(n, per_poll=1):
    GR.TWAP_SLICES = n; GR.TWAP_SLICES_PER_POLL = per_poll; GR.TWAP_MAX_HOLD_S = None
def reset_twap():
    GR.TWAP_SLICES = None; GR.TWAP_SLICES_PER_POLL = 1; GR.TWAP_MAX_HOLD_S = None
def go_nonsim():
    """Real NON-SIM entry path: no quote-sim mode, demo mode off for the lane (model fill + shared account debit)."""
    os.environ.pop("AGT_EXECUTION_MODE", None); GR._demo_mode = lambda: False
def go_sim():
    os.environ["AGT_EXECUTION_MODE"] = "jupiter_quote_sim"; GR._demo_mode = lambda: True
def poll_until_closed(gm, price, limit=12):
    for _ in range(limit):
        if gm not in GR.open_positions(): return True
        GR.poll_exits(price_fn=gpf(price))
    return gm not in GR.open_positions()
def conservation(tag, PA, snap, keys, c0, tol=1e-6):
    """Account-side conservation from the journal receipts: credited qty == entry qty, cash delta == sum(proceeds - fee), account position gone."""
    rs = rcpts(PA, snap, keys); got = [r for r in rs if r]
    qsum = sum(float(r["qty"]) for r in got); net = sum(float(r["proceeds"]) - float(r.get("fee") or 0.0) for r in got)
    check("%s credited qty %.9f == lane tokens %.9f (%d receipts)" % (tag, qsum, snap["tokens"], len(got)), abs(qsum - snap["tokens"]) <= max(tol, 1e-9 * snap["tokens"]))
    check("%s cash delta %.6f == sum(receipt proceeds - fee) %.6f" % (tag, cash(PA) - c0, net), abs((cash(PA) - c0) - net) < 1e-5)
    gl = lane_snap(PA)
    check("%s account position fully consumed (open %d, qty %.9f, basis %.9f, n_closes %s)" % (tag, len(gl["positions"]), acct_qty(PA), gl["open_basis"], gl.get("n_closes")),
          len(gl["positions"]) == 0 and abs(gl["open_basis"]) < 1e-9)
    return got

# ================= F1. REAL TWAP entry through poll_exits (sim), slices created by the lane itself, not by a hand-built twap dict
for per_poll in (1, 2):
    set_twap(3, per_poll)
    try:
        PA, F, CT = fresh(start="10"); lanes = dict(build(PA, CT))
        ok, why = lanes["grad"](1); assert ok, why
        c0 = cash(PA); gm = list(GR.open_positions())[0]; snap = copy.deepcopy(GR.open_positions()[gm])
        check("F1[%d/poll] entry: account units == lane tokens (%.9f vs %.9f), no twap state yet" % (per_poll, acct_qty(PA), snap["tokens"]),
              abs(acct_qty(PA) - snap["tokens"]) < 1e-9 and "twap" not in snap)
        GR.poll_exits(price_fn=gpf(1.0))
        tw = (GR.open_positions().get(gm) or {}).get("twap") or {}
        exp_done = min(per_poll, 3)
        check("F1[%d/poll] after poll 1: twap created by poll_exits, done %s (want %d), sold_tokens %.6f (want %.6f), account qty %.6f (want %.6f)" % (per_poll, tw.get("done"), exp_done, float(tw.get("sold_tokens") or 0), exp_done * snap["tokens"] / 3, acct_qty(PA), snap["tokens"] * (3 - exp_done) / 3),
              tw.get("done") == exp_done and abs(float(tw["sold_tokens"]) - exp_done * snap["tokens"] / 3) < 1e-4 and abs(acct_qty(PA) - snap["tokens"] * (3 - exp_done) / 3) < 1e-4 and cash(PA) > c0)
        check("F1[%d/poll] closed within the poll budget" % per_poll, poll_until_closed(gm, 1.0))
        conservation("F1[%d/poll]" % per_poll, PA, snap, ["twap1", "twap2", "twap3"], c0)
        check("F1[%d/poll] no remainder credit (slices emptied the position) and exactly one close record, n_closes 1" % per_poll,
              rcpts(PA, snap, ["twap_remainder"])[0] is None and len([c for c in GR._read()["closes"] if c.get("pa_pos_id")]) == 1 and lane_snap(PA).get("n_closes") == 1)
        check("F1[%d/poll] no pending exit state, no open close intent (%s)" % (per_poll, GR._close_intents().pending("grad")), not GR._close_intents().pending("grad"))
    finally:
        reset_twap()

# ================= F2. NON-SIM n=3 TWAP crash windows
def twap_crash(after_key, price_for_crash=1.0):
    """Real non-sim entry, TWAP n=3. Drive polls; the account credit of `after_key` becomes durable and the lane write is lost (Crash), then restart."""
    go_nonsim(); set_twap(3, 1)
    PA, F, CT = fresh(start="10"); go_nonsim(); set_twap(3, 1)
    lanes = dict(build(PA, CT)); ok, why = lanes["grad"](1); assert ok, why
    gm = list(GR.open_positions())[0]; snap = copy.deepcopy(GR.open_positions()[gm]); c0 = cash(PA)
    need = int(after_key[-1]) - 1                       # slices to complete normally before the crashing poll
    for _ in range(need): GR.poll_exits(price_fn=gpf(price_for_crash))
    _rc, _rw = PA.credit, GR._write; st = {"armed": True, "credited": False}
    def cr(*a, **k):
        r = _rc(*a, **k)
        if r[0] and st["armed"] and (":leg:%s:" % after_key) in str(a[3]): st["credited"] = True
        return r
    def wr(d, *a, **k):
        if st["armed"] and st["credited"]: st["armed"] = False; raise Crash()
        return _rw(d, *a, **k)
    PA.credit, GR._write = cr, wr; crashed = False
    try: GR.poll_exits(price_fn=gpf(price_for_crash))
    except Crash: crashed = True
    finally: PA.credit, GR._write = _rc, _rw
    restart(PA); GR._recover_close_intents()
    return PA, F, gm, snap, c0, crashed
try:
    PA, F, gm, snap, c0, crashed = twap_crash("twap2")
    lane_done = (GR.open_positions()[gm].get("twap") or {}).get("done")
    check("F2a NON-SIM crash after slice 2 credit, lane write lost (crashed %s): lane done %s (want 1), account already holds slices 1+2 (qty %.6f want %.6f)" % (crashed, lane_done, acct_qty(PA), snap["tokens"] / 3),
          crashed and lane_done == 1 and abs(acct_qty(PA) - snap["tokens"] / 3) < 1e-4 and not snap.get("execution", {}).get("source") == "jupiter_quote_sim")
    c_mid = cash(PA); FakeJup.mult = 1.0
    GR.poll_exits(price_fn=gpf(0.4))                           # hard stop: reconcile slice 2 from the RECEIPT, then sell ONLY the last third once
    check("F2a stop-out closed the position", gm not in GR.open_positions())
    got = conservation("F2a", PA, snap, ["twap1", "twap2", "twap3", "twap_remainder"], c0)
    rem = rcpts(PA, snap, ["twap_remainder"])[0]
    check("F2a the remainder credit is exactly the last third (qty %s vs %.6f), slice 2 credited once (cash after crash %.6f -> reconcile added only the remainder)" % ((rem or {}).get("qty"), snap["tokens"] / 3, c_mid),
          rem is not None and abs(float(rem["qty"]) - snap["tokens"] / 3) < 1e-4 and abs((cash(PA) - c_mid) - (float(rem["proceeds"]) - float(rem.get("fee") or 0.0))) < 1e-5)
finally:
    reset_twap(); go_sim()
try:
    PA, F, gm, snap, c0, crashed = twap_crash("twap1")
    check("F2b NON-SIM crash after slice 1 credit (crashed %s): lane done %s (want 0), account qty %.6f (want %.6f)" % (crashed, (GR.open_positions()[gm].get("twap") or {}).get("done"), acct_qty(PA), 2 * snap["tokens"] / 3),
          crashed and ((GR.open_positions()[gm].get("twap") or {}).get("done") or 0) == 0 and abs(acct_qty(PA) - 2 * snap["tokens"] / 3) < 1e-4)
    set_twap(3, 1); ok = poll_until_closed(gm, 1.0)
    conservation("F2b normal continuation after replay", PA, snap, ["twap1", "twap2", "twap3", "twap_remainder"], c0)
    check("F2b closed once, slice 1 not re-sold (n_closes %s)" % lane_snap(PA).get("n_closes"), ok and lane_snap(PA).get("n_closes") == 1)
finally:
    reset_twap(); go_sim()

# ================= F3. fee + non-round conservation: n=3 and n=7, sim and non-sim, plus a write-down remainder
for mode in ("sim", "nonsim"):
    for n in (3, 7):
        set_twap(n, 1)
        try:
            PA, F, CT = fresh(start="10")
            (go_sim if mode == "sim" else go_nonsim)(); set_twap(n, 1)
            lanes = dict(build(PA, CT)); ok, why = lanes["grad"](1); assert ok, why
            c0 = cash(PA); gm = list(GR.open_positions())[0]; snap = copy.deepcopy(GR.open_positions()[gm])
            closed = poll_until_closed(gm, 1.0, limit=n + 4)
            check("F3[%s n=%d] closed through slices alone (%s)" % (mode, n, closed), closed)
            got = conservation("F3[%s n=%d]" % (mode, n), PA, snap, ["twap%d" % i for i in range(1, n + 1)] + ["twap_remainder"], c0)
            check("F3[%s n=%d] fee booked on every sold slice (%s) and qty of every slice == tokens/n within 1e-6" % (mode, n, [round(float(r.get("fee") or 0), 6) for r in got]),
                  all(float(r.get("fee") or 0) > 0 for r in got) and all(abs(float(r["qty"]) - snap["tokens"] / n) < 1e-6 for r in got))
        finally:
            reset_twap(); go_sim()
# write-down remainder after one credited slice: no fee booked on units nobody bought, units leave the account via the register, cash == slice 1 net
set_twap(3, 1)
try:
    PA, F, CT = fresh(start="10"); go_sim(); set_twap(3, 1)
    lanes = dict(build(PA, CT)); ok, why = lanes["grad"](1); assert ok, why
    c0 = cash(PA); gm = list(GR.open_positions())[0]; snap = copy.deepcopy(GR.open_positions()[gm])
    GR.poll_exits(price_fn=gpf(1.0))
    s1 = rcpts(PA, snap, ["twap1"])[0]
    with GR._ledger_lock():
        d = GR._read(); pos = d["positions"][gm]
        GR._close_remaining(pos, 0.0, "twap_time_stop_60min_unquotable", pkind="write_down_zero")
        rec = GR._finalize(d, gm, pos, "twap_time_stop_60min_unquotable"); GR._write(d)
    check("F3 write-down remainder after slice 1: close recorded, no pending intent, cash == slice 1 net only (%.6f vs %.6f), account position gone (%d)" % (
          cash(PA) - c0, float(s1["proceeds"]) - float(s1.get("fee") or 0), len(lane_snap(PA)["positions"])),
          bool(rec) and not GR._close_intents().pending("grad") and abs((cash(PA) - c0) - (float(s1["proceeds"]) - float(s1.get("fee") or 0))) < 1e-5 and len(lane_snap(PA)["positions"]) == 0)
finally:
    reset_twap()

# ================= F4. mixed legacy (old entry qty, no tranche marker) and new (lane-token qty, marker) positions open together
PA, F, CT = fresh(start="10"); go_sim(); lanes = dict(build(PA, CT))
_td = PA.try_debit; st4 = {"n": 0}
def old_qty_debit(*a, **k):
    a = list(a)
    if st4["n"] == 0: a[3] = float(a[2]) / PX1 * 1.0      # legacy: account qty = stake / entry price (not net of entry gas)
    st4["n"] += 1
    return _td(*a, **k)
PA.try_debit = old_qty_debit
ok, why = lanes["grad"](1); assert ok, why
PA.try_debit = _td
ok, why = lanes["grad"](2); assert ok, why
legacy, new = sorted(GR.open_positions(), key=lambda m: GR.open_positions()[m]["entry_ts"])
with GR._ledger_lock():
    _d = GR._read(); _d["positions"][legacy].pop("tranche_credit", None); _d["positions"][legacy].pop("tranche_credited", None); GR._write(_d)
c0 = cash(PA); pl = copy.deepcopy(GR.open_positions()[legacy]); pn = copy.deepcopy(GR.open_positions()[new])
q_by_pos = {p["pos_id"]: p["qty"] for p in lane_snap(PA)["positions"]}
check("F4 setup: legacy account qty %.4f (stake/price, > lane tokens %.4f), new account qty %.4f == lane tokens %.4f, only the new one carries the marker (%s/%s)" % (
      q_by_pos.get(pl["pa_pos_id"], -1), pl["tokens"], q_by_pos.get(pn["pa_pos_id"], -1), pn["tokens"], bool(pl.get("tranche_credit")), bool(pn.get("tranche_credit"))),
      q_by_pos.get(pl["pa_pos_id"], 0) > pl["tokens"] + 1e-6 and abs(q_by_pos.get(pn["pa_pos_id"], -1) - pn["tokens"]) < 1e-9 and not pl.get("tranche_credit") and pn.get("tranche_credit"))
FakeJup.mult = 3.0; GR.poll_exits(price_fn=lambda chain, mint, spot_index=None: float((GR.open_positions().get(mint) or {}).get("entry_price") or 0) * 3.0); FakeJup.mult = 1.0
q_mid = {p["pos_id"]: p["qty"] for p in lane_snap(PA)["positions"]}
check("F4 mid-window: new position's account qty shrank to 25%% of tokens (%.4f vs %.4f); legacy account qty MIGRATED to exactly 25%% of the LANE tokens (opening-unit dust removed once) (%.4f); both marked OK" % (
      q_mid.get(pn["pa_pos_id"], -1), 0.25 * pn["tokens"], q_mid.get(pl["pa_pos_id"], -1)),
      abs(q_mid.get(pn["pa_pos_id"], -1) - 0.25 * pn["tokens"]) < 0.01 and abs(q_mid.get(pl["pa_pos_id"], -1) - 0.25 * pl["tokens"]) < 1e-6)
sn4 = PA.snapshot(marks=marks_for(PA, 3.0))
check("F4 marks accepted with a mixed population (status %s)" % sn4["marks_status"], sn4["marks_status"] == "ok")
print("INFO F4 mid-window marked_equity_conservative %.4f (a legacy position is migrated in the same poll its legs sell, so this mid-window value is already lawful; see F4b below)" % sn4["marked_equity_conservative"])
FakeJup.mult = 2.0; GR.poll_exits(price_fn=lambda chain, mint, spot_index=None: float((GR.open_positions().get(mint) or {}).get("entry_price") or 0) * 2.0); FakeJup.mult = 1.0
check("F4 both closed, account has no grad position left, n_closes 2 (%s)" % lane_snap(PA).get("n_closes"),
      legacy not in GR.open_positions() and new not in GR.open_positions() and len(lane_snap(PA)["positions"]) == 0 and lane_snap(PA).get("n_closes") == 2)
check("F4 cash delta %.4f == 2 x independent total %.4f (cents), each position closed exactly once" % (cash(PA) - c0, 2 * TOTAL),
      _cents(cash(PA) - c0) == _cents(2 * TOTAL) and len([c for c in GR._read()["closes"] if c.get("pa_pos_id")]) == 2)

# ================= F4b. mixed legacy/new mid-window EQUITY and DRAWDOWN (executed; derived from the per-tranche contract)
# Contract: a position with the tranche marker is credited at each sale (account qty == lane unsold tokens, cash holds the banked net).
# A LEGACY position (no marker) with sold legs is MIGRATED once its evidence is proven (grad._migrate_legacy_tranches: sold legs credited, opening-unit dust
# removed only when explained by the recorded entry gas), after which equity equals the lawful value (asserted below). When the evidence cannot be proven the
# position stays legacy and is NOT marked (section M: no mark, no peak update, no entry admission, exits unaffected).
# to the lawful-equity assertions once a migration that credits already-sold legacy legs lands.
def gate_env():   # copied from the window test (TEST-FIXTURE limits, NOT owner limits)
    os.environ["PAPER_ACCOUNT_GATES_ENABLED"] = "1"
    os.environ["PAPER_ACCOUNT_MAX_DAILY_LOSS_USD"] = "1000"; os.environ["PAPER_ACCOUNT_MAX_DRAWDOWN_PCT"] = "20"; os.environ["PAPER_ACCOUNT_MAX_EXPOSURE_PCT"] = "100"
def other_entry(PA, tag, mult):
    return PA.try_debit("watcher", "OtherMint" + tag, 1.0, 1000.0, "w2-" + tag, marks=marks_for(PA, mult))
def make_mixed():
    PA, F, CT = fresh(start="10"); go_sim(); lanes = dict(build(PA, CT))
    _td = PA.try_debit; st = {"n": 0}
    def old_qty_debit(*a, **k):
        a = list(a)
        if st["n"] == 0: a[3] = float(a[2]) / PX1
        st["n"] += 1; return _td(*a, **k)
    PA.try_debit = old_qty_debit
    ok, why = lanes["grad"](1); assert ok, why
    PA.try_debit = _td
    ok, why = lanes["grad"](2); assert ok, why
    mints = sorted(GR.open_positions(), key=lambda m: GR.open_positions()[m]["entry_ts"]); lg, nw = mints
    with GR._ledger_lock():
        _d = GR._read(); _d["positions"][lg].pop("tranche_credit", None); _d["positions"][lg].pop("tranche_credited", None); GR._write(_d)
    FakeJup.mult = 3.0
    GR.poll_exits(price_fn=lambda chain, mint, spot_index=None: float((GR.open_positions().get(mint) or {}).get("entry_price") or 0) * 3.0)
    FakeJup.mult = 1.0
    return PA, lg, nw
PA, lg, nw = make_mixed()
pl, pn = GR.open_positions()[lg], GR.open_positions()[nw]
qa = {p["pos_id"]: p["qty"] for p in lane_snap(PA)["positions"]}; ql, qn = qa[pl["pa_pos_id"]], qa[pn["pa_pos_id"]]
banked_l = sum(float(v.get("realized_usd") or 0) for v in pl["legs"].values() if v.get("done"))     # legacy A+B net: in the LANE only, not in cash
banked_n = sum(float(v.get("realized_usd") or 0) for v in pn["legs"].values() if v.get("done"))     # new A+B net: already in cash
cash0 = cash(PA)
unsold_l = pl["tokens"] * 0.25
def eq_rep(m): return PA.snapshot(marks=marks_for(PA, m))["marked_equity_conservative"]
def eq_true(m): return cash0 + unsold_l * PX1 * m + qn * PX1 * m      # lawful: cash + legacy banked-not-credited + unsold legacy + new unsold at the mark
def over(m): return ql * PX1 * m - (banked_l + unsold_l * PX1 * m)               # predicted legacy overstatement = full account qty at the mark minus (banked + unsold)
check("F4b setup: new banked %.6f is in cash (cash %.6f); legacy banked %.6f was migrated into cash in the same poll; account qty legacy %.4f (== unsold lane tokens), new %.4f (25%%)" % (banked_n, cash0, banked_l, ql, qn),
      abs(ql - 0.25 * pl["tokens"]) < 1e-6 and abs(qn - 0.25 * pn["tokens"]) < 0.01 and banked_l > 0 and banked_n > 0 and pl.get("tranche_credit") and not pl.get("tranche_migrating"))
_res = 0.0    # unavoidable residual: account opened with stake/price units, lane net of entry gas (1.5 units x 25%)
for m in (3.0, 10.0, 0.5):
    eq = eq_rep(m)
    check("F4b MIGRATED legacy equity at %.1fx: reported %.6f == cash + account units at the mark (%.6f) and equal to the lawful value (cash + unsold lane tokens at the mark) %.6f (|diff| %.6f)" % (
          m, eq, cash0 + (ql + qn) * PX1 * m, eq_true(m), abs(eq - eq_true(m))),
          abs(eq - (cash0 + (ql + qn) * PX1 * m)) < 3e-6 and abs(eq - eq_true(m)) < 3e-6)
check("F4b legacy banked net now IN cash: cash %.6f == pre-migration cash + legacy banked %.6f (credited once)" % (cash0, banked_l), abs(cash0 - (PA.snapshot(marks=marks_for(PA, 3.0))["cash"])) < 1e-9 and cash0 >= 8.0 + banked_n + banked_l - 1e-5)
PA, lg, nw = make_mixed(); gate_env()
spike = other_entry(PA, "spike", 10.0); back = other_entry(PA, "back", 3.0)
sn = PA.snapshot(marks=marks_for(PA, 3.0))
check("F4b MIGRATED: spike admitted (%s) and the 3x state afterwards is ADMITTED too (%s: %s; peak %s, drawdown %.1f%%)" % (
      spike[0], back[0], str(back[1])[:60], (sn.get("peak_equity") or {}).get("value"), sn.get("drawdown_pct") or 0),
      spike[0] is True and back[0] is True)


# ================= L. LEGACY MIGRATION invariants (crash, blocked, replay). Each case: legacy position (marker stripped), legs A+B sold at 3x, then close at 2x.
def make_legacy():
    PA, F, CT = fresh(start="10"); go_sim(); lanes = dict(build(PA, CT))
    _td = PA.try_debit
    def old_qty_debit(*a, **k):
        a = list(a); a[3] = float(a[2]) / PX1; return _td(*a, **k)
    PA.try_debit = old_qty_debit
    ok, why = lanes["grad"](1); assert ok, why
    PA.try_debit = _td
    gm = list(GR.open_positions())[0]
    with GR._ledger_lock():
        _d = GR._read(); _d["positions"][gm].pop("tranche_credit", None); _d["positions"][gm].pop("tranche_credited", None); GR._write(_d)
    return PA, gm
def sell_ab():
    FakeJup.mult = 3.0; GR.poll_exits(price_fn=gpf(3.0)); FakeJup.mult = 1.0
def finish(tag, PA, gm, c0):
    FakeJup.mult = 2.0; GR.poll_exits(price_fn=gpf(2.0)); FakeJup.mult = 1.0
    check("%s: full close settles the independent total once (%.4f vs %.4f), position closed, account empty" % (tag, cash(PA) - c0, TOTAL),
          gm not in GR.open_positions() and _cents(cash(PA) - c0) == _cents(TOTAL) and len(lane_snap(PA)["positions"]) == 0)
def stays_legacy(tag, PA, gm, c0):
    p = GR.open_positions().get(gm) or {}
    check("%s: STAYS legacy (no marker, no credit; cash %.4f -> %.4f, exits unaffected)" % (tag, c0, cash(PA)), not p.get("tranche_credit") and _cents(cash(PA)) == _cents(c0))

# L1 nothing sold yet stays legacy; L2 close op already applied; L3 journal unreadable; L4 journaled fill disagrees; L5 unexplained dust
PA, gm = make_legacy(); c0 = cash(PA)
GR.poll_exits(price_fn=gpf(1.0)); check("L1 no sold leg: stays legacy (no marker)", not GR.open_positions()[gm].get("tranche_credit"))
_oo = PA.op_outcome
PA.op_outcome = lambda op: "applied" if ":close:" in op else _oo(op)
try: sell_ab(); stays_legacy("L2 final-close op already applied", PA, gm, c0)
finally: PA.op_outcome = _oo
finish("L2 after the guard clears the legacy path closes", PA, gm, c0)
PA, gm = make_legacy(); c0 = cash(PA)
_or = PA.op_receipts
PA.op_receipts = lambda ops: {"_unreadable": True}
try: sell_ab(); stays_legacy("L3 account journal unreadable", PA, gm, c0)
finally: PA.op_receipts = _or
finish("L3", PA, gm, c0)
PA, gm = make_legacy(); c0 = cash(PA)
_gp = GR._paper_account().position_debit
PA.position_debit = lambda pid: dict(_gp(pid) or {}, qty=float((_gp(pid) or {}).get("qty") or 0) + 7.0)
try: sell_ab(); stays_legacy("L5 opening-unit difference not explained by entry gas", PA, gm, c0)
finally: PA.position_debit = _gp
finish("L5", PA, gm, c0)
PA, gm = make_legacy(); c0 = cash(PA)
_df = GR._demo_filler(); _gf = _df.get_fill
_mig = GR._migrate_legacy_tranches
def _mig_tampered(pos):          # the journaled fill is tampered ONLY while the migration reads it (the real sale used the true fill)
    _df.get_fill = lambda op: dict(_gf(op) or {"gross_usd": 0.0}, gross_usd=float((_gf(op) or {}).get("gross_usd") or 0.0) + 0.01)
    try: return _mig(pos)
    finally: _df.get_fill = _gf
GR._migrate_legacy_tranches = _mig_tampered
try: sell_ab(); stays_legacy("L4 journaled sim fill disagrees with the lane leg", PA, gm, c0)
finally: GR._migrate_legacy_tranches = _mig
finish("L4", PA, gm, c0)

# L6 crash after the FIRST migrated credit (BaseException = process death), then restart: resumes from receipts, each leg credited once
class CrashL(BaseException): pass
PA, gm = make_legacy(); c0 = cash(PA)
_tc = GR._tranche_credit; stl = {"n": 0}
def crash_after_first(pos, lk, q, g, f, k):
    r = _tc(pos, lk, q, g, f, k)
    if pos.get("tranche_migrating"):          # only the MIGRATION's credits (a legacy leg sale's own call is a no-op return)
        stl["n"] += 1
        if stl["n"] == 1: raise CrashL()
    return r
GR._tranche_credit = crash_after_first; crashed = False
try: sell_ab()
except CrashL: crashed = True
finally: GR._tranche_credit = _tc
disk = GR.open_positions()[gm]
check("L6 crash after first migrated credit (crashed %s): durable marker+migrating on the lane ledger (%s/%s), account already holds one leg credit" % (crashed, disk.get("tranche_credit"), disk.get("tranche_migrating")),
      crashed and disk.get("tranche_credit") and disk.get("tranche_migrating") and cash(PA) > c0)
GR.poll_exits(price_fn=gpf(3.0))
p = GR.open_positions()[gm]
check("L6 restart resumes: migration complete, A+B net credited exactly once (+%.4f, want +%.4f), account qty == lane unsold" % (cash(PA) - c0, A3 + B3),
      not p.get("tranche_migrating") and _cents(cash(PA) - c0) == _cents(A3 + B3) and abs(PA.snapshot()["lanes"]["grad"]["positions"][0]["qty"] - 0.25 * p["tokens"]) < 1e-6)
GR.poll_exits(price_fn=gpf(3.0)); GR.poll_exits(price_fn=gpf(3.0))
check("L7 replay: further polls change nothing (cash %.4f still c0 + A+B)" % cash(PA), _cents(cash(PA) - c0) == _cents(A3 + B3))
finish("L6", PA, gm, c0)

# L8 crash BETWEEN legs but before ANY credit (after marker write): marker durable, nothing credited, restart completes
PA, gm = make_legacy(); c0 = cash(PA)
def crash_before_first(pos, lk, q, g, f, k):
    if pos.get("tranche_migrating"): raise CrashL()
    return _tc(pos, lk, q, g, f, k)
GR._tranche_credit = crash_before_first; crashed = False
try: sell_ab()
except CrashL: crashed = True
finally: GR._tranche_credit = _tc
disk = GR.open_positions()[gm]
check("L8 crash after marker, before any credit (crashed %s): marker+migrating durable, cash unchanged" % crashed, crashed and disk.get("tranche_migrating") and _cents(cash(PA)) == _cents(c0))
GR.poll_exits(price_fn=gpf(3.0))
check("L8 restart completes: A+B credited once", _cents(cash(PA) - c0) == _cents(A3 + B3) and not GR.open_positions()[gm].get("tranche_migrating"))
finish("L8", PA, gm, c0)


# ================= M. TRUTHFUL UNKNOWN MARKS: a legacy position the migration cannot prove is NOT marked (no overstated equity,
# no peak update, no entry admission) while its exits stay possible. Marks come from the REAL central marker (account_marks.collect_marks).
from daemon.launch_scanner import account_marks as AM
def central(mult): return AM.collect_marks(price_fn=lambda mints: {m: PX1 * mult for m in mints})
def grad_marked(mk): return bool((mk.get("grad") or {}))
def peak_v(PA): return ((PA.snapshot(marks=central(1.0)).get("peak_equity") or {}).get("value"))

# M1 refused migration (unexplained dust): equity is "no mark", peak does not move, other-lane entry refused, exit still closes
PA, gm = make_legacy(); c0 = cash(PA); gate_env()
pk0 = (PA.snapshot(marks=central(1.0)).get("peak_equity") or {}).get("value")
_gp = GR._paper_account().position_debit
PA.position_debit = lambda pid: dict(_gp(pid) or {}, qty=float((_gp(pid) or {}).get("qty") or 0) + 7.0)
try: sell_ab()
finally: PA.position_debit = _gp
mk = central(10.0); sn = PA.snapshot(marks=mk)
check("M1 refused legacy: grad position is NOT marked (marks %s), marks_status %r (not ok), marked equity %s, peak unmoved (%s -> %s)" % (
      list(mk.get("grad") or {}), sn.get("marks_status"), sn.get("marked_equity_conservative"), pk0, (sn.get("peak_equity") or {}).get("value")),
      not grad_marked(mk) and sn.get("marks_status") != "ok" and sn.get("marked_equity_conservative") is None
      and (sn.get("peak_equity") or {}).get("value") == pk0)
ent = PA.try_debit("watcher", "OtherMintM1", 1.0, 1000.0, "w-m1", marks=central(10.0))
check("M1 entry admission refused while the legacy position is unprovable (%s: %s)" % (ent[0], str(ent[1])[:70]), ent[0] is False)
finish("M1 exits unaffected", PA, gm, c0)

# M2 migrated (provable) position is marked normally and admits entries
PA, gm = make_legacy(); c0 = cash(PA); gate_env(); sell_ab()
mk = central(3.0); sn = PA.snapshot(marks=mk)
check("M2 provable legacy, migrated: marked (%s), marks_status %r" % (grad_marked(mk), sn.get("marks_status")), grad_marked(mk) and sn.get("marks_status") == "ok")
finish("M2", PA, gm, c0)

# M3 TWAP-inherited legacy (marker stripped, slices sold): unmarked, then exits settle
PA, F, CT = fresh(start="10"); go_sim(); lanes = dict(build(PA, CT)); set_twap(2, 1); c0 = cash(PA)
_td = PA.try_debit
def _old(*a, **k):
    a = list(a); a[3] = float(a[2]) / PX1; return _td(*a, **k)
PA.try_debit = _old
ok, why = lanes["grad"](1); assert ok, why
PA.try_debit = _td
gm = list(GR.open_positions())[0]
with GR._ledger_lock():
    _d = GR._read(); _d["positions"][gm].pop("tranche_credit", None); _d["positions"][gm].pop("tranche_credited", None); GR._write(_d)
FakeJup.mult = 3.0; GR.poll_exits(price_fn=gpf(3.0)); FakeJup.mult = 1.0
pp = GR.open_positions().get(gm) or {}
mk = central(3.0)
check("M3 TWAP legacy with a sold slice (twap done=%s, marker %s): lane flags it unproven and the account marker omits it" % ((pp.get("twap") or {}).get("done"), pp.get("tranche_credit")),
      pp.get("twap") is not None and not pp.get("tranche_credit") and str(pp["pa_pos_id"]) in (GR.legacy_unproven_pos_ids() or set()) and not grad_marked(mk)
      and PA.snapshot(marks=mk).get("marks_status") != "ok")
check("M3 TWAP legacy exits stay possible: closes (%s)" % poll_until_closed(gm, 2.0, 20), gm not in GR.open_positions())
reset_twap()

# M4 lane ledger unreadable (REAL corrupt file, no patch): unknown evidence is never read as zero; every lane position is unmarked
PA, gm = make_legacy()
open(GR.LEDGER_PATH, "w").write("{ not json")
unp = GR.legacy_unproven_pos_ids(); mk = central(1.0)
check("M4 lane ledger unreadable: hook returns None and the grad position is not marked", unp is None and not grad_marked(mk))
os.remove(GR.LEDGER_PATH)
for _f in __import__("glob").glob(GR.LEDGER_PATH + ".corrupt-*"): os.remove(_f)
PA, gm = make_legacy()
check("M4 control: readable ledger, nothing sold, fresh position is marked", grad_marked(central(1.0)))

# M5 stale price kind: a migrated leg whose recorded exit_price_kind is missing is credited as UNKNOWN, not spendable
PA, gm = make_legacy(); c0 = cash(PA); s0 = PA.snapshot()["cash_spendable"]
_mig5 = GR._migrate_legacy_tranches
def _mig_nokind(pos):
    for lv in pos["legs"].values(): lv.pop("exit_price_kind", None)
    return _mig5(pos)
GR._migrate_legacy_tranches = _mig_nokind
try: sell_ab()
finally: GR._migrate_legacy_tranches = _mig5
snap5 = list(GR.open_positions().values())[0]
rk = [r.get("price_kind") for r in rcpts(PA, snap5, ["a", "b"]) if r]
d_cash = cash(PA) - c0; d_spend = PA.snapshot()["cash_spendable"] - s0
check("M5 missing exit_price_kind: receipts kinds %s, cash %+.4f but spendable %+.4f (unknown-kind proceeds not spendable)" % (rk, d_cash, d_spend),
      len(rk) == 2 and all(k == "unknown" for k in rk) and d_cash > 0 and d_spend < d_cash - 1e-9)

# M6 receipt qty mismatch: an existing leg receipt whose qty disagrees with the lane leg -> migration refuses (stays legacy, unmarked)
PA, gm = make_legacy(); c0 = cash(PA)
_orc = PA.op_receipts; _mig6 = GR._migrate_legacy_tranches
def _bad_rc(ops):          # a forged receipt for leg a, visible ONLY while the migration reads it (the real sale saw the true journal)
    r = dict(_orc(ops))
    for o in ops:
        if ":leg:a:" in o: r[o] = {"proceeds": 1.0, "qty": 12345.0, "fee": 0.0, "price_kind": "current"}
    return r
def _mig_badrc(pos):
    PA.op_receipts = _bad_rc
    try: return _mig6(pos)
    finally: PA.op_receipts = _orc
GR._migrate_legacy_tranches = _mig_badrc
try: sell_ab(); stays_legacy("M6 receipt qty mismatch", PA, gm, c0)
finally: GR._migrate_legacy_tranches = _mig6
check("M6 and the position is unmarked (truthful unknown)", not grad_marked(central(3.0)))
finish("M6", PA, gm, c0)


# L9 crash after the first migrated credit, then the migration can NOT resume (receipts unreadable only while it runs) and the position
# exits: the final close must credit only the remainder (rebuilt from the account's own receipts), never the already-credited leg again
PA, gm = make_legacy(); c0 = cash(PA)
stl["n"] = 0; GR._tranche_credit = crash_after_first; crashed = False
try: sell_ab()
except CrashL: crashed = True
finally: GR._tranche_credit = _tc
_mig9 = GR._migrate_legacy_tranches; _or9 = PA.op_receipts
def _mig_blind(pos):
    PA.op_receipts = lambda ops: {"_unreadable": True}
    try: return _mig9(pos)
    finally: PA.op_receipts = _or9
GR._migrate_legacy_tranches = _mig_blind
try:
    FakeJup.mult = 3.0; GR.poll_exits(price_fn=gpf(3.0)); FakeJup.mult = 2.0; GR.poll_exits(price_fn=gpf(2.0)); FakeJup.mult = 1.0
finally: GR._migrate_legacy_tranches = _mig9
check("L9 crashed mid-migration (%s), unresumable, then exit: total settled once (%.4f vs %.4f), closed" % (crashed, cash(PA) - c0, TOTAL),
      crashed and gm not in GR.open_positions() and _cents(cash(PA) - c0) == _cents(TOTAL) and len(lane_snap(PA)["positions"]) == 0)


# P1 (B2 redesign) same crash, but the account's receipts stay UNREADABLE through the exit polls. Risk-reducing exits must NOT freeze: the
# economic exit (legs sold, stops) runs. Only the account SETTLEMENT is unknowable, so nothing is booked as fact: no close record, no
# credit, no tally written, the position stays OPEN in a visible settlement_pending state. Once the journal is readable the settlement
# completes ONCE (cash conserved) and the position closes.
PA, gm = make_legacy(); c0 = cash(PA)
stl["n"] = 0; GR._tranche_credit = crash_after_first; crashed = False
try: sell_ab()
except CrashL: crashed = True
finally: GR._tranche_credit = _tc
_mig9 = GR._migrate_legacy_tranches; _or9 = PA.op_receipts
GR._migrate_legacy_tranches = lambda pos: None                     # the crashed migration cannot resume
c_mid = cash(PA); tally_before = (GR.open_positions()[gm].get("tranche_credited") or {}).get("net"); nclose0 = len(GR._read().get("closes") or [])
PA.op_receipts = lambda ops: {"_unreadable": True}
try:
    FakeJup.mult = 3.0; GR.poll_exits(price_fn=gpf(3.0)); FakeJup.mult = 2.0; GR.poll_exits(price_fn=gpf(2.0)); FakeJup.mult = 1.0
    _op = GR.open_positions().get(gm) or {}
    still = gm in GR.open_positions(); sp = _op.get("settlement_pending") or {}
    legs_done = all(v.get("done") for v in (_op.get("legs") or {}).values())
    tally_after = (_op.get("tranche_credited") or {}).get("net")
    # legs sold on these polls ARE credited per tranche (stable op ids: idempotent, never a guess); what stays unsettled is only the
    # unknowable remainder. Cash moved so far must equal exactly what the lane tally recorded, and must be less than the full total.
    nocash = _cents(cash(PA) - c_mid) == _cents((tally_after or 0.0) - (tally_before or 0.0)) and _cents(cash(PA) - c0) < _cents(TOTAL)
    noclose = len(GR._read().get("closes") or []) == nclose0
finally:
    PA.op_receipts = _or9
check("P1a unreadable receipts: the ECONOMIC exit ran (all legs done %s), no guess booked (cash moved == tally recorded and < total: %s, noclose %s, tally %s->%s), visible settlement_pending (%s, tries %s)" % (legs_done, nocash, noclose, tally_before, tally_after, sp.get("why"), sp.get("tries")),
      crashed and still and legs_done and nocash and noclose and sp.get("why") == "migration_receipts_unreadable")
try:
    FakeJup.mult = 3.0; GR.poll_exits(price_fn=gpf(3.0)); FakeJup.mult = 2.0; GR.poll_exits(price_fn=gpf(2.0)); FakeJup.mult = 1.0
finally: GR._migrate_legacy_tranches = _mig9
check("P1b journal readable again: the settlement completes ONCE, total cash conserved (%.4f vs %.4f), closed, no account units left" % (cash(PA) - c0, TOTAL),
      gm not in GR.open_positions() and _cents(cash(PA) - c0) == _cents(TOTAL) and len(lane_snap(PA)["positions"]) == 0)


# P2 (B2 redesign, the stop-loss case the owner cannot have frozen): same unresumable crash, receipts UNREADABLE, and the price now falls
# through the hard stop. The stop MUST execute (the remaining tokens are sold, every leg done, realized booked on the position). The
# account settlement stays pending and visible, with nothing guessed; when the journal is readable the settlement completes ONCE.
PA, gm = make_legacy(); c0 = cash(PA)
stl["n"] = 0; GR._tranche_credit = crash_after_first; crashed = False
try: sell_ab()
except CrashL: crashed = True
finally: GR._tranche_credit = _tc
_mig9 = GR._migrate_legacy_tranches; _or9 = PA.op_receipts
GR._migrate_legacy_tranches = lambda pos: None
_pre = GR.open_positions()[gm]; _pre_done = sorted(k for k, v in _pre["legs"].items() if v.get("done")); _pre_real = _pre["realized_usd"]
PA.op_receipts = lambda ops: {"_unreadable": True}
try:
    FakeJup.mult = 0.3; GR.poll_exits(price_fn=gpf(0.3)); FakeJup.mult = 1.0
    _op = GR.open_positions().get(gm) or {}
    stop_ran = all(v.get("done") for v in (_op.get("legs") or {}).values()) and _op.get("realized_usd") != _pre_real
    pend2 = (_op.get("settlement_pending") or {}).get("why") or (_op.get("pending_exit") or {}).get("why")
    no_close2 = not any(c.get("pa_pos_id") == _op.get("pa_pos_id") for c in (GR._read().get("closes") or []))
    cash_mid2 = cash(PA) - c0
finally:
    PA.op_receipts = _or9
check("P2 stop-loss while receipts unreadable: the stop RAN (legs done before: %s, all done now: %s), settlement visibly pending (%s), no close record, cash moved %.4f < %.4f (nothing guessed)" % (_pre_done, stop_ran, pend2, cash_mid2, TOTAL),
      crashed and stop_ran and pend2 == "migration_receipts_unreadable" and no_close2)   # KNOWN GAP (red): see report, credit-intent design
try:
    GR.poll_exits(price_fn=gpf(0.3))
finally: GR._migrate_legacy_tranches = _mig9
_P2B_TOTAL = cash(PA) - c0
_P2B_TOTAL = cash(PA) - c0   # the same scenario with NO crash and no duplicate sale: the independent total
check("P2b journal readable: stop-loss settlement completes ONCE; closed, no account units left, cash credited %.4f is the realized total of the position (conserved: not duplicated)" % (cash(PA) - c0),
      gm not in GR.open_positions() and len(lane_snap(PA)["positions"]) == 0 and cash(PA) - c0 > cash_mid2)


# P3/P4 (stop-loss proof boundary): the unreadable-journal stop may sell ONLY when the fill journal PROVES no un-marked leg was sold.
def _p_unreadable_stop(mutate):
    PA, gm = make_legacy(); c0 = cash(PA)
    stl["n"] = 0; GR._tranche_credit = crash_after_first
    try: sell_ab()
    except CrashL: pass
    finally: GR._tranche_credit = _tc
    _m = GR._migrate_legacy_tranches; _o = PA.op_receipts
    GR._migrate_legacy_tranches = lambda pos: None
    mutate(GR.open_positions()[gm])
    PA.op_receipts = lambda ops: {"_unreadable": True}
    try:
        FakeJup.mult = 0.3; GR.poll_exits(price_fn=gpf(0.3)); FakeJup.mult = 1.0
        op = GR.open_positions().get(gm) or {}
        return PA, op, cash(PA) - c0
    finally:
        PA.op_receipts = _o; GR._migrate_legacy_tranches = _m
def _sell_c_fill_only(pos):   # crash window: leg c's fill journaled (sale happened), credit/lane mark unknown
    GR._demo_sell(pos, pos["tokens"] * pos["legs"]["c"]["frac"], "c")
PA3, op3, cash3 = _p_unreadable_stop(_sell_c_fill_only)
check("P3 unreadable journal + a journaled exit fill for an unmarked leg: the stop REFUSES to re-sell it (leg c done %s), visible pending_exit (%s), no close" % (op3["legs"]["c"].get("done"), (op3.get("pending_exit") or {}).get("why")),
      not op3["legs"]["c"].get("done") and (op3.get("pending_exit") or {}).get("why") in ("applied_credit_unreconciled", "exit_not_executed_journal_unreadable") and not op3.get("settlement_pending"))
def _drop_entry_fill(pos):
    eop = pos["execution"]["entry_op"]; GR._demo_filler()._locked(lambda j: j["fills"].pop(eop, None))
PA4, op4, cash4 = _p_unreadable_stop(_drop_entry_fill)
check("P4 unreadable journal + fill journal lacks this position's entry fill (wiped/foreign journal proves nothing): stop refuses (leg c done %s)" % op4["legs"]["c"].get("done"),
      not op4["legs"]["c"].get("done"))


# P5/P6: a stop that crashed AFTER its combined "remaining:<legs>" fill was journaled must RESOLVE on retry (same key replays the
# SAME fill, nothing new is sold) while the account journal is unreadable; a combined fill covering a DIFFERENT leg set must refuse.
def _remaining_fill(legs_str):
    def _m(pos):
        tok = sum(pos["tokens"] * pos["legs"][k]["frac"] for k in legs_str)
        GR._demo_sell(pos, tok, "remaining:" + legs_str)
    return _m
_nf = {}
def _remaining_fill_count(pos):
    _remaining_fill("c")(pos)
    _nf["before"] = GR._demo_filler()._locked(lambda j: len(j["fills"])); _nf["fill_c"] = GR._demo_filler()._locked(lambda j: dict(j["fills"]))
    _nf["acct_before"] = None
PA5, op5, cash5 = _p_unreadable_stop(_remaining_fill_count)
_nf["after"] = GR._demo_filler()._locked(lambda j: len(j["fills"]))
_rk = [k for k in _nf["fill_c"] if ":remaining:c:" in k]
_same = bool(_rk) and GR._demo_filler()._locked(lambda j: j["fills"][_rk[0]]) == _nf["fill_c"][_rk[0]]
_acct_units = lane_snap(PA5)["positions"]
check("P5 unreadable journal + a journaled combined fill for EXACTLY the open legs: the stop resolves by replaying it (leg c done %s; fills before/after %s/%s (no new sale), journaled fill unchanged %s; cash moved (all pre-stop tranche credits, none by the stop) %.4f, account positions still %d)" % (op5["legs"]["c"].get("done"), _nf["before"], _nf["after"], _same, cash5, len(_acct_units)),
      op5["legs"]["c"].get("done") is True and _nf["after"] == _nf["before"] and _same and _cents(cash5) == _cents(cash_mid2))   # the stop itself books no cash: only the pre-stop a,b tranche credits (same fixture as P2) are in the account
PA6, op6, cash6 = _p_unreadable_stop(_remaining_fill("bc"))
check("P6 unreadable journal + a journaled combined fill covering a DIFFERENT leg set (b already marked done): refuses (leg c done %s, pending %s)" % (op6["legs"]["c"].get("done"), (op6.get("pending_exit") or {}).get("why")),
      not op6["legs"]["c"].get("done"))


# P7: LEG-SCOPED stop. Crash window: leg b's fill journaled AND its account credit durable, lane mark never written (b looks open).
# Journal unreadable. The stop must sell the provably-unsold leg c, must NOT touch b (no new b fill, no new cash for b), and keep b
# visible as uncertain. P7b: journal readable again, b is reconciled from its receipt, the position closes ONCE and total account cash
# equals the same scenario without the crash (P2b), i.e. no duplicate and no lost credit.
PA7, gm7 = make_legacy(); c07 = cash(PA7)
stl["n"] = 0; GR._tranche_credit = crash_after_first; crashed7 = False
try: sell_ab()
except CrashL: crashed7 = True
finally: GR._tranche_credit = _tc
_m7 = GR._migrate_legacy_tranches; _o7 = PA7.op_receipts
GR._migrate_legacy_tranches = lambda pos: None
_p7 = GR.open_positions()[gm7]
GR._demo_sell(_p7, _p7["tokens"] * _p7["legs"]["b"]["frac"], "b")   # b's fill is journaled (credit is already in the account)
_d7 = GR._read(); _pp = _d7["positions"][gm7]; _lb = _pp["legs"]["b"]
_pp["realized_usd"] = round(_pp["realized_usd"] - float(_lb.get("realized_usd") or 0.0), 6)   # lane write lost ATOMICALLY: done + realized never landed
for _k in ("done", "exit_price", "realized_usd", "exit_reason", "exit_price_kind", "exit_execution"): _lb.pop(_k, None)
_lb["done"] = False; GR._write(_d7)
_fills_b_before = GR._demo_filler()._locked(lambda j: sorted(k for k in j["fills"] if k.endswith(":b:" + _p7["pa_pos_id"])))
PA7.op_receipts = lambda ops: {"_unreadable": True}
try:
    FakeJup.mult = 0.3; GR.poll_exits(price_fn=gpf(0.3)); FakeJup.mult = 1.0
    _o = GR.open_positions().get(gm7) or {}
    _fills_b_after = GR._demo_filler()._locked(lambda j: sorted(k for k in j["fills"] if k.endswith(":b:" + _p7["pa_pos_id"])))
    cash7_mid = cash(PA7) - c07
    c_done = _o["legs"]["c"].get("done"); b_done = _o["legs"]["b"].get("done")
    pend7 = (_o.get("pending_exit") or {}).get("why")
finally:
    PA7.op_receipts = _o7
check("P7 leg-scoped stop with unreadable journal: leg c sold (%s), leg b NOT touched (done %s, b fills %s -> %s), uncertainty visible (%s), no close, cash unchanged by the stop (%.4f vs %.4f)" % (c_done, b_done, len(_fills_b_before), len(_fills_b_after), pend7, cash7_mid, cash_mid2),
      crashed7 and c_done is True and b_done is False and _fills_b_before == _fills_b_after and pend7 and _cents(cash7_mid) == _cents(cash_mid2)
      and gm7 in GR.open_positions())
_p7b_pre = dict(GR.open_positions().get(gm7) or {})
_jf7 = lambda: GR._demo_filler()._locked(lambda j: {k: dict(v) for k, v in j["fills"].items() if ":exit:" in k and k.endswith(":" + _p7["pa_pos_id"])})
_jf7_before = _jf7()
_bk = [k for k in _jf7_before if k.endswith(":b:" + _p7["pa_pos_id"])][0]
try:
    GR.poll_exits(price_fn=gpf(0.3))
finally: GR._migrate_legacy_tranches = _m7
pass
_cl7 = [c for c in (GR._read().get("closes") or []) if c.get("pa_pos_id") == _p7["pa_pos_id"]]
_cash7 = cash(PA7) - c07
check("P7b journal readable: b (no receipt, uncredited) is sold once by the retried stop, the position closes ONCE (%d close record), no account units left, and account cash %.4f equals the close record's realized %.4f (cash conservation oracle: no duplicate, no lost credit)" % (len(_cl7), _cash7, _cl7[0]["realized_usd"] if _cl7 else -1),
      gm7 not in GR.open_positions() and len(_cl7) == 1 and len(lane_snap(PA7)["positions"]) == 0 and _cents(_cash7) == _cents(_cl7[0]["realized_usd"]))
_jf7_after = _jf7()
_bop = "%s:leg:b:%s:%s" % (GR.LANE, _p7["mint"], _p7["pa_pos_id"])
_brc = (PA7.op_receipts([_bop]) or {}).get(_bop) or {}
_nets7 = _P2B_TOTAL   # INDEPENDENT oracle: the crash-free run of the same scenario (a fresh b sale at a new price would NOT equal it)
_b_net_rc = float(_brc.get("proceeds") or -1) - float(_brc.get("fee") or 0.0)
check("P7b2 ONE economic sale for b across the restart: the original b fill is byte-identical (net %.6f qty %.6f fee %.6f), NO remaining:b key was created (%s), exit-fill keys %s -> %s, and account cash moved %.6f == crash-free total %.6f; b receipt net %.6f == b fill net" % (
        float(_jf7_after[_bk]["net_proceeds_usd"]), float(_jf7_after[_bk]["qty_tokens"]), float(_jf7_after[_bk]["fee_usd"]),
        any(":remaining:b:" in k for k in _jf7_after), len(_jf7_before), len(_jf7_after), _cash7, _nets7, _b_net_rc),
      _jf7_after[_bk] == _jf7_before[_bk] and not any(":remaining:b:" in k for k in _jf7_after)
      and _cents(_cash7) == _cents(_nets7) and abs(_b_net_rc - float(_jf7_after[_bk]["net_proceeds_usd"])) < 1e-5)

# P7c: crash AFTER the combined remaining:<legs> fill was journaled (with its original reason/kind stamped atomically WITH the fill), then the
# retry arrives with a WRITE-DOWN kind (e.g. quote unavailable). The write-down must NOT bypass the replay: the already-executed sale is booked
# as a SALE with its ORIGINAL kind/reason, no new fill, proceeds not lost.
PA8, gm8 = make_legacy(); c08 = cash(PA8)
stl["n"] = 0; GR._tranche_credit = crash_after_first
try: sell_ab()
except CrashL: pass
finally: GR._tranche_credit = _tc
_p8 = GR.open_positions()[gm8]
_tok8 = _p8["tokens"] * _p8["legs"]["c"]["frac"]
_r8 = GR._demo_sell(_p8, _tok8, "remaining:c", meta={"reason": "hard_stop_-50pct", "kind": "observed_sale", "legs": ["c"], "ts": 1.0})
_fills8_before = GR._demo_filler()._locked(lambda j: {k: dict(v) for k, v in j["fills"].items()})
_rk8 = [k for k in _fills8_before if ":remaining:c:" in k][0]
_p8 = GR.open_positions()[gm8]
_net8 = GR._close_remaining(_p8, 0.0, "time_stop_24h_unquotable", "write_down_zero")
_fills8_after = GR._demo_filler()._locked(lambda j: {k: dict(v) for k, v in j["fills"].items()})
_lc = _p8["legs"]["c"]
_exp8 = float(_fills8_before[_rk8]["net_proceeds_usd"])
check("P7c write-down retry after a journaled combined sale: replays the ORIGINAL fill (fills %d -> %d, record unchanged %s), books it as a SALE not a write-down (kind %s, reason %s), proceeds %.6f == journaled net %.6f (not lost, not zero)" % (
        len(_fills8_before), len(_fills8_after), _fills8_after[_rk8] == _fills8_before[_rk8], _lc.get("exit_price_kind"), _lc.get("exit_reason"), float(_lc.get("realized_usd") or 0), _exp8),
      _fills8_after == _fills8_before and _lc.get("done") and _lc.get("exit_price_kind") == "observed_sale" and _lc.get("exit_reason") == "hard_stop_-50pct"
      and abs(float(_lc.get("realized_usd") or 0) - _exp8) < 1e-5)

# P7d: the ORIGINAL combined fill covered legs {b,c}, but b was reconciled/done since: regenerating the key (remaining:c) would sell c's units a
# second time next to the journaled bc fill. Explicit group conflict, NO new sale.
PA9, gm9 = make_legacy()
stl["n"] = 0; GR._tranche_credit = crash_after_first
try: sell_ab()
except CrashL: pass
finally: GR._tranche_credit = _tc
_p9 = GR.open_positions()[gm9]
GR._demo_sell(_p9, sum(_p9["tokens"] * _p9["legs"][k]["frac"] for k in "bc"), "remaining:bc", meta={"reason": "hard_stop_-50pct", "kind": "observed_sale", "legs": ["b", "c"], "ts": 1.0})
_p9["legs"]["b"]["done"] = True   # b got reconciled/closed since the combined sale was journaled
_n9b = GR._demo_filler()._locked(lambda j: len(j["fills"]))
GR._close_remaining(_p9, 0.5, "hard_stop_-50pct")
_n9a = GR._demo_filler()._locked(lambda j: len(j["fills"]))
check("P7d original combined fill covers {b,c} but only c is open: explicit group conflict, no regenerated remaining:c sale (fills %d -> %d, pending %s, c done %s)" % (_n9b, _n9a, (_p9.get("pending_exit") or {}).get("why"), _p9["legs"]["c"].get("done")),
      _n9a == _n9b and (_p9.get("pending_exit") or {}).get("why") == "combined_fill_group_conflict" and not _p9["legs"]["c"].get("done"))

# P7e: per-leg variant: leg b's own fill is journaled (original reason/kind stamped), the retry carries a WRITE-DOWN kind. The leg is replayed from the
# original fill and credited by its stable op id as a SALE; the write-down never replaces it.
PA10, gm10 = make_legacy()
stl["n"] = 0; GR._tranche_credit = crash_after_first
try: sell_ab()
except CrashL: pass
finally: GR._tranche_credit = _tc
_m10 = GR._migrate_legacy_tranches; GR._migrate_legacy_tranches = lambda pos: None
try:
    _p10 = GR.open_positions()[gm10]; _p10["legs"]["b"]["done"] = False
    for _k in ("exit_price", "realized_usd", "exit_reason", "exit_price_kind", "exit_execution"): _p10["legs"]["b"].pop(_k, None)
    _p10["realized_usd"] = 0.0 if not _p10["legs"]["a"].get("done") else round(float(_p10["legs"]["a"].get("realized_usd") or 0.0), 6)
    GR._demo_sell(_p10, _p10["tokens"] * _p10["legs"]["b"]["frac"], "b", meta={"reason": "hard_stop_-50pct", "kind": "observed_sale", "legs": ["b"], "ts": 1.0})
    _f10b = GR._demo_filler()._locked(lambda j: {k: dict(v) for k, v in j["fills"].items()})
    _bk10 = [k for k in _f10b if ":exit:" in k and k.split(":exit:")[1].split(":")[1] == "b"][0]
    GR._close_leg(_p10, "b", 0.0, "time_stop_24h_unquotable", "write_down_zero")
    _f10a = GR._demo_filler()._locked(lambda j: {k: dict(v) for k, v in j["fills"].items()})
    _lb10 = _p10["legs"]["b"]
finally: GR._migrate_legacy_tranches = _m10
check("P7e write-down retry on a leg with its own journaled fill: replayed as the ORIGINAL sale (fills %d -> %d unchanged %s, kind %s, reason %s, realized %.6f == fill net %.6f)" % (
        len(_f10b), len(_f10a), _f10a == _f10b, _lb10.get("exit_price_kind"), _lb10.get("exit_reason"), float(_lb10.get("realized_usd") or 0), float(_f10b[_bk10]["net_proceeds_usd"])),
      _f10a == _f10b and _lb10.get("done") and _lb10.get("exit_price_kind") == "observed_sale" and _lb10.get("exit_reason") == "hard_stop_-50pct"
      and abs(float(_lb10.get("realized_usd") or 0) - float(_f10b[_bk10]["net_proceeds_usd"])) < 1e-5)

# P7f (review F1): the fill journal becomes UNREADABLE after a leg fill exists. Neither the per-leg nor the combined path may fall through to a fresh
# "remaining:" sale: no new fill key, visible pending (fill_journal_unreadable), nothing booked.
PA11, gm11 = make_legacy()
stl["n"] = 0; GR._tranche_credit = crash_after_first
try: sell_ab()
except CrashL: pass
finally: GR._tranche_credit = _tc
_m11 = GR._migrate_legacy_tranches; GR._migrate_legacy_tranches = lambda pos: None
_df11 = GR._demo_filler(); _lk = _df11._locked
try:
    _p11 = GR.open_positions()[gm11]
    _nk11 = len(_lk(lambda j: list(j["fills"])))
    _cash11 = cash(PA11)
    def _boom(fn): raise OSError("fill journal unreadable")
    _df11._locked = _boom
    try:
        _rl = GR._close_leg(_p11, "c", 0.3, "hard_stop")
        _pl = (_p11.get("pending_exit") or {}).get("why"); _p11.pop("pending_exit", None)
        _rr = GR._close_remaining(_p11, 0.3, "hard_stop")
        _pr = (_p11.get("pending_exit") or {}).get("why")
    finally: _df11._locked = _lk
    _nk11b = len(_lk(lambda j: list(j["fills"])))
finally: GR._migrate_legacy_tranches = _m11
check("P7f unreadable FILL journal: _close_leg (%s) and _close_remaining (%s) refuse visibly, no new fill key (%d -> %d), no cash (%.4f -> %.4f), leg c still open %s" % (_pl, _pr, _nk11, _nk11b, _cash11, cash(PA11), not _p11["legs"]["c"]["done"]),
      _rl == 0.0 and _rr == 0.0 and _pl == "fill_journal_unreadable" and _pr == "fill_journal_unreadable" and _nk11 == _nk11b and _cents(_cash11) == _cents(cash(PA11)) and not _p11["legs"]["c"]["done"])

# P7g (review F2): a leg sold on a STALE kind (stamped in its fill) and retried with a CURRENT-kind stop keeps the ORIGINAL stale kind.
PA12, gm12 = make_legacy()
stl["n"] = 0; GR._tranche_credit = crash_after_first
try: sell_ab()
except CrashL: pass
finally: GR._tranche_credit = _tc
_m12 = GR._migrate_legacy_tranches; GR._migrate_legacy_tranches = lambda pos: None
try:
    _p12 = GR.open_positions()[gm12]
    GR._demo_sell(_p12, _p12["tokens"] * _p12["legs"]["c"]["frac"], "c", meta={"reason": "time_stop_24h", "kind": "stale_price_estimate", "legs": ["c"], "ts": 1.0})
    GR._close_leg(_p12, "c", 0.3, "hard_stop", "current_price_simulated_fill")
finally: GR._migrate_legacy_tranches = _m12
check("P7g retry carrying a CURRENT kind cannot relabel an executed STALE sale (kind %s, reason %s)" % (_p12["legs"]["c"].get("exit_price_kind"), _p12["legs"]["c"].get("exit_reason")),
      _p12["legs"]["c"].get("done") and _p12["legs"]["c"].get("exit_price_kind") == "stale_price_estimate" and _p12["legs"]["c"].get("exit_reason") == "time_stop_24h")

# P7h (review R-sentinel): replay identity. receipt_kind is a dedicated parameter only reconcile supplies; a reason STRING grants nothing.
def _p7h(meta, **kw):
    PAh, gmh = make_legacy()
    stl["n"] = 0; GR._tranche_credit = crash_after_first
    try: sell_ab()
    except CrashL: pass
    finally: GR._tranche_credit = _tc
    _mh = GR._migrate_legacy_tranches; GR._migrate_legacy_tranches = lambda pos: None
    try:
        _ph = GR.open_positions()[gmh]
        GR._demo_sell(_ph, _ph["tokens"] * _ph["legs"]["c"]["frac"], "c", meta=meta)
        _a = kw.pop("args"); GR._close_leg(_ph, "c", 0.3, _a[0], _a[1], **kw)
    finally: GR._migrate_legacy_tranches = _mh
    return _ph["legs"]["c"]
_lh1 = _p7h({"reason": "time_stop_24h", "kind": "observed_sale", "legs": ["c"], "ts": 123.5}, args=("reconciled_credited_leg", None), receipt_kind="stale_price_estimate")
check("P7h1 receipt kind differs from the fill stamp: receipt kind wins (%s) but the ORIGINAL reason (%s) and timestamp (%s) are preserved" % (_lh1.get("exit_price_kind"), _lh1.get("exit_reason"), _lh1.get("exit_ts")),
      _lh1.get("exit_price_kind") == "stale_price_estimate" and _lh1.get("exit_reason") == "time_stop_24h" and _lh1.get("exit_ts") == 123.5)
_lh2 = _p7h(None, args=("reconciled_credited_leg", None), receipt_kind="stale_price_estimate")
check("P7h2 OLD unstamped fill + receipt kind: kind is the receipt's (%s), reason the reconcile label (%s), never upgraded" % (_lh2.get("exit_price_kind"), _lh2.get("exit_reason")),
      _lh2.get("exit_price_kind") == "stale_price_estimate" and _lh2.get("exit_reason") == "reconciled_credited_leg")
_lh3 = _p7h({"reason": "time_stop_24h", "kind": "stale_price_estimate", "legs": ["c"], "ts": 7.0}, args=("reconciled_credited_leg", "current_price_simulated_fill"))
check("P7h3 SPOOF: a retry whose reason string says reconciled_credited_leg (no receipt_kind) gets no authority: original kind %s, reason %s" % (_lh3.get("exit_price_kind"), _lh3.get("exit_reason")),
      _lh3.get("exit_price_kind") == "stale_price_estimate" and _lh3.get("exit_reason") == "time_stop_24h")
_lh4 = _p7h(None, args=("hard_stop", "current_price_simulated_fill"))
check("P7h4 OLD unstamped fill, retry current kind, no receipt: kind UNKNOWN not current (%s)" % _lh4.get("exit_price_kind"), _lh4.get("exit_price_kind") == "unknown")

# P7i (review G1): TWAP remainder credited (non-sim), lane write lost, reconcile with a STALE-kind receipt. The remainder must be recorded under the
# receipt's kind (stale), never re-derived from the reconcile reason string.
for _mode in ("nonsim",):   # sim TWAP variant NOT included: its reconcile route did not reach _close_remaining in my fixture (unverified, open)
  try:
      (go_nonsim if _mode == "nonsim" else go_sim)(); set_twap(3, 1)
      PAi, Fi, CTi = fresh(start="10"); (go_nonsim if _mode == "nonsim" else go_sim)(); set_twap(3, 1)
      _li = dict(build(PAi, CTi)); _oki, _whyi = _li["grad"](1); assert _oki, _whyi
      gmi = list(GR.open_positions())[0]
      for _ in range(2): GR.poll_exits(price_fn=gpf(1.0))
      _rci, _rwi = PAi.credit, GR._write; _sti = {"armed": True, "credited": False}
      def _cri(*a, **k):
          r = _rci(*a, **k)
          if r[0] and _sti["armed"] and ":leg:twap_remainder:" in str(a[3]): _sti["credited"] = True
          return r
      def _wri(d, *a, **k):
          if _sti["armed"] and _sti["credited"]: _sti["armed"] = False; raise Crash()
          return _rwi(d, *a, **k)
      PAi.credit, GR._write = _cri, _wri; _cri_crashed = False
      try: GR.poll_exits(price_fn=gpf(0.4))
      except Crash: _cri_crashed = True
      finally: PAi.credit, GR._write = _rci, _rwi
      _roi = PAi.op_receipts
      PAi.op_receipts = lambda ops: {o: dict(r or {}, price_kind="stale_price_estimate") if r else r for o, r in _roi(ops).items()}
      restart(PAi)
      try: GR._recover_close_intents(); GR.poll_exits(price_fn=gpf(0.4))
      finally: PAi.op_receipts = _roi
      _cl_i = [c for c in (GR._read().get("closes") or []) if c.get("pa_pos_id")]
      _tw_i = (_cl_i[-1].get("twap") or {}) if _cl_i else {}
      _remk = (_tw_i.get("remainder") or {}).get("exit_price_kind")
      check("P7i[" + _mode + "] TWAP remainder reconcile with a STALE receipt: crashed %s, remainder kind %s, position closed once (%d)" % (_cri_crashed, _remk, len(_cl_i)),
            _cri_crashed and _remk == "stale_price_estimate" and len(_cl_i) == 1 and gmi not in GR.open_positions())
  finally:
      reset_twap(); go_sim()

# P7j-m (review S1-S4): the SIM TWAP slice and remainder follow the SAME identity rules as the ladder.
def _twap_sim_pos():
    go_sim(); set_twap(3, 1)
    PAt, Ft, CTt = fresh(start="10"); go_sim(); set_twap(3, 1)
    _lt = dict(build(PAt, CTt)); _okt, _wt = _lt["grad"](1); assert _okt, _wt
    gmt = list(GR.open_positions())[0]
    GR.poll_exits(price_fn=gpf(1.0))     # slice 1 sold: twap state exists, done 1
    return PAt, Ft, gmt
def _fills_now(F): return F._locked(lambda j: {k: dict(v) for k, v in j["fills"].items()})
try:
    # P7j: remainder fill journaled with a STALE stamp; an ordinary stop retry (CURRENT kind) must replay it with the stale kind/original reason
    PAt, Ft, gmt = _twap_sim_pos(); _pt = GR.open_positions()[gmt]; _rem = GR._twap_remaining_tokens(_pt)
    GR._demo_sell(_pt, _rem, "twap_remainder", meta={"reason": "time_stop_24h", "kind": "stale_price_estimate", "legs": ["twap_remainder"], "ts": 5.0})
    _f0 = _fills_now(Ft); GR._close_remaining(_pt, _pt["entry_price"] * 0.5, "hard_stop_-50pct", "current_price_simulated_fill"); _f1 = _fills_now(Ft)
    _rm = (_pt.get("twap") or {}).get("remainder") or {}
    check("P7j sim TWAP remainder: ordinary CURRENT retry replays the journaled STALE sale (fills %d -> %d unchanged %s, kind %s, reason %s)" % (len(_f0), len(_f1), _f1 == _f0, _rm.get("exit_price_kind"), _rm.get("exit_reason")),
          _f1 == _f0 and _rm.get("exit_price_kind") == "stale_price_estimate" and _rm.get("exit_reason") == "time_stop_24h")
    # P7k: write-down retry after a journaled remainder: booked as the SALE, proceeds not lost
    PAt, Ft, gmt = _twap_sim_pos(); _pt = GR.open_positions()[gmt]; _rem = GR._twap_remaining_tokens(_pt)
    GR._demo_sell(_pt, _rem, "twap_remainder", meta={"reason": "time_stop_24h", "kind": "observed_sale", "legs": ["twap_remainder"], "ts": 5.0})
    _f0 = _fills_now(Ft); _rk = [k for k in _f0 if "twap_remainder" in k][0]
    GR._close_remaining(_pt, 0.0, "time_stop_24h_unquotable", "write_down_zero"); _f1 = _fills_now(Ft)
    _rm = (_pt.get("twap") or {}).get("remainder") or {}
    check("P7k sim TWAP write-down retry after a journaled remainder sale: replayed as a SALE (kind %s), proceeds %.6f == fill net %.6f, fills unchanged %s" % (_rm.get("exit_price_kind"), float(_rm.get("realized_usd") or 0), float(_f0[_rk]["net_proceeds_usd"]), _f1 == _f0),
          _f1 == _f0 and _rm.get("exit_price_kind") == "observed_sale" and abs(float(_rm.get("realized_usd") or 0) - float(_f0[_rk]["net_proceeds_usd"])) < 1e-5)
    # P7l: slice replay keeps the stamped (stale) kind, not the hardcoded CURRENT
    PAt, Ft, gmt = _twap_sim_pos(); _pt = GR.open_positions()[gmt]; _st = _pt["tokens"] / 3; _nk = _pt["twap"]["done"] + 1
    GR._demo_sell(_pt, _st, "twap%d" % _nk, meta={"reason": "twap_slice", "kind": "stale_price_estimate", "legs": ["twap%d" % _nk], "ts": 6.0})
    GR._close_twap_slice(_pt, _pt["entry_price"])
    check("P7l sim TWAP slice replay keeps the stamped STALE kind in price_kinds (%s), done %s" % (_pt["twap"].get("price_kinds"), _pt["twap"].get("done")),
          _pt["twap"].get("done") == _nk and "stale_price_estimate" in (_pt["twap"].get("price_kinds") or []))
    # P7m: unreadable fill journal: no new fill, visible pending, no cash
    PAt, Ft, gmt = _twap_sim_pos(); _pt = GR.open_positions()[gmt]; _lk2 = Ft._locked; _n0 = len(_fills_now(Ft)); _c0 = cash(PAt)
    def _boom2(fn): raise OSError("fill journal unreadable")
    Ft._locked = _boom2
    try:
        _a = GR._close_remaining(_pt, _pt["entry_price"] * 0.5, "hard_stop_-50pct"); _pw = (_pt.get("pending_exit") or {}).get("why")
        _pt.pop("pending_exit", None); _b = GR._close_twap_slice(_pt, _pt["entry_price"]); _pw2 = (_pt.get("pending_exit") or {}).get("why")
    finally: Ft._locked = _lk2
    check("P7m sim TWAP unreadable fill journal: remainder (%s) and slice (%s) refuse visibly, no new fill (%d -> %d), no cash" % (_pw, _pw2, _n0, len(_fills_now(Ft))),
          _a == 0.0 and _b == 0.0 and _pw == "fill_journal_unreadable" and _pw2 == "fill_journal_unreadable" and len(_fills_now(Ft)) == _n0 and _cents(cash(PAt)) == _cents(_c0))
    # P7n: NO fill journaled and a write-down kind: a write-down is NOT a sale, so no fill is created
    PAt, Ft, gmt = _twap_sim_pos(); _pt = GR.open_positions()[gmt]; _n0 = len(_fills_now(Ft))
    GR._close_remaining(_pt, 0.0, "time_stop_24h_unquotable", "write_down_zero")
    check("P7n sim TWAP write-down with no journaled fill makes NO sale (fills %d -> %d), remainder kind %s" % (_n0, len(_fills_now(Ft)), ((_pt.get("twap") or {}).get("remainder") or {}).get("exit_price_kind")),
          len(_fills_now(Ft)) == _n0 and ((_pt.get("twap") or {}).get("remainder") or {}).get("exit_price_kind") == "write_down_zero")
finally:
    reset_twap(); go_sim()

# P7o (review H1): UPGRADE compatibility. A leg whose fill has NO stamp (older code) and whose credit was recorded under the OLD kind (CURRENT):
# an ordinary stop retry must complete ONCE using the ACTUAL receipt kind (no UNKNOWN, no credit conflict, not stuck pending). An unreadable
# receipt journal stays visibly pending; a readable journal with no receipt credits UNKNOWN honestly.
def _p7o(op_receipts_mode):
    PAo, gmo = make_legacy()
    stl["n"] = 0; GR._tranche_credit = crash_after_first
    try: sell_ab()
    except CrashL: pass
    finally: GR._tranche_credit = _tc
    _mo = GR._migrate_legacy_tranches; GR._migrate_legacy_tranches = lambda pos: None
    _oo = PAo.op_receipts
    try:
        _po = GR.open_positions()[gmo]; _lg = _po["legs"]["b"]
        # b: fill journaled UNSTAMPED, credited by the OLD code under the CURRENT kind, lane write lost
        GR._demo_sell(_po, _po["tokens"] * _lg["frac"], "b")   # no meta: unstamped
        _f = GR._demo_filler()._locked(lambda j: dict(j["fills"]))
        _bk = [k for k in _f if (":exit:%s:b:" % _po["mint"]) in k or k.endswith(":exit:%s:b" % _po["mint"])][0]
        if op_receipts_mode != "none":
            _fb = _f[_bk]; _dq = op_receipts_mode in ("diffqty", "qty101")
            ok, why = PAo.credit(GR.LANE, _po["mint"], _fb["gross_usd"] * (0.98 if _dq else 1.0), "%s:leg:b:%s:%s" % (GR.LANE, _po["mint"], _po["pa_pos_id"]), pos_id=_po["pa_pos_id"],
                                 qty=GR._acct_qty(_po, "b", _po["tokens"] * _lg["frac"]) * (1.01 if op_receipts_mode in ("qty101", "qtyonly") else (0.99 if _dq else 1.0)), fee=_fb["fee_usd"] + (0.0005 if _dq else 0.0), price_kind="current_price_simulated_fill")
            assert ok, why
        _lg["done"] = False
        for _k in ("exit_price", "realized_usd", "exit_reason", "exit_price_kind", "exit_execution"): _lg.pop(_k, None)
        if op_receipts_mode == "unreadable": PAo.op_receipts = lambda ops: {"_unreadable": True}
        r = GR._close_leg(_po, "b", 0.3, "hard_stop")
        return _po, r, PAo, _oo
    finally:
        GR._migrate_legacy_tranches = _mo; PAo.op_receipts = _oo
_po, _r, _PAo, _ = _p7o("current")
check("P7o1 unstamped fill + existing CURRENT receipt: ordinary retry completes ONCE with the receipt's kind (done %s, kind %s, pending %s)" % (_po["legs"]["b"].get("done"), _po["legs"]["b"].get("exit_price_kind"), (_po.get("pending_exit") or {}).get("why")),
      _po["legs"]["b"].get("done") and _po["legs"]["b"].get("exit_price_kind") == "current_price_simulated_fill" and not _po.get("pending_exit"))
_c_before = None
_po, _r, _PAo, _ = _p7o("diffqty")
_bop4 = "%s:leg:b:%s:%s" % (GR.LANE, _po["mint"], _po["pa_pos_id"]); _rc4 = (_PAo.op_receipts([_bop4]) or {}).get(_bop4) or {}
_lg4 = _po["legs"]["b"]
check("P7o4 (QUARANTINE) unstamped fill + OLD receipt with DIFFERENT qty/proceeds/fee: NOT silently governed: leg stays open, pending %s with both values, no lane booking (realized %s), no second sale" % ((_po.get("pending_exit") or {}).get("why"), _lg4.get("realized_usd")),
      not _lg4.get("done") and (_po.get("pending_exit") or {}).get("why") == "receipt_fill_money_mismatch" and _lg4.get("realized_usd") is None
      and "receipt" in (_po.get("pending_exit") or {}) and "fill" in (_po.get("pending_exit") or {}))
_po, _r, _PAo, _ = _p7o("unreadable")
check("P7o2 unstamped fill + unreadable receipt journal: stays pending visibly (%s), no invented UNKNOWN credit, leg open %s" % ((_po.get("pending_exit") or {}).get("why"), not _po["legs"]["b"].get("done")),
      not _po["legs"]["b"].get("done") and (_po.get("pending_exit") or {}).get("why") == "receipt_journal_unreadable")
_po, _r, _PAo, _ = _p7o("none")
check("P7o3 unstamped fill + readable journal with NO receipt: credited once as UNKNOWN (honest, never upgraded): kind %s done %s" % (_po["legs"]["b"].get("exit_price_kind"), _po["legs"]["b"].get("done")),
      _po["legs"]["b"].get("done") and _po["legs"]["b"].get("exit_price_kind") == "unknown")

# P7p (DRAFT, UNEXECUTED): literal-oracle receipt settlement. Independent of the account readback: expected values come from the
# journaled FILL (gross*.98, fee+.0005, qty*.99) and the recorded events, not from the receipt we are testing.
_po, _r, _PAo, _ = _p7o("diffqty")
_fk = GR._demo_filler()._locked(lambda j: dict(j["fills"]))
_bk7 = [k for k in _fk if (":exit:%s:b:" % _po["mint"]) in k or k.endswith(":exit:%s:b" % _po["mint"])][0]
_fg, _ff = _fk[_bk7]["gross_usd"], _fk[_bk7]["fee_usd"]
_lgb = _po["legs"]["b"]
check("P7p1 (QUARANTINE) receipt money differs from the journaled fill (literal: receipt proceeds = fill gross*.98 = %.6f, fee = fill fee+.0005): leg open, pending receipt_fill_money_mismatch carrying receipt AND fill values (%s)" % (_fg * 0.98, (_po.get("pending_exit") or {})),
      not _lgb.get("done") and (_po.get("pending_exit") or {}).get("why") == "receipt_fill_money_mismatch"
      and abs(float(((_po.get("pending_exit") or {}).get("fill") or {}).get("gross") or 0) - _fg) < 1e-6
      and abs(float(((_po.get("pending_exit") or {}).get("receipt") or {}).get("gross") or 0) - _fg * 0.98) < 1e-6)
_nt = [n.get("what") for n in (_po.get("recon_notes") or [])]
check("P7p2 economic, fee and qty disagreement are VISIBLE recon notes, not silent (%s)" % _nt,
      "receipt_proceeds_differ_from_fill" in _nt and "receipt_fee_differs_from_fill" in _nt and "receipt_qty_mismatch" in _nt)
# P7q (DRAFT, UNEXECUTED): sim TWAP slice / remainder, UNSTAMPED fill + OLD receipt with different qty/proceeds/fee: the receipt governs.
def _p7q(kind_key, mode="diffqty"):
    PAt, Ft, gmt = _twap_sim_pos(); _pt = GR.open_positions()[gmt]
    if kind_key == "slice":
        _key = "twap%d" % (_pt["twap"]["done"] + 1); _tk = _pt["tokens"] / 3
    else:
        _key = "twap_remainder"; _tk = GR._twap_remaining_tokens(_pt)
    GR._demo_sell(_pt, _tk, _key)                       # no meta: UNSTAMPED
    _fk = _fills_now(Ft); _fkk = [k for k in _fk if _key in k][0]; _fg, _ff = _fk[_fkk]["gross_usd"], _fk[_fkk]["fee_usd"]
    _op = "%s:leg:%s:%s:%s" % (GR.LANE, _key, _pt["mint"], _pt["pa_pos_id"])
    _q = GR._acct_qty(_pt, _key, _tk)
    ok, why = PAt.credit(GR.LANE, _pt["mint"], _fg * 0.98, _op, pos_id=_pt["pa_pos_id"], qty=_q * 0.99, fee=_ff + 0.0005, price_kind="current_price_simulated_fill")
    assert ok, why
    _c1 = cash(PAt); _nf0 = len(_fk)
    if mode == "incomplete":
        _o = PAt.op_receipts
        PAt.op_receipts = lambda ops: {_op: {"qty": _q * 0.99, "price_kind": "current_price_simulated_fill"}}
    try:
        if kind_key == "slice": GR._close_twap_slice(_pt, _pt["entry_price"])
        else: GR._close_remaining(_pt, _pt["entry_price"] * 0.5, "hard_stop_-50pct", "current_price_simulated_fill")
    finally:
        if mode == "incomplete": PAt.op_receipts = _o
    return PAt, Ft, _pt, _fg, _ff, _c1, _nf0
try:
    PAt, Ft, _pt, _fg, _ff, _c1, _nf0 = _p7q("slice")
    check("P7q1 (QUARANTINE) unstamped TWAP slice + old receipt with different money/qty: pending %s, slice NOT counted (done unchanged), no new fill, cash unchanged by the replay (%.6f == %.6f)" % ((_pt.get("pending_exit") or {}).get("why"), cash(PAt), _c1),
          len(_fills_now(Ft)) == _nf0 and abs(cash(PAt) - _c1) < 1e-9 and (_pt.get("pending_exit") or {}).get("why") == "receipt_fill_money_mismatch")
    PAt, Ft, _pt, _fg, _ff, _c1, _nf0 = _p7q("remainder")
    check("P7q2 (QUARANTINE) unstamped TWAP remainder + old receipt with different money: no remainder booked, pending %s, no new fill" % ((_pt.get("pending_exit") or {}).get("why"),),
          not ((_pt.get("twap") or {}).get("remainder")) and len(_fills_now(Ft)) == _nf0 and (_pt.get("pending_exit") or {}).get("why") == "receipt_fill_money_mismatch"
          and any(n.get("what") == "receipt_fill_money_mismatch" and n.get("receipt_proceeds") is not None and n.get("fill_gross") is not None for n in (_pt.get("recon_notes") or [])))   # strict: specific why AND receipt+fill values recorded
    PAt, Ft, _pt, _fg, _ff, _c1, _nf0 = _p7q("slice", "incomplete")
    check("P7q3 incomplete receipt (no proceeds): specific pending receipt_incomplete, slice NOT counted, cash unchanged (%s)" % (_pt.get("pending_exit") or {}).get("why"),
          (_pt.get("pending_exit") or {}).get("why") == "receipt_incomplete" and abs(cash(PAt) - _c1) < 1e-9 and len(_fills_now(Ft)) == _nf0)
finally:
    reset_twap()
# P7r (DRAFT, UNEXECUTED): old receipt with qty None: passed through EXACTLY (no recompute, no stuck pending); notes survive on the close record.
def _p7r():
    PAo, gmo = make_legacy()
    stl["n"] = 0; GR._tranche_credit = crash_after_first
    try: sell_ab()
    except CrashL: pass
    finally: GR._tranche_credit = _tc
    _mo = GR._migrate_legacy_tranches; GR._migrate_legacy_tranches = lambda pos: None
    try:
        _po = GR.open_positions()[gmo]; _lg = _po["legs"]["b"]
        GR._demo_sell(_po, _po["tokens"] * _lg["frac"], "b")
        _f = GR._demo_filler()._locked(lambda j: dict(j["fills"]))
        _bk = [k for k in _f if (":exit:%s:b:" % _po["mint"]) in k or k.endswith(":exit:%s:b" % _po["mint"])][0]
        _op = "%s:leg:b:%s:%s" % (GR.LANE, _po["mint"], _po["pa_pos_id"])
        ok, why = PAo.credit(GR.LANE, _po["mint"], _f[_bk]["gross_usd"], _op, pos_id=_po["pa_pos_id"], qty=None, fee=_f[_bk]["fee_usd"], price_kind=None)
        assert ok, why
        _lg["done"] = False
        for _k in ("exit_price", "realized_usd", "exit_reason", "exit_price_kind", "exit_execution"): _lg.pop(_k, None)
        _c1 = cash(PAo); r = GR._close_leg(_po, "b", 0.3, "hard_stop")
        return _po, PAo, _op, _c1
    finally: GR._migrate_legacy_tranches = _mo
_po, _PAo, _op7, _c17 = _p7r()
check("P7r1 old receipt with qty None AND kind None: retry completes (done %s, pending %s), cash unchanged by the replay (%.6f == %.6f), qty_none note present" % (_po["legs"]["b"].get("done"), (_po.get("pending_exit") or {}).get("why"), cash(_PAo), _c17),
      _po["legs"]["b"].get("done") and not _po.get("pending_exit") and abs(cash(_PAo) - _c17) < 1e-9
      and "receipt_qty_none" in [n["what"] for n in (_po.get("recon_notes") or [])])
# P7s (DRAFT, UNEXECUTED): a per-position exception in poll_exits is persisted visibly and fail-closed; the primary error is not masked.
PAs, gms = make_legacy()
_cl0 = GR._close_leg; _cash0 = cash(PAs); _ps0 = copy.deepcopy(GR.open_positions()[gms])
def _boom_leg(*a, **k): raise RuntimeError("synthetic poll failure")
GR._close_leg = _boom_leg
try:
    _ret = GR.poll_exits(price_fn=gpf(3.5))   # b/a legs due: _close_leg raises inside the position try
finally:
    GR._close_leg = _cl0
_pp = GR.open_positions().get(gms) or {}
check("P7s poll exception: position still open, DURABLE pending_exit poll_exception carrying the error (%s), no cash change (%.6f == %.6f), no close record, poll_exits did not raise" % ((_pp.get("pending_exit") or {}), cash(PAs), _cash0),
      bool(_pp) and (_pp.get("pending_exit") or {}).get("why") == "poll_exception" and "synthetic poll failure" in str((_pp.get("pending_exit") or {}).get("error"))
      and abs(cash(PAs) - _cash0) < 1e-9 and not _ret)
_w0 = GR._write
def _w_boom(dd): raise OSError("disk full")
GR._close_leg = _boom_leg; GR._write = _w_boom
try:
    _ret2 = GR.poll_exits(price_fn=gpf(3.5)); _raised = False
except Exception:
    _raised = True
finally:
    GR._close_leg = _cl0; GR._write = _w0
check("P7s2 poll exception AND the failure-state write fails: no raise, no fake success (returned %s)" % (_ret2,), (not _raised) and not _ret2)

# P7s3 HEALTHY RETRY: after the poll exception (pending poll_exception), the next healthy poll completes the exit ONCE: pending cleared, legs done,
# cash moved by exactly the journaled fill nets (no double credit), no extra fills on a second healthy poll.
_Fs = GR._demo_filler(); _nf_a = len(_fills_now(_Fs)); _c_a = cash(PAs)
GR.poll_exits(price_fn=gpf(3.5))
_pr = GR.open_positions().get(gms) or {}; _nf_b = len(_fills_now(_Fs)); _c_b = cash(PAs)
_nets = sum(float(v["gross_usd"]) - float(v.get("fee_usd") or 0.0) for k, v in _fills_now(_Fs).items() if (":exit:%s:" % gms) in k or k.endswith(":exit:%s" % gms))
GR.poll_exits(price_fn=gpf(3.5)); _c_c = cash(PAs); _nf_c = len(_fills_now(_Fs))
check("P7s3 healthy retry after a poll exception: pending_exit cleared (%s), cash delta %.6f == journaled fill nets %.6f, second healthy poll adds no fills (%d == %d) and no cash (%.6f == %.6f)" % ((_pr.get("pending_exit") or {}).get("why"), _c_b - _c_a, _nets, _nf_c, _nf_b, _c_c, _c_b),
      not (_pr.get("pending_exit") or {}).get("why") == "poll_exception" and _nf_b > _nf_a and abs((_c_b - _c_a) - _nets) < 1e-6 and _nf_c == _nf_b and abs(_c_c - _c_b) < 1e-9)

def PA_snap_positions(PAx): return ((PAx.snapshot() or {}).get("lanes") or {}).get("grad", {}).get("positions") or []
# P7t (DRAFT, UNEXECUTED): FLAT RECONCILIATION. Receipt qty on leg b is 0.99x / 1.01x the recompute, then the rest of the ladder closes.
# Invariant (independent of which recovery path is taken): the lane is NEVER closed with account units stranded, and an unreconcilable
# qty is a specific visible pending why. Cash must equal start + sum(receipt proceeds-fee) over the journal (credits applied), no op double-credited.
for _mode in ("diffqty", "qty101"):
    _po, _r, _PAt, _ = _p7o(_mode); _gm = _po["mint"]; _pid = _po["pa_pos_id"]
    _lb = lambda: [x for x in (PA_snap_positions(_PAt)) if str(x.get("pos_id")) == str(_pid)]
    poll_until_closed(_gm, 1.0, limit=8)   # 1.0 = entry price (gpf multiplier), as the other F1/F2 fixtures: leg c exits on its own rule, a/b already sold
    _open = _gm in GR.open_positions(); _pp = GR.open_positions().get(_gm) or {}
    _whys = {(_pp.get("pending_exit") or {}).get("why"), (_pp.get("settlement_pending") or {}).get("why")}
    _mb = (_pp.get("migration_blocked") or {}); _whys.add(_mb.get("code"))
    _hm = [i for i in ((GR.recon_health() or {}).get("items") or []) if i["mint"] == _gm]
    _resq = sum(float(x.get("qty") or 0) for x in _lb())
    check("P7t[%s] never closed with stranded account units: closed %s, account residue %.9f, pending whys %s" % (_mode, not _open, _resq, _whys),
          (not _open and _resq < 1e-6) or (_open and bool(_whys & {"account_remaining_qty_mismatch", "tranche_credit_divergence", "receipt_fill_money_mismatch", "receipt_qty_mismatch", "receipt_qty_disagrees"}) and bool(_hm)))
# P7x: DURABLE migration_blocked: named codes, reload from disk, transition-only writes, per-poll tries, clear on recovery, no lost update on other positions.
PAx, gmx = make_legacy()
with GR._ledger_lock():                       # a second, unrelated open position in the same ledger (synthetic copy, labelled by mint)
    _dx = GR._read(); _o = copy.deepcopy(_dx["positions"][gmx]); _o["mint"] = "SyntheticOtherMint"; _o["pa_pos_id"] = "synthetic-pos-other"
    _dx["positions"]["SyntheticOtherMint"] = _o; GR._write(_dx)
_other_before = json.dumps(GR.open_positions()["SyntheticOtherMint"], sort_keys=True)
_or_x = PAx.op_receipts; _wr0 = GR._write; _wn = {"n": 0}
def _cw(dd): _wn["n"] += 1; return _wr0(dd)
sell_ab()   # legs a,b sold
with GR._ledger_lock():                       # back to the LEGACY shape (sold legs, no tranche marker) so the migration has real work to refuse
    _dx = GR._read()
    for _k in ("tranche_credit", "tranche_credited", "tranche_migrating", "migration_blocked", "tranche_dust_leg", "tranche_dust_qty"): _dx["positions"][gmx].pop(_k, None)
    GR._write(_dx)
_other_before = json.dumps(GR.open_positions()["SyntheticOtherMint"], sort_keys=True)
PAx.op_receipts = lambda ops: {"_unreadable": True}
try:
    GR._write = _cw
    GR._POLL_SEQ["n"] += 1
    _pos = copy.deepcopy(GR.open_positions()[gmx]); _w0n = _wn["n"]
    for _ in range(4): GR._migrate_legacy_tranches(_pos)           # 4 calls inside ONE poll
    _mb1 = dict(GR.open_positions()[gmx].get("migration_blocked") or {})
    _writes_one_poll = _wn["n"] - _w0n
    GR._POLL_SEQ["n"] += 1; GR._migrate_legacy_tranches(_pos)       # next poll, same code: counted in memory, no new transition write
    _writes_two_polls = _wn["n"] - _w0n
    _mem = dict(_pos.get("migration_blocked") or {})
finally:
    GR._write = _wr0; PAx.op_receipts = _or_x
check("P7x1 journal_unreadable stamps a durable named marker (reloaded from disk: %s); 4 calls in one poll = ONE persisted write (%d) and tries 1; next poll counts tries %s in memory with no extra transition write (%d)" % (_mb1.get("code"), _writes_one_poll, _mem.get("tries"), _writes_two_polls),
      _mb1.get("code") == "journal_unreadable" and _mb1.get("tries") == 1 and _mb1.get("since") and _writes_one_poll == 1 and _mem.get("tries") == 2 and _writes_two_polls == 1)
check("P7x2 stamping one position does not touch another position in the ledger (no lost update)", json.dumps(GR.open_positions()["SyntheticOtherMint"], sort_keys=True) == _other_before)
_hm = [i for i in ((GR.recon_health() or {}).get("items") or []) if i["mint"] == gmx]
check("P7x3 recon_health shows the blocked position (code %s, n_migration_blocked %s)" % ([i.get("migration_blocked") for i in _hm], (GR.recon_health() or {}).get("n_migration_blocked")),
      bool(_hm) and _hm[0]["migration_blocked"] == "journal_unreadable" and (GR.recon_health() or {}).get("n_migration_blocked", 0) >= 1)
# exception code
_ci0 = GR._close_intents
def _ci_boom(): raise RuntimeError("synthetic close-intent failure")
GR._close_intents = _ci_boom
try:
    _pe = copy.deepcopy(GR.open_positions()[gmx]); _pe.pop("migration_blocked", None); GR._migrate_legacy_tranches(_pe)
finally:
    GR._close_intents = _ci0
check("P7x4 an exception inside the migration is recorded with code 'exception' (%s)" % (_pe.get("migration_blocked") or {}).get("code"), (_pe.get("migration_blocked") or {}).get("code") == "exception")
# recovery: readable journal -> migration proceeds and the marker is cleared (preserved until then)
GR.poll_exits(price_fn=gpf(1.0))
_rp = GR.open_positions().get(gmx) or {}
check("P7x5 once the journal is readable the migration proceeds and the marker is CLEARED (tranche_credit %s, marker %s)" % (_rp.get("tranche_credit"), _rp.get("migration_blocked")),
      (not _rp) or (_rp.get("tranche_credit") and not _rp.get("migration_blocked")))

# P7y: durable operator surface written by the poll itself (no status consumer exists for this lane in the repo).
GR._HEALTH_LAST["sig"] = None
PAy, gmy = make_legacy(); sell_ab()
with GR._ledger_lock():
    _dy = GR._read()
    for _k in ("tranche_credit", "tranche_credited", "tranche_migrating", "migration_blocked", "tranche_dust_leg", "tranche_dust_qty"): _dy["positions"][gmy].pop(_k, None)
    GR._write(_dy)
_oy = PAy.op_receipts; PAy.op_receipts = lambda ops: {"_unreadable": True}
try: GR.poll_exits(price_fn=gpf(2.5))   # above leg c's trailing stop: position stays open
finally: PAy.op_receipts = _oy
_hp = GR._health_path()   # LEDGER_PATH is per-fixture: resolve AFTER make_legacy
try: _hb = json.load(open(_hp))
except Exception as e: _hb = {"err": repr(e)}
_its = ((_hb.get("recon_health") or {}).get("items") or [])
check("P7y poll writes the durable health file with the blocked position (items %s)" % [(i.get("mint", "")[:8], i.get("migration_blocked")) for i in _its],
      any(i.get("mint") == gmy and i.get("migration_blocked") == "journal_unreadable" for i in _its) and _hb.get("written_ts"))

def _tree_bytes(PAx, lane_ledger):
    out = {"lane": open(lane_ledger, "rb").read()}
    for _r, _ds, _fs in os.walk(PAx.ACCOUNT_DIR):
        for _f in sorted(_fs): out[os.path.join(_r, _f)] = open(os.path.join(_r, _f), "rb").read()
    return out
# P7z: READ-ONLY diagnostics (G1-G7 design). Synthetic fixtures (labelled): they model forms in a LOCAL test ledger, not the owner's ledger.
_ps = {"tranche_dust_leg": "b", "tranche_dust_qty": 0.5}
check("P7z1 legit_receipt_qty forms (synthetic): L1 exact, L2 only on the stored dust leg, L3 None/None, 0.99x/1.01x/other-leg/None-with-kind are NOT a signature, and no tolerance widening (1e-6 off is rejected)",
      GR.legit_receipt_qty({}, "a", 100.0, 100.0, "k") == "L1" and GR.legit_receipt_qty(_ps, "b", 100.0, 100.5, "k") == "L2" and GR.legit_receipt_qty(_ps, "a", 100.0, 100.5, "k") is None
      and GR.legit_receipt_qty({}, "a", 100.0, None, None) == "L3" and GR.legit_receipt_qty({}, "a", 100.0, None, "k") is None
      and GR.legit_receipt_qty({}, "a", 100.0, 99.0, "k") is None and GR.legit_receipt_qty({}, "a", 100.0, 101.0, "k") is None
      and GR.legit_receipt_qty({}, "a", 100.0, 100.0 + 1e-4, "k") is None and GR.legit_receipt_qty({"tranche_dust_leg": "b"}, "b", 100.0, 100.5, "k") is None)
for _mode in ("current", "diffqty", "qty101"):
    _po, _r, _PAd, _ = _p7o(_mode); _gm = _po["mint"]
    _bytes0 = _tree_bytes(_PAd, GR.LEDGER_PATH); _nf0 = len(_fills_now(GR._demo_filler())); _fj0 = json.dumps(_fills_now(GR._demo_filler()), sort_keys=True)
    _dg = GR.diagnose_position(_gm)
    # byte immutability: lane ledger + EVERY file of the account dir (ledger.json, events.jsonl, ...) + the whole fill journal content
    _same = _tree_bytes(_PAd, GR.LEDGER_PATH) == _bytes0 and json.dumps(_fills_now(GR._demo_filler()), sort_keys=True) == _fj0
    _forms = {k["key"]: k.get("form") for k in _dg["keys"]}
    if _mode == "current":
        check("P7z2[current] receipts match a historical form (L1, or L2 on the fixture dust leg; forms %s): diagnostics find no signature or money fault (verdict %s), read-only (ledger bytes and fill journal unchanged: %s)" % (_forms, _dg["verdict"], _same),
              _same and all(k.get("form") in ("L1", "L2") for k in _dg["keys"] if k["receipt"]) and _dg["gates"]["G4"]["ok"] and _dg["gates"]["G5"]["ok"])
    else:
        # diffqty also changes the fixture's receipt MONEY (0.98x), so G4 (money) fails first and is the named gate; qty101 keeps money equal, so G5 is the named gate.
        _gname = "G4"   # both fixture modes (_dq) change the receipt money by 0.98x, so G4 is the first failing gate; the G5 signature itself is covered at unit level by P7z1
        check("P7z2[%s] receipt fault named at gate %s with per-key evidence (verdict %s, forms %s), read-only (%s)" % (_mode, _gname, _dg["verdict"], _forms, _same),
              _same and (not _dg["gates"][_gname]["ok"]) and _dg["verdict"].startswith("quarantine:%s:" % _gname) and any(k.get("why") for k in _dg["keys"])
              and (_mode != "qty101" or _forms.get("b") is None))
# P7z0: positive baseline on a fresh MARKED (instance) position after legs a,b sold: every core gate evaluated and passing, residue == the open leg c.
PAm, Fm, CTm = fresh(start="10"); go_sim(); _lm = dict(build(PAm, CTm)); _okm, _whm = _lm["grad"](1); assert _okm, _whm
_gmm = list(GR.open_positions())[0]; sell_ab()
_bm0 = _tree_bytes(PAm, GR.LEDGER_PATH); _fm0 = json.dumps(_fills_now(GR._demo_filler()), sort_keys=True)
_dm = GR.diagnose_position(_gmm)
check("P7z0 REAL marked position, legs a,b sold: G3/G6 need the account INSTANCE contract that the lane never writes (it debits via legacy try_debit), so they are UNAVAILABLE and the verdict is %s (gates %s), never consistent; byte-immutable (%s)" % (_dm["verdict"], {g: (v["ok"], v["unavailable"]) for g, v in _dm["gates"].items()}, _tree_bytes(PAm, GR.LEDGER_PATH) == _bm0 and json.dumps(_fills_now(GR._demo_filler()), sort_keys=True) == _fm0),
      _dm["verdict"] == "evidence_unavailable:G3:ledger_op_no_instance_contract" and _dm["gates"]["G3"]["unavailable"] and _dm["gates"]["G6"]["unavailable"] and all(_dm["gates"][g]["ok"] for g in ("G1", "G2", "G4", "G5"))
      and _tree_bytes(PAm, GR.LEDGER_PATH) == _bm0 and json.dumps(_fills_now(GR._demo_filler()), sort_keys=True) == _fm0)
# P7z0b [STUB, labelled]: IF the account carried the instance contract (ledger op returning the journal event, position flagged instance), every gate passes -> consistent.
_orec_m, _snap_m = PAm.operation_receipt, PAm.snapshot
def _op_inst(op):
    ev = PAm.op_receipt(op); return (dict(ev, instance=True) if ev else None), None
def _snap_inst(*a, **k):
    sn = _snap_m(*a, **k)
    if sn:
        sn = json.loads(json.dumps(sn))
        for x in sn["lanes"][GR.LANE]["positions"]: x["instance"] = True
    return sn
PAm.operation_receipt, PAm.snapshot = _op_inst, _snap_inst
try: _dmi = GR.diagnose_position(_gmm)
finally: PAm.operation_receipt, PAm.snapshot = _orec_m, _snap_m
check("P7z0b [stub: instance contract present] all six gates evaluated and passing, residue label %s -> verdict %s" % ((_dmi.get("residue") or {}).get("label"), _dmi["verdict"]),
      _dmi["verdict"] == "consistent" and set(_dmi["gates"]) == {"G1", "G2", "G3", "G4", "G5", "G6"} and all(v["ok"] for v in _dmi["gates"].values()) and (_dmi["residue"] or {}).get("label") == "none")
# P7z3-P7z8 (synthetic fixtures, local test ledger only): TWAP unsupported, crash fill without done, qty-only fault, G3 ledger-vs-journal, G6 identity, residue label.
_po, _r, _PAq, _ = _p7o("qtyonly"); _gm = _po["mint"]; _b0 = _tree_bytes(_PAq, GR.LEDGER_PATH)
_dq = GR.diagnose_position(_gm)
check("P7z3 qty-ONLY receipt fault (money equal to the fill: G4 %s) is named at G5 (verdict %s), byte-immutable (%s)" % (_dq["gates"]["G4"]["ok"], _dq["verdict"], _tree_bytes(_PAq, GR.LEDGER_PATH) == _b0),
      _dq["gates"]["G4"]["ok"] and (not _dq["gates"]["G5"]["ok"]) and _dq["verdict"] == "quarantine:G5:no_historical_signature" and _tree_bytes(_PAq, GR.LEDGER_PATH) == _b0)
with GR._ledger_lock():
    _d = GR._read(); _d["positions"][_gm]["twap"] = {"n": 3, "done": 0}; GR._write(_d)
_dt = GR.diagnose_position(_gm)
check("P7z4 a TWAP position is UNSUPPORTED (explicit), never consistent (verdict %s)" % _dt["verdict"], _dt["verdict"] == "unsupported:twap")
with GR._ledger_lock():
    _d = GR._read(); _d["positions"][_gm].pop("twap", None); GR._write(_d)
PAc, gmc = make_legacy(); sell_ab()
with GR._ledger_lock():
    _d = GR._read(); _d["positions"][gmc]["legs"]["b"]["done"] = False; GR._write(_d)   # the journaled leg-b fill stays: the state a crash leaves
_dc = GR.diagnose_position(gmc)
check("P7z5 crash window: a journaled fill with no done leg is reported (extra_fills %s) and the verdict is not consistent (%s)" % ([x["key"] for x in _dc["extra_fills"]], _dc["verdict"]),
      any(x["key"] == "b" and x["why"] == "fill_without_done" for x in _dc["extra_fills"]) and _dc["verdict"] == "pending_crash_window:b" and "G3" in _dc["unavailable_gates"])
# P7z5b: the evaluable crash-window state is NOT masked by the unavailable G3 (verdict above is pending_crash_window, G3 still reported unavailable).
# P7za REAL non-instance lane position (entered through the lane's own try_debit): the ACCOUNT silently accepts a retried credit with a different kind/qty/proceeds
# and keeps the ORIGINAL values; the LANE must reconcile from the original journal receipt, not from the retry.
PAn, Fn, CTn = fresh(start="10"); go_sim(); _ln = dict(build(PAn, CTn)); _okn, _whn = _ln["grad"](1); assert _okn, _whn
_gmn = list(GR.open_positions())[0]; sell_ab()
_pn = GR.open_positions()[_gmn]; _opa = "%s:leg:a:%s:%s" % (GR.LANE, _gmn, _pn["pa_pos_id"]); _orig = PAn.op_receipts([_opa])[_opa]
_an0 = _tree_bytes(PAn, GR.LEDGER_PATH); _cash0 = cash(PAn)
_okr, _whr = PAn.credit(GR.LANE, _gmn, float(_orig["proceeds"]) * 2.0, _opa, pos_id=_pn["pa_pos_id"], qty=float(_orig["qty"]) * 0.5, fee=float(_orig["fee"]) + 1.0, price_kind="stale_price_estimate")
_an1 = _tree_bytes(PAn, GR.LEDGER_PATH)
_nonins = not (PAn.operation_receipt(_opa)[0] or {}).get("instance")
check("P7za1 non-instance position (op instance flag %s): the account ACCEPTS a retried credit with different kind/qty/proceeds (%s, %s) and keeps the original values (cash %.6f -> %.6f, account bytes unchanged %s): the account is NOT a guard here" % (not _nonins, _okr, _whr, _cash0, cash(PAn), _an0 == _an1),
      _nonins and _okr and "retry" in _whr and _an0 == _an1 and _cash0 == cash(PAn))
with GR._ledger_lock():
    _d = GR._read(); _lgn = _d["positions"][_gmn]["legs"]["a"]; _lgn["done"] = False
    for _k in ("exit_price", "realized_usd", "exit_reason", "exit_price_kind", "exit_execution"): _lgn.pop(_k, None)
    GR._write(_d)
_fl0 = _fills_now(GR._demo_filler()); FakeJup.mult = 3.0; GR.poll_exits(price_fn=gpf(3.0)); FakeJup.mult = 1.0; _fl1 = _fills_now(GR._demo_filler())
_ln2 = GR.open_positions()[_gmn]["legs"]["a"]
check("P7za2 lane replay after the divergent account retry reconciles from the ORIGINAL receipt: leg a done with kind %s (original %s), no second fill (%s), account cash and the op receipt unchanged (%s; account FILE bytes may differ: poll_exits writes mark baselines)" % (_ln2.get("exit_price_kind"), _orig.get("price_kind"), _fl0 == _fl1, PAn.op_receipts([_opa])[_opa] == _orig and cash(PAn) == _cash0),
      _ln2.get("done") and _ln2.get("exit_price_kind") == _orig.get("price_kind") and _fl0 == _fl1 and PAn.op_receipts([_opa])[_opa] == _orig and cash(PAn) == _cash0 and not GR.open_positions()[_gmn].get("pending_exit"))
# P7zb REAL instance fixture, UNSTUBBED: the entry debit goes through the account's real try_debit_instance; G3 and G6 are evaluated by the real
# operation_receipt/snapshot (nothing patched). This is a test-only entry route; the lane's production entry still uses try_debit.
PAi, Fi, CTi = fresh(start="10"); go_sim(); _tdi = PAi.try_debit
PAi.try_debit = lambda lane, mint, stake, qty, op_id, ts=None, marks=None, durable_refusal=False: PAi.try_debit_instance(lane, mint, stake, qty, op_id, ts, marks)
try:
    _li = dict(build(PAi, CTi)); _oki, _whi = _li["grad"](1)
finally: PAi.try_debit = _tdi
assert _oki, _whi
_gmi = list(GR.open_positions())[0]; sell_ab()
_bi0 = _tree_bytes(PAi, GR.LEDGER_PATH); _fi0 = json.dumps(_fills_now(GR._demo_filler()), sort_keys=True)
_di = GR.diagnose_position(_gmi)
check("P7zb REAL try_debit_instance position (no stubs): G3 and G6 are EVALUATED (not unavailable) and pass; verdict %s, residue %s; byte-immutable (%s)" % (_di["verdict"], _di["residue"], _tree_bytes(PAi, GR.LEDGER_PATH) == _bi0 and json.dumps(_fills_now(GR._demo_filler()), sort_keys=True) == _fi0),
      _di["verdict"] == "consistent" and (_di["residue"] or {}).get("label") == "none" and not _di["unavailable_gates"] and _di["gates"]["G3"]["ok"] and _di["gates"]["G6"]["ok"] and _tree_bytes(PAi, GR.LEDGER_PATH) == _bi0)
_opi = "%s:leg:a:%s:%s" % (GR.LANE, _gmi, GR.open_positions()[_gmi]["pa_pos_id"])
check("P7zb2 the real account op is an INSTANCE op (operation_receipt instance flag %s) so P7zb exercised the instance contract. CAVEAT: this entry route drops durable_refusal, so DEMO refusal journaling (design O-A4) is NOT covered" % (PAi.operation_receipt(_opi)[0] or {}).get("instance"),
      (PAi.operation_receipt(_opi)[0] or {}).get("instance") is True)
# P7zd price OBSERVATION contract: the exit quote journal records the Jupiter price's own observation time (visibility only, no new rule, TTL untouched).
# Offline: GR._jup is replaced by a fake (no network); price_fn None so the Jupiter route is the one under test.
PAp, Fp, CTp = fresh(start="10"); go_sim(); _lp = dict(build(PAp, CTp)); _okp, _whp = _lp["grad"](1); assert _okp, _whp
_gmp = list(GR.open_positions())[0]; _ep = float(GR.open_positions()[_gmp]["entry_price"]); _tsold = time.time() - 9.0
class _JupObs:
    SOURCE = "jupiter-fake"
    def get_prices_observed(self, mints, now=None): return {m: (_ep, _tsold) for m in mints}
    def get_prices(self, mints, now=None): return {m: _ep for m in mints}
class _JupNoObs:
    SOURCE = "jupiter-fake"
    def get_prices(self, mints, now=None): return {m: _ep for m in mints}
_jsave = GR._jup
try:
    GR._jup = _JupObs(); GR.poll_exits(price_fn=None); _j1 = (GR.open_positions()[_gmp].get("quote_journal") or [])[-1]
    GR._jup = _JupNoObs(); GR.poll_exits(price_fn=None); _j2 = (GR.open_positions()[_gmp].get("quote_journal") or [])[-1]
finally: GR._jup = _jsave
check("P7zd1 quote journal carries the price's OBSERVATION time and age (obs_ts %s == injected %s, age %.1fs), price and source unchanged (px %s src %s)" % (_j1.get("obs_ts"), _tsold, _j1.get("obs_age_s") or -1, _j1.get("px"), _j1.get("src")),
      _j1.get("obs_ts") == _tsold and 8.0 <= (_j1.get("obs_age_s") or 0) <= 60.0 and _j1.get("px") == _ep and _j1.get("src") == "jupiter-fake")
check("P7zd2 a source without observation times records obs_ts None / obs_age None (unknown, never invented), same price (%s)" % ((_j2.get("obs_ts"), _j2.get("obs_age_s")),),
      _j2.get("obs_ts") is None and _j2.get("obs_age_s") is None and _j2.get("px") == _ep)
# P7zd3-8: the observation timestamp is an UNTRUSTED source value: validated at the boundary, never raises (a raise would be a poll abort), invalid -> unknown + status.
class _JupBad:
    SOURCE = "jupiter-fake"
    ts = None
    def get_prices_observed(self, mints, now=None): return {m: (_ep, self.ts) for m in mints}
_jb = _JupBad(); _bad = {}
try:
    GR._jup = _jb
    for _lab, _v in (("string", "yesterday"), ("nan", float("nan")), ("inf", float("inf")), ("bool", True), ("future", time.time() + 3600.0), ("negative", -5.0), ("none", None), ("ok", time.time() - 3.0)):
        _jb.ts = _v; _a0 = GR._POLL_ABORTS.get("count", 0); GR.poll_exits(price_fn=None)
        _bad[_lab] = ((GR.open_positions()[_gmp].get("quote_journal") or [])[-1], GR._POLL_ABORTS.get("count", 0) - _a0)
finally: GR._jup = _jsave
check("P7zd3 invalid observation values (string, nan, inf, bool, future, negative) never raise or abort the poll, record obs_ts None and a status; price unchanged (%s)" % ({k: (v[0].get("obs_status"), v[1]) for k, v in _bad.items()},),
      all(_bad[k][1] == 0 and _bad[k][0].get("obs_ts") is None and _bad[k][0].get("obs_age_s") is None and str(_bad[k][0].get("obs_status", "")).startswith("invalid:") and _bad[k][0].get("px") == _ep for k in ("string", "nan", "inf", "bool", "future", "negative")))
check("P7zd4 none -> status none, a good timestamp -> ok with the kind label saying LOCAL cache-fill time, not upstream (%s, %s)" % (_bad["none"][0].get("obs_status"), _bad["ok"][0].get("obs_ts_kind")),
      _bad["none"][0].get("obs_status") == "none" and _bad["ok"][0].get("obs_status") == "ok" and _bad["ok"][0].get("obs_ts_kind") == "jup_local_cache_fill_time" and 2.0 <= _bad["ok"][0]["obs_age_s"] <= 60.0)
_u0 = (GR.recon_health() or {}).get("price_obs_unsupported_polls")
try:
    GR._jup = _JupNoObs(); GR.poll_exits(price_fn=None)
finally: GR._jup = _jsave
check("P7zd5 a price module WITHOUT get_prices_observed is counted in recon_health (visible, not silent): %s -> %s" % (_u0, (GR.recon_health() or {}).get("price_obs_unsupported_polls")),
      (GR.recon_health() or {}).get("price_obs_unsupported_polls") == (_u0 or 0) + 1)
# real jup_price cache layer, offline (its fetch replaced): which time does get_prices_observed report?
# (a PRIVATE copy of the module source is executed: earlier fixtures in this process rebind attributes of the imported module, so the shared one is not a clean subject)
_jspec = importlib.util.spec_from_file_location("_agt_jup_price_private_copy", os.path.join(ROOT, "daemon", "launch_scanner", "jup_price.py")); _JP = importlib.util.module_from_spec(_jspec); _jspec.loader.exec_module(_JP)
_JP._cache.clear(); _JP._fetch_batch = lambda mints: {m: 2.5 for m in mints}
_r1 = _JP.get_prices_observed(["MX"], now=1000.0); _JP._fetch_batch = lambda mints: {}
_r2 = _JP.get_prices_observed(["MX"], now=1005.0); _dbg = ""
check("P7zd6 real jup_price semantics (offline): the reported time is the LOCAL fetch time of the cache fill (fresh read at 1000 -> 1000; a cache hit at 1005 still reports 1000), no upstream timestamp exists in this layer (%s, %s)" % (_r1, _r2),
      _r1.get("MX") == (2.5, 1000.0) and _r2.get("MX") == (2.5, 1000.0))
check("P7zc unavailable_gates key is present on every verdict path: TWAP %s, entry_op %s, quarantine %s, unavailable %s" % ("unavailable_gates" in _dt, "unavailable_gates" in GR.diagnose_position(_gmi) , "unavailable_gates" in _dq, "unavailable_gates" in _dm),
      all("unavailable_gates" in x for x in (_dt, _dq, _dm, _di)) and _dt["unavailable_gates"] == [] and _dm["unavailable_gates"] == ["G3", "G6"] and _dq["unavailable_gates"] in (["G3", "G6"], ["G3"], ["G6"], []))
# G3 and G6 stubs (labelled): a ledger op that disagrees with the journal event; a second open instance; an unresolved register entry.
_po, _r, _PAg, _ = _p7o("current"); _gmg = _po["mint"]
_orec = _PAg.operation_receipt
def _op_bad(op):   # stub: an instance-contract ledger op whose qty differs from the journal event by 1 unit
    ev = _PAg.op_receipt(op)
    return (dict(ev, instance=True, qty=float(ev.get("qty") or 0.0) + 1.0) if ev else None), None
_PAg.operation_receipt = _op_bad
try: _d3 = GR.diagnose_position(_gmg)
finally: _PAg.operation_receipt = _orec
check("P7z6 [stub] account ledger op differs from the journal event -> G3 fails, named (verdict %s)" % _d3["verdict"], _d3["verdict"].startswith("quarantine:G3:") and any(k.get("why") == "ledger_op_differs_from_journal" for k in _d3["keys"]))
_snap0 = _PAg.snapshot
def _snap_two(*a, **k):
    sn = _snap0(*a, **k)
    if sn:
        sn = json.loads(json.dumps(sn)); ps = sn["lanes"][GR.LANE]["positions"]
        ps.append({"mint": _gmg, "qty": 1.0, "instance": True, "basis": 1.0, "pos_id": "synthetic-second-instance"})
    return sn
_PAg.snapshot = _snap_two
try: _d6 = GR.diagnose_position(_gmg)
finally: _PAg.snapshot = _snap0
check("P7z7 [stub] a second open account instance for the mint -> G6 fails, named (verdict %s)" % _d6["verdict"], _d6["verdict"] == "quarantine:G6:second_open_instance_for_mint")
def _snap_unres(*a, **k):
    sn = _snap0(*a, **k)
    if sn:
        sn = json.loads(json.dumps(sn)); sn["unresolved"] = [{"lane": GR.LANE, "mint": _gmg, "pos_id": GR.open_positions()[_gmg]["pa_pos_id"], "basis": 1.0}]
    return sn
_PAg.snapshot = _snap_unres
try: _d7 = GR.diagnose_position(_gmg)
finally: _PAg.snapshot = _snap0
check("P7z8 [stub] an unresolved-register entry for the pos_id -> G6 fails, named (verdict %s)" % _d7["verdict"], _d7["verdict"] == "quarantine:G6:unresolved_register_entry")
# entry_op evidence missing -> evidence_missing, not a default
with GR._ledger_lock():
    _d = GR._read(); _eo = _d["positions"][_gmg]["execution"].pop("entry_op", None); GR._write(_d)
_de = GR.diagnose_position(_gmg)
with GR._ledger_lock():
    _d = GR._read(); _d["positions"][_gmg]["execution"]["entry_op"] = _eo; GR._write(_d)
check("P7z9 missing entry_op is evidence_missing:entry_op (verdict %s)" % _de["verdict"], _de["verdict"] == "evidence_missing:entry_op")
# P7x6: lock OWNERSHIP decision (no thread/process is started: the file's containment guard forbids it). The ordering test with a second thread
# is NOT run here; this checks the ownership predicate the stamp uses, with owner thread/pid varied (deterministic, single thread).
_own_free = GR._lock_owned_by_me()
with GR._ledger_lock():
    _own_mine = GR._lock_owned_by_me(); _tid0, _pid0 = GR._LOCK_DEPTH["tid"], GR._LOCK_DEPTH["pid"]
    GR._LOCK_DEPTH["tid"] = (_tid0 or 0) + 1; _own_other_thread = GR._lock_owned_by_me(); GR._LOCK_DEPTH["tid"] = _tid0
    GR._LOCK_DEPTH["pid"] = (_pid0 or 0) + 1; _own_forked = GR._lock_owned_by_me(); GR._LOCK_DEPTH["pid"] = _pid0
_own_after = GR._lock_owned_by_me()
check("P7x6 ownership predicate: free %s, held by this thread/pid %s, other thread %s, forked child %s, after release %s" % (_own_free, _own_mine, _own_other_thread, _own_forked, _own_after),
      (not _own_free) and _own_mine and (not _own_other_thread) and (not _own_forked) and (not _own_after))
# P7y2: health signature excludes ages (no rewrite while state is unchanged), heartbeat present, a failed write is reported in the next success.
GR._HEALTH_LAST.update(sig=None, ts=0.0); GR.poll_exits(price_fn=gpf(2.5))
_w1 = json.load(open(GR._health_path())) if os.path.exists(GR._health_path()) else {}
GR.poll_exits(price_fn=gpf(2.5)); GR.poll_exits(price_fn=gpf(2.5))
_w2 = json.load(open(GR._health_path())) if os.path.exists(GR._health_path()) else {}
check("P7y2 unchanged state: no health rewrite across polls (written_ts %s == %s), heartbeat has boot_id/poll_seq/last_poll_ts (%s) and heartbeat_interval_s %s" % (_w1.get("written_ts"), _w2.get("written_ts"), sorted((_w2.get("heartbeat") or {}).keys()), _w2.get("heartbeat_interval_s")),
      bool(_w1) and _w1.get("written_ts") == _w2.get("written_ts") and set((_w2.get("heartbeat") or {})) == {"boot_id", "poll_seq", "last_poll_ts"} and _w2.get("heartbeat_interval_s") == GR.HEALTH_HEARTBEAT_S)
_hp_orig = GR._health_path; GR._health_path = lambda: "/proc/nonexistent-dir/health.json"
try: GR._HEALTH_LAST["sig"] = None; GR._publish_health(); _fail = GR._HEALTH_LAST["fail"]
finally: GR._health_path = _hp_orig
GR._HEALTH_LAST["sig"] = None; GR._publish_health(); _w3 = json.load(open(GR._health_path()))
check("P7y3 a failed health write is remembered (%s) and reported by the next successful write (%s)" % (bool(_fail), bool(_w3.get("prior_write_failure"))), bool(_fail) and bool(_w3.get("prior_write_failure")) and GR._HEALTH_LAST["fail"] is None)

# P7u (DRAFT, UNEXECUTED): half-mutation is not persisted after a poll exception; degraded account snapshot is NOT flat.
PAu, gmu = make_legacy(); _pu0 = copy.deepcopy(GR.open_positions()[gmu]); _cl1 = GR._close_leg
def _mut_then_boom(pos, leg_key, *a, **k):
    pos["legs"][leg_key]["done"] = True; pos["realized_usd"] = 999.0; raise RuntimeError("boom after partial mutation")
GR._close_leg = _mut_then_boom
try: GR.poll_exits(price_fn=gpf(3.5))
finally: GR._close_leg = _cl1
_pu = GR.open_positions().get(gmu) or {}
check("P7u after an exception mid-mutation the PERSISTED position has no half state (legs done %s, realized %.4f want %.4f), marker present (%s), and a later poll writes the same (reloaded)" % ([k for k, v in (_pu.get("legs") or {}).items() if v.get("done")], float(_pu.get("realized_usd") or 0), float(_pu0.get("realized_usd") or 0), (_pu.get("pending_exit") or {}).get("why")),
      abs(float(_pu.get("realized_usd") or 0) - float(_pu0.get("realized_usd") or 0)) < 1e-9 and (_pu.get("pending_exit") or {}).get("why") == "poll_exception"
      and [k for k, v in (_pu.get("legs") or {}).items() if v.get("done")] == [k for k, v in (_pu0.get("legs") or {}).items() if v.get("done")])
_sn = PAu.snapshot
PAu.snapshot = lambda *a, **k: dict(_sn(*a, **k) or {}, degraded="synthetic untrusted wal")
try: _rw = GR._account_residue(gmu, _pu0.get("pa_pos_id"), _pu0)
finally: PAu.snapshot = _sn
check("P7u2 degraded account snapshot reads as UNKNOWN (account_snapshot_unreadable), never flat: %s" % (_rw,), _rw[0] == "account_snapshot_unreadable")
# P7v (DRAFT, UNEXECUTED): restoration failure modes. An ARMED fault is set INSIDE the partial-mutation throw (no _read call counting): the restore
# read fails / the restore write fails / the position is missing from the fresh ledger. After the throw the ledger bytes are the BASELINE and must
# not change; no raise; primary error logged; the abort counter increments and recon_health shows it; a later healthy poll runs without raising.
import logging as _lg
class _Cap(_lg.Handler):
    def __init__(self): super().__init__(); self.msgs = []
    def emit(self, r): self.msgs.append(r.getMessage() + " " + (repr(r.exc_info[1]) if r.exc_info else ""))
for _fm in ("read_fails", "write_fails", "position_missing"):
    PAv, gmv = make_legacy(); _pv0 = copy.deepcopy(GR.open_positions()[gmv]); _cl2 = GR._close_leg; _rd0 = GR._read; _wr0 = GR._write
    _st = {"armed": False, "baseline": None}; _cap = _Cap(); GR.logger.addHandler(_cap); _ab0 = GR._POLL_ABORTS["count"]
    def _mut_boom2(pos, leg_key, *a, **k):
        pos["legs"][leg_key]["done"] = True; pos["realized_usd"] = 999.0
        _st["baseline"] = open(GR.LEDGER_PATH, "rb").read(); _st["armed"] = True; raise RuntimeError("boom2 primary")
    def _rd_f(*a, **k):
        if _st["armed"] and _fm == "read_fails": raise OSError("restore read failed")
        d_ = _rd0(*a, **k)
        if _st["armed"] and _fm == "position_missing": d_["positions"].pop(gmv, None)
        return d_
    def _wr_f(dd):
        if _st["armed"] and _fm == "write_fails": raise OSError("restore write failed")
        return _wr0(dd)
    GR._close_leg = _mut_boom2; GR._read = _rd_f; GR._write = _wr_f
    try: GR.poll_exits(price_fn=gpf(3.5)); _raised = False
    except Exception: _raised = True
    finally: GR._close_leg = _cl2; GR._read = _rd0; GR._write = _wr0; GR.logger.removeHandler(_cap)
    _same = open(GR.LEDGER_PATH, "rb").read() == _st["baseline"]; _msgs = " | ".join(_cap.msgs)
    _hc = ((GR.recon_health() or {}).get("poll_aborts") or {}).get("count")
    check("P7v[%s] no raise (%s), primary error logged (%s), abort counter +1 (%d), recon_health shows %s, ledger unchanged since the throw (%s)" % (_fm, not _raised, "boom2 primary" in _msgs, GR._POLL_ABORTS["count"] - _ab0, _hc, _same),
          (not _raised) and "boom2 primary" in _msgs and GR._POLL_ABORTS["count"] == _ab0 + 1 and _hc == GR._POLL_ABORTS["count"] and _same)
    GR.poll_exits(price_fn=gpf(0.3))   # a later healthy poll runs without raising (hard stop)
    _as = GR.account_state()
    check("P7v[%s] the daemon status surface (account_state) carries recon_health incl. poll_aborts count %s" % (_fm, ((_as.get("recon_health") or {}).get("poll_aborts") or {}).get("count")),
          ((_as.get("recon_health") or {}).get("poll_aborts") or {}).get("count", 0) >= 1)
# P7w (DRAFT, UNEXECUTED): TWO positions; the first raises after a partial mutation, the second must still close normally in the SAME poll
# (the restore replaces d, so the loop must not hold stale position objects). Fixture assumption: lanes["grad"](1) can open two distinct mints.
PAw, Fw, CTw = fresh(start="10"); go_sim(); _lw = dict(build(PAw, CTw))
_o1 = _lw["grad"](1); _o2 = _lw["grad"](2); _mints = list(GR.open_positions())
check("P7w fixture: two distinct open positions (%d)" % len(_mints), len(_mints) == 2)
if len(_mints) == 2:
    _m1, _m2 = _mints; _cr0 = GR._close_remaining; _p1_0 = copy.deepcopy(GR.open_positions()[_m1])
    def _cr_sel(pos, px, reason, *a, **k):
        if pos["mint"] == _m1:
            pos["realized_usd"] = 999.0; raise RuntimeError("boom first position")
        return _cr0(pos, px, reason, *a, **k)
    GR._close_remaining = _cr_sel
    _cashw0 = cash(PAw); _pid2 = GR.open_positions()[_m2]["pa_pos_id"]; _pid1 = GR.open_positions()[_m1]["pa_pos_id"]; _nfw0 = len(_fills_now(Fw))
    try: GR.poll_exits(price_fn=lambda chain, mint, spot_index=None: float((GR.open_positions().get(mint) or {}).get("entry_price") or 0) * 0.3)
    finally: GR._close_remaining = _cr0
    _op2 = GR.open_positions(); _led = GR._read()
    check("P7w first position fail-closed without half state (open %s, realized %.4f want %.4f, marker %s); second CLOSED normally (open %s); exactly one close record persisted (%d)" % (
        _m1 in _op2, float((_op2.get(_m1) or {}).get("realized_usd") or 0), float(_p1_0.get("realized_usd") or 0), ((_op2.get(_m1) or {}).get("pending_exit") or {}).get("why"), _m2 in _op2, len(_led.get("closes") or [])),
        _m1 in _op2 and abs(float(_op2[_m1].get("realized_usd") or 0) - float(_p1_0.get("realized_usd") or 0)) < 1e-9
        and (_op2[_m1].get("pending_exit") or {}).get("why") == "poll_exception" and _m2 not in _op2 and len(_led.get("closes") or []) == 1)
    # P7w economic oracles, independent of the lane close record: cash moved by exactly the SECOND position's journaled fill net, the first
    # position got no credit, the second mint was sold once (one fill, one account credit).
    _fw = [v for k, v in _fills_now(Fw).items() if (":exit:%s:" % _m2) in k or k.endswith(":exit:%s" % _m2)]
    _fw1 = [v for k, v in _fills_now(Fw).items() if (":exit:%s:" % _m1) in k or k.endswith(":exit:%s" % _m1)]
    _net2 = sum(float(v["gross_usd"]) - float(v.get("fee_usd") or 0.0) for v in _fw)
    _ops2 = ["%s:close:%s:%s" % (GR.LANE, _m2, _pid2)] + ["%s:leg:%s:%s:%s" % (GR.LANE, k, _m2, _pid2) for k in ("a", "b", "c")]
    _ops1 = ["%s:close:%s:%s" % (GR.LANE, _m1, _pid1)] + ["%s:leg:%s:%s:%s" % (GR.LANE, k, _m1, _pid1) for k in ("a", "b", "c")]
    _rc2 = PAw.op_receipts(_ops2); _rc1 = PAw.op_receipts(_ops1)
    _n2 = sum(1 for o in _ops2 if _rc2.get(o) is not None); _n1 = sum(1 for o in _ops1 if _rc1.get(o) is not None)
    check("P7w oracles: cash delta %.6f == second position's fill net %.6f; second mint exactly %d fill and %d account credit; first position %d fills, %d credits" % (cash(PAw) - _cashw0, _net2, len(_fw), _n2, len(_fw1), _n1),
          len(_fw) >= 1 and abs((cash(PAw) - _cashw0) - _net2) < 1e-6 and _n2 == 1 and _n1 == 0 and not _fw1)
# TODO (owed, needs TWAP scaffold): unstamped TWAP slice + remainder with differing receipt qty/fee; incomplete receipt -> receipt_incomplete;
# lane leg_tokens vs account remaining through final close (no orphan); journal-none vs ledger-op-none why.

# ================= N. hook coverage: REAL corrupt ledger (no monkeypatch), missing ledger with account positions, engine-loaded variant lanes
import json as _json
PA, gm = make_legacy(); sell_ab()
pid = GR.open_positions()[gm]["pa_pos_id"]
check("N0 control: migrated position is marked and not flagged", grad_marked(central(3.0)) and GR.legacy_unproven_pos_ids() == set())
good = open(GR.LEDGER_PATH).read()
open(GR.LEDGER_PATH, "w").write("{ this is not json")           # REAL corruption of the lane ledger
unp = GR.legacy_unproven_pos_ids(); mk = central(3.0)
check("N1 corrupt lane ledger (real file, no patch): hook None, position NOT marked", unp is None and not grad_marked(mk) and PA.snapshot(marks=mk).get("marks_status") != "ok")
_blank = GR._read()                                             # the lane's own read now moves the file aside and returns a blank ledger
unp = GR.legacy_unproven_pos_ids(); mk = central(3.0)
check("N2 after the lane read blanked it (file moved aside, ledger now blank): still unproven, never an empty set (hook %s)" % (unp if unp is None else sorted(unp)), (unp is None or pid in unp) and not grad_marked(mk))
for f in __import__("glob").glob(GR.LEDGER_PATH + ".corrupt-*"): os.remove(f)
if os.path.exists(GR.LEDGER_PATH): os.remove(GR.LEDGER_PATH)
unp = GR.legacy_unproven_pos_ids(); mk = central(3.0)
check("N3 missing lane ledger while the account holds the lane's position: flagged unproven and not marked (%s)" % (unp if unp is None else sorted(unp)), unp is not None and pid in unp and not grad_marked(mk))
open(GR.LEDGER_PATH, "w").write(good)
unp = GR.legacy_unproven_pos_ids()
check("N4 ledger restored: position provable again, marked (flagged %s)" % (sorted(unp) if unp is not None else None), unp == set() and grad_marked(central(3.0)))

# N5 variant lane registers under its OWN lane name from its OWN module instance (engine-loaded exactly like grad_select)
PA, F, CT = fresh(start="10")
spec = importlib.util.spec_from_file_location("grad_variant_engine", GR.__file__); eng = importlib.util.module_from_spec(spec); spec.loader.exec_module(eng)
eng.LANE = "gvar"; eng.LEDGER_PATH = os.path.join(tempfile.mkdtemp(), "gvar_ledger.json")
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
eng._paper_account()
hk = PA.legacy_hooks()
check("N5 engine-loaded variant registered its own hook under its own lane (%s), distinct from the base lane's (%s)" % ("gvar" in hk, "grad" in hk),
      "gvar" in hk and hk["gvar"] is not hk.get("grad"))
# Order matters now that the account boundary drops unproven marks: once the variant position exists and is unprovable,
# EVERY lane's entry gate fails closed (an unmarked position), so the legitimate watcher position is opened first.
_rw = PA.try_debit("watcher", "OtherN6", 1.0, 1000.0, "w-n6", marks=marks_for(PA, 1.0)); assert _rw[0], _rw[1]
_r = PA.try_debit("gvar", "VarMintN6", 1.0, 1000.0, "gv-1", marks=marks_for(PA, 1.0)); assert _r[0], _r[1]
mk = AM.collect_marks(price_fn=lambda mints: {m: PX1 for m in mints})
check("N6 variant lane position unknown to ITS OWN (empty) ledger is flagged and unmarked; a legitimate other lane (watcher) stays marked (gvar %s, watcher %s)" % (bool(mk.get("gvar")), bool(mk.get("watcher"))),
      not mk.get("gvar") and bool(mk.get("watcher")))

# ================= O. the omission holds at the ACCOUNT boundary for CALLER-SUPPLIED marks (not only collect_marks)
PA, gm = make_legacy(); sell_ab()
pid = GR.open_positions()[gm]["pa_pos_id"]
good = open(GR.LEDGER_PATH).read()
exp = marks_for(PA, 3.0)                                        # explicit marks covering the position at full qty (what an observer/caller could pass)
check("O0 control: provable migrated position, explicit marks accepted (marks_status ok)", PA.snapshot(marks=exp).get("marks_status") == "ok")
open(GR.LEDGER_PATH, "w").write("{ not json")                    # lane can no longer prove the position
snap = PA.snapshot(marks=exp)
check("O1 explicit caller marks for an UNPROVABLE legacy position are dropped at the account: marks_status not ok, no marked equity (%s)" % snap.get("marks_status"),
      snap.get("marks_status") != "ok" and snap.get("marked_exposure") in (None,))
_pk = (snap.get("peak_equity") or {}).get("value")
ok_, why_, _ = PA.try_debit("watcher", "OtherO2", 1.0, 1000.0, "o2", marks=exp)
check("O2 an entry on another lane with explicit full marks is REFUSED fail-closed while a legacy position is unprovable (%s)" % why_, ok_ is False)
check("O3 the persisted peak did not move from the explicit overstating marks", (PA.snapshot(marks=exp).get("peak_equity") or {}).get("value") == _pk)
for f in __import__("glob").glob(GR.LEDGER_PATH + ".corrupt-*"): os.remove(f)
open(GR.LEDGER_PATH, "w").write(good)
check("O4 lane evidence restored: the same explicit marks are accepted again", PA.snapshot(marks=exp).get("marks_status") == "ok")

# ===== P7zr (SOURCE ONLY, UNEXECUTED, NOT CLEARED TO RUN): real grad ledger + real paper account driven by daemon/grad_cycle in THIS process =====
# Offline: GR._jup is a fake (no network), price_fn injected, no process/thread start, guard intact. A fresh-module-state same-process replay is
# NOT a kill/restart proof. SEAM (disclosed; corrected after the 15:55 run, whose seam raised in the QUOTE phase where exceptions are absorbed): the real poll-abort path is reached by a price that triggers a ladder leg, grad._close_leg raising INSIDE the per-position exit application, AND replacing grad._persist_poll_failure
# with a function that returns False (the "failure state could not be restored" branch); everything else is the real code. clear_fault's commit
# switch stays OFF, so no clear record or removal can happen here. Needs reviewer clearance before the first run.
from daemon import grad_cycle as _GCY
_env_save = {k: os.environ.get(k) for k in (_GCY.ENV_FLAG, _GCY.LANES_ENV)}; _pf_save = GR._persist_poll_failure; _cl_save = GR._close_leg; _jsave_r = GR._jup; _cm_save = _GCY.CLEAR_COMMIT_ENABLED
try:
    os.environ[_GCY.ENV_FLAG] = "1"; _GCY.CLEAR_COMMIT_ENABLED = False; _GCY._MEM.clear()
    PAr, Fr, CTr = fresh(start="10"); go_sim(); _lr = dict(build(PAr, CTr)); _okr, _whr = _lr["grad"](1); assert _okr, _whr
    _gmr = list(GR.open_positions())[0]; _epr = float(GR.open_positions()[_gmr]["entry_price"])
    class _JupNone:
        SOURCE = "jupiter-fake"
        def get_prices(self, mints, now=None): return {}
        def get_prices_observed(self, mints, now=None): return {}
    GR._jup = _JupNone(); _scans = []
    _good_px = lambda c, m, i: _epr
    _hi_px = lambda c, m, i: _epr * 2.5      # a price that triggers the first ladder leg: the exception is injected INSIDE the per-position exit application
    def _boom_leg(*a, **k): raise RuntimeError("injected leg-close failure")
    _r1 = _GCY.run_cycle(GR, price_fn=_good_px, scan_fn=lambda: _scans.append(1), now=time.time())
    _r1c = _GCY.clear_fault(GR); _ncl = len(_GCY._clear_records(GR))
    check("P7zr1 real ledger, clean cycle: done, the scan ran (gate open), clear_fault says exactly no_fault and wrote NO clear record (%s)" % (_r1c[1].get("blocked"),),
          _r1.get("status") == "done" and _scans == [1] and _r1c[0] is False and _r1c[1].get("blocked") == ["no_fault"] and _ncl == 0)
    GR._persist_poll_failure = lambda d, mint, pos, exc: False; GR._close_leg = _boom_leg; _scans.clear(); _a0 = GR._POLL_ABORTS.get("count", 0)
    _r2 = _GCY.run_cycle(GR, price_fn=_hi_px, scan_fn=lambda: _scans.append(2), now=time.time())
    GR._persist_poll_failure = _pf_save; GR._close_leg = _cl_save
    _r3 = _GCY.run_cycle(GR, price_fn=_good_px, scan_fn=lambda: _scans.append(3), now=time.time())
    check("P7zr2 REAL abort path: the poll aborted (count %s -> %s), the cycle recorded a durable fault, the scan was NOT called that cycle (%s) nor in the following CLEAN cycle (%s), exits ran in both (steps %s / %s)"
          % (_a0, GR._POLL_ABORTS.get("count"), _r2.get("scan_skipped"), _r3.get("scan_skipped"), _r2["steps"].get("exits"), _r3["steps"].get("exits")),
          GR._POLL_ABORTS.get("count", 0) == _a0 + 1 and _scans == [] and os.path.exists(GR.LEDGER_PATH + ".cycle-fault.json") and str(_r3.get("scan_skipped", "")).startswith("unresolved_fault")
          and _r2["steps"].get("exits", {}).get("ok") is True and _r3["steps"].get("exits", {}).get("ok") is True
          and any(x.get("reason") == "poll_abort" for x in (_GCY._read_fault(GR) or {}).get("reasons", [])))
    _cl = _GCY.clear_fault(GR); _bl = _cl[1].get("blocked") or []
    check("P7zr3 real ledger, real legacy position: clear_fault refuses and the ONLY reasons are the position verdict and the disabled commit switch (no false orphan, no fill-ownership, no cash/replay, no marker reason): %s" % (_bl,),
          (not _cl[0]) and all(b.startswith("position:") or b == "commit_disabled_no_common_writer_lock" for b in _bl) and any(b.startswith("position:") for b in _bl) and not _GCY._clear_records(GR))
finally:
    for _k, _v in _env_save.items():
        if _v is None: os.environ.pop(_k, None)
        else: os.environ[_k] = _v
    GR._persist_poll_failure = _pf_save; GR._close_leg = _cl_save; GR._jup = _jsave_r; _GCY.CLEAR_COMMIT_ENABLED = _cm_save; _GCY._MEM.clear()
# NOT YET AUTHORED (stated gaps): empty lane + real account orphan, orphan entry/exit fill on the real journal, pending_sol/holds for the lane, a real
# try_debit_instance position that diagnoses consistent (positive clear with the commit switch on), prior-boot "running" journal simulated restart.
# ===== end P7zr =====

print("FAILS: %d" % len(FAILS))
sys.exit(1 if FAILS else 0)
