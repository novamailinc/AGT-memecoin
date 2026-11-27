#!/usr/bin/env python3
"""ENTRY-FIRST, ZERO-POLL variant (bb45; bb43/bb44 pins preserved). The fresh recovery process attempts a NEW entry FIRST and NEVER polls exits, so the only code that can recognise the durable defect is STARTUP recognition (_recover_close_intents -> _pending_unusable); the retry guard (_nonsim_figures/_recorded_credit_op) is proven NOT reached (call counters 0, poll_exits counter 0). The defective position's exit is never retried. Distinct counters: startup_logs ("has an unusable recorded op") vs retry_logs ("unusable for the retry"). Control E5 (no defect): characterises healthy-pending entry admission, labelled, not an acceptance claim.\nPrevious header: ADDITION (bb44, separately reviewable; bb43 pins unchanged): DURABLE-DEFECT restart case for the WATCHER and MOMENTUM lanes. A labelled SETUP injection in the killed child records a real durable defect in the close intent (price_kind missing / half qty / close reason missing) and the child then SIGKILLs itself after the account credit. A FRESH recovery process with NO injection must recognise the defect from the DURABLE journal + ledger alone (_pending_unusable), freeze, block entries, credit nothing, and keep blocking after another restart; with the freeze write failing it must still block from durable data. Without the recorded-op helper the cases print SKIP (never PASS).\nPrevious header: EXTENSION (bb43, fix coverage): scenarios for the recorded-op settle in the WATCHER and MOMENTUM lanes. Leg a (as test_other_lanes_nonsim_kill_requote), REMAINING close (hard stop, ONE combined exit, op remaining:abc), and named FAIL-CLOSED injections into the recorded op (qty mismatch, price_kind missing, amount nan, freeze write failing), each with entries blocked, no second credit, restart persistence. Injections need the recorded-op helper: on a source without it they print SKIP and are never counted as PASS.
REAL PROCESS-KILL test, NON-DEMO (modelled-fill) exit for the WATCHER and MOMENTUM lanes: SIGKILL after the account credit, then a FRESH process retries at a
DIFFERENT price. Same structure as tests/test_ceo_nonsim_kill_requote.py (which covers the ceo lane and its fix).
ENGINEERING TEST of the REAL lane module, REAL shared paper account and REAL close-intent journal in a scratch root, canned price feed (not real-provider
acceptance). AGT_EXECUTION_MODE is unset in the test processes (modelled-fill path, the repo default), so the exit uses the live honest_exit path (_pa_settle).
Parent mode (no args) spawns, per lane and scenario, ONE child that opens a position and polls at 2.0x; the child SIGKILLs ITSELF (os.kill(os.getpid(), SIGKILL)) at
close_intents.mark_account_applied (labelled injection point, real process death). A FRESH recovery process on the same scratch root then polls at the retry multiple.
Scenarios per lane: S1 after_credit, retry 2.3x (the case under test); S3 CONTROL: after_credit, retry 2.0x (same price: proves harness and kill are real).
Contract: leg a done; the account credit booked ONCE (cash unchanged by the recovery); the lane leg realized equals the RECORDED net (recorded amount - fee), not a
new quote; lifecycle exactly [intent, account_applied, lane_applied, applied]; nothing pending; no hold.
EXPECTED on source without the recorded-op settle for these lanes: S1 RED by assertion (lane realized at the new quote), S3 GREEN. Not yet run. Uses the REAL killswitch module redirected to a scratch state dir (no stub).
Run: python3 -B tests/test_other_lanes_nonsim_kill_requote.py"""
if __name__ != "__main__":
    import pytest
    pytest.skip("script-style test", allow_module_level=True)
import sys, os
MODE = sys.argv[1] if len(sys.argv) > 1 else "parent"
import tempfile as _tf
ROOTDIR = sys.argv[2] if MODE != "parent" else _tf.mkdtemp(prefix="otherlane-parent-unused-")
LANE_ARG = sys.argv[3] if MODE != "parent" else None
import importlib, json as _json, signal as _signal, subprocess, time
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
LANES = ("watcher", "momentum")
if LANE_ARG:
    from daemon.launch_scanner import watcher_lane as _W, momentum_lane as _M
    L = {"watcher": _W, "momentum": _M}[LANE_ARG]
    LANE_NAME = L.LANE
class _Safe:
    def check_token(self, chain, mint, timeout=10): return {"passed": True, "score": 80, "flags": [], "fails": [], "unknown": False}
class _Veto:
    def veto_check(self, mint, chain, symbol="", safety_report=None, run_bundle=False, lane="unknown"):
        return {"passed": True, "vetoes": [], "flags": [], "details": {}}
def _tape():
    n = time.time()
    return {"fetched_at": n, "received_at": n, "dex": "pump", "usd_liquidity": 100000.0, "usd_market_cap": 500000.0, "chg_m5": 10.0, "buys_m5": 100, "sells_m5": 40,
            "vol_m5": 9000.0, "buys_h1": 400, "sells_h1": 150, "price_usd": 0.001, "pair_created_ms": (time.time() - 3600) * 1000, "tape_source": "dexscreener"}
MINT = "OtherLaneMint1"
def _enter(MINT=MINT):
    if LANE_ARG == "watcher":
        ev = {"smart_wallets": 3, "wallets": ["Wal0", "Wal1", "Wal2"], "wallet_groups": ["g0", "g1", "g2"], "safety": "ok",
              "components": {"convergence_groups": 3, "sightings": 3, "sol_spent_total": 4.2, "safety_score": 80}}
        sig = BUS.make_signal("solana", "smart-watcher", MINT, symbol="WATCH", tier="converged", mcap=500000.0, liquidity=100000.0, evidence=ev)
        return L.consider_fire(sig, time.time())
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

def child_run(mult, defect=None):
    PA = fresh(); r = _enter()
    pos = (L.open_positions() or {}).get(MINT) or {}
    print("ENTRY lane=%s ok=%s cash=%.6f nonsim=%s pa_pos_id=%s why=%r" % (LANE_ARG, bool(r[0]), cash(PA), (pos.get("execution") or {}).get("source") != "jupiter_quote_sim", bool(pos.get("pa_pos_id")), r[1] if not r[0] else ""), flush=True)
    if defect == "nopk":
        _o = L._pa_settle; L._pa_settle = lambda pos, qty, gross, fee, suffix, price_kind=None: _o(pos, qty, gross, fee, suffix, None)
    elif defect == "qtyhalf":
        _o = L._pa_settle; L._pa_settle = lambda pos, qty, gross, fee, suffix, price_kind=None: _o(pos, qty * 0.5, gross, fee, suffix, price_kind)
    elif defect == "noreason":
        _oi = L._intent_open; L._intent_open = lambda ls, pos, ops, reason: _oi(ls, pos, ops, None)
    CI.mark_account_applied = lambda lane, iid: _die()   # labelled injection point: real SIGKILL of THIS process right after the durable account credit
    L.poll_exits(price_fn=pf_mult(float(mult)))
    print("CHILD SURVIVED (kill point not reached)", flush=True); sys.exit(7)

HITS = {"n": 0}; FZC = {"n": 0}; LOGS = []
import logging as _logging
class _H(_logging.Handler):
    def emit(self, rec):
        try: LOGS.append(rec.getMessage())
        except Exception: pass
def _frozen_open_n():
    ents, st, _t = CI._read(CI.journal_path(LANE_NAME))
    if st == "torn": return 0
    succ = {e.get("supersedes") for e in ents if e.get("ev") == "intent" and e.get("supersedes")}
    return len([i for i, x in CI._state(ents).items() if x["intent"] is not None and not x["applied"] and x["frozen"] is not None and i not in succ])
def _inject(kind):
    """Labelled NAMED fault injection in THIS recovery process only (scratch root; no repo state/config/flag change)."""
    real = getattr(L, "_recorded_credit_op", None)
    if kind in ("qty_mismatch", "amount_nan", "price_kind_missing"):
        assert real is not None, "recorded-op helper absent"
        def wrap(pos, suffix):
            op = real(pos, suffix)
            if op is None: return None
            HITS["n"] += 1   # the mutation REACHED the guard input
            op = dict(op)
            if kind == "qty_mismatch": op["qty"] = float(op["qty"]) * 1.5
            elif kind == "amount_nan": op["amount"] = float("nan")
            else: op["price_kind"] = None
            return op
        L._recorded_credit_op = wrap
    elif kind == "pk_freezefail":   # two labelled injections in the SAME recovery process: unusable recorded op AND a failing freeze write
        _inject("price_kind_missing"); _inject("freeze_false")
    elif kind == "freeze_false":
        def _fz(lane, iid, why):
            FZC["n"] += 1
            return False   # labelled injection: the freeze write FAILS
        CI.freeze = _fz

CNT = {"pending_unusable": 0, "recorded_credit_op": 0, "poll_exits": 0, "freeze": 0}
def child_entry_first(inject=None):
    PA = fresh()
    _logging.getLogger().addHandler(_H()); _logging.getLogger().setLevel(_logging.WARNING)
    if inject == "freeze_false":
        def _fz(lane, iid, why):
            CNT["freeze"] += 1
            return False   # labelled injection: freeze write fails
        CI.freeze = _fz
    _pu = L._pending_unusable
    def _pu2(*a, **k):
        CNT["pending_unusable"] += 1; return _pu(*a, **k)
    L._pending_unusable = _pu2
    _rc = L._recorded_credit_op
    def _rc2(*a, **k):
        CNT["recorded_credit_op"] += 1; return _rc(*a, **k)
    L._recorded_credit_op = _rc2
    _pe = L.poll_exits
    def _pe2(*a, **k):
        CNT["poll_exits"] += 1; return _pe(*a, **k)
    L.poll_exits = _pe2
    ents0, st0, _t = CI._read(CI.journal_path(LANE_NAME))
    pre = {"pre_cash": cash(PA), "pre_ev": [e.get("ev") for e in ents0 if e.get("intent_id")]}
    print("PRE " + _json.dumps(pre), flush=True)
    _e = _enter("OtherLaneMint2")   # NEW entry FIRST; no poll_exits anywhere in this process
    pos = (L.open_positions() or {}).get(MINT) or {}
    ents, st, _ = CI._read(CI.journal_path(LANE_NAME))
    leg = pos.get("legs", {}).get("a", {})
    res = {"cash": cash(PA), "a_done": bool(leg.get("done")), "pos_open": bool(pos), "journal_status": st, "ev": [e.get("ev") for e in ents if e.get("intent_id")],
           "n_intents": len([e for e in ents if e.get("ev") == "intent"]), "pending": len(CI.pending(LANE_NAME)), "frozen_open": _frozen_open_n(),
           "entry_blocked": bool(L._ENTRY_BLOCKED), "entry_try": repr(_e)[:300], "entry_opened": bool(_e[0]), "n_pos_all": len(L.open_positions() or {}),
           "startup_logs": len([m for m in LOGS if "has an unusable recorded op" in m or "rejected by the gate" in m or "GATE verdict=freeze" in m]),
           # STARTUP recognition is counted by its log evidence, and the gated lanes (momentum) recognise the defect in the PRE-APPLY GATE, not in the
           # _pending_unusable branch: the gate logs "rejected by the gate" / "GATE verdict=freeze" instead of "has an unusable recorded op". All three
           # are startup recognition (they run in _recover_close_intents before any entry/close); the retry-guard counters below prove the retry was never reached.
           "retry_logs": len([m for m in LOGS if "unusable for the retry" in m]),
           "freeze_logs": len([m for m in LOGS if "freeze event" in m]), "cnt": dict(CNT)}
    print("RESULT " + _json.dumps(res), flush=True)

def child_recover(mult, inject=None, try_entry=None):
    PA = fresh()
    _logging.getLogger().addHandler(_H()); _logging.getLogger().setLevel(_logging.WARNING)
    if inject: _inject(inject)
    ents0, st0, _t = CI._read(CI.journal_path(LANE_NAME))
    i0 = [e for e in ents0 if e.get("ev") == "intent"]
    op0 = ((i0[0].get("settle_ops") or [{}])[0]) if i0 else {}
    pre = {"pre_cash": cash(PA), "pre_ev": [e.get("ev") for e in ents0 if e.get("intent_id")], "op_amount": op0.get("amount"), "op_fee": op0.get("fee"), "n_pre": len(i0)}
    print("PRE " + _json.dumps(pre), flush=True)
    for _ in range(3):
        L.poll_exits(price_fn=pf_mult(float(mult)))
    pos = (L.open_positions() or {}).get(MINT) or {}
    ents, st, _ = CI._read(CI.journal_path(LANE_NAME))
    leg = pos.get("legs", {}).get("a", {})
    et = None
    if try_entry:
        _e = _enter("OtherLaneMint2"); et = repr(_e)[:300]
    res = {"cash": cash(PA), "a_done": bool(leg.get("done")), "a_realized": leg.get("realized_usd"), "journal_status": st,
           "ev": [e.get("ev") for e in ents if e.get("intent_id")], "n_intents": len([e for e in ents if e.get("ev") == "intent"]),
           "pending": len(CI.pending(LANE_NAME)), "pend_why": (pos.get("pending_exit") or {}).get("why"),
           "n_pos": len(L.open_positions() or {}), "hits": HITS["n"], "frozen_open": _frozen_open_n(), "freeze_calls": FZC["n"], "entry_blocked": bool(L._ENTRY_BLOCKED), "entry_try": et,
           "guard_logs": len([m for m in LOGS if "unusable for the retry" in m or "has an unusable recorded op" in m])}
    print("RESULT " + _json.dumps(res), flush=True)

if MODE == "entryfirst": child_entry_first((sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] != "-" else None)); sys.exit(0)
if MODE == "run": child_run(sys.argv[4], (sys.argv[5] if len(sys.argv) > 5 else None)); sys.exit(0)
if MODE == "recover": child_recover(sys.argv[4], (sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] != "-" else None), (sys.argv[6] if len(sys.argv) > 6 else None)); sys.exit(0)

FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n, flush=True)
    if not c: FAILS.append(n)
def spawn(args):
    p = subprocess.run([sys.executable, "-B", os.path.abspath(__file__)] + args, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr
FOUR = ["intent", "account_applied", "lane_applied", "applied"]
REFUSAL = "close-intent divergence frozen: entries blocked until operator resolution"   # existing production refusal text
def recover(root, lane, mult, inject=None, try_entry=False):
    rc, out, err = spawn(["recover", root, lane, str(mult), inject or "-"] + (["try"] if try_entry else []))
    rl = [l for l in out.splitlines() if l.startswith("RESULT ")]; pl = [l for l in out.splitlines() if l.startswith("PRE ")]
    return rc, err, (_json.loads(pl[0][4:]) if pl else None), (_json.loads(rl[0][7:]) if rl else None)
def killed(root, lane, kmult, tag, defect=None):
    rc, out, err = spawn(["run", root, lane, kmult] + ([defect] if defect else []))
    el = [l for l in out.splitlines() if l.startswith("ENTRY")]
    check("%s: child KILLED by SIGKILL (rc %d, want -9), KILLHIT, non-sim entry (%s)" % (tag, rc, el[:1]),
          rc == -_signal.SIGKILL and "SURVIVED" not in out and ("KILLHIT lane=%s" % lane) in out and bool(el) and "ok=True" in el[0] and "nonsim=True" in el[0] and "pa_pos_id=True" in el[0])
    return float(el[0].split("cash=")[1].split()[0]) if el else None
def _has_helper(lane):
    p = os.path.join(ROOT, "daemon", "launch_scanner", lane + "_lane.py")
    return "def _recorded_credit_op" in open(p).read()
def entry_first(root, lane, inject=None):
    rc, out, err = spawn(["entryfirst", root, lane, inject or "-"])
    rl = [l for l in out.splitlines() if l.startswith("RESULT ")]; pl = [l for l in out.splitlines() if l.startswith("PRE ")]
    return rc, err, (_json.loads(pl[0][4:]) if pl else None), (_json.loads(rl[0][7:]) if rl else None)
for lane in LANES:
    if not _has_helper(lane):
        print("SKIP all %s: recorded-op helper absent in this source (NOT a pass)" % lane); continue
    for dtag, defect, inj in (("E1-price-kind-missing", "nopk", None), ("E2-half-qty", "qtyhalf", None), ("E3-close-reason-missing", "noreason", None), ("E4-price-kind-missing-freeze-write-fails", "nopk", "freeze_false")):
        tag = "%s-%s" % (dtag, lane)
        root = _tf.mkdtemp(prefix="otherlane-ef-%s-" % tag)
        entry_cash = killed(root, lane, "2.0", tag, defect)
        rc2, err2, pre, r = entry_first(root, lane, inj)
        check("%s: entry-first recovery exited 0, no crash trace (rc %d, stderr tail %r)" % (tag, rc2, err2[-160:]), rc2 == 0 and "Traceback" not in err2 and r is not None)
        if r is None or pre is None or entry_cash is None: continue
        print("  %s PRE %s" % (tag, pre)); print("  %s RESULT %s" % (tag, r))
        check("%s: durable pre-state [intent] only, credit already in cash (%r > entry %r)" % (tag, pre["pre_cash"], entry_cash), pre["pre_ev"] == ["intent"] and pre["pre_cash"] > entry_cash + 1e-9)
        c = r["cnt"]
        check("%s: STARTUP recognition did it: startup_logs %d >= 1, _pending_unusable calls %d >= 1; retry guard NEVER reached: retry_logs %d == 0, _recorded_credit_op calls %d == 0, poll_exits calls %d == 0"
              % (tag, r["startup_logs"], c["pending_unusable"], r["retry_logs"], c["recorded_credit_op"], c["poll_exits"]),
              r["startup_logs"] >= 1 and c["pending_unusable"] >= 1 and r["retry_logs"] == 0 and c["recorded_credit_op"] == 0 and c["poll_exits"] == 0)
        check("%s: the NEW entry was REFUSED with the production refusal (opened %r, %r), entries blocked (flag %r), no new position (n_pos_all %d)" % (tag, r["entry_opened"], r["entry_try"], r["entry_blocked"], r["n_pos_all"]),
              r["entry_opened"] is False and REFUSAL in r["entry_try"] and r["entry_blocked"] is True and r["n_pos_all"] == 1)
        check("%s: the defective position was NEVER retried and credited nothing more: leg a not done (%r), still open (%r), cash %r == %r, pending %d" % (tag, r["a_done"], r["pos_open"], r["cash"], pre["pre_cash"], r["pending"]),
              r["a_done"] is False and r["pos_open"] is True and _cents(r["cash"]) == _cents(pre["pre_cash"]))
        if inj == "freeze_false":
            check("%s: freeze ATTEMPTED (%d) and NOT durable (ev %r, frozen_open %d); freeze failure logged (%d)" % (tag, c["freeze"], r["ev"], r["frozen_open"], r["freeze_logs"]),
                  c["freeze"] >= 1 and "frozen" not in r["ev"] and r["frozen_open"] == 0 and r["freeze_logs"] >= 1 and r["pending"] == 1)
        else:
            check("%s: intent FROZEN durably (ev %r, frozen_open %d, 1 intent)" % (tag, r["ev"], r["frozen_open"]), "frozen" in r["ev"] and r["frozen_open"] == 1 and r["n_intents"] == 1 and "lane_applied" not in r["ev"])
        rc3, err3, pre3, r3 = entry_first(root, lane, inj)
        # ANOTHER fresh entry-first restart: still blocked from durable data. The durable block takes one of two forms, and the check accepts either:
        # (a) the intent was frozen durably on the first restart (frozen_open >= 1, "frozen" in the journal events) — frozen is terminal, so no
        #     re-recognition log can or should fire on this restart; the frozen intent IS the durable block. (b) the freeze never landed (E4, freeze
        #     write fails): the intent is still pending, so startup recognition fires again (startup_logs >= 1). Either way nothing is credited and no poll runs.
        _durable_block = (r3["frozen_open"] >= 1 and "frozen" in r3["ev"]) or r3["startup_logs"] >= 1
        check("%s: ANOTHER fresh entry-first restart: still blocked from durable data, nothing credited (rc %d; entry %r, cash %r; frozen_open %d, ev %r, startup_logs %d)" % (tag, rc3, (r3 or {}).get("entry_try"), (r3 or {}).get("cash"), (r3 or {}).get("frozen_open", -1), (r3 or {}).get("ev"), (r3 or {}).get("startup_logs", -1)),
              rc3 == 0 and r3 is not None and r3["entry_opened"] is False and REFUSAL in r3["entry_try"] and _cents(r3["cash"]) == _cents(pre["pre_cash"]) and _durable_block and r3["cnt"]["poll_exits"] == 0)
    # E5 CONTROL: a healthy pending intent (no defect) - characterisation only, labelled
    tag = "E5-control-healthy-pending-%s" % lane
    root = _tf.mkdtemp(prefix="otherlane-ef-%s-" % tag)
    entry_cash = killed(root, lane, "2.0", tag, None)
    rc2, err2, pre, r = entry_first(root, lane, None)
    check("%s: control recovery exited 0 (rc %d)" % (tag, rc2), rc2 == 0 and "Traceback" not in err2 and r is not None)
    if r is not None and pre is not None:
        print("  %s PRE %s" % (tag, pre)); print("  %s RESULT %s  [CHARACTERISATION of healthy-pending entry admission: not asserted]" % (tag, r))
        check("%s: healthy intent is NOT frozen and startup recognition does not fire (startup_logs %d == 0, frozen_open %d, retry_logs %d), no poll (calls %d)" % (tag, r["startup_logs"], r["frozen_open"], r["retry_logs"], r["cnt"]["poll_exits"]),
              r["startup_logs"] == 0 and r["frozen_open"] == 0 and r["retry_logs"] == 0 and r["cnt"]["poll_exits"] == 0 and "frozen" not in r["ev"])
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
