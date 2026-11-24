#!/usr/bin/env python3
"""INERT remaining grad fixtures (SOURCE-DRAFTED, NOT EXECUTED by the author). Harness copied from tests/test_grad_partial_exit_window.py
at 2b2671061bd2c55ef3d1ad09993307825716550b. TEST-FIXTURE inputs only, NOT owner limits.
F1 real TWAP entry through poll_exits (sim), F2 n=3 non-sim TWAP crash, F3 fee + non-round conservation (n=3 and n=7, sim and non-sim, write-down),
F4 mixed legacy/new entry qty. Expectations come from the account journal receipts, not from a price model, so they hold for the non-sim fill model.
Run (producer wrapper only): python3 tests/test_grad_fixtures_remaining.py
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
check("F4 mid-window: new position's account qty shrank to 25%% of tokens (%.4f vs %.4f); legacy account qty unchanged (%.4f, deferred as before); both marked OK" % (
      q_mid.get(pn["pa_pos_id"], -1), 0.25 * pn["tokens"], q_mid.get(pl["pa_pos_id"], -1)),
      abs(q_mid.get(pn["pa_pos_id"], -1) - 0.25 * pn["tokens"]) < 0.01 and abs(q_mid.get(pl["pa_pos_id"], -1) - q_by_pos[pl["pa_pos_id"]]) < 1e-9)
sn4 = PA.snapshot(marks=marks_for(PA, 3.0))
check("F4 marks accepted with a mixed population (status %s)" % sn4["marks_status"], sn4["marks_status"] == "ok")
print("INFO F4 mid-window marked_equity_conservative %.4f (legacy leg-sale proceeds are not yet credited: known deferred behavior for legacy only)" % sn4["marked_equity_conservative"])
FakeJup.mult = 2.0; GR.poll_exits(price_fn=lambda chain, mint, spot_index=None: float((GR.open_positions().get(mint) or {}).get("entry_price") or 0) * 2.0); FakeJup.mult = 1.0
check("F4 both closed, account has no grad position left, n_closes 2 (%s)" % lane_snap(PA).get("n_closes"),
      legacy not in GR.open_positions() and new not in GR.open_positions() and len(lane_snap(PA)["positions"]) == 0 and lane_snap(PA).get("n_closes") == 2)
check("F4 cash delta %.4f == 2 x independent total %.4f (cents), each position closed exactly once" % (cash(PA) - c0, 2 * TOTAL),
      _cents(cash(PA) - c0) == _cents(2 * TOTAL) and len([c for c in GR._read()["closes"] if c.get("pa_pos_id")]) == 2)

print("FAILS: %d" % len(FAILS))
sys.exit(1 if FAILS else 0)
