#!/usr/bin/env python3
"""bb81 WRITE-DOWN frozen-hold harness, MOMENTUM, SOURCE ONLY (never run). REAL momentum_lane._intent_gate and REAL close_intents journal on a scratch home; no mocks of the code under test, only a labelled mutant that hides the write-down family from frozen_for_op."""
if __name__ != "__main__":
    import pytest
    pytest.skip("script-style test", allow_module_level=True)
import sys, os
MODE = sys.argv[1] if len(sys.argv) > 1 else "parent"
import tempfile as _tf
ROOTDIR = sys.argv[2] if MODE != "parent" else _tf.mkdtemp(prefix="genbind-parent-unused-")
LANE_ARG = "momentum"
import logging as _logging, importlib, json as _json, signal as _signal, subprocess, time, hashlib, shutil
T0 = time.time(); BUDGET_S = 100.0
if MODE != "parent": os.environ["FOREX_AGENT_HOME"] = os.path.join(ROOTDIR, "home")   # child processes: scratch account home BEFORE any lane import reads it
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

import stat
def _jl(root):
    """raw journal lines of the scratch account: (events, bad_count, exists)"""
    p = os.path.join(root, "home", "paper_account", "events.jsonl"); evs = []; bad = 0
    try:
        with open(p, "rb") as f:
            for raw in f:
                s = raw.decode("utf-8", "replace").strip()
                if not s: continue
                try: evs.append(_json.loads(s))
                except ValueError: bad += 1
    except OSError: return [], 0, False
    return evs, bad, True
LANE_NAME = L.LANE
def _acct_dir(root): return os.path.join(root, "home", "paper_account")
def _nl(): 
    p = CI.journal_path(LANE_NAME)
    try:
        with open(p, "rb") as f: return len([x for x in f.read().splitlines() if x.strip()])
    except OSError: return 0
def child_wd(mutant=""):
    PA = fresh(); cash(PA)
    r = _enter(MINT); pos = (L.open_positions() or {}).get(MINT) or {}
    pid = pos.get("pa_pos_id"); out = {"entered": bool(r[0]), "pid": pid}
    wdop = "%s:writedown:%s:%s" % (LANE_NAME, MINT, pid)
    out["control_clean_gate"] = L._intent_gate(pos, "a") is True and not pos.get("pending_exit")
    i1 = CI.begin(LANE_NAME, MINT, pid, [{"kind": "write_down", "op_id": wdop, "mint": MINT, "pos_id": pid, "reason": "gen refused"}],
                  {"realized_usd": 0.0, "closed_reason": "x", "close_ts": time.time(), "leg_suffix": "writedown"})
    CI.freeze(LANE_NAME, i1, "gen refused")
    out["frozen_seen"] = [x[0] for x in (CI.frozen_for_op(LANE_NAME, MINT, pid, wdop) or [])] == [i1]
    if mutant == "nowd":
        _orig = CI.frozen_for_op
        CI.frozen_for_op = lambda lane, mint, pos_id, op_id: [] if ":writedown:" in str(op_id) else _orig(lane, mint, pos_id, op_id)
    n0 = _nl(); out["g_leg"] = L._intent_gate(pos, "a"); pe = dict(pos.get("pending_exit") or {})
    out["g_rem"] = L._intent_gate(pos, "remaining:abc")
    for _ in range(3): L._intent_gate(pos, "a")
    n1 = _nl(); out["lines"] = [n0, n1]; out["why"] = pe.get("why")
    out["detail_ids"] = [d.get("intent_id") for d in (((pos.get("pending_exit") or {}).get("detail") or {}).get("frozen_intents") or [])]
    out["i1"] = i1
    other = dict(pos, pa_pos_id="momentum/Other#1"); out["other_pos_not_held"] = L._intent_gate(other, "a") is True
    print("RESULT " + _json.dumps(out, default=str), flush=True)
if MODE == "wd": child_wd(sys.argv[3] if len(sys.argv) > 3 else ""); sys.exit(0)
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n, flush=True)
    if not c: FAILS.append(n)
def run(mutant=""):
    root = _tf.mkdtemp(prefix="lanepub-W-")
    p = subprocess.run([sys.executable, "-B", os.path.abspath(__file__), "wd", root] + ([mutant] if mutant else []), capture_output=True, text=True, timeout=60, env=dict(os.environ))
    l = [x for x in p.stdout.splitlines() if x.startswith("RESULT ")]
    if not l: print("FAIL SETUP wd child (rc %s, %s)" % (p.returncode, p.stderr[-300:])); raise SystemExit(2)
    return _json.loads(l[0][7:])
w = run()
check("W0 setup: real position entered, clean-journal gate allowed (positive control), a frozen write-down intent exists (%r)" % ({k: w.get(k) for k in ("entered", "control_clean_gate", "frozen_seen")},), w["entered"] and w["control_clean_gate"] is True and w["frozen_seen"] is True)
check("W1 a FROZEN write-down intent holds a leg close AND a remaining close of that position (%r)" % ({k: w[k] for k in ("g_leg", "g_rem")},), w["g_leg"] is False and w["g_rem"] is False)
check("W2 the hold names the real cause frozen_intent_unresolved and lists the frozen write-down intent id (%r, %r vs %r)" % (w["why"], w["detail_ids"], w["i1"]), w["why"] == "frozen_intent_unresolved" and w["detail_ids"] == [w["i1"]])
check("W3 BOUNDED JOURNAL: five held gate calls add NO journal line (%r)" % (w["lines"],), w["lines"][0] == w["lines"][1])
check("W4 independence: the same hold does not apply to a different position (%r)" % (w["other_pos_not_held"],), w["other_pos_not_held"] is True)
m = run("nowd")
check("WM MUTANT (write-down family hidden from the pre-check = BB80 behavior): the gate lets the close through and the journal grows (gate %r/%r, lines %r), so W1 and W3 would fail" % (m["g_leg"], m["g_rem"], m["lines"]), m["g_leg"] is True and m["lines"][1] > m["lines"][0])
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
