#!/usr/bin/env python3
"""bb78 SUBSEQUENT-CYCLE / RESTART DISPOSITION harness, MOMENTUM, SOURCE ONLY. Reuses the bb77 preamble and fault (RLIMIT_FSIZE=0 around the real PA.credit). After the faulted settle it observes the lane ledger, intent journal and account WAL, then a second real poll_exits cycle in the same process and a fresh-process restart, asserting INVARIANTS (no account credit, no debit, no rebinding, one obligation, position open, intent unapplied) and never inventing convergence. See rows in the parent section."""
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
def child_open():
    PA = fresh(); cash(PA)
    r = _enter(MINT); p = (L.open_positions() or {}).get(MINT) or {}
    ad = _acct_dir(ROOTDIR); hd = os.path.join(ROOTDIR, "home")
    print("RESULT " + _json.dumps({"ok": bool(r[0]), "reason": str(r[1])[:160], "pa_pos_id": p.get("pa_pos_id"), "pa_account_id": p.get("pa_account_id"), "pa_entry_op": p.get("pa_entry_op"),
          "registry_primary": os.path.exists(os.path.join(ad, "lane_registry.jsonl")), "registry_mirror": os.path.exists(os.path.join(hd, "lane_registry.paper_account.mirror.jsonl"))}, default=str), flush=True)
def child_probe(order, mutant):
    PA = fresh()
    if mutant == "nullreg": PA._registry_blockers = lambda: []
    out = {}
    def direct():
        before = len([e for e in _jl(ROOTDIR)[0] if e.get("event") == "debit"])
        try: r = PA.try_debit(LANE_NAME, "DirectProbeMint", 1.0, 1000.0, "probe:direct:1")
        except Exception as e: r = (False, "EXC %s" % type(e).__name__)
        after = len([e for e in _jl(ROOTDIR)[0] if e.get("event") == "debit"])
        out["direct_ok"] = bool(r[0]); out["direct_why"] = str(r[1])[:300]; out["direct_new_debits"] = after - before
    def lane():
        L._RECOVERY_DONE = False; L._ENTRY_BLOCKED = False
        out["lane_blocked"] = bool(L._recover_close_intents()); e = _enter(MINT2)
        out["lane_entry_ok"] = bool(e[0]); out["lane_entry_reason"] = str(e[1])[:200]
    if order == "direct_first": direct(); lane()
    else: lane(); direct()
    print("RESULT " + _json.dumps(out, default=str), flush=True)
def _fault_core(mutant=""):
    """ROOT-SAFE real OS write failure: RLIMIT_FSIZE=0 (SIGXFSZ ignored) is set ONLY around the real PA.credit call, in this child only. Every real write that grows a
    file then fails in the kernel with EFBIG (root is not exempt). No permission, mount, disk, identity or host change; the limit dies with the process and is restored in
    a finally. Failures are OBSERVED as real OSError(EFBIG) captured from the production code's own logged exc_info and by an in-window write probe."""
    import resource, errno, logging
    PA = fresh(); cash(PA)
    if mutant == "nofreeze": CI.freeze = lambda *a, **k: True   # MUTANT: the old behavior, a freeze that claims success and writes nothing
    # the real account load creates the NEW generation (the parent removed the old account files)
    ad = _acct_dir(ROOTDIR); out = {"euid": os.geteuid()}
    hits = {"refuse": 0, "credit": 0}; errs = []
    class _H(logging.Handler):
        def emit(self, rec):
            ei = rec.exc_info
            if ei and isinstance(ei[1], OSError): errs.append((rec.getMessage()[:80], ei[1].errno))
    h = _H(); lg = _logging.getLogger("daemon.launch_scanner.paper_account"); lg.addHandler(h)
    _rg = PA._refuse_generation
    def _rg2(*a, **k):
        hits["refuse"] += 1; return _rg(*a, **k)
    PA._refuse_generation = _rg2
    _cr = PA.credit; soft_hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    _signal.signal(_signal.SIGXFSZ, _signal.SIG_IGN)
    def _cr2(*a, **k):
        hits["credit"] += 1
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, (0, soft_hard[1]))
            try:
                with open(os.path.join(ROOTDIR, "fsize_probe.tmp"), "w") as f: f.write("x"); f.flush()
                out["write_probe_failed"] = False
            except OSError as e: out["write_probe_failed"] = (e.errno == errno.EFBIG)
            return _cr(*a, **k)
        finally:
            resource.setrlimit(resource.RLIMIT_FSIZE, soft_hard)
    PA.credit = _cr2
    def _pf(chain, mint, spot_index=None):
        pos = (L.open_positions() or {}).get(mint) or {}
        return float(pos.get("entry_price") or 0) * 2.0
    try: L.poll_exits(price_fn=_pf); out["poll_exc"] = None
    except Exception as e: out["poll_exc"] = type(e).__name__
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, soft_hard)
    lg.removeHandler(h); PA.credit = _cr
    out["credit_called"] = hits["credit"]; out["refuse_reached"] = hits["refuse"]
    out["efbig_errors"] = sorted(set(m for m, e in errs if e == errno.EFBIG)); out["n_efbig"] = len([1 for m, e in errs if e == errno.EFBIG])
    mp = os.path.join(ad, "unresolved_generation_refusals.jsonl")
    out["marker_records"] = len([1 for ln in open(mp) if ln.strip()]) if os.path.exists(mp) else 0
    evs = _jl(ROOTDIR)[0]
    out["refusal_journal_records"] = len([e for e in evs if e.get("event") == "settle_generation_refused"])
    out["credit_events"] = len([e for e in evs if e.get("event") == "credit"])
    try: r = PA.try_debit(LANE_NAME, "PostFaultMint", 1.0, 1000.0, "probe:postfault:1")
    except Exception as e: r = (False, "EXC %s" % type(e).__name__)
    out["postfault_direct_ok"] = bool(r[0]); out["postfault_direct_why"] = str(r[1])[:300]
    return PA, out
def _snap(PA):
    """State fingerprint from the persisted files (not from in-memory objects)."""
    d = {}
    try: led = _json.load(open(L.LEDGER_PATH)); pos = (led.get("positions") or {}).get(MINT) or {}
    except Exception as e: pos = {"_err": type(e).__name__}
    d["pos_open"] = bool(pos) and "_err" not in pos
    d["legs_done"] = {k: bool((pos.get("legs") or {}).get(k, {}).get("done")) for k in ("a", "b", "c")} if d["pos_open"] else None
    d["pa_account_id"] = pos.get("pa_account_id"); d["pa_pos_id"] = pos.get("pa_pos_id"); d["tokens"] = pos.get("tokens")
    d["n_positions"] = len((led.get("positions") or {})) if d["pos_open"] else None
    jp = CI.journal_path(LANE_NAME)
    try: raw = open(jp, "rb").read()
    except OSError: raw = b""
    d["journal_sha"] = hashlib.sha256(raw).hexdigest()[:16]; d["journal_len"] = len(raw)
    ents = [_json.loads(x) for x in raw.decode("utf-8", "replace").splitlines() if x.strip().startswith("{")]
    d["intent_ops"] = sorted(set(op.get("op_id") for e in ents if e.get("ev") == "intent" for op in (e.get("settle_ops") or []) if isinstance(op, dict)))
    d["n_intent_events"] = len([e for e in ents if e.get("ev") == "intent"])
    d["ev_kinds"] = sorted(set(e.get("ev") for e in ents))
    d["pending_unfrozen"] = len(CI.pending(LANE_NAME)); d["frozen_unresolved"] = len(CI.frozen_unresolved(jp)[0])
    evs = _jl(ROOTDIR)[0]
    d["credit"] = len([e for e in evs if e.get("event") == "credit"]); d["debit"] = len([e for e in evs if e.get("event") == "debit"])
    d["wal_events"] = sorted(set(e.get("event") for e in evs))
    mp = os.path.join(_acct_dir(ROOTDIR), "unresolved_generation_refusals.jsonl")
    d["marker_records"] = len([1 for ln in open(mp) if ln.strip()]) if os.path.exists(mp) else 0
    return d
def _pf2(mult):
    def f(chain, mint, spot_index=None):
        pos = (L.open_positions() or {}).get(mint) or {}
        return float(pos.get("entry_price") or 0) * mult
    return f
def child_fault2(mutant=""):
    PA, out = _fault_core(mutant)
    s1 = _snap(PA); out["s1"] = s1
    L._ENTRY_BLOCKED_AFTER_FAULT = bool(getattr(L, "_ENTRY_BLOCKED", None))
    try: L.poll_exits(price_fn=_pf2(2.0)); out["c2_exc"] = None
    except Exception as e: out["c2_exc"] = type(e).__name__
    s2 = _snap(PA); out["s2"] = s2
    out["entry_blocked_flag"] = bool(getattr(L, "_ENTRY_BLOCKED", False))
    try: e = _enter(MINT2); out["c2_entry_ok"] = bool(e[0]); out["c2_entry_reason"] = str(e[1])[:160]
    except Exception as ex: out["c2_entry_ok"] = False; out["c2_entry_reason"] = "EXC %s" % type(ex).__name__
    out["s3"] = _snap(PA)
    print("RESULT " + _json.dumps(out, default=str), flush=True)
def child_restart2():
    PA = fresh(); out = {"s0": _snap(PA)}
    L._RECOVERY_DONE = False; L._ENTRY_BLOCKED = False
    out["recovery_blocked"] = bool(L._recover_close_intents()); out["s1"] = _snap(PA)
    try: L.poll_exits(price_fn=_pf2(2.0)); out["poll_exc"] = None
    except Exception as e: out["poll_exc"] = type(e).__name__
    out["s2"] = _snap(PA)
    try: r = PA.try_debit(LANE_NAME, "DirectProbeMint", 1.0, 1000.0, "probe:direct:2")
    except Exception as e: r = (False, "EXC %s" % type(e).__name__)
    out["direct_ok"] = bool(r[0]); out["direct_why"] = str(r[1])[:240]
    e = _enter(MINT2); out["entry_ok"] = bool(e[0]); out["entry_reason"] = str(e[1])[:160]
    try: out["ledger_account_id"] = _json.load(open(os.path.join(_acct_dir(ROOTDIR), "ledger.json"))).get("account_id")
    except Exception: out["ledger_account_id"] = None
    out["s3"] = _snap(PA)
    print("RESULT " + _json.dumps(out, default=str), flush=True)

if MODE == "open": child_open(); sys.exit(0)
if MODE == "probe": child_probe(sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else ""); sys.exit(0)
if MODE == "fault2": child_fault2(sys.argv[3] if len(sys.argv) > 3 else ""); sys.exit(0)
if MODE == "restart2": child_restart2(); sys.exit(0)

FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n, flush=True)
    if not c: FAILS.append(n)
class _TO(Exception): pass
def spawn(args):
    left = BUDGET_S - (time.time() - T0)
    if left < 8: raise _TO("budget exhausted")
    try:
        p = subprocess.run([sys.executable, "-B", os.path.abspath(__file__)] + args, capture_output=True, text=True, timeout=min(25.0, left), env=dict(os.environ))
    except subprocess.TimeoutExpired:
        raise _TO("spawn timeout %r" % (args[:2],))
    return p.returncode, p.stdout, p.stderr
def res(out):
    l = [x for x in out.splitlines() if x.startswith("RESULT ")]
    return _json.loads(l[0][7:]) if l else None
def newroot(tag): return _tf.mkdtemp(prefix="lanepub-%s-" % tag)
def do_open(root):
    rc, out, err = spawn(["open", root]); r = res(out)
    if rc != 0 or r is None or not r.get("ok"):
        print("FAIL SETUP open (rc %s, %r, %s)" % (rc, r, err[-200:])); raise SystemExit(2)
    return r
def wipe(root):
    ad = _acct_dir(root)
    for n in ("ledger.json", "ledger.json.bak", "events.jsonl"):
        if os.path.exists(os.path.join(ad, n)): os.remove(os.path.join(ad, n))
def probe(root, order, mutant=""):
    rc, out, err = spawn(["probe", root, order] + ([mutant] if mutant else [])); r = res(out)
    if r is None: print("FAIL SETUP probe (rc %s, %s)" % (rc, err[-200:])); raise SystemExit(2)
    return r
NEED = "persisted lane evidence blocks admission"
def run_fault(mutant=""):
    root = newroot("F"); do_open(root); wipe(root)
    rc, out, err = spawn(["fault2", root] + ([mutant] if mutant else [])); f = res(out)
    if f is None: print("FAIL SETUP fault2 child (rc %s, %s)" % (rc, err[-300:])); raise SystemExit(2)
    return root, f
def same(a, b, keys): return all(a.get(k) == b.get(k) for k in keys)
try:
    rootF, f = run_fault()
    s1, s2, s3 = f["s1"], f["s2"], f["s3"]
    check("F0 the bb77 fault reproduced inside this harness: credit reached refusal, 2+ real EFBIG, nothing durable in the account (%r)" % ({k: f.get(k) for k in ("credit_called", "refuse_reached", "n_efbig", "credit_events", "refusal_journal_records")},), (f.get("credit_called") or 0) >= 1 and (f.get("refuse_reached") or 0) >= 1 and (f.get("n_efbig") or 0) >= 2 and f.get("credit_events") == 0)
    check("F1 after the faulted settle the lane position is OPEN, bound to its original generation, with no leg done (%r)" % ({k: s1[k] for k in ("pos_open", "legs_done", "pa_pos_id", "n_positions")},), s1["pos_open"] is True and s1["legs_done"] == {"a": False, "b": False, "c": False} and s1["n_positions"] == 1)
    check("F2 exactly one obligation exists: one distinct credit op id across the journal, it is FROZEN and unapplied, none pending-unfrozen (%r)" % ({k: s1[k] for k in ("intent_ops", "frozen_unresolved", "pending_unfrozen")},), len(s1["intent_ops"]) == 1 and s1["frozen_unresolved"] == 1 and s1["pending_unfrozen"] == 0)
    check("F3 SECOND CYCLE (same process, storage restored): no account credit or debit, position still open and unchanged, same single obligation, nothing pending-unfrozen (%r)" % ({k: s2[k] for k in ("credit", "debit", "pos_open", "legs_done", "intent_ops", "frozen_unresolved", "pending_unfrozen")},), s2["credit"] == 0 and s2["debit"] == 0 and s2["pos_open"] is True and s2["legs_done"] == s1["legs_done"] and s2["intent_ops"] == s1["intent_ops"] and s2["pending_unfrozen"] == 0 and s2["frozen_unresolved"] >= 1 and s2["pa_account_id"] == s1["pa_account_id"] and s2["tokens"] == s1["tokens"])
    check("F4 second cycle with storage restored: the refusal is now DURABLY recorded in the account marker (observation, hypothesis; marker_records %r -> %r)" % (s1["marker_records"], s2["marker_records"]), s2["marker_records"] >= 1)
    check("F5 no second buy: the public lane entry after the faulted cycles is refused and the lane holds one position, account has the original debit count (%r)" % ({k: f.get(k) for k in ("c2_entry_ok", "c2_entry_reason")}, ), f["c2_entry_ok"] is False and s3["n_positions"] == 1 and s3["debit"] == s1["debit"])
    rc, out, err = spawn(["restart2", rootF]); r = res(out)
    if r is None: print("FAIL SETUP restart2 (rc %s, %s)" % (rc, err[-300:])); raise SystemExit(2)
    q0, q1, q2, q3 = r["s0"], r["s1"], r["s2"], r["s3"]
    check("R1 FRESH process: lane recovery is blocked and applies nothing: the journal bytes are identical before and after recovery, no credit, no account_applied event (%r)" % ({k: q1[k] for k in ("journal_sha", "credit")}, q0["journal_sha"]), r["recovery_blocked"] is True and q1["journal_sha"] == q0["journal_sha"] and q1["credit"] == 0 and "account_applied" not in q1["ev_kinds"])
    check("R2 FRESH process poll_exits cycle: no credit, no debit, position unchanged and still bound to the ORIGINAL generation, no new obligation (%r)" % ({k: q2[k] for k in ("credit", "debit", "pos_open", "pa_account_id", "intent_ops", "pending_unfrozen")},), q2["credit"] == 0 and q2["debit"] == 0 and q2["pos_open"] is True and q2["pa_account_id"] == s1["pa_account_id"] and q2["intent_ops"] == s1["intent_ops"] and q2["pending_unfrozen"] == 0)
    check("R3 NO REBINDING: the lane position keeps the old generation id while the account ledger holds a different one (%r vs %r)" % (q3["pa_account_id"], r["ledger_account_id"]), q3["pa_account_id"] == s1["pa_account_id"] and bool(r["ledger_account_id"]) and r["ledger_account_id"] != q3["pa_account_id"])
    check("R4 FRESH process: public debit and public lane entry still refused, still no second position (%r)" % ({k: r[k] for k in ("direct_ok", "direct_why", "entry_ok")},), r["direct_ok"] is False and NEED in r["direct_why"] and r["entry_ok"] is False and q3["n_positions"] == 1 and q3["debit"] == s1["debit"])
    rootM, m = run_fault("nofreeze")
    check("M MUTANT (freeze claims success and writes nothing, the old behavior): the same scenario leaves NO frozen obligation, so F2 would fail (%r)" % ({k: m["s1"][k] for k in ("frozen_unresolved", "intent_ops", "pending_unfrozen")},), m["s1"]["frozen_unresolved"] == 0)
except _TO as e:
    print("FAIL BUDGET %s" % e); FAILS.append("budget")
except SystemExit:
    raise
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
