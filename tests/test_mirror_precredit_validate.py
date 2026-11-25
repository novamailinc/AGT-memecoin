#!/usr/bin/env python3
"""PRE-APPLY VALIDATION regression for the MIRROR lane (bb50). ASSERTING (not a characterisation).
Contract (engineering correctness inside the build request; NO owner economic policy is selected here): a RECORDED close-intent credit op that is defective
(price_kind missing / quantity not the exit quantity / close reason missing) must NOT mutate the simulated account in shared recovery. It is frozen and entries are
blocked (existing freeze + entry-block contract, directive Section 23: stop entries when execution status is uncertain); nothing is liquidated, compensated or
rewritten. A healthy recorded op is still applied exactly once (positive control). Real separate-process SIGKILL (labelled injection: before PA.credit, or at
CI.mark_account_applied), REAL mirror lane, REAL paper account, REAL close-intent journal in a scratch root, canned feed (engineering fixtures, not provider acceptance).
Scenarios (each: kill, then ONE fresh process opens a NEW entry FIRST with zero polls; the recovery runs inside that entry's startup gate):
 P0 healthy control (valid op, killed before credit): applied once, entry admitted, no freeze.
 P1 price_kind missing / P2 half quantity / P3 close reason missing (killed before credit): account NOT mutated, frozen, entries blocked.
 P4 = P1 plus a failing freeze write (double fault, labelled injection): account NOT mutated, entries still blocked from the durable record.
 P5 = P1 with the ledger position ABSENT at recovery / P6 = P1 with the position of ANOTHER instance (labelled ledger edit in the recovery scratch root): the op is not waved
    through as valid: its own fields are judged.
 P7 defective op killed AFTER the credit and before the applied mark (credit-before-mark): the credit is already durable (account op evidence); no second credit, frozen and
    blocked, nothing compensated.
EXPECTED on bb49 source (no pre-apply validation): P0 green; P1-P3, P5, P6 red by assertion (credited before freeze); P4 red; P7 asserts no second credit and the freeze/block
(may be green or red, reported as found). On the bb50 source all 78 checks expected green (hand count: P0 9, P1-P3 and P5-P6 10 each, P4 10, P7 9; UNVERIFIED). NOT run.
Run: python3 -B tests/test_mirror_precredit_validate.py"""
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
LANES = ("mirror",)
if LANE_ARG:
    from daemon.launch_scanner import mirror as _Mi
    L = {"mirror": _Mi}[LANE_ARG]
    LANE_NAME = "degen_mirror"   # pos["lane"] for solana (mirror.LANE_BY_CHAIN)
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
    return L.mirror_card(chain="solana", source="test", mint=MINT, symbol="TKN", score=70.0, tier="pick", mcap=500_000.0, liquidity=50_000.0)
def fresh(start="10"):
    h = os.path.join(ROOTDIR, "home"); os.makedirs(h, exist_ok=True); os.environ["FOREX_AGENT_HOME"] = h
    for k in list(os.environ):
        if k.startswith("PAPER_ACCOUNT"): os.environ.pop(k)
    os.environ["PAPER_ACCOUNT_ENABLED"] = "1"; os.environ["PAPER_ACCOUNT_START"] = start
    import daemon.launch_scanner.paper_account as PA
    PA = importlib.reload(PA); G.install(PA)
    led = os.path.join(ROOTDIR, "led"); os.makedirs(led, exist_ok=True)
    L.LEDGER_PATH = os.path.join(led, LANE_ARG + ".json")
    L._LOCK_PATH = os.path.join(led, LANE_ARG + ".lock")   # module constant fixed at import: redirect to scratch
    L._fresh_tape = lambda chain, mint: _tape()
    L.quote_price = G.stamped_const(L, 0.01)
    L.RUNNER_CONFIRM_GAP_S = 0.0
    L._safety = _Safe()
    L._veto = _Veto()
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
def _acct_qty(PA):
    return [p.get("qty") for p in ((PA.snapshot() or {}).get("lanes", {}).get(LANE_NAME, {}).get("positions") or []) if p.get("mint") == MINT]

def child_run(mult, defect=None, kill_point=None):
    PA = fresh(); r = _enter()
    pos = (L.open_positions() or {}).get(MINT) or {}
    if not (r[0] and pos.get("pa_pos_id")):
        print("ENTRY-REFUSED lane=%s ok=%s reason=%r pos_present=%s pa_pos_id=%s ledger=%s" % (LANE_ARG, bool(r[0]), r[1], bool(pos), bool(pos.get("pa_pos_id")), L.LEDGER_PATH), flush=True)
        sys.exit(5)
    print("ENTRY lane=%s ok=%s cash=%.6f nonsim=%s pa_pos_id=%s why=%r" % (LANE_ARG, bool(r[0]), cash(PA), (pos.get("execution") or {}).get("source") != "jupiter_quote_sim", bool(pos.get("pa_pos_id")), ""), flush=True)
    # labelled DEFECT injections: they corrupt what the lane RECORDS in the durable intent (the account then sees exactly that record)
    if defect == "nopk":
        _o = L._pa_settle; L._pa_settle = lambda pos, qty, gross, fee, suffix, price_kind=None: _o(pos, qty, gross, fee, suffix, None)
    elif defect == "qtyhalf":
        _o = L._pa_settle; L._pa_settle = lambda pos, qty, gross, fee, suffix, price_kind=None: _o(pos, qty * 0.5, gross, fee, suffix, price_kind)
    elif defect == "noreason":
        _b = CI.begin; CI.begin = lambda lane, mint, pid, ops, info, *a, **k: _b(lane, mint, pid, ops, dict(info, closed_reason=None), *a, **k)
    if kill_point == "before_credit":
        PA.credit = lambda *a, **k: _die()   # labelled injection point: real SIGKILL right BEFORE the shared account credit (op never applied)
    else:
        CI.mark_account_applied = lambda lane, iid: _die()   # labelled injection point: real SIGKILL right after the durable credit
    L.poll_exits(price_fn=pf_mult(float(mult)))
    print("CHILD SURVIVED (kill point not reached)", flush=True); sys.exit(7)

def child_entry_first(inject=None, ledger_edit=None):
    PA = fresh()
    _logging.getLogger().addHandler(_H()); _logging.getLogger().setLevel(_logging.WARNING)
    if ledger_edit:   # labelled ledger edit in the SCRATCH root only
        d = L._read(); pos = d["positions"][MINT]
        if ledger_edit == "absent": del d["positions"][MINT]
        else: pos["pa_pos_id"] = "foreign-instance-id"
        L._write(d)
    if inject == "freeze_false":
        def _fz(lane, iid, why):
            HITS["n"] += 1
            return False   # labelled injection: the freeze write FAILS
        CI.freeze = _fz
    cnt = {"poll_exits": 0}
    _pe = L.poll_exits
    def _pe2(*a, **k):
        cnt["poll_exits"] += 1; return _pe(*a, **k)
    L.poll_exits = _pe2
    ents0, st0, _t = CI._read(CI.journal_path(LANE_NAME))
    i0 = [e for e in ents0 if e.get("ev") == "intent"]
    op0 = ((i0[0].get("settle_ops") or [{}])[0]) if i0 else {}
    pre = {"pre_cash": cash(PA), "pre_ev": [e.get("ev") for e in ents0 if e.get("intent_id")], "n_pre": len(i0), "op_id": op0.get("op_id"), "pre_acct_qty": _acct_qty(PA)}
    print("PRE " + _json.dumps(pre), flush=True)
    _e = _enter("OtherLaneMint2")   # NEW entry FIRST; no poll_exits anywhere in this process
    ents, st, _ = CI._read(CI.journal_path(LANE_NAME))
    res = {"cash": cash(PA), "journal_status": st, "ev": [e.get("ev") for e in ents if e.get("intent_id")], "pending": len(CI.pending(LANE_NAME)), "frozen_open": _frozen_open_n(),
           "entry_blocked": bool(L._ENTRY_BLOCKED), "entry_try": repr(_e)[:300], "entry_opened": bool(_e[0]), "op_outcome": PA.op_outcome(pre["op_id"]) if pre["op_id"] else None,
           "acct_qty": _acct_qty(PA), "freeze_attempts": HITS["n"], "notwritten_logs": len([m for m in LOGS if "NOT written" in m]), "cnt": cnt}
    print("RESULT " + _json.dumps(res), flush=True)

if MODE == "run": child_run(sys.argv[4], (sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] != "-" else None), (sys.argv[6] if len(sys.argv) > 6 else None)); sys.exit(0)
if MODE == "entryfirst": child_entry_first((sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] != "-" else None), (sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] != "-" else None)); sys.exit(0)

FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n, flush=True)
    if not c: FAILS.append(n)
def spawn(args):
    p = subprocess.run([sys.executable, "-B", os.path.abspath(__file__)] + args, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr
REFUSAL = "close-intent divergence frozen: entries blocked until operator resolution"   # existing production refusal text
lane = "mirror"
# tag, defect, kill_point, inject, ledger_edit, kind
SCEN = (("P0-healthy-control", None, "before_credit", None, None, "healthy"),
        ("P1-price-kind-missing", "nopk", "before_credit", None, None, "defect"),
        ("P2-half-qty", "qtyhalf", "before_credit", None, None, "defect"),
        ("P3-close-reason-missing", "noreason", "before_credit", None, None, "defect"),
        ("P4-price-kind-missing-plus-failed-freeze", "nopk", "before_credit", "freeze_false", None, "doublefault"),
        ("P5-price-kind-missing-position-absent", "nopk", "before_credit", None, "absent", "defect"),
        ("P6-price-kind-missing-foreign-instance", "nopk", "before_credit", None, "foreign", "defect"),
        ("P7-price-kind-missing-killed-after-credit", "nopk", None, None, None, "aftercredit"))
for dtag, defect, kp, inject, ledger_edit, kind in SCEN:
    tag = "%s-%s" % (dtag, lane)
    root = _tf.mkdtemp(prefix="mirror-pc-%s-" % tag)
    rc, out, err = spawn(["run", root, lane, "2.0", defect or "-"] + ([kp] if kp else []))
    _el0 = [l for l in out.splitlines() if l.startswith("ENTRY ")]; _rf = [l for l in out.splitlines() if l.startswith("ENTRY-REFUSED")]
    _entry_ok = bool(_el0) and "ok=True" in _el0[0] and "nonsim=True" in _el0[0] and "pa_pos_id=True" in _el0[0] and not _rf and "Traceback" not in err
    check("%s: SETUP: mirror entry OPENED non-sim with a pinned position instance (%s%s; rc %d; stderr tail %r)" % (tag, _el0[:1], _rf[:1], rc, err[-160:]), _entry_ok)
    check("%s: child KILLED by SIGKILL (rc %d, want -9), KILLHIT reached, %s" % (tag, rc, kp or "after the credit"), rc == -_signal.SIGKILL and "SURVIVED" not in out and "KILLHIT lane=%s" % lane in out)
    if not _entry_ok: continue
    entry_cash = float(_el0[0].split("cash=")[1].split()[0])
    rc2, out2, err2 = spawn(["entryfirst", root, lane, inject or "-", ledger_edit or "-"])
    pl = [l for l in out2.splitlines() if l.startswith("PRE ")]; rl = [l for l in out2.splitlines() if l.startswith("RESULT ")]
    pre = _json.loads(pl[0][4:]) if pl else None; r = _json.loads(rl[0][7:]) if rl else None
    check("%s: entry-first recovery process exited 0, no crash trace (rc %d, stderr tail %r)" % (tag, rc2, err2[-160:]), rc2 == 0 and pre is not None and r is not None and "Traceback" not in err2)
    if pre is None or r is None: continue
    print("  RAW %s PRE %s RESULT %s" % (tag, pre, r), flush=True)
    if kind == "aftercredit":
        check("%s: durable pre-state: [intent] only, the credit ALREADY in cash (%r > entry cash %r), op account evidence 'applied' is expected later" % (tag, pre["pre_cash"], entry_cash), pre["pre_ev"] == ["intent"] and pre["pre_cash"] > entry_cash + 1e-9)
    else:
        check("%s: durable pre-state: [intent] only, cash == entry cash (op NEVER applied): %r == %r" % (tag, pre["pre_cash"], entry_cash), pre["pre_ev"] == ["intent"] and _cents(pre["pre_cash"]) == _cents(entry_cash))
    check("%s: zero polls in the recovery process (%d)" % (tag, r["cnt"]["poll_exits"]), r["cnt"]["poll_exits"] == 0)
    if kind == "healthy":
        check("%s: valid recorded op applied EXACTLY once by recovery (op_outcome %r, account qty %r -> %r)" % (tag, r["op_outcome"], pre["pre_acct_qty"], r["acct_qty"]),
              r["op_outcome"] == "applied" and r["acct_qty"] and pre["pre_acct_qty"] and r["acct_qty"][0] < pre["pre_acct_qty"][0] - 1e-9)
        check("%s: not frozen, entries NOT blocked, the new entry admitted (frozen_open %d, blocked %r, opened %r)" % (tag, r["frozen_open"], r["entry_blocked"], r["entry_opened"]), r["frozen_open"] == 0 and r["entry_blocked"] is False and r["entry_opened"] is True)
        check("%s: lifecycle starts [intent, account_applied] (%r)" % (tag, r["ev"][:2]), r["ev"][:2] == ["intent", "account_applied"])
        check("%s: no refusal text in the entry result" % tag, REFUSAL not in r["entry_try"])
    else:
        if kind == "aftercredit":
            check("%s: NO SECOND credit: cash unchanged by recovery (%r vs %r) and account qty unchanged (%r vs %r); op_outcome %r (the pre-existing credit)" % (tag, r["cash"], pre["pre_cash"], pre["pre_acct_qty"], r["acct_qty"], r["op_outcome"]),
                  _cents(r["cash"]) == _cents(pre["pre_cash"]) and r["acct_qty"] == pre["pre_acct_qty"] and r["op_outcome"] == "applied")
        else:
            check("%s: the defective record did NOT mutate the account: cash unchanged (%r vs %r), account qty unchanged (%r vs %r)" % (tag, r["cash"], pre["pre_cash"], pre["pre_acct_qty"], r["acct_qty"]),
                  _cents(r["cash"]) == _cents(pre["pre_cash"]) and r["acct_qty"] == pre["pre_acct_qty"])
            check("%s: account op evidence: the op was NEVER applied (op_outcome %r)" % (tag, r["op_outcome"]), r["op_outcome"] != "applied")
        _fzexp = 0 if kind == "doublefault" else 1
        check("%s: entries BLOCKED, new entry refused with the production refusal (blocked %r, opened %r, entry %r)" % (tag, r["entry_blocked"], r["entry_opened"], r["entry_try"][:80]), r["entry_blocked"] is True and r["entry_opened"] is False and REFUSAL in r["entry_try"])
        if kind == "doublefault":
            check("%s: failed freeze write: no durable freeze (frozen_open %d), the freeze was attempted (%d) and the log says NOT written (%d)" % (tag, r["frozen_open"], r["freeze_attempts"], r["notwritten_logs"]), r["frozen_open"] == 0 and r["freeze_attempts"] >= 1 and r["notwritten_logs"] >= 1)
        else:
            check("%s: intent durably FROZEN, one open frozen (frozen_open %d)" % (tag, r["frozen_open"]), r["frozen_open"] == _fzexp)
        check("%s: lifecycle %r: %s" % (tag, r["ev"], "no account_applied (never applied)" if kind != "aftercredit" else "intent then frozen, never applied/lane_applied"),
              ("account_applied" not in r["ev"] and "applied" not in r["ev"]) if kind != "aftercredit" else ("applied" not in r["ev"] and "lane_applied" not in r["ev"]))
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
