#!/usr/bin/env python3
"""PRE-APPLY GATE regression for the MOMENTUM lane (bb58). ASSERTING. Scope: MOMENTUM ONLY; watcher, CEO, mirror (separately tested) and grad are NOT covered by this file. Port of tests/test_mirror_gate_v2.py (same scenarios G0-G30, same INSTRUMENT vs BEHAVIOR split); ENGINEERING TEST of the REAL momentum_lane, REAL shared paper account and REAL close-intent journal in a scratch root, canned price feed, real SIGKILL of the child process. Journal/ledger edits are LABELLED scratch-root injections; G26-G28 drive the REAL write_down_zero writer (_close_leg -> _pa_write_down). Expectations are hand predictions (UNVERIFIED): pre-gate bb56 source red on identity/hold/kind cases, green on controls. NOT run.
Run: python3 -B tests/test_momentum_gate.py"""
if __name__ != "__main__":
    import pytest
    pytest.skip("script-style test", allow_module_level=True)
import sys, os
MODE = sys.argv[1] if len(sys.argv) > 1 else "parent"
import tempfile as _tf
ROOTDIR = sys.argv[2] if MODE != "parent" else _tf.mkdtemp(prefix="otherlane-parent-unused-")
LANE_ARG = sys.argv[3] if MODE != "parent" else None
import logging as _logging, importlib, json as _json, signal as _signal, subprocess, time
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import tests._guard_fixture as G
G.block_network()   # BEFORE any repo/lane import
sys.path.insert(0, os.path.join(ROOT, "daemon", "launch_scanner"))
os.environ.pop("AGT_EXECUTION_MODE", None)   # modelled-fill (non-demo) mode, test processes only

from daemon.launch_scanner import killswitch as KSM
_ks = os.path.join(ROOTDIR, "ks"); os.makedirs(_ks, exist_ok=True); KSM.STATE_DIR = _ks
for _n, _f in (("STATE_FILE", "state.json"), ("SHADOW_FILE", "shadow.json"), ("LOCK_FILE", "state.lock")):
    setattr(KSM, _n, os.path.join(_ks, _f))
import broker.memecoin.execution.mainnet_executor as _MX   # constants only; CONSTRUCTION is forbidden
for _nm in dir(_MX):
    _o = getattr(_MX, _nm)
    if isinstance(_o, type) and _o.__module__ == _MX.__name__ and "Executor" in _nm:
        def _mk(_c=_o):
            def _init(self, *a, **k): raise RuntimeError("live executor construction forbidden")
            return _init
        _o.__init__ = _mk()
from daemon.launch_scanner import signal_bus as BUS
import daemon.launch_scanner.close_intents as CI
LANES = ("momentum",)
if LANE_ARG:
    from daemon.launch_scanner import momentum_lane as _Mo
    L = {"momentum": _Mo}[LANE_ARG]
    LANE_NAME = L.LANE   # FINAL runtime lane (the module assigns LANE twice at import)
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
MINT = os.environ.get("GATE_MINT", "OtherLaneMint1")
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
    L.LEDGER_PATH = os.path.join(led, LANE_ARG + ".json"); L.ensure_ledger()
    L._fresh_tape = lambda chain, mint: _tape()
    L.quote_price = G.stamped_quote(L, lambda chain, mint, spot_index=None: 0.001)
    L._safety = _Safe()
    if hasattr(L, "_veto"): L._veto = _Veto()
    L._ENTRY_BLOCKED = False; L._RECOVERY_DONE = False
    return PA
def cash(PA): return PA.snapshot()["cash"]
def pf_mult(m):
    def pf(chain, mint, spot_index=None):
        pos = (L.open_positions() or {}).get(mint) or {}
        return float(pos.get("entry_price") or 0) * m
    return pf
def _cents(x): return int(round(float(x) * 100))
def _die():
    print("KILLHIT lane=%s at mark_account_applied" % LANE_ARG, flush=True)   # hit counter: the lane reached the injected point
    os.kill(os.getpid(), _signal.SIGKILL)


HITS = {"n": 0}; LOGS = []
class _H(_logging.Handler):
    def emit(self, rec):
        try: LOGS.append(rec.getMessage())
        except Exception: pass
def _frozen_open_n():
    ents, st, _t = CI._read(CI.journal_path(LANE_NAME))
    if st == "torn": return 0
    succ = {e.get("supersedes") for e in ents if e.get("ev") == "intent" and e.get("supersedes")}
    return len([i for i, x in CI._state(ents).items() if x["intent"] is not None and not x["applied"] and x["frozen"] is not None and i not in succ])
def _credit_events(PA, op_id):
    ents = PA._journal_read()[0]
    return len([e for e in ents if e.get("op_id") == op_id and e.get("event") in ("credit", "credit_orphan")]), len([e for e in ents if e.get("op_id") == op_id and e.get("event") == "credit"])
def _acct_qty(PA):
    return [p.get("qty") for p in ((PA.snapshot() or {}).get("lanes", {}).get(LANE_NAME, {}).get("positions") or []) if p.get("mint") == MINT]

def _jpath(): return CI.journal_path(LANE_NAME)
def _jread_lines():
    with open(_jpath()) as f: return [_json.loads(x) for x in f if x.strip()]
def _jwrite_lines(evs):
    with open(_jpath(), "w") as f:
        for e in evs: f.write(_json.dumps(e) + "\n")
def jedit(mode):
    """LABELLED injection: edit the durable journal in the SCRATCH root only."""
    evs = _jread_lines()
    it = [e for e in evs if e.get("ev") == "intent"][0]
    op = it["settle_ops"][0]; pid = it["pos_id"]; mint = it["mint"]
    wdid = "%s:writedown:%s:%s" % (it["lane"], mint, pid)
    if mode == "missing_pos_id": it["pos_id"] = None
    elif mode == "missing_mint": it["mint"] = None
    elif mode == "opid_mismatch": op["op_id"] = op["op_id"] + "X"
    elif mode == "op_mint_mismatch": op["mint"] = "SomeOtherMint"
    elif mode == "op_pid_mismatch": op["pos_id"] = "%s/%s#999" % (LANE_NAME, mint)
    elif mode == "pid_badprefix": it["pos_id"] = "x#1"; op["pos_id"] = "x#1"; op["op_id"] = "%s:%s:%s:x#1" % (it["lane"], it["close_info"]["leg_suffix"], mint)
    elif mode == "unknown_kind": op["kind"] = "debit"
    elif mode == "missing_kind": op.pop("kind", None)
    elif mode == "wd_noreason": it["settle_ops"] = [{"kind": "write_down", "op_id": wdid, "mint": mint, "pos_id": pid}]
    elif mode == "wd_valid": it["settle_ops"] = [{"kind": "write_down", "op_id": wdid, "mint": mint, "pos_id": pid, "reason": "gate_test_recorded_reason", "expected_account_id": op.get("expected_account_id"), "entry_op_id": op.get("entry_op_id")}]; it["close_info"]["leg_suffix"] = "writedown"   # a VALID write_down carries the generation binding the real _pa_write_down writer records; without it the gate (by design) holds the op as unbound
    elif mode == "multiop_bad": it["settle_ops"].append({"kind": "write_down", "op_id": wdid, "mint": mint, "pos_id": pid})
    elif mode == "wd_foreign_key": op["amount"] = 5.0   # real write_down record plus a field its writer never records
    elif mode == "wd_real_noreason": op.pop("reason", None)   # real write_down record with the reason removed (legacy/missing field)
    elif mode == "credit_foreign_key": op["rogue"] = 1
    elif mode == "multiop_unknown": it["settle_ops"].append({"kind": "debit", "op_id": wdid, "mint": mint, "pos_id": pid})
    elif mode == "marker_noop": evs.append({"ev": "account_applied", "intent_id": it["intent_id"], "ts": time.time()})
    elif mode == "terminal":
        for k in ("account_applied", "lane_applied", "applied"): evs.append({"ev": k, "intent_id": it["intent_id"], "ts": time.time()})
    _jwrite_lines(evs)
def ledit(mode, pid_of_first=None):
    """LABELLED injection: edit the lane ledger in the SCRATCH root only."""
    if mode == "corrupt":
        with open(L.LEDGER_PATH, "w") as f: f.write("{not json")
        return
    if mode == "missing":
        os.remove(L.LEDGER_PATH); return
    d = L._read(); pos = d["positions"].get(MINT)
    if mode == "empty": d = {"positions": {}, "closes": []}
    elif mode == "absent":
        d["positions"].pop(MINT, None); d["closes"] = [c for c in d.get("closes", []) if c.get("mint") != MINT] + [{"mint": "UnrelatedMint", "lane": LANE_NAME, "pa_pos_id": "%s/UnrelatedMint#1" % LANE_NAME}]   # no position AND no close record of the instance; ledger NOT empty
    elif mode == "foreign" and pos is not None: pos["pa_pos_id"] = "foreign-instance-id"
    elif mode in ("closed_same", "closed_foreign", "closed_nolane"):
        pid = (pos or {}).get("pa_pos_id") or pid_of_first
        d["positions"].pop(MINT, None)
        d.setdefault("closes", []).append({"mint": MINT, "lane": LANE_NAME, "pa_pos_id": pid if mode in ("closed_same", "closed_nolane") else "%s/%s#998" % (LANE_NAME, MINT), "realized_usd": 0.0})
        if mode == "closed_nolane": d["closes"][-1].pop("lane")
    L._write(d)
def _inv(paths):
    out = {}
    for base in paths:
        for dp, dn, fn in os.walk(base):
            for n in fn + dn:
                p = os.path.join(dp, n)
                try: s = os.stat(p); out[p] = (s.st_size, s.st_mtime_ns)
                except OSError: out[p] = None
    return out
def _fhash(p):
    import hashlib
    try:
        with open(p, "rb") as f: return hashlib.sha256(f.read()).hexdigest()
    except OSError: return None

def child_run(mult, kill_point=None, two=False):
    PA = fresh(); r = _enter()
    pos = (L.open_positions() or {}).get(MINT) or {}
    if not (r[0] and pos.get("pa_pos_id")):
        print("ENTRY-REFUSED lane=%s ok=%s reason=%r" % (LANE_ARG, bool(r[0]), r[1]), flush=True); sys.exit(5)
    print("ENTRY lane=%s ok=%s cash=%.6f nonsim=%s pa_pos_id=%s" % (LANE_ARG, bool(r[0]), cash(PA), (pos.get("execution") or {}).get("source") != "jupiter_quote_sim", bool(pos.get("pa_pos_id"))), flush=True)
    if two:
        r2 = _enter("OtherLaneMint2"); p2 = (L.open_positions() or {}).get("OtherLaneMint2") or {}
        if not (r2[0] and p2.get("pa_pos_id")): print("ENTRY-REFUSED second %r" % (r2[1],), flush=True); sys.exit(5)
        print("ENTRY2 cash=%.6f" % cash(PA), flush=True)
        PA.credit = lambda *a, **k: (False, "injected: credit NOT applied (labelled)")   # both intents stay open, no credit, then a real SIGKILL below
        L.poll_exits(price_fn=lambda chain, mint, spot_index=None: float(((L.open_positions() or {}).get(mint) or {}).get("entry_price") or 0) * float(mult))
        print("KILLHIT lane=%s two-intents" % LANE_ARG, flush=True); os.kill(os.getpid(), _signal.SIGKILL)
    if kill_point == "wd":   # REAL write_down_zero writer (_close_leg -> _pa_write_down -> CI.begin), real SIGKILL right BEFORE the account write_down
        PA.write_down = lambda *a, **k: _die()
        _p2 = (L.open_positions() or {}).get(MINT)
        L._close_leg(_p2, "a", float(_p2["entry_price"]) * float(mult), "gate_test_reason", "write_down_zero")
        print("CHILD SURVIVED (write_down kill point not reached)", flush=True); sys.exit(7)
    if kill_point == "before_credit":
        PA.credit = lambda *a, **k: _die()
    else:
        CI.mark_account_applied = lambda lane, iid: _die()
    L.poll_exits(price_fn=pf_mult(float(mult)))
    print("CHILD SURVIVED (kill point not reached)", flush=True); sys.exit(7)

def child_entry_first(jmode=None, lmode=None, inject=None, probe=False):
    PA = fresh()
    _logging.getLogger().addHandler(_H()); _logging.getLogger().setLevel(_logging.INFO)
    if jmode: jedit(jmode)
    if lmode: ledit(lmode)
    if inject == "freeze_false":
        def _fz(lane, iid, why):
            HITS["n"] += 1; return False
        CI.freeze = _fz
    cnt = {"poll_exits": 0}; _pe = L.poll_exits
    def _pe2(*a, **k):
        cnt["poll_exits"] += 1; return _pe(*a, **k)
    L.poll_exits = _pe2
    ents0, st0, _t = CI._read(CI.journal_path(LANE_NAME))
    ops0 = [o.get("op_id") for e in ents0 if e.get("ev") == "intent" for o in (e.get("settle_ops") or []) if isinstance(o, dict)]
    pre = {"pre_cash": cash(PA), "pre_ev": [e.get("ev") for e in ents0 if e.get("intent_id")], "n_pre": len([e for e in ents0 if e.get("ev") == "intent"]), "ops": ops0, "pre_acct_qty": _acct_qty(PA),
           "op_keys": sorted((([o for e in ents0 if e.get("ev") == "intent" for o in (e.get("settle_ops") or []) if isinstance(o, dict)] or [{}])[0]).keys()), "pre_credit_events": [_credit_events(PA, o)[1] for o in ops0 if o]}
    if probe:   # READ-ONLY PURITY of the gate readers: ledger bytes, directory inventory and metadata (catches lock/ensure/mtime side effects)
        paths = [os.path.dirname(L.LEDGER_PATH), os.environ["FOREX_AGENT_HOME"]]
        h0, i0 = _fhash(L.LEDGER_PATH), _inv(paths)
        ran = False; err = None
        try:
            if hasattr(L, "_gate_lane"):
                L._gate_lane(LANE_NAME, CI); L._read_status(); ran = True
        except Exception as e: err = repr(e)
        pre["purity"] = {"ran": ran, "err": err, "same_bytes": _fhash(L.LEDGER_PATH) == h0, "same_inventory": _inv(paths) == i0,
                         "diff": sorted(set(_inv(paths).items()) ^ set(i0.items()), key=str)[:4] if _inv(paths) != i0 else []}
        pre["read_status"] = list(L._read_status())[1:] if hasattr(L, "_read_status") else None
        _g0 = getattr(L, "_GATE_STATS", None)
        pre["probe_stats"] = dict(_g0) if isinstance(_g0, dict) else None
        if isinstance(_g0, dict):
            for _k in list(_g0): _g0[_k] = 0   # the probe must NOT supply behavioural gate hits: counters and captured logs reset before the real recovery
        del LOGS[:]
    print("PRE " + _json.dumps(pre, default=str), flush=True)
    _e = _enter("OtherLaneMintNew")
    ents, st, _ = CI._read(CI.journal_path(LANE_NAME))
    gs = getattr(L, "_GATE_STATS", None)
    res = {"cash": cash(PA), "journal_status": st, "ev": [e.get("ev") for e in ents if e.get("intent_id")], "pending": len(CI.pending(LANE_NAME)), "frozen_open": _frozen_open_n(),
           "entry_blocked": bool(L._ENTRY_BLOCKED), "entry_try": repr(_e)[:300], "entry_opened": bool(_e[0]), "outcomes": {o: PA.op_outcome(o) for o in ops0 if o},
           "credit_events": [_credit_events(PA, o)[1] for o in ops0 if o], "acct_qty": _acct_qty(PA), "freeze_attempts": HITS["n"], "cnt": cnt,
           "gate_stats": dict(gs) if isinstance(gs, dict) else None,
           "gate_logs": {k: len([m for m in LOGS if "GATE verdict=%s" % k in m]) for k in ("ok", "freeze", "hold")}, "credited_logged": len([m for m in LOGS if "credited" in m and "GATE" in m or "credit-before-mark" in m]),
           "hold_logged": len([m for m in LOGS if "HOLD (nothing appended" in m]), "gate_journal_why": [e.get("why") for e in ents if e.get("ev") == "frozen"]}
    print("RESULT " + _json.dumps(res, default=str), flush=True)

def _a(i, d=None): return (sys.argv[i] if len(sys.argv) > i and sys.argv[i] != "-" else d)
if MODE == "run": child_run(sys.argv[4], _a(5), _a(6) == "two"); sys.exit(0)
if MODE == "entryfirst": child_entry_first(_a(4), _a(5), _a(6), _a(7) == "probe"); sys.exit(0)

FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n, flush=True)
    if not c: FAILS.append(n)
def spawn(args, env=None):
    e = dict(os.environ); e.update(env or {})
    p = subprocess.run([sys.executable, "-B", os.path.abspath(__file__)] + args, capture_output=True, text=True, timeout=90, env=e)
    return p.returncode, p.stdout, p.stderr
REFUSAL = "close-intent divergence frozen: entries blocked until operator resolution"
lane = "momentum"
# tag, kill_point, jmode, lmode, inject, kind, two, probe
S = (("G0-healthy-control", "before_credit", None, None, None, "converge", False, False),
     ("G1-FIELD-VALID-position-absent", "before_credit", None, "absent", None, "freeze", False, False),
     ("G2-FIELD-VALID-foreign-instance", "before_credit", None, "foreign", None, "freeze", False, False),
     ("G3-pos_id-missing", "before_credit", "missing_pos_id", None, None, "hold", False, False),
     ("G4-mint-missing", "before_credit", "missing_mint", None, None, "hold", False, False),
     ("G5-op_id-not-expected", "before_credit", "opid_mismatch", None, None, "freeze", False, False),
     ("G6-op-mint-mismatch", "before_credit", "op_mint_mismatch", None, None, "freeze", False, False),
     ("G7-op-pos_id-mismatch", "before_credit", "op_pid_mismatch", None, None, "freeze", False, False),
     ("G8-pos_id-not-this-lane-mint-instance", "before_credit", "pid_badprefix", None, None, "freeze", False, False),
     ("G9-unknown-kind", "before_credit", "unknown_kind", None, None, "freeze", False, False),
     ("G10-missing-kind", "before_credit", "missing_kind", None, None, "freeze", False, False),
     ("G11-write_down-without-recorded-reason-SYNTHETIC-edit", "before_credit", "wd_noreason", None, None, "hold", False, False),
     ("G12-multi-op-later-op-reasonless-write_down", "before_credit", "multiop_bad", None, None, "hold", False, False),
     ("G12b-multi-op-later-op-unknown-kind", "before_credit", "multiop_unknown", None, None, "freeze", False, False),
     ("G13-ledger-corrupt-with-pending", "before_credit", None, "corrupt", None, "hold", False, True),
     ("G14-ledger-missing-with-pending", "before_credit", None, "missing", None, "hold", False, False),
     ("G15-ledger-REAL-EMPTY-no-positions-no-closes-with-pending", "before_credit", None, "empty", None, "hold", False, False),
     ("G16-absent-plus-failed-freeze", "before_credit", None, "absent", "freeze_false", "doublefault", False, False),
     ("G17-marker-without-account-outcome", "before_credit", "marker_noop", None, None, "hold", False, False),
     ("G18-CONTROL-lane-closed-exact-instance-account-op-outstanding", "before_credit", None, "closed_same", None, "converge", False, False),
     ("G19-lane-closed-OTHER-instance", "before_credit", None, "closed_foreign", None, "freeze", False, False),
     ("G20-CONTROL-valid-write_down", "before_credit", "wd_valid", None, None, "converge", False, False),
     ("G21-CONTROL-already-terminal-intent", "before_credit", "terminal", None, None, "terminal", False, False),
     ("G22-valid-sibling-converges-invalid-frozen", None, None, "absent", None, "sibling", True, False),
     ("G23-credit-before-mark-plus-position-absent", None, None, "absent", None, "creditedfreeze", False, False),
     ("G24-CONTROL-credit-before-mark-valid-instance", None, None, None, None, "creditedconverge", False, False),
     ("G26-CONTROL-REAL-write_down_zero-writer-record", "wd", None, None, None, "converge", False, False),
     ("G27-REAL-write_down-record-plus-foreign-field", "wd", "wd_foreign_key", None, None, "freeze", False, False),
     ("G28-REAL-write_down-record-reason-removed-legacy", "wd", "wd_real_noreason", None, None, "hold", False, False),
     ("G29-credit-record-plus-foreign-field", "before_credit", "credit_foreign_key", None, None, "freeze", False, False),
     ("G30-close-record-same-id-NO-lane-field-legacy", "before_credit", None, "closed_nolane", None, "hold", False, False),
     ("G25-READ-ONLY-purity-ok-ledger", "before_credit", None, None, None, "converge", False, True))
for tag0, kp, jm, lm, inj, kind, two, probe in S:
    tag = "%s-%s" % (tag0, lane)
    root = _tf.mkdtemp(prefix="momentum-gate-%s-" % tag)
    rc, out, err = spawn(["run", root, lane, "2.0", kp or "-", "two" if two else "-"])
    _el = [l for l in out.splitlines() if l.startswith("ENTRY ")]; _rf = [l for l in out.splitlines() if l.startswith("ENTRY-REFUSED")]
    ok_e = bool(_el) and "ok=True" in _el[0] and "nonsim=True" in _el[0] and "pa_pos_id=True" in _el[0] and not _rf and "Traceback" not in err
    check("%s: SETUP entry opened non-sim with a pinned instance (%s%s rc %d stderr %r)" % (tag, _el[:1], _rf[:1], rc, err[-120:]), ok_e)
    check("%s: child KILLED by SIGKILL (rc %d), KILLHIT reached" % (tag, rc), rc == -_signal.SIGKILL and "SURVIVED" not in out and "KILLHIT lane=%s" % lane in out or (kp is None and not two and rc == -_signal.SIGKILL and "KILLHIT lane=%s" % lane in out))
    if not ok_e: continue
    entry_cash = float(_el[0].split("cash=")[1].split()[0])
    if two:   # baseline = cash AFTER the second entry (no credit applied, both entries debited)
        _e2 = [l for l in out.splitlines() if l.startswith("ENTRY2 ")]
        if _e2: entry_cash = float(_e2[0].split("cash=")[1].split()[0])
    rc2, out2, err2 = spawn(["entryfirst", root, lane, jm or "-", lm or "-", inj or "-", "probe" if probe else "-"])
    pl = [l for l in out2.splitlines() if l.startswith("PRE ")]; rl = [l for l in out2.splitlines() if l.startswith("RESULT ")]
    pre = _json.loads(pl[0][4:]) if pl else None; r = _json.loads(rl[0][7:]) if rl else None
    check("%s: entry-first recovery process exited 0, no crash trace (rc %d, stderr tail %r)" % (tag, rc2, err2[-160:]), rc2 == 0 and pre is not None and r is not None and "Traceback" not in err2)
    if pre is None or r is None: continue
    print("  RAW %s PRE %s RESULT %s" % (tag, pre, r), flush=True)
    check("%s: zero polls in the recovery process (%d)" % (tag, r["cnt"]["poll_exits"]), r["cnt"]["poll_exits"] == 0)
    gs = r["gate_stats"]
    def hit(k): return gs is not None and gs.get(k, 0) >= 1 and r["gate_logs"].get(k, 0) >= 1
    blocked = r["entry_blocked"] is True and r["entry_opened"] is False and REFUSAL in r["entry_try"]
    applied = [o for o, v in r["outcomes"].items() if v == "applied"]
    no_mut = _cents(r["cash"]) == _cents(pre["pre_cash"]) and r["acct_qty"] == pre["pre_acct_qty"]
    if kind not in ("creditedfreeze", "creditedconverge"):
        check("%s: durable pre-state: cash == entry cash (no op applied yet): %r == %r" % (tag, pre["pre_cash"], entry_cash), _cents(pre["pre_cash"]) == _cents(entry_cash))
    if kind in ("freeze", "doublefault"):
        check("%s: INSTRUMENT gate-hit freeze (stats %r, logs %r)" % (tag, gs, r["gate_logs"]), hit("freeze"))
        check("%s: NO account mutation: no op applied %r, cash unchanged (%r vs %r), account qty unchanged (%r vs %r), no credit events %r" % (tag, applied, r["cash"], pre["pre_cash"], pre["pre_acct_qty"], r["acct_qty"], r["credit_events"]),
              not applied and no_mut and all(c == 0 for c in r["credit_events"]))
        check("%s: entries BLOCKED with the production refusal (blocked %r opened %r entry %r)" % (tag, r["entry_blocked"], r["entry_opened"], r["entry_try"][:80]), blocked)
        if kind == "freeze":
            check("%s: intent durably FROZEN (frozen_open %d), lifecycle %r has no account_applied/applied" % (tag, r["frozen_open"], r["ev"]), r["frozen_open"] == 1 and "account_applied" not in r["ev"] and "applied" not in r["ev"])
        else:
            check("%s: failed freeze write: freeze attempted (%d), no durable freeze (frozen_open %d), pending intent stays (%d)" % (tag, r["freeze_attempts"], r["frozen_open"], r["pending"]), r["freeze_attempts"] >= 1 and r["frozen_open"] == 0 and r["pending"] >= 1)
    elif kind == "hold":
        check("%s: INSTRUMENT gate-hit hold (stats %r, logs %r, hold log %d)" % (tag, gs, r["gate_logs"], r["hold_logged"]), hit("hold") and r["hold_logged"] >= 1)
        check("%s: NO account mutation: no op applied %r, cash unchanged, account qty unchanged, no credit events" % (tag, applied), not applied and no_mut and all(c == 0 for c in r["credit_events"]))
        check("%s: entries BLOCKED with the production refusal" % tag, blocked)
        check("%s: NON-MUTATING hold: nothing appended to the journal (lifecycle %r == pre %r), no freeze, intent still pending (%d)" % (tag, r["ev"], pre["pre_ev"], r["pending"]), r["ev"] == pre["pre_ev"] and r["frozen_open"] == 0 and r["pending"] >= 1)
    elif kind == "converge":
        check("%s: INSTRUMENT gate-hit ok (stats %r)" % (tag, gs), hit("ok"))
        check("%s: the recorded op applied EXACTLY once (outcomes %r, credit events %r)" % (tag, r["outcomes"], r["credit_events"]), len(applied) == 1 and (all(c == 1 for c in r["credit_events"]) or "writedown" in "".join(pre["ops"])))
        check("%s: not frozen, entries NOT blocked, new entry admitted (frozen_open %d blocked %r opened %r)" % (tag, r["frozen_open"], r["entry_blocked"], r["entry_opened"]), r["frozen_open"] == 0 and r["entry_blocked"] is False and r["entry_opened"] is True)
    elif kind == "terminal":
        check("%s: nothing pending, no freeze, op NOT re-driven (outcomes %r), entry admitted" % (tag, r["outcomes"]), pre["n_pre"] == 1 and r["pending"] == 0 and r["frozen_open"] == 0 and not applied and r["entry_opened"] is True)
    elif kind == "sibling":
        o1, o2 = pre["ops"][0], pre["ops"][1]
        check("%s: INSTRUMENT gate-hit freeze and ok (stats %r)" % (tag, gs), hit("freeze") and hit("ok"))
        check("%s: invalid intent's op NOT applied (%r), valid sibling's op applied exactly once (%r, credit events %r)" % (tag, r["outcomes"].get(o1), r["outcomes"].get(o2), r["credit_events"]), r["outcomes"].get(o1) != "applied" and r["outcomes"].get(o2) == "applied" and r["credit_events"] == [0, 1])
        check("%s: invalid frozen (frozen_open %d), entries blocked" % (tag, r["frozen_open"]), r["frozen_open"] == 1 and blocked)
    elif kind == "creditedfreeze":
        check("%s: INSTRUMENT gate-hit freeze with credited diagnostics (stats %r, journal why %r)" % (tag, gs, r["gate_journal_why"]), hit("freeze") and any("credited" in (w or "") for w in r["gate_journal_why"]))
        check("%s: NO second credit and nothing compensated: credit events %r, cash %r vs %r, qty %r vs %r" % (tag, r["credit_events"], r["cash"], pre["pre_cash"], r["acct_qty"], pre["pre_acct_qty"]), r["credit_events"] == [1] and no_mut)
        check("%s: frozen and entries blocked (frozen_open %d)" % (tag, r["frozen_open"]), r["frozen_open"] == 1 and blocked)
    elif kind == "creditedconverge":
        check("%s: INSTRUMENT gate-hit ok and credit-before-mark reported (stats %r, logged %d)" % (tag, gs, r["credited_logged"]), hit("ok") and r["credited_logged"] >= 1)
        check("%s: exactly one credit (events %r), qty unchanged (%r vs %r), marker written (%r), not frozen, entry admitted" % (tag, r["credit_events"], r["acct_qty"], pre["pre_acct_qty"], r["ev"][:2]),
              r["credit_events"] == [1] and r["acct_qty"] == pre["pre_acct_qty"] and r["ev"][:2] == ["intent", "account_applied"] and r["frozen_open"] == 0 and r["entry_opened"] is True)
    if "G26" in tag0:
        check("%s: BEHAVIOR the REAL writer's record has exactly the keys entry_op_id/expected_account_id/kind/mint/op_id/pos_id/reason: %r" % (tag, pre.get("op_keys")), pre.get("op_keys") == ["entry_op_id", "expected_account_id", "kind", "mint", "op_id", "pos_id", "reason"])   # the real _pa_write_down writer records the generation binding (the gate needs it to judge the op); the 5-key expectation predates that binding
    if probe:
        pu = pre.get("purity") or {}
        check("%s: READ-ONLY readers: the gate/_read_status ran (%r) and left ledger BYTES, directory inventory and mtimes unchanged (bytes %r, inventory %r, diff %r, err %r)" % (tag, pu.get("ran"), pu.get("same_bytes"), pu.get("same_inventory"), pu.get("diff"), pu.get("err")),
              pu.get("ran") is True and pu.get("same_bytes") is True and pu.get("same_inventory") is True)
        if lm == "corrupt":
            check("%s: _read_status reports the corrupt ledger explicitly: %r" % (tag, pre.get("read_status")), pre.get("read_status") == ["corrupt"])
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
