#!/usr/bin/env python3
"""POLL-LEVEL crash-recovery for watcher, momentum and mirror (same scenarios as smoke 1d/1e for ceo). Harness copied from the smoke. INERT all-lane DEMO smoke: watcher, momentum, ceo, mirror and challenge-trader entries contend for ONE shared simulated
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


# ---- 1f. POLL-LEVEL crash recovery for watcher, momentum and mirror through the REAL DemoFiller + REAL paper account + REAL close-intent journal (engineering test, canned FakeJup).
# INJECTED named fault at close_intents._append (labelled). A: sale fill lands, the durable intent write fails, the poll rolls back; restart at 1.9x (below the 2.0x trigger, above the 1.4x
# leg-c trail floor): the journaled sale is booked ONCE from the journal, no new sale, no new quote. B: gate fails BEFORE any sale, price recovers: nothing sold.
# R: restart at 1.0x: leg a booked once from the journal AND leg c sells as its OWN distinct fill on the trail.
import json as _json
import daemon.launch_scanner.close_intents as CI
_real_append = CI._append
def _no_intent_only(path, event): return False if event.get("ev") == "intent" else _real_append(path, event)
def _no_write(path, event): return False
def _journal(F):
    with open(F._jpath()) as fh: return _json.load(fh)["fills"]
def _cents(x): return int(round(float(x) * 100))
def pf_for_mult(mod, m):
    def pf(chain, mint, spot_index=None):
        pos = (mod.open_positions() or {}).get(mint) or {}
        return float(pos.get("entry_price") or 0) * m
    return pf
PREFIX = {"watcher": "watcher", "momentum": "momentum", "mirror": "degen_mirror"}   # REAL exit-op lane prefix per lane (mirror uses pos["lane"] == "degen_mirror", mirror.py _exit_op 1347)
def _sells(F, lane): return {k: v for k, v in _journal(F).items() if k.startswith(PREFIX[lane] + ":exit:") and v.get("status") == "filled"}
def _buys(F, mint): return [v for v in _journal(F).values() if v.get("side") == "buy" and v.get("mint") == mint and v.get("status") == "filled"]
def _open(name, mod):
    PA, F, CT = fresh(); lanes = dict(build(PA, CT)); ok, why = lanes[name](1)
    ps = mod.open_positions() or {}
    return PA, F, ok, why, ps
from unittest.mock import patch as _p
for name, mod in (("watcher", W), ("momentum", MO), ("mirror", MI)):
    PA, F, ok, why, ps = _open(name, mod)
    check("1f %s setup: a position is open on the real account (%s %s)" % (name, ok, str(why)[:40]), ok and len(ps) == 1)
    if len(ps) != 1: continue
    mint = next(iter(ps)); c0 = cash(PA); FakeJup.mult = 2.0
    check("1f %s: entry BUY fill is visible in the journal (%d) and the position's lane is %r (exit prefix %r)" % (name, len(_buys(F, mint)), ps[mint].get("lane"), PREFIX[name]),
          len(_buys(F, mint)) == 1 and (ps[mint].get("lane") or name) == PREFIX[name])
    with _p.object(CI, "_append", _no_intent_only): mod.poll_exits(price_fn=pf_for_mult(mod, 2.0))
    p = (mod.open_positions() or {}).get(mint) or {}; sl = _sells(F, name)
    check("1f %s A fault poll: sale FILL journaled (%d), leg a NOT booked (done %r), cash unchanged (%.4f vs %.4f), pending_exit why %r"
          % (name, len(sl), p.get("legs", {}).get("a", {}).get("done"), cash(PA), c0, (p.get("pending_exit") or {}).get("why")),
          len(sl) == 1 and p.get("legs", {}).get("a", {}).get("done") is False and _cents(cash(PA)) == _cents(c0) and (p.get("pending_exit") or {}).get("why") == "close_intent_not_durable")
    FakeJup.mult = 1.9; nq = JUP.calls
    mod.poll_exits(price_fn=pf_for_mult(mod, 1.9)); mod.poll_exits(price_fn=pf_for_mult(mod, 1.9))
    p = (mod.open_positions() or {}).get(mint) or {}; sl2 = _sells(F, name); net = sum(v["net_proceeds_usd"] for v in sl2.values())
    check("1f %s A resume at 1.9x: leg a booked from the JOURNALED fill (done %r), exactly one sale fill (%d), no new quote request (%d -> %d), cash +%.4f == journaled net %.4f, no pending_exit"
          % (name, p.get("legs", {}).get("a", {}).get("done"), len(sl2), nq, JUP.calls, cash(PA) - c0, net),
          p.get("legs", {}).get("a", {}).get("done") is True and len(sl2) == 1 and JUP.calls == nq and _cents(cash(PA) - c0) == _cents(net) and not p.get("pending_exit"))
    FakeJup.mult = 1.0
    # B: gate failure before any sale
    PA, F, ok, why, ps = _open(name, mod); mint = next(iter(ps)); c0 = cash(PA); FakeJup.mult = 2.0
    with _p.object(CI, "_append", _no_write):
        mod.poll_exits(price_fn=pf_for_mult(mod, 2.0)); mod.poll_exits(price_fn=pf_for_mult(mod, 2.0))
    p = (mod.open_positions() or {}).get(mint) or {}
    check("1f %s B gate failure before any sale: nothing sold (%d fills), leg a not done" % (name, len(_sells(F, name))), not _sells(F, name) and p.get("legs", {}).get("a", {}).get("done") is False)
    FakeJup.mult = 1.9; nq = JUP.calls
    mod.poll_exits(price_fn=pf_for_mult(mod, 1.9))
    check("1f %s B storage back, price 1.9x below the trigger: still NOTHING sold (%d fills, quote calls %d -> %d), cash unchanged (%.4f)" % (name, len(_sells(F, name)), nq, JUP.calls, cash(PA)),
          not _sells(F, name) and JUP.calls == nq and _cents(cash(PA)) == _cents(c0))
    # positive control for B (so "nothing sold" is not vacuous): with storage working, the SAME poll at 2.0x sells leg a under the exact prefix
    FakeJup.mult = 2.0; mod.poll_exits(price_fn=pf_for_mult(mod, 2.0)); FakeJup.mult = 1.0
    check("1f %s B control: with storage back and the price at 2.0x the real exit sells leg a under prefix %r (%d fill)" % (name, PREFIX[name], len(_sells(F, name))), len(_sells(F, name)) == 1)
    # R: restart at 1.0x after a leg-A fault: a booked once from the journal, c is its own distinct fill
    PA, F, ok, why, ps = _open(name, mod); mint = next(iter(ps)); c0 = cash(PA); FakeJup.mult = 2.0
    with _p.object(CI, "_append", _no_intent_only): mod.poll_exits(price_fn=pf_for_mult(mod, 2.0))
    FakeJup.mult = 1.0; mod.poll_exits(price_fn=pf_for_mult(mod, 1.0)); mod.poll_exits(price_fn=pf_for_mult(mod, 1.0))
    p = (mod.open_positions() or {}).get(mint) or {}; sl3 = _sells(F, name); got = sorted(k.split(":")[3] for k in sl3); net3 = sum(v["net_proceeds_usd"] for v in sl3.values())
    check("1f %s R reversal to 1.0x: fills by leg %r (want ['a', 'c'], each once), leg a done %r, cash +%.4f == sum of journaled nets %.4f"
          % (name, got, p.get("legs", {}).get("a", {}).get("done"), cash(PA) - c0, net3),
          got == ["a", "c"] and p.get("legs", {}).get("a", {}).get("done") is True and _cents(cash(PA) - c0) == _cents(net3))
check("no network attempt reached the transport (%s)" % G.NET_ATTEMPTS[:3], not G.NET_ATTEMPTS)
check("no live executor was constructed (%s)" % LIVE_BUILT, not LIVE_BUILT)
check("no process/thread start attempted", not STARTS)
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
