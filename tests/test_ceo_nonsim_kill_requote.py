#!/usr/bin/env python3
"""REAL PROCESS-KILL test, NON-DEMO (modelled-fill) ceo exit: SIGKILL after the account credit, then a FRESH process retries at a DIFFERENT price.
ENGINEERING TEST of the REAL ceo lane, REAL shared paper account and REAL close-intent journal in a scratch root, canned price feed. The lane is NOT in demo
mode: AGT_EXECUTION_MODE is unset in the test processes (demo_fill.mode_enabled() is False; that is the production default when the variable is not set),
so the exit uses the live honest_exit price path (_pa_settle) and no DemoFiller. Test-scoped only: no config or flag is changed anywhere.
Parent mode (no args) spawns, per scenario, ONE child that opens a ceo position and polls at 2.0x; the child SIGKILLs ITSELF (os.kill(os.getpid(), SIGKILL), never
any other pid) at the named point (a labelled injection at close_intents.mark_account_applied / mark_lane_applied). A FRESH recovery process on the same scratch
root then polls at the scenario's retry multiple. The parent signals no process.
Scenarios: S1 after_credit, retry 2.3x (the case under test); S2 pre_lane_marker, retry 2.3x; S3 after_credit, retry 2.0x (CONTROL: identical price, proves harness
and kill are real and the retry price is the only variable).
Contract: leg a done after recovery; the account credit is booked ONCE; the retry settles the SAME amounts the durable intent recorded (net = recorded amount - recorded
fee), not a new quote: account cash delta since entry == recorded net == the lane leg realized; lifecycle exactly one intent (intent, account_applied, lane_applied,
applied); no pending intent; journal cleanly readable; no hold (pending_exit empty).
Run: python3 -B tests/test_ceo_nonsim_kill_requote.py"""
if __name__ != "__main__":
    import pytest
    pytest.skip("script-style test", allow_module_level=True)
import sys, os
MODE = sys.argv[1] if len(sys.argv) > 1 else "parent"
import tempfile as _tf
ROOTDIR = sys.argv[2] if MODE != "parent" else _tf.mkdtemp(prefix="nonsimkill-parent-unused-")
import importlib, json as _json, signal as _signal, subprocess, time
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import tests._guard_fixture as G
G.block_network()   # BEFORE any repo/lane import
sys.path.insert(0, os.path.join(ROOT, "daemon", "launch_scanner"))
os.environ.pop("AGT_EXECUTION_MODE", None)   # modelled-fill (non-demo) mode, test processes only

from unittest.mock import patch
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
from daemon.launch_scanner import ceo as C, signal_bus as BUS
import daemon.launch_scanner.close_intents as CI

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

def fresh(start="10"):
    h = os.path.join(ROOTDIR, "home"); os.makedirs(h, exist_ok=True); os.environ["FOREX_AGENT_HOME"] = h
    for k in list(os.environ):
        if k.startswith("PAPER_ACCOUNT"): os.environ.pop(k)
    os.environ["PAPER_ACCOUNT_ENABLED"] = "1"; os.environ["PAPER_ACCOUNT_START"] = start
    import daemon.launch_scanner.paper_account as PA
    PA = importlib.reload(PA); G.install(PA)
    led = os.path.join(ROOTDIR, "led"); os.makedirs(led, exist_ok=True)
    C.LEDGER_PATH = os.path.join(led, "ceo.json"); C.ensure_ledger(); C.BUS_PATH = os.path.join(led, "bus.jsonl")
    C._ks = KS(); C._safety = Safe(); C._veto = Veto(); C._fresh_tape = lambda chain, mint: tape()
    C.quote_price = G.stamped_quote(C, lambda chain, mint, spot_index=None: 0.001)
    C.RUNNER_CONFIRM_GAP_S = 0.0; C._ENTRY_BLOCKED = False; C._RECOVERY_DONE = False
    return PA

def cash(PA): return PA.snapshot()["cash"]
def csig(mint):
    return BUS.make_signal(chain="solana", source="test-feed", mint=mint, symbol="TKN", score=70.0, tier="pick", mcap=500000.0, liquidity=50000.0)
def pf_mult(m):
    def pf(chain, mint, spot_index=None):
        pos = (C.open_positions() or {}).get(mint) or {}
        return float(pos.get("entry_price") or 0) * m
    return pf
def _cents(x): return int(round(float(x) * 100))
def _die(): os.kill(os.getpid(), _signal.SIGKILL)
MINT = "CeoMint1"
KILL_MULT = float(sys.argv[4]) if MODE == "run" and len(sys.argv) > 4 else 2.0

def child_run(point):
    PA = fresh(); r = C.consider_card(csig(MINT))
    pos = (C.open_positions() or {}).get(MINT) or {}
    print("ENTRY ok=%s cash=%.6f nonsim=%s pa_pos_id=%s" % (bool(r[0]), cash(PA), (pos.get("execution") or {}).get("source") != "jupiter_quote_sim", bool(pos.get("pa_pos_id"))), flush=True)
    if point.startswith("after_credit"):
        CI.mark_account_applied = lambda lane, iid: _die()
        _defect = point.split("_")[2] if point.count("_") >= 2 else None   # after_credit_<nopk|noreason|qtyhalf>: labelled SETUP injection recording a real durable defect, then the real kill
        if _defect == "nopk":
            _o = C._pa_settle
            C._pa_settle = lambda pos, qty, gross, fee, suffix, price_kind=None: _o(pos, qty, gross, fee, suffix, None)
        elif _defect == "qtyhalf":
            _o = C._pa_settle
            C._pa_settle = lambda pos, qty, gross, fee, suffix, price_kind=None: _o(pos, qty * 0.5, gross, fee, suffix, price_kind)
        elif _defect == "noreason":
            _oi = C._intent_open
            C._intent_open = lambda ls, pos, ops, reason: _oi(ls, pos, ops, None)
    elif point == "pre_lane_marker":
        CI.mark_lane_applied = lambda lane, iid: _die()
    elif point == "before_credit":
        PA.credit = lambda *a, **k: _die()   # labelled injection: the close intent is durable (begin returned), the account credit never happens
    C.poll_exits(price_fn=pf_mult(KILL_MULT))
    print("CHILD SURVIVED point=%s (kill point not reached)" % point, flush=True); sys.exit(7)

def _frozen_open_n():
    ents, st, _t = CI._read(CI.journal_path("ceo"))
    if st == "torn": return 0
    succ = {e.get("supersedes") for e in ents if e.get("ev") == "intent" and e.get("supersedes")}
    return len([i for i, x in CI._state(ents).items() if x["intent"] is not None and not x["applied"] and x["frozen"] is not None and i not in succ])
HITS = {"n": 0}
FZC = {"n": 0}
LOGS = []
import logging as _logging
class _H(_logging.Handler):
    def emit(self, rec):
        try: LOGS.append(rec.getMessage())
        except Exception: pass
def _inject(kind):
    """Labelled NAMED fault injection in THIS recovery process only (scratch root; no repo state, config or flag changes)."""
    real = getattr(C, "_recorded_credit_op", None)   # absent on a source without the recorded-op helper: only the helper-based injections need it
    if kind in ("qty_mismatch", "qty_near", "amount_nan", "fee_negative", "amount_missing", "price_kind_missing", "reason_missing"):
        assert real is not None, "recorded-op helper absent: this injection is not applicable"
        def wrap(pos, suffix):
            op = real(pos, suffix)
            if op is None: return None
            HITS["n"] += 1   # the mutation REACHED the guard input
            op = dict(op)
            if kind == "qty_mismatch": op["qty"] = float(op["qty"]) * 1.5
            elif kind == "qty_near": op["qty"] = float(op["qty"]) * (1 + 1e-10)
            elif kind == "amount_nan": op["amount"] = float("nan")
            elif kind == "fee_negative": op["fee"] = -1.0
            elif kind == "amount_missing": op.pop("amount", None)
            elif kind == "price_kind_missing": op["price_kind"] = None
            else: op["_close_info"] = {}
            return op
        C._recorded_credit_op = wrap
    elif kind == "freeze_false":
        def _fz(lane, iid, why):
            FZC["n"] += 1
            return False   # labelled injection: the freeze write FAILS (readable journal, event not durable)
        CI.freeze = _fz
    elif kind == "foreign_pos_id":
        d = C._read(); d["positions"][MINT]["pa_pos_id"] = "foreign-pos-id-xyz"; C._write(d)   # the OPEN position is now a different instance than the pending intent's
    elif kind == "journal_corrupt":
        jp = CI.journal_path("ceo"); lines = open(jp).read().split("\n")
        lines.insert(1, "{garbage-not-json"); open(jp, "w").write("\n".join(lines))

def child_recover(mult, inject=None, try_entry=None):
    PA = fresh()
    _logging.getLogger().addHandler(_H()); _logging.getLogger().setLevel(_logging.WARNING)
    if inject: _inject(inject)
    ents0, st0, _t = CI._read(CI.journal_path("ceo"))
    intents0 = [e for e in ents0 if e.get("ev") == "intent"]
    op0 = ((intents0[0].get("settle_ops") or [{}])[0]) if intents0 else {}
    pre = {"pre_cash": cash(PA), "pre_status": st0, "pre_ev": [e.get("ev") for e in ents0 if e.get("intent_id")],
           "op_amount": op0.get("amount"), "op_fee": op0.get("fee"), "op_id": op0.get("op_id"), "op_qty": op0.get("qty"),
           "op_reason": ((intents0[0].get("close_info") or {}).get("closed_reason") if intents0 else None)}
    print("PRE " + _json.dumps(pre), flush=True)
    if inject == "different_trigger":
        _orig = C._close_leg   # labelled: the retry fires under a DIFFERENT trigger reason than the recorded one
        C._close_leg = lambda pos, k, px, reason, pkind=None: _orig(pos, k, px, "operator_probe_other_trigger", pkind)
    et_first = None
    if try_entry == "tryfirst":   # a NEW entry is attempted BEFORE any exit retry runs in this fresh process
        et_first = repr(C.consider_card(csig("CeoMint2")))[:300]; try_entry = None
    for _ in range(3):
        C.poll_exits(price_fn=pf_mult(float(mult)))
    pos = (C.open_positions() or {}).get(MINT) or {}
    ents, st, _ = CI._read(CI.journal_path("ceo"))
    guard = [m for m in LOGS if "unusable for the retry" in m]
    et = None
    if try_entry:
        et = repr(C.consider_card(csig("CeoMint2")))[:300]
    res = {"cash": cash(PA), "a_done": bool(pos.get("legs", {}).get("a", {}).get("done")), "a_realized": pos.get("legs", {}).get("a", {}).get("realized_usd"),
           "a_exit_price": pos.get("legs", {}).get("a", {}).get("exit_price"), "entry_price": pos.get("entry_price"),
           "a_reason": pos.get("legs", {}).get("a", {}).get("exit_reason"),
           "pending": len(CI.pending("ceo")), "journal_status": st, "ev": [e.get("ev") for e in ents if e.get("intent_id")],
           "n_intents": len([e for e in ents if e.get("ev") == "intent"]), "pend_why": (pos.get("pending_exit") or {}).get("why"),
           "n_pos": len(C.open_positions() or {}), "hits": HITS["n"], "guard_logs": len(guard),
           "frozen_open": _frozen_open_n(), "freeze_calls": FZC["n"], "rec_logs": len([m for m in LOGS if "has an unusable recorded op" in m]), "entry_blocked": bool(C._ENTRY_BLOCKED), "entry_try": et,
           "n_pos_all": len(C.open_positions() or {}), "entry_first": et_first,
           "fz_logs": len([m for m in LOGS if "freeze event for" in m or "freeze event was NOT written" in m])}
    print("RESULT " + _json.dumps(res), flush=True)

def child_recover_kill(mult):
    PA = fresh()
    CI.mark_lane_applied = lambda lane, iid: _die()   # labelled injection: this RECOVERY process dies at the lane marker (the lane file is already written)
    for _ in range(3):
        C.poll_exits(price_fn=pf_mult(float(mult)))
    print("CHILD SURVIVED (lane-marker kill not reached)", flush=True); sys.exit(7)

if MODE == "run": child_run(sys.argv[3]); sys.exit(0)
if MODE == "recover_kill": child_recover_kill(sys.argv[3]); sys.exit(0)
if MODE == "recover": child_recover(sys.argv[3], (sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] != "-" else None), (sys.argv[5] if len(sys.argv) > 5 else None)); sys.exit(0)

# ---- parent
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n, flush=True)
    if not c: FAILS.append(n)
def spawn(args):
    p = subprocess.run([sys.executable, "-B", os.path.abspath(__file__)] + args, capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout, p.stderr
FOUR = ["intent", "account_applied", "lane_applied", "applied"]
HAS_HELPER = None
REFUSAL = "close-intent divergence frozen: entries blocked until operator resolution"   # the EXISTING production refusal text, unchanged
def recover(root, mult, inject=None, try_entry=False):
    rc, out, err = spawn(["recover", root, str(mult), inject or "-"] + ([try_entry if isinstance(try_entry, str) else "try"] if try_entry else []))
    rl = [l for l in out.splitlines() if l.startswith("RESULT ")]; pl = [l for l in out.splitlines() if l.startswith("PRE ")]
    return rc, err, (_json.loads(pl[0][4:]) if pl else None), (_json.loads(rl[0][7:]) if rl else None)
SCEN = (("S1", "after_credit", "2.0", "2.3", "leg", False), ("S2", "pre_lane_marker", "2.0", "2.3", "leg", False), ("S3-control", "after_credit", "2.0", "2.0", "leg", False),
        ("S4-remaining", "after_credit", "0.45", "0.4", "remaining", False), ("S6-second-restart-lane-marker-kill", "after_credit", "2.0", "2.3", "leg", True),
        ("S7-kill-after-begin-BEFORE-credit", "before_credit", "2.0", "2.3", "leg", False))
for tag, point, kmult, mult, kind, kill2 in SCEN:
    root = _tf.mkdtemp(prefix="nonsimkill-%s-" % tag)
    rc, out, err = spawn(["run", root, point, kmult])
    ec = [l for l in out.splitlines() if l.startswith("ENTRY")]
    check("%s: child KILLED by SIGKILL (rc %d, want -%d), kill point reached" % (tag, rc, _signal.SIGKILL), rc == -_signal.SIGKILL and "SURVIVED" not in out)
    check("%s: entry opened on the NON-DEMO path (%s)" % (tag, ec[:1]), bool(ec) and "ok=True" in ec[0] and "nonsim=True" in ec[0] and "pa_pos_id=True" in ec[0])
    entry_cash = float(ec[0].split("cash=")[1].split()[0]) if ec else None
    if kill2:
        rck, outk, errk = spawn(["recover_kill", root, mult])
        check("%s: the FIRST recovery process is itself SIGKILLed at the lane marker (rc %d, want -%d, survived %r)" % (tag, rck, _signal.SIGKILL, "SURVIVED" in outk), rck == -_signal.SIGKILL and "SURVIVED" not in outk)
    rc2, err2, pre, r = recover(root, mult)
    check("%s: recovery process exited 0 with PRE and RESULT (rc %d, stderr tail %r)" % (tag, rc2, err2[-160:]), rc2 == 0 and pre is not None and r is not None and "Traceback" not in err2)
    if pre is None or r is None: continue
    print("  %s PRE %s" % (tag, pre)); print("  %s RESULT %s" % (tag, r))
    credited = point != "before_credit"
    want_ev = ["intent"] if (point in ("after_credit", "before_credit") and not kill2) else None
    check("%s: durable state before the final recovery: journal status %r (want None), lifecycle %r%s, cash %.6f %s entry %.6f, recorded op %r amount %r fee %r qty %r reason %r"
          % (tag, pre["pre_status"], pre["pre_ev"], (" (want %r)" % want_ev) if want_ev else " (starts with intent)", pre["pre_cash"], ">" if credited else "==", entry_cash,
             pre["op_id"], pre["op_amount"], pre["op_fee"], pre.get("op_qty"), pre.get("op_reason")),
          pre["pre_status"] is None and (pre["pre_ev"] == want_ev if want_ev else pre["pre_ev"][:1] == ["intent"])
          and ((_cents(pre["pre_cash"]) > _cents(entry_cash)) if credited else (_cents(pre["pre_cash"]) == _cents(entry_cash)))
          and pre["op_amount"] is not None and str(pre["op_id"]).startswith("ceo:%s:" % ("leg:a" if kind == "leg" else "remaining")))
    check("%s: PROVENANCE (separate from the causal checks): the durable intent records a real trigger reason %r, not the op suffix" % (tag, pre.get("op_reason")),
          bool(pre.get("op_reason")) and not str(pre.get("op_reason")).startswith(("leg:", "remaining:")))
    rec_net = round(float(pre["op_amount"]) - float(pre["op_fee"] or 0.0), 6)
    if kind == "leg":
        check("%s: retry at %sx: leg a done %r, pending_exit %r (no hold loop)" % (tag, mult, r["a_done"], r["pend_why"]), r["a_done"] is True and not r["pend_why"])
        check("%s: credit booked ONCE at the RECORDED amounts: cash delta since entry %.6f == recorded net %.6f, lane leg realized %r == recorded net"
              % (tag, r["cash"] - entry_cash, rec_net, r["a_realized"]),
              _cents(r["cash"] - entry_cash) == _cents(rec_net) and r["a_realized"] is not None and abs(float(r["a_realized"]) - rec_net) < 1e-6)
        check("%s: PROVENANCE (separate): leg exit_reason %r == recorded close reason %r" % (tag, r["a_reason"], pre.get("op_reason")), r["a_reason"] == pre.get("op_reason"))
        done_ok = r["a_done"] is True
    else:
        check("%s: retry at %sx: the remaining close finished, position closed (n_pos %d), pending_exit %r (no hold loop)" % (tag, mult, r["n_pos"], r["pend_why"]), r["n_pos"] == 0 and not r["pend_why"])
        check("%s: credit booked ONCE at the RECORDED amounts: cash delta since entry %.6f == recorded net %.6f" % (tag, r["cash"] - entry_cash, rec_net), _cents(r["cash"] - entry_cash) == _cents(rec_net))
        done_ok = r["n_pos"] == 0
    check("%s: lifecycle %r (first four %r == %r), %d intent(s), pending %d, journal readable %r"
          % (tag, r["ev"], r["ev"][:4], FOUR, r["n_intents"], r["pending"], r["journal_status"]),
          r["ev"][:4] == FOUR and r["pending"] == 0 and r["journal_status"] is None and (kind != "leg" or r["n_intents"] == 1))
    rc3, err3, pre3, r3 = recover(root, mult)
    same = r3 is not None and all(r3[k] == r[k] for k in ("cash", "a_done", "a_realized", "ev", "pending", "n_pos", "n_intents", "journal_status"))
    check("%s: a SECOND fresh restart changes nothing (rc %d; cash %r -> %r, lifecycle %r -> %r) and the exit is complete (%r)"
          % (tag, rc3, r["cash"], (r3 or {}).get("cash"), r["ev"], (r3 or {}).get("ev"), done_ok), rc3 == 0 and same and done_ok)

# ---- fail-closed named injections (real SIGKILL-after-credit setup, then a recovery process with a labelled injection in that process only).
# They need the recorded-op helper: on a source without it (old code) they are SKIPPED and printed as SKIP, never counted as PASS.
HAS_HELPER = hasattr(C, "_recorded_credit_op")
FZ = (("F1-qty-mismatch", "qty_mismatch"), ("F2-amount-nan", "amount_nan"), ("F3-fee-negative", "fee_negative"), ("F4-amount-missing", "amount_missing"),
      ("F5-qty-near-tolerance-1e-10", "qty_near"), ("F6-price-kind-missing", "price_kind_missing"), ("F7-close-reason-missing", "reason_missing"))
for tag, inj in (FZ if HAS_HELPER else ()):
    root = _tf.mkdtemp(prefix="nonsimkill-%s-" % tag)
    rc, out, err = spawn(["run", root, "after_credit", "2.0"])
    check("%s: setup child KILLED by SIGKILL (rc %d)" % (tag, rc), rc == -_signal.SIGKILL and "SURVIVED" not in out)
    rc2, err2, pre, r = recover(root, "2.3", inj, True)
    check("%s: recovery exited 0, no crash trace in stderr (rc %d, stderr tail %r)" % (tag, rc2, err2[-200:]), rc2 == 0 and "Traceback" not in err2 and r is not None)
    if r is None or pre is None: continue
    print("  %s PRE %s" % (tag, pre)); print("  %s RESULT %s" % (tag, r))
    check("%s: the mutation REACHED the guard (hits %d >= 1) and the guard logged its refusal (guard_logs %d >= 1, once per refusal)" % (tag, r["hits"], r["guard_logs"]), r["hits"] >= 1 and r["guard_logs"] >= 1)
    check("%s: NO second credit (cash %.6f == pre-recovery cash %.6f), leg a NOT done (%r), position still open (n_pos %d)" % (tag, r["cash"], pre["pre_cash"], r["a_done"], r["n_pos"]),
          _cents(r["cash"]) == _cents(pre["pre_cash"]) and r["a_done"] is False and r["n_pos"] == 1)
    check("%s: the intent is FROZEN (lifecycle %r, frozen_open %d, pending %d), still exactly 1 intent, entries BLOCKED (in-process flag %r; entry attempt %r)"
          % (tag, r["ev"], r["frozen_open"], r["pending"], r["entry_blocked"], r["entry_try"]),
          "frozen" in r["ev"] and r["frozen_open"] == 1 and r["pending"] == 0 and r["n_intents"] == 1 and "lane_applied" not in r["ev"]
          and r["entry_blocked"] is True and REFUSAL in str(r["entry_try"]) and r["n_pos_all"] == 1)
    rc3, err3, pre3, r3 = recover(root, "2.3", None, True)
    check("%s: after a FRESH restart (no injection) the frozen intent still BLOCKS entries and nothing is credited or closed (rc %d; frozen_open %r, blocked %r, entry %r, cash %r)"
          % (tag, rc3, (r3 or {}).get("frozen_open"), (r3 or {}).get("entry_blocked"), (r3 or {}).get("entry_try"), (r3 or {}).get("cash")),
          rc3 == 0 and r3 is not None and r3["frozen_open"] == 1 and r3["entry_blocked"] is True and REFUSAL in str(r3["entry_try"])
          and _cents(r3["cash"]) == _cents(pre["pre_cash"]) and r3["a_done"] is False and r3["n_pos_all"] == 1)

if HAS_HELPER:
    # different trigger: recorded close reason wins over the retry's new trigger reason
    tag = "F8-different-trigger-keeps-recorded-reason"
    root = _tf.mkdtemp(prefix="nonsimkill-%s-" % tag)
    rc, out, err = spawn(["run", root, "after_credit", "2.0"])
    check("%s: setup child KILLED by SIGKILL (rc %d)" % (tag, rc), rc == -_signal.SIGKILL and "SURVIVED" not in out)
    rc2, err2, pre, r = recover(root, "2.3", "different_trigger")
    check("%s: recovery exited 0 and leg a done" % tag, rc2 == 0 and r is not None and r["a_done"] is True)
    if r is not None and pre is not None:
        print("  %s PRE %s" % (tag, pre)); print("  %s RESULT %s" % (tag, r))
        check("%s: leg exit_reason %r is the RECORDED close reason %r, not the new trigger 'operator_probe_other_trigger'" % (tag, r["a_reason"], pre.get("op_reason")),
              r["a_reason"] == pre.get("op_reason") and r["a_reason"] != "operator_probe_other_trigger")

# F10: durable defect + the freeze write FAILS: the block must come from DURABLE recognition (journal + ledger), not from the freeze event
tag = "F10-durable-defect-freeze-write-fails"
root = _tf.mkdtemp(prefix="nonsimkill-%s-" % tag)
rc, out, err = spawn(["run", root, "after_credit_nopk", "2.0"])
check("%s: setup child (intent journaled WITHOUT price_kind, labelled) KILLED by SIGKILL (rc %d)" % (tag, rc), rc == -_signal.SIGKILL and "SURVIVED" not in out)
rc2, err2, pre, r = recover(root, "2.3", "freeze_false", True)
check("%s: recovery exited 0, no crash trace in stderr (rc %d, stderr tail %r)" % (tag, rc2, err2[-200:]), rc2 == 0 and "Traceback" not in err2 and r is not None)
if r is not None and pre is not None:
    print("  %s PRE %s" % (tag, pre)); print("  %s RESULT %s" % (tag, r))
    check("%s: freeze was ATTEMPTED (%d calls) and NOT durable (lifecycle %r, frozen_open %d), restart recognition logged (%d)" % (tag, r["freeze_calls"], r["ev"], r["frozen_open"], r["rec_logs"]),
          r["freeze_calls"] >= 1 and "frozen" not in r["ev"] and r["frozen_open"] == 0 and r["rec_logs"] >= 1)
    check("%s: entries BLOCKED anyway (flag %r, entry attempt %r); no second credit (cash %.6f vs %.6f), leg a not done (%r), n_pos %d"
          % (tag, r["entry_blocked"], r["entry_try"], r["cash"], pre["pre_cash"], r["a_done"], r["n_pos_all"]),
          r["entry_blocked"] is True and REFUSAL in str(r["entry_try"]) and _cents(r["cash"]) == _cents(pre["pre_cash"]) and r["a_done"] is False and r["n_pos_all"] == 1)
    rc3, err3, pre3, r3 = recover(root, "2.3", "freeze_false", True)
    check("%s: FRESH restart, freeze STILL failing: entries still blocked from the durable data alone (rc %d; frozen_open %r, ev %r, entry %r, cash %r)"
          % (tag, rc3, (r3 or {}).get("frozen_open"), (r3 or {}).get("ev"), (r3 or {}).get("entry_try"), (r3 or {}).get("cash")),
          rc3 == 0 and r3 is not None and r3["frozen_open"] == 0 and "frozen" not in r3["ev"] and REFUSAL in str(r3["entry_try"])
          and _cents(r3["cash"]) == _cents(pre["pre_cash"]) and r3["n_pos_all"] == 1)
    check("%s: that fresh restart HIT the recognition (rec_logs %r >= 1) and the failed freeze was logged truthfully (freeze_calls %r >= 1, fz_logs %r >= 1)"
          % (tag, (r3 or {}).get("rec_logs"), (r3 or {}).get("freeze_calls"), (r3 or {}).get("fz_logs")),
          r3 is not None and r3["rec_logs"] >= 1 and r3["freeze_calls"] >= 1 and r3["fz_logs"] >= 1)
    # F11: ordering: in a fresh process the entry is attempted BEFORE any exit retry
    tag = "F11-entry-attempted-BEFORE-first-exit-retry"
    rc4, err4, pre4, r4 = recover(root, "2.3", "freeze_false", "tryfirst")
    check("%s: refused BEFORE any exit poll (entry %r), then no credit/close (cash %r vs %r, leg a done %r, n_pos %r), recognition hit (%r)"
          % (tag, (r4 or {}).get("entry_first"), (r4 or {}).get("cash"), pre["pre_cash"], (r4 or {}).get("a_done"), (r4 or {}).get("n_pos_all"), (r4 or {}).get("rec_logs")),
          rc4 == 0 and r4 is not None and REFUSAL in str(r4["entry_first"]) and _cents(r4["cash"]) == _cents(pre["pre_cash"]) and r4["a_done"] is False
          and r4["n_pos_all"] == 1 and r4["rec_logs"] >= 1)
    rc5, err5, pre5, r5 = recover(root, "2.3", "freeze_false", "tryfirst")
    check("%s: SECOND fresh process, entry again BEFORE any exit poll: refused (%r), recognition hit (%r), freeze attempted but still not durable (calls %r, ev %r), no credit (cash %r)"
          % (tag, (r5 or {}).get("entry_first"), (r5 or {}).get("rec_logs"), (r5 or {}).get("freeze_calls"), (r5 or {}).get("ev"), (r5 or {}).get("cash")),
          rc5 == 0 and r5 is not None and REFUSAL in str(r5["entry_first"]) and r5["rec_logs"] >= 1 and r5["freeze_calls"] >= 1 and "frozen" not in r5["ev"]
          and _cents(r5["cash"]) == _cents(pre["pre_cash"]) and r5["a_done"] is False)

# F13: durable-defect matrix, entry attempted BEFORE any exit poll in TWO fresh processes with the freeze write failing, for leg and remaining kinds
for kind13, kmult13, ret13 in (("leg", "2.0", "2.3"), ("remaining", "0.45", "0.4")):
    for dfx in ("nopk", "noreason", "qtyhalf"):
        tag = "F13-%s-%s-entry-first-freezeFalse" % (kind13, dfx)
        root = _tf.mkdtemp(prefix="nonsimkill-%s-" % tag)
        rc, out, err = spawn(["run", root, "after_credit_" + dfx, kmult13])
        check("%s: setup (real durable defect, labelled) KILLED by SIGKILL (rc %d)" % (tag, rc), rc == -_signal.SIGKILL and "SURVIVED" not in out)
        ra, ea, pa_, rr1 = recover(root, ret13, "freeze_false", "tryfirst")
        rb, eb, pb_, rr2 = recover(root, ret13, "freeze_false", "tryfirst")
        for nn, (rcx, errx, prx, rx) in (("fresh #1", (ra, ea, pa_, rr1)), ("fresh #2", (rb, eb, pb_, rr2))):
            check("%s %s: entry BEFORE any exit poll refused (%r); recognition hit (%r), freeze attempted (%r) not durable (ev %r); no extra credit (cash %r vs pre %r); position/leg not closed (n_pos %r, a_done %r)"
                  % (tag, nn, (rx or {}).get("entry_first"), (rx or {}).get("rec_logs"), (rx or {}).get("freeze_calls"), (rx or {}).get("ev"), (rx or {}).get("cash"), (prx or {}).get("pre_cash"), (rx or {}).get("n_pos_all"), (rx or {}).get("a_done")),
                  rcx == 0 and rx is not None and REFUSAL in str(rx["entry_first"]) and rx["rec_logs"] >= 1 and rx["freeze_calls"] >= 1 and "frozen" not in rx["ev"]
                  and _cents(rx["cash"]) == _cents(prx["pre_cash"]) and rx["n_pos_all"] == 1 and rx["a_done"] is False)

# F12: foreign pa_pos_id (the open position is now a different instance than the pending intent's): the existing lane_apply False -> freeze contract
tag = "F12-foreign-pa_pos_id"
root = _tf.mkdtemp(prefix="nonsimkill-%s-" % tag)
rc, out, err = spawn(["run", root, "after_credit", "2.0"])
check("%s: setup child KILLED by SIGKILL (rc %d)" % (tag, rc), rc == -_signal.SIGKILL and "SURVIVED" not in out)
rc2, err2, pre, r = recover(root, "2.3", "foreign_pos_id", "tryfirst")
check("%s: recovery exited 0, no crash trace in stderr (rc %d, stderr tail %r)" % (tag, rc2, err2[-200:]), rc2 == 0 and "Traceback" not in err2 and r is not None)
if r is not None and pre is not None:
    print("  %s PRE %s" % (tag, pre)); print("  %s RESULT %s" % (tag, r))
    # EXISTING contract (unchanged): the lane's own settle is never blocked by an account failure. After the poll the foreign position's leg a is booked in the lane,
    # its NEW intent's account credit is refused (the fixture's foreign pa_pos_id has no account position), so that second intent is FROZEN: divergence visible, entries blocked.
    check("%s: entry BEFORE any exit poll refused (%r); the pending intent is FROZEN first (ev %r, exactly 2 intents %r, both frozen); no credit for either (cash %r vs %r); the lane booked the foreign leg a (%r) while its new intent is frozen, not silently applied"
          % (tag, r["entry_first"], r["ev"], r["n_intents"], r["cash"], pre["pre_cash"], r["a_done"]),
          REFUSAL in str(r["entry_first"]) and r["ev"] == ["intent", "account_applied", "frozen", "intent", "frozen"] and r["n_intents"] == 2
          and _cents(r["cash"]) == _cents(pre["pre_cash"]) and r["a_done"] is True and r["pending"] == 0 and r["entry_blocked"] is True)
    rc3, err3, pre3, r3 = recover(root, "2.3", None, "tryfirst")
    check("%s: FRESH process: the persisted frozen intent blocks entry BEFORE any exit poll (%r), still the same two frozen intents, no new events (ev %r), nothing credited (cash %r)" % (tag, r3 and r3["entry_first"], r3 and r3["ev"], r3 and r3["cash"]),
          rc3 == 0 and r3 is not None and REFUSAL in str(r3["entry_first"]) and r3["ev"] == r["ev"] and r3["n_intents"] == 2 and _cents(r3["cash"]) == _cents(pre["pre_cash"]) and r3["a_done"] is False)

# H1: HEALTHY positive control (documents the CURRENT admission behavior; it is not a policy claim): the same real SIGKILL-after-credit pending intent with a
# VALID recorded op. In a fresh process a new entry attempted BEFORE any exit poll is NOT refused with the divergence text, no recognition or freeze ran, and the
# exit then completes at the recorded amounts. This isolates the refusal text of the F-cases to the recognition/frozen branches.
tag = "H1-healthy-pending-admission-control"
root = _tf.mkdtemp(prefix="nonsimkill-%s-" % tag)
rc, out, err = spawn(["run", root, "after_credit", "2.0"])
check("%s: setup child KILLED by SIGKILL (rc %d)" % (tag, rc), rc == -_signal.SIGKILL and "SURVIVED" not in out)
rc2, err2, pre, r = recover(root, "2.3", None, "tryfirst")
if r is not None and pre is not None:
    print("  %s PRE %s" % (tag, pre)); print("  %s RESULT %s" % (tag, r))
    check("%s: entry BEFORE any exit poll is NOT refused with the divergence text (entry %r), no recognition hit (%r), no freeze call (%r), nothing frozen (ev %r)"
          % (tag, r["entry_first"], r["rec_logs"], r["freeze_calls"], r["ev"]),
          rc2 == 0 and REFUSAL not in str(r["entry_first"]) and r["rec_logs"] == 0 and r["freeze_calls"] == 0 and "frozen" not in r["ev"])
    check("%s: the pending exit then completes once at the recorded amounts (leg a done %r, lifecycle %r, cash %r)" % (tag, r["a_done"], r["ev"][:4], r["cash"]),
          r["a_done"] is True and r["ev"][:4] == FOUR)
else:
    check("%s: recovery produced PRE and RESULT (rc %d, stderr tail %r)" % (tag, rc2, err2[-160:]), False)

# journal corrupt mid-file: sentinel/entry block + hold classification read in the child
tag = "F9-journal-corrupt"
root = _tf.mkdtemp(prefix="nonsimkill-%s-" % tag)
rc, out, err = spawn(["run", root, "after_credit", "2.0"])
check("%s: setup child KILLED by SIGKILL (rc %d)" % (tag, rc), rc == -_signal.SIGKILL and "SURVIVED" not in out)
rc2, err2, pre, r = recover(root, "2.3", "journal_corrupt", True)
check("%s: recovery exited 0, no crash trace in stderr (rc %d, stderr tail %r)" % (tag, rc2, err2[-200:]), rc2 == 0 and "Traceback" not in err2 and r is not None)
if r is not None and pre is not None:
    print("  %s PRE %s" % (tag, pre)); print("  %s RESULT %s" % (tag, r))
    check("%s: NO second credit (cash %.6f vs pre %.6f), leg a NOT done (%r), position open (n_pos %d), no lane_applied/applied written (%r)" % (tag, r["cash"], pre["pre_cash"], r["a_done"], r["n_pos"], r["ev"]),
          _cents(r["cash"]) == _cents(pre["pre_cash"]) and r["a_done"] is False and r["n_pos"] == 1 and "lane_applied" not in r["ev"] and "applied" not in r["ev"])
    check("%s: journal reported corrupt (status %r), entries BLOCKED (flag %r, entry attempt %r), exit held visibly (pending_exit %r)" % (tag, r["journal_status"], r["entry_blocked"], r["entry_try"], r["pend_why"]),
          r["journal_status"] is not None and r["entry_blocked"] is True and REFUSAL in str(r["entry_try"]) and bool(r["pend_why"]) and r["n_pos_all"] == 1)
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
