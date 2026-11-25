#!/usr/bin/env python3
"""bb68 LIVE-REFUSAL lane test (momentum), SOURCE ONLY, NOT RUN until cleared. ONE process. Real lane entry (real PA.try_debit, real binding), then a LABELLED injection on
the scratch account (wipe_all: a new account generation) in the SAME process, then a real close attempt (L.poll_exits with the canned feed doubling the price). Predeclared
(hand-written, UNVERIFIED): the close-time credit is REFUSED by the account with a generation refusal, no credit/write_down event is journaled for the old pos_id, the lane
sets _ENTRY_BLOCKED IN THE SAME PROCESS, the close intent is FROZEN durably (journal on), and a fresh entry attempt is NOT opened. Engineering canned fixtures only."""
if __name__ != "__main__":
    import pytest
    pytest.skip("script-style test", allow_module_level=True)
import sys, os
MODE = sys.argv[1] if len(sys.argv) > 1 else "parent"
import tempfile as _tf
ROOTDIR = _tf.mkdtemp(prefix="genbind-live-refusal-")
LANE_ARG = "momentum"
import logging as _logging, importlib, json as _json, signal as _signal, subprocess, time, hashlib, shutil
T0 = time.time(); BUDGET_S = 100.0
os.environ["FOREX_AGENT_HOME"] = os.path.join(ROOTDIR, "home")   # in-process: scratch home BEFORE any lane import   # child processes: scratch account home BEFORE any lane import reads it
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import tests._guard_fixture as G
G.block_network()
sys.path.insert(0, os.path.join(ROOT, "daemon", "launch_scanner"))
os.environ.pop("AGT_EXECUTION_MODE", None)
from daemon.launch_scanner import killswitch as KSM
_ks = os.path.join(ROOTDIR, "ks"); os.makedirs(_ks, exist_ok=True); KSM.STATE_DIR = _ks
for _n, _f in (("STATE_FILE", "state.json"), ("SHADOW_FILE", "shadow.json"), ("LOCK_FILE", "state.lock")):
    setattr(KSM, _n, os.path.join(_ks, _f))
import broker.memecoin.execution.mainnet_executor as _MX
for _nm in dir(_MX):
    _o = getattr(_MX, _nm)
    if isinstance(_o, type) and _o.__module__ == _MX.__name__ and "Executor" in _nm:
        def _mk(_c=_o):
            def _init(self, *a, **k): raise RuntimeError("live executor construction forbidden")
            return _init
        _o.__init__ = _mk()
import daemon.launch_scanner.close_intents as CI
if True:
    from daemon.launch_scanner import momentum_lane as L
    LANE_NAME = L.LANE
    assert LANE_NAME == "momentum", LANE_NAME
class _Safe:
    def check_token(self, chain, mint, timeout=10): return {"passed": True, "score": 80, "flags": [], "fails": [], "unknown": False}
class _Veto:
    def veto_check(self, mint, chain, symbol="", safety_report=None, run_bundle=False, lane="unknown"):
        return {"passed": True, "vetoes": [], "flags": [], "details": {}}
def _tape():
    n = time.time()
    return {"fetched_at": n, "received_at": n, "dex": "pump", "usd_liquidity": 100000.0, "usd_market_cap": 500000.0, "chg_m5": 10.0, "buys_m5": 100, "sells_m5": 40,
            "vol_m5": 9000.0, "buys_h1": 400, "sells_h1": 150, "price_usd": 0.001, "pair_created_ms": (time.time() - 3600) * 1000, "tape_source": "dexscreener"}
MINT = "EpochDiagMint1"; MINT2 = "EpochDiagMint2"
def _enter(MINT=MINT):
    d = {"pool_address": "PoolOther", "mint": MINT, "symbol": "RUN", "name": "RUN", "chain": "solana", "source": "gecko-trending", "lane": "lowcap", "url": "",
         "chg_24h_pct": 55.0, "chg_1h_pct": 8.0, "vol_24h_usd": 250000.0, "liq_usd": 100000.0, "usd_market_cap": 500000.0}
    return L.consider_candidate(d, time.time())
def fresh(start="10"):
    h = os.path.join(ROOTDIR, "home"); os.makedirs(h, exist_ok=True); os.environ["FOREX_AGENT_HOME"] = h
    for k in list(os.environ):
        if k.startswith("PAPER_ACCOUNT"): os.environ.pop(k)
    os.environ["PAPER_ACCOUNT_ENABLED"] = "1"; os.environ["PAPER_ACCOUNT_START"] = start
    import daemon.launch_scanner.paper_account as PA
    PA = importlib.reload(PA); G.install(PA)
    led = os.path.join(ROOTDIR, "led"); os.makedirs(led, exist_ok=True)
    L.LEDGER_PATH = os.path.join(led, LANE_ARG + ".json")
    L.ensure_ledger(); L.quote_price = G.stamped_quote(L, lambda chain, mint, spot_index=None: 0.001)
    L._fresh_tape = lambda chain, mint: _tape()
    L._safety = _Safe()
    if hasattr(L, "_veto"): L._veto = _Veto()
    L._ENTRY_BLOCKED = False; L._RECOVERY_DONE = False
    return PA
def cash(PA):
    """API cash, or None when the snapshot is None/degraded/has no numeric cash (never a crash: the unavailable state is itself evidence)."""
    try:
        sn = PA.snapshot()
        c = sn.get("cash") if isinstance(sn, dict) else None
        return float(c) if isinstance(c, (int, float)) and not isinstance(c, bool) else None
    except Exception:
        return None
def cash_str(c): return "%.6f" % c if c is not None else "None"

FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n, flush=True)
    if not c: FAILS.append(n)
PA = fresh(); cash(PA)
r = _enter(MINT)
pos = (L.open_positions() or {}).get(MINT) or {}
check("SETUP real entry opened with a pa_pos_id and a bound pa_account_id (opened %s, pos_id %s, bound %s)" % (bool(r[0]), pos.get("pa_pos_id"), bool(pos.get("pa_account_id"))), bool(r[0]) and bool(pos.get("pa_pos_id")) and bool(pos.get("pa_account_id")))
check("SETUP entries not blocked before the injection", not L._ENTRY_BLOCKED)
if FAILS: print("FAILS: %d" % len(FAILS)); sys.exit(1)
ad = os.path.join(ROOTDIR, "home", "paper_account")
for n in ("ledger.json", "ledger.json.bak", "events.jsonl"):
    if os.path.exists(os.path.join(ad, n)): os.remove(os.path.join(ad, n))
cash(PA)   # real load: a NEW account generation is created
def _pf(chain, mint, spot_index=None):
    p = (L.open_positions() or {}).get(mint) or {}
    return float(p.get("entry_price") or 0) * 2.0
L.poll_exits(price_fn=_pf)
evs = []
try:
    evs = [_json.loads(l) for l in open(os.path.join(ad, "events.jsonl")) if l.strip()]
except OSError: pass
old_pid = pos.get("pa_pos_id")
check("no credit or write_down journaled for the old pos_id on the new generation (%d)" % len([e for e in evs if e.get("event") in ("credit", "write_down") and e.get("pos_id") == old_pid]),
      not [e for e in evs if e.get("event") in ("credit", "write_down") and e.get("pos_id") == old_pid])
check("the account journaled a generation refusal", any(e.get("event") == "settle_generation_refused" for e in evs))
check("LIVE refusal blocked entries IN THIS PROCESS (_ENTRY_BLOCKED=%s)" % L._ENTRY_BLOCKED, bool(L._ENTRY_BLOCKED))
ents, st, _ = CI._read(CI.journal_path(L.LANE))
fro = [i for i, x in CI._state(ents).items() if x["intent"] is not None and not x["applied"] and x["frozen"] is not None]
check("the close intent is FROZEN durably (frozen intents %d, journal status %r)" % (len(fro), st), len(fro) >= 1)
e2 = _enter("EpochDiagNew")
check("a fresh entry attempt is NOT opened afterwards (%r)" % (repr(e2)[:160],), not e2[0])
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
