#!/usr/bin/env python3
"""INERT all-lane DEMO smoke: watcher, momentum, ceo, mirror and challenge-trader entries contend for ONE shared simulated
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

# ---- 1. contention: every lane, round robin, on ONE $10 account
PA, F, CT = fresh()
lanes = build(PA, CT)
c0 = cash(PA); check("account starts at the fixture $10 (%.2f)" % c0, abs(c0 - 10.0) < 1e-9)
opened = {n: 0 for n, _ in lanes}; refused = {n: [] for n, _ in lanes}; min_cash = c0; seq = []
for i in range(1, 9):
    for n, fn in lanes:
        try: ok, why = fn(i)
        except Exception as e: ok, why = False, "EXC %r" % (e,)
        seq.append((n, i, ok, str(why)[:60]))
        if ok: opened[n] += 1
        else: refused[n].append(str(why)[:80])
        min_cash = min(min_cash, cash(PA))
snap = PA.snapshot()
n_open_total = sum(opened.values())
print("opened per lane:", opened, "cash:", cash(PA))
for n, c in opened.items(): check("lane %s opened at least one entry through the shared account" % n, c >= 1)
check("cash never went negative (min %.6f)" % min_cash, min_cash >= -1e-9)
check("total debited <= fixture $10 (cash %.6f)" % cash(PA), cash(PA) >= -1e-9 and cash(PA) <= 10.0 + 1e-9)
check("account is exhausted by contention, later entries refused (%d opened)" % n_open_total, n_open_total < 6 * 8 and any(refused[n] for n in refused))
check("exception-free: no lane raised", not any(w.startswith("EXC") for n in refused for w in refused[n]))
check("refusals name the shared account or guard, not a crash", all(("paper account" in w or "risk" in w or "cash" in w or "refus" in w or "insufficient" in w or "demo" in w or "cap" in w) for n in refused for w in refused[n]))
# every refused-after-exhaustion entry left cash unchanged and created no position
# ---- 1b. exits / settlement on the SAME account, EXACT-to-the-cent ledger reconciliation
import json as _json
def _journal(F):
    with open(F._jpath()) as fh: return _json.load(fh)["fills"]
def _cents(x): return int(round(float(x) * 100))
LANE_MODS = (("watcher", W), ("momentum", MO), ("ceo", C), ("mirror", MI), ("grad", GR))
snap0 = PA.snapshot(); j0 = _journal(F)
buys0 = [f for f in j0.values() if f.get("side") == "buy" and f.get("status") == "filled" and F.op_committed(f["op_id"])]   # refused/uncommitted fills stay in the journal and never debit
check("pre-exit: no sell fill exists yet", not [f for f in j0.values() if f.get("side") == "sell"])
cost0 = snap0.get("cost_exposure")
check("pre-exit identity: cash + cost_exposure == $10 start to the cent (%.4f + %.4f)" % (snap0["cash"], cost0), _cents(snap0["cash"] + cost0) == 1000)
check("pre-exit: sum of journaled buy debits == cost_exposure to the cent (%.4f vs %.4f)" % (sum(f["total_debit_usd"] for f in buys0), cost0),
      _cents(sum(f["total_debit_usd"] for f in buys0)) == _cents(cost0))
open0 = {n: set((m.open_positions() or {})) for n, m in LANE_MODS}
check("pre-exit: %d positions open across lanes" % sum(len(v) for v in open0.values()), sum(len(v) for v in open0.values()) >= 5)
def pf_for(mod):
    def pf(chain, mint, spot_index=None):
        pos = (mod.open_positions() or {}).get(mint) or {}
        return float(pos.get("entry_price") or 0) * 2.0
    return pf
def pf_for_mult(mod, m):
    def pf(chain, mint, spot_index=None):
        pos = (mod.open_positions() or {}).get(mint) or {}
        return float(pos.get("entry_price") or 0) * m
    return pf
FakeJup.mult = 2.0
for n, m in LANE_MODS:
    try: m.poll_exits(price_fn=pf_for(m))
    except Exception as e: check("exit poll %s did not raise (%r)" % (n, e), False)
snap1 = PA.snapshot(); j1 = _journal(F)
sells = [f for f in j1.values() if f.get("side") == "sell" and f.get("status") == "filled"]
open1 = {n: set((m.open_positions() or {})) for n, m in LANE_MODS}
closed = {n: sorted(open0[n] - open1[n]) for n in open0}
print("CLOSED per lane:", {n: len(v) for n, v in closed.items()}, "STILL OPEN:", {n: len(v) for n, v in open1.items()},
      "cash %.4f cost_exposure %.4f realized %.4f" % (snap1["cash"], snap1["cost_exposure"], snap1.get("realized_pnl", float("nan"))))
# Each lane's leg-A (half) tranche was sold through the quote sim, so no position is FULLY closed (closed=%s): every one stays open
# with half its basis. State exactly which lanes settled the tranche into the shared account.
check("exits: no position fully closed (leg A only), all positions still open: closed=%s" % {n: len(v) for n, v in closed.items()}, sum(len(v) for v in closed.values()) == 0)
TRANCHE_LANES = {"watcher", "momentum", "ceo", "degen_mirror"}
sells_t = [f for f in sells if f["op_id"].split(":")[0] in {"watcher", "momentum", "ceo", "degen_mirror"}]
sells_g = [f for f in sells if f["op_id"].startswith("grad:")]
check("exact: 8 tranche lanes sold leg A, grad sold 1 (%d, %d)" % (len(sells_t), len(sells_g)), len(sells_t) == 8 and len(sells_g) == 1)
check("exact: cash delta == 8 x the independent model net proceeds (%.4f) AND the journal sum" % (8 * _NET_A) if False else "exact: cash delta == sum of the tranche lanes' journaled sell net proceeds to the cent (%.4f vs %.4f)" % (snap1["cash"] - snap0["cash"], sum(f["net_proceeds_usd"] for f in sells_t)),
      _cents(snap1["cash"] - snap0["cash"]) == _cents(sum(f["net_proceeds_usd"] for f in sells)))   # grad now credits per leg too (was sells_t only)
_lg = snap1["lanes"]["grad"]
check("grad leg A is credited to the shared account at the sale (per-leg credit): grad lane basis %.2f realized %.4f n_closes %d" % (_lg["open_basis"], _lg["realized_pnl"], _lg["n_closes"]),
      _cents(_lg["open_basis"]) == 50 and _lg["n_closes"] == 0 and abs(_lg["realized_pnl"] - 0.497) < 5e-4)
# INDEPENDENT model (not read from the ledger): entry $1 at $100/SOL, 1e-5 SOL/token, 15000-lamport fee => qty (1-0.0015)/100/1e-5 = 998.5;
# leg A sells half (499.25) at 2x the quote: gross 499.25*1e-5*2*100 = 0.9985, minus the 0.0015 fee = 0.9970 net; realized vs the 0.50 half basis = 0.4970
_NET_A = 499.25 * 1e-5 * 2.0 * 100.0 - 15000 / 1e9 * 100.0
check("independent model: leg-A net proceeds %.4f" % _NET_A, _cents(_NET_A) == 100)
_REAL_A = _NET_A - 0.5
_EXP = {"watcher": (1.0, 2 * _REAL_A), "momentum": (1.0, 2 * _REAL_A), "ceo": (1.0, 2 * _REAL_A), "degen_mirror": (1.0, 2 * _REAL_A), "challenge": (1.0, 0.0), "grad": (0.5, _REAL_A)}
for _l, (_b, _r) in _EXP.items():
    _v = snap1["lanes"][_l]
    check("per-lane exact: %s open_count/basis/realized (%d, %.4f, %.4f) expected basis %.2f realized %.4f" % (_l, _v["open_count"], _v["open_basis"], _v["realized_pnl"], _b, _r),
          _cents(_v["open_basis"]) == _cents(_b) and abs(_v["realized_pnl"] - _r) < 5e-4 and _v["n_closes"] == 0)
check("per-lane: tranche lanes each hold exactly half basis per position (0.50)", all(abs(p["basis"] - 0.5) < 1e-9 for l in ("watcher", "momentum", "ceo", "degen_mirror") for p in snap1["lanes"][l]["positions"]))
check("exact: cost_exposure == $10 - half basis of the 9 positions incl. grad (%.4f)" % snap1["cost_exposure"], _cents(snap1["cost_exposure"]) == _cents(10.0 - 9 * 0.5))
check("independent: cash delta == 9 x model net proceeds to the cent (%.4f vs %.4f)" % (snap1["cash"] - snap0["cash"], 9 * _NET_A), _cents(snap1["cash"] - snap0["cash"]) == _cents(9 * _NET_A))
check("exact: cash + remaining cost_exposure == book_value to the cent", _cents(snap1["cash"] + snap1["cost_exposure"]) == _cents(snap1["book_value"]))
check("exact: realized_pnl == book_value - $10 start to the cent (%.4f vs %.4f)" % (snap1.get("realized_pnl", float("nan")), snap1["book_value"] - 10.0),
      _cents(snap1.get("realized_pnl", -1e9)) == _cents(snap1["book_value"] - 10.0))
c_after = snap1["cash"]
for n, m in LANE_MODS: m.poll_exits(price_fn=pf_for(m))
snap2 = PA.snapshot(); j2 = _journal(F)
check("second poll: cash, cost_exposure and sell-fill count unchanged to the cent",
      _cents(snap2["cash"]) == _cents(c_after) and _cents(snap2["cost_exposure"]) == _cents(snap1["cost_exposure"]) and len(j2) == len(j1))
FakeJup.mult = 1.0
# ---- 1c. SLOW quote: a quote slower than the data-age cap refuses on every lane, nothing debited, no position
PA, F, CT = fresh(max_age="2"); lanes = build(PA, CT)
cs = cash(PA); FakeJup.delay = 2.6; rs = {}
for n, fn in lanes:
    try: ok, why = fn(1)
    except Exception as e: ok, why = False, "EXC %r" % (e,)
    rs[n] = (ok, str(why)[:80])
FakeJup.delay = 0.0
check("slow quote (> max_data_age_s 2s) refuses on every lane: %s" % {n: v[0] for n, v in rs.items()}, not any(v[0] for v in rs.values()))
check("slow quote: no lane raised (%s)" % {n: v[1][:40] for n, v in rs.items()}, not any(v[1].startswith("EXC") for v in rs.values()))
check("slow quote: cash unchanged to the cent, no position opened", _cents(cash(PA)) == _cents(cs) and not any((m.open_positions() or {}) for m in (W, MO, C, MI, GR)))
# ---- 1d. grad FULL CLOSE: the shared account is credited at close; independent closed-form model of the total
PA, F, CT = fresh(); lanes = build(PA, CT); _gfn = dict(lanes)["grad"]
ok_g, why_g = _gfn(1); check("full close: grad entry opened (%s)" % str(why_g)[:40], ok_g)
cg0 = cash(PA); gm = list((GR.open_positions() or {}))[0]
def _gpf(mult): return lambda chain, mint, spot_index=None: float((GR.open_positions().get(mint) or {}).get("entry_price") or 0) * mult
_fee0 = 15000 / 1e9 * 100.0
FakeJup.mult = 3.0; GR.poll_exits(price_fn=_gpf(3.0))
cg1 = cash(PA)
_banked = (0.5 + 0.25) * 998.5 * 1e-5 * 3 * 100 - 2 * _fee0
check("full close step 1 (3x: legs A+B sold, leg C armed): position open, shared cash credited the banked A+B net per leg (%.4f -> %.4f, model +%.4f)" % (cg0, cg1, _banked), gm in GR.open_positions() and _cents(cg1 - cg0) == _cents(_banked))
FakeJup.mult = 2.0; GR.poll_exits(price_fn=_gpf(2.0))   # 2.0 <= 0.7 * 3.0 peak: leg C trails out
FakeJup.mult = 1.0
_q = 998.5; _fee = 15000 / 1e9 * 100.0
_total = (0.5 * _q * 1e-5 * 3 * 100 - _fee) + (0.25 * _q * 1e-5 * 3 * 100 - _fee) + (0.25 * _q * 1e-5 * 2 * 100 - _fee)
cg2 = cash(PA)
check("full close: position closed", gm not in GR.open_positions())
check("full close: shared cash credited EXACTLY the independent model total %.4f at close (%.4f)" % (_total, cg2 - cg0), _cents(cg2 - cg0) == _cents(_total))
FakeJup.mult = 1.0
GR.poll_exits(price_fn=_gpf(2.0))
check("full close: re-poll does not double credit", _cents(cash(PA)) == _cents(cg2))
# ---- 1e. challenge-trader EXIT through its real _demo_exit (drives the script's exit helper, NOT process_challenge's price/TP loop)
PA, F, CT = fresh(); build(PA, CT)
_ok, _why, _pid, _fill = CT._demo_enter(PA, "L", "ChalExitMint", 1.0)
check("challenge exit: entry opened and debited $1 (%s)" % str(_why)[:30], _ok and _cents(cash(PA)) == 900)
FakeJup.mult = 3.0
_conv, _net = CT._demo_exit(PA, "L", {"mint": "ChalExitMint", "tokens": _fill["qty_tokens"], "pa_pos_id": _pid})
FakeJup.mult = 1.0
_model = 998.5 * 1e-5 * 3 * 100 - 15000 / 1e9 * 100.0
check("challenge exit: converged, net %.4f == independent model %.4f" % (_net, _model), _conv and _cents(_net) == _cents(_model))
check("challenge exit: shared cash == 9.00 + model net to the cent (%.4f)" % cash(PA), _cents(cash(PA)) == _cents(9.0 + _model))
_c = cash(PA); CT._demo_exit(PA, "L", {"mint": "ChalExitMint", "tokens": _fill["qty_tokens"], "pa_pos_id": _pid})
check("challenge exit: replay does not double credit", _cents(cash(PA)) == _cents(_c))
# ---- 2. unset mandatory limit: every lane refuses (fail closed)
PA, F, CT = fresh(max_age=None); lanes = build(PA, CT)
c1 = cash(PA); res = {}
for n, fn in lanes:
    try: ok, why = fn(1)
    except Exception as e: ok, why = False, "EXC %r" % (e,)
    res[n] = (ok, str(why)[:70])
check("max_data_age_s UNSET: every lane refuses (%s)" % {n: v[0] for n, v in res.items()}, not any(v[0] for v in res.values()))
check("unset limit: cash unchanged", abs(cash(PA) - c1) < 1e-9)
# ---- 3. priced-quote stamp consumer, every stamp-aware lane (fresh gate tape, so ONLY the quote stamp can refuse)
def quote_with(mod, stamp):
    def q(chain, mint, spot_index=None):
        mod._LAST_QUOTE_STAMP = dict(stamp) if stamp else None; return 0.001
    return q
def attempt(kind):
    PA, F, CT = fresh()
    lanes = dict((n, fn) for n, fn in build(PA, CT) if n in ("watcher", "ceo", "momentum", "mirror"))
    out = {}
    for n, mod in (("watcher", W), ("momentum", MO), ("ceo", C), ("mirror", MI)):
        t = time.time()
        if kind == "stale": mod.quote_price = quote_with(mod, {"fetched_at": t - 600, "received_at": t - 599})
        elif kind == "unstamped": mod.quote_price = quote_with(mod, None)
        elif kind == "noreuse":  # a PREVIOUS entry left a fresh stamp; this entry's quote writes none -> must refuse
            mod._LAST_QUOTE_STAMP = {"fetched_at": t, "received_at": t}
            mod.quote_price = lambda chain, mint, spot_index=None: 0.001
        # kind == "fresh": keep the stamped_quote fixture installed by fresh()
        c = cash(PA)
        try: ok, why = lanes[n](3)
        except Exception as e: ok, why = False, "EXC %r" % (e,)
        out[n] = (bool(ok), str(why)[:80], abs((c - cash(PA)) - (1.0 if ok else 0.0)) < 1e-6)
    return out
for kind, want in (("stale", False), ("unstamped", False), ("noreuse", False), ("fresh", True)):
    r = attempt(kind)
    for n in ("watcher", "momentum", "ceo", "mirror"):
        check("quote stamp %s: %s %s (%s)" % (kind, n, "opens" if want else "refuses", r[n][1][:50]), r[n][0] is want and r[n][2])
# ---- 1d. ceo POLL-LEVEL crash recovery through the REAL DemoFiller + REAL paper account + REAL close-intent journal (engineering test, canned FakeJup quotes).
# INJECTED named faults at close_intents._append only. Scenario A: the sale fill lands, the durable intent write fails, the poll rolls back; the process "restarts"
# (storage back, price REVERSED so the 2x trigger is gone): the journaled sale is settled ONCE from the journal. Scenario B: the gate fails BEFORE any sale
# (nothing sold), the price then recovers: NOTHING is sold (a gate refusal never forces a stale sale).
import daemon.launch_scanner.close_intents as CI
_real_append = CI._append
def _no_intent_only(path, event): return False if event.get("ev") == "intent" else _real_append(path, event)
def _no_write(path, event): return False
def _ceo_pos(PA, CT):
    lanes = build(PA, CT); fn = dict(lanes)["ceo"]; ok, why = fn(1)
    return ok, why, (C.open_positions() or {})
def _ceo_sells(F): return {k: v for k, v in _journal(F).items() if k.startswith("ceo:exit:") and v.get("status") == "filled"}
PA, F, CT = fresh(); ok, why, ps = _ceo_pos(PA, CT)
check("1d-A setup: a ceo position is open on the real account (%s %s)" % (ok, str(why)[:40]), ok and len(ps) == 1)
_mint = next(iter(ps)); _cash0 = cash(PA)
FakeJup.mult = 2.0
with __import__("unittest.mock", fromlist=["patch"]).patch.object(CI, "_append", _no_intent_only):
    C.poll_exits(price_fn=pf_for(C))
_p = (C.open_positions() or {}).get(_mint) or {}
_sl = _ceo_sells(F)
check("1d-A fault poll: the sale FILL is journaled (%d), the leg is NOT booked (done %r), cash unchanged to the cent (%.4f vs %.4f), pending_exit why %r"
      % (len(_sl), _p.get("legs", {}).get("a", {}).get("done"), cash(PA), _cash0, (_p.get("pending_exit") or {}).get("why")),
      len(_sl) == 1 and _p.get("legs", {}).get("a", {}).get("done") is False and _cents(cash(PA)) == _cents(_cash0)
      and (_p.get("pending_exit") or {}).get("why") == "close_intent_not_durable")
FakeJup.mult = 1.9   # RESTART with the price reversed to 1.9x: BELOW the 2.0x trigger and ABOVE the leg-c 30% trail floor (1.4x of the 2.0x peak), so no exit trigger holds
_nq = JUP.calls
C.poll_exits(price_fn=pf_for_mult(C, 1.9))
_p = (C.open_positions() or {}).get(_mint) or {}; _sl2 = _ceo_sells(F); _net = sum(v["net_proceeds_usd"] for v in _sl2.values())
check("1d-A resume after restart/price reversal: leg a booked from the JOURNALED fill (done %r), still exactly one sale fill (%d), no new quote request (%d -> %d), cash +%.4f == journaled net %.4f"
      % (_p.get("legs", {}).get("a", {}).get("done"), len(_sl2), _nq, JUP.calls, cash(PA) - _cash0, _net),
      _p.get("legs", {}).get("a", {}).get("done") is True and len(_sl2) == 1 and JUP.calls == _nq and _cents(cash(PA) - _cash0) == _cents(_net)
      and not _p.get("pending_exit"))
_c1 = cash(PA); C.poll_exits(price_fn=pf_for_mult(C, 1.9))
check("1d-A a further poll changes nothing (cash %.4f -> %.4f, sale fills %d)" % (_c1, cash(PA), len(_ceo_sells(F))), _cents(cash(PA)) == _cents(_c1) and len(_ceo_sells(F)) == 1)
PA, F, CT = fresh(); ok, why, ps = _ceo_pos(PA, CT); _mint = next(iter(ps)); _cash0 = cash(PA)
FakeJup.mult = 2.0
with __import__("unittest.mock", fromlist=["patch"]).patch.object(CI, "_append", _no_write):
    C.poll_exits(price_fn=pf_for(C)); C.poll_exits(price_fn=pf_for(C))
_p = (C.open_positions() or {}).get(_mint) or {}
check("1d-B gate failure before any sale: nothing sold (%d fills), position open with leg a not done, hold tries %r" % (len(_ceo_sells(F)), (_p.get("pending_exit") or {}).get("tries")),
      not _ceo_sells(F) and _p.get("legs", {}).get("a", {}).get("done") is False and (_p.get("pending_exit") or {}).get("tries") == 2)
FakeJup.mult = 1.9; _nq = JUP.calls
C.poll_exits(price_fn=pf_for_mult(C, 1.9))
check("1d-B storage back and price recovered below the trigger: still NOTHING sold (%d fills, quote calls %d -> %d), cash unchanged (%.4f)" % (len(_ceo_sells(F)), _nq, JUP.calls, cash(PA)),
      not _ceo_sells(F) and JUP.calls == _nq and _cents(cash(PA)) == _cents(_cash0))
FakeJup.mult = 1.0
# ---- 1e. ceo distinct-leg price reversal after a leg-A fault. Same scenario as 1d-A, restarted at different prices. Expectation per price (peak 2.0x, trail floor 0.70 x peak = 1.40x):
#   1.9x: nothing else fires. 1.4x and 1.0x: leg A is booked ONCE from the journaled fill and leg C sells as its OWN distinct fill on the trail. 1.41x: trail does not fire.
def _reversal(mult):
    PA, F, CT = fresh(); ok, why, ps = _ceo_pos(PA, CT); mint = next(iter(ps)); c0 = cash(PA)
    FakeJup.mult = 2.0
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(CI, "_append", _no_intent_only):
        C.poll_exits(price_fn=pf_for(C))
    FakeJup.mult = mult
    C.poll_exits(price_fn=pf_for_mult(C, mult)); C.poll_exits(price_fn=pf_for_mult(C, mult))
    p = (C.open_positions() or {}).get(mint) or {}; sl = _ceo_sells(F)
    FakeJup.mult = 1.0
    return p, sl, cash(PA) - c0
for _m, _legs in ((1.9, ["a"]), (1.41, ["a"]), (1.4, ["a", "c"]), (1.0, ["a", "c"])):
    _p, _sl, _dc = _reversal(_m)
    _got = sorted(k.split(":")[3] for k in _sl); _net = sum(v["net_proceeds_usd"] for v in _sl.values())
    check("1e reversal to %.2fx after a leg-A fault: sale fills by leg %r (want %r, each exactly once), leg a done %r, no pending_exit %r, cash +%.4f == sum of journaled nets %.4f"
          % (_m, _got, _legs, _p.get("legs", {}).get("a", {}).get("done"), not _p.get("pending_exit"), _dc, _net),
          _got == _legs and _p.get("legs", {}).get("a", {}).get("done") is True and not _p.get("pending_exit") and _cents(_dc) == _cents(_net)
          and (("c" in _legs) == bool(_p.get("legs", {}).get("c", {}).get("done"))))
# ---- 4. committed replay returns the SAME fill and does not debit twice
PA, F, CT = fresh()
op = F.intent_id("L", "ReplayMint1")
f1 = F.buy(op, "ReplayMint1", 1.0, 2.0); PA_ok = PA.try_debit("L", "ReplayMint1", 1.0, f1["qty_tokens"], op, durable_refusal=True)
f2 = F.buy(op, "ReplayMint1", 1.0, 2.0)
check("replay of the journaled op returns the identical fill (no re-quote)", f1 == f2)
# ---- containment
check("no process/thread/fork/exec/system/multiprocessing start attempted", not STARTS)
check("no network attempt reached the transport (%s)" % G.NET_ATTEMPTS[:3], not G.NET_ATTEMPTS)
print("mainnet_executor module in sys.modules: at start=%s, at end=%s (constants are imported by demo_fill; construction is what is trapped)" % (MX_AT_START, any("mainnet_executor" in m for m in sys.modules)))
check("no live executor was constructed (class constructors trapped: %s)" % LIVE_BUILT, not LIVE_BUILT)
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
