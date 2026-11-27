#!/usr/bin/env python3
"""GRAD GENERATION-KEY characterization (reviewer finding: no-account demo mode leaves pa_pos_id unset, so _fill_key is suffix-less and a same-mint RE-ENTRY replays the earlier generation's sale fill). Harness copied from the smoke. INERT all-lane DEMO smoke: watcher, momentum, ceo, mirror and challenge-trader entries contend for ONE shared simulated
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

import json as _json
GR.MAX_CLOSES = 1   # real eviction path: one newer close evicts the older mint from d["closes"], so the "one entry per mint ever" guard no longer blocks a re-entry (TEST-FIXTURE bound; production value 500)
def _cand(mint):
    return {"mint": mint, "symbol": "TKN", "name": "TKN / SOL", "chain": "solana", "dex": "pumpswap", "created_ts": time.time() - 60.0,
            "pool_address": "POOL" + mint, "reserve_usd": 6000.0, "fdv_usd": 69000.0, "price_usd": 0.00007, "source": "gecko-new-pool"}
def _jr(F):
    with open(F._jpath()) as fh: return _json.load(fh)["fills"]
def _close_all(mint, mult):
    FakeJup.mult = mult
    with GR._ledger_lock():
        d = GR._read(); pos = d["positions"][mint]; px = pos["entry_price"] * mult
        for k in ("a", "b", "c"): GR._close_leg(pos, k, px, "test")
        GR._finalize(d, mint, pos, "test"); GR._write(d)
    FakeJup.mult = 1.0
def scenario(label, account_on):
    PA, F, CT = fresh()
    if not account_on: os.environ.pop("PAPER_ACCOUNT_ENABLED", None)
    A = "GenA%spump" % label; B = "GenB%spump" % label
    ok1, w1 = GR.consider_candidate(_cand(A))[:2]
    p1 = (GR._read()["positions"].get(A) or {})
    if not account_on:
        # NO-ACCOUNT DEMO MODE IS UNIMPLEMENTED BY DESIGN (not a regression): the mandatory entry guard (entry_guard.mandatory_entry_refusal)
        # refuses every entry unless the shared paper account is enabled, and its contract states there is NO acknowledgement, config key or env
        # override that skips the check. The intended demo always runs on the one shared simulated $10 balance. So the entry is correctly REFUSED
        # here, no position exists, and the fill-key replay the detector below hunts for is UNREACHABLE while the guard stands (no entry -> no
        # fill). The close / re-entry / detector steps are skipped for this mode; the detector still runs for the acct control below.
        check("%s: no-account entry is REFUSED by the mandatory entry guard (%s %s); no position, no marker, no pa_pos_id" % (label, ok1, str(w1)[:60]),
              bool(ok1) is False and "mandatory entry guard" in str(w1) and not p1)
        return None
    check("%s: generation 1 of the mint opens (%s %s); marker %r, pa_pos_id %r" % (label, ok1, str(w1)[:30], p1.get("tranche_credit"), bool(p1.get("pa_pos_id"))), bool(ok1) and p1.get("tranche_credit") is True and bool(p1.get("pa_pos_id")) == account_on)
    _close_all(A, 2.0)
    okb, wb = GR.consider_candidate(_cand(B))[:2]; _close_all(B, 2.0)
    ok2, w2 = GR.consider_candidate(_cand(A))[:2]
    p2 = (GR._read()["positions"].get(A) or {})
    check("%s: the SAME mint re-enters as generation 2 after eviction from closes (%s %s)" % (label, ok2, str(w2)[:40]), bool(ok2) and bool(p2))
    n_before = len([k for k in _jr(F) if k.startswith("grad:exit:%s:a" % A)])
    FakeJup.mult = 3.0
    with GR._ledger_lock():
        d = GR._read(); pos = d["positions"][A]; GR._close_leg(pos, "a", pos["entry_price"] * 3.0, "test"); GR._write(d)
    FakeJup.mult = 1.0
    fills = {k: v for k, v in _jr(F).items() if k.startswith("grad:exit:%s:a" % A) and v.get("status") == "filled"}
    nets = sorted(round(float(v["net_proceeds_usd"]), 6) for v in fills.values())
    gen2_leg = (GR._read()["positions"].get(A) or {}).get("legs", {}).get("a", {})
    check("%s: DEFECT DETECTOR: generation 2 leg-a sale is its OWN fill, not a replay of generation 1 (leg-a fills before %d, after %d, keys %r, nets %r)"
          % (label, n_before, len(fills), sorted(k[-40:] for k in fills), nets), n_before == 1 and len(fills) == 2 and len(set(nets)) == 2)
    return fills
scenario("acct", True)    # control: with the account, pa_pos_id scopes the key (expected to pass today)
scenario("noacct", False) # no-account demo mode: the entry is refused by the mandatory entry guard by design (no override exists); the fill-key replay is unreachable while the guard stands
check("no process/thread/fork/exec/system/multiprocessing start attempted", not STARTS)
check("no network attempt reached the transport (%s)" % G.NET_ATTEMPTS[:3], not G.NET_ATTEMPTS)
check("no live executor was constructed (%s)" % LIVE_BUILT, not LIVE_BUILT)
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
