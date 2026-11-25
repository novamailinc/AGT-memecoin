#!/usr/bin/env python3
"""bb76 REAL-LANE / PUBLIC-ENTRY admission harness, MOMENTUM, SOURCE ONLY (NOT RUN until main clears an exact HEAD/blob/script). Scratch dirs only, canned feed
(tests/_guard_fixture price feed and the lane's canned tape, engineering-only, no network, live executors forbidden). Every scenario uses a FRESH PROCESS per phase.
What is REAL: the lane's own entry (momentum_lane.consider_candidate -> PA.try_debit -> lane ledger write, so pa_pos_id/pa_account_id/pa_entry_op and the lane
registration are written by the production code), the account generation transition (the account files are removed between processes and a new account is created by the
real account load, so account_id changes and pos_ids repeat), the PUBLIC admission operations (PA.try_debit and the lane's consider_candidate) after a restart, and a
file-permission fault (non-root only; account directory chmod 0555 and events.jsonl chmod 0444 so BOTH refusal recorders fail at the operating-system level during a real
settle through L.poll_exits; permissions restored in a finally block; no mount, disk or shared-storage change). What is NOT real: the feed, tape and safety stubs.
Predeclared (hand-written, UNVERIFIED):
  A  real entry opens a bound position; the registry primary and mirror exist after it.
  B  POSITIVE CONTROL matching state, fresh process: lane recovery not blocked, public lane entry admitted, direct account debit admitted.
  C  generation transition (account files removed, new account created): FIRST a direct PA.try_debit is refused at ACCOUNT level (message names the persisted lane evidence and
     generation-mismatch) with zero debit events in the new WAL, THEN lane recovery is blocked and a public lane entry is refused.
  D  real permission fault during a real settle: _refuse_generation is REACHED, the OS refuses both recorders (no marker file, no refusal journal record, proven by a failed create
     probe), permissions are restored, and a FRESH process still refuses a direct debit and a public lane entry on persisted lane evidence alone.
  E  MUTANT: the same C state with the registry check nulled in the child admits the direct debit (so C's refusal is the account-level check).
If _refuse_generation is not reached in D, the row FAILS (the fault was not exercised); nothing is relaxed."""
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
def child_fault():
    PA = fresh(); cash(PA)   # the real account load creates the NEW generation (the parent removed the old account files)
    ad = _acct_dir(ROOTDIR); jp = os.path.join(ad, "events.jsonl"); out = {"euid": os.geteuid()}
    if os.geteuid() == 0:
        print("RESULT " + _json.dumps({"skipped_root": True}), flush=True); return
    hits = {"refuse": 0}; _rg = PA._refuse_generation
    def _rg2(*a, **k):
        hits["refuse"] += 1; return _rg(*a, **k)
    PA._refuse_generation = _rg2
    cur = {}
    try:
        os.chmod(jp, 0o444); os.chmod(ad, 0o555)
        try:
            fd = os.open(os.path.join(ad, "fault_probe.tmp"), os.O_WRONLY | os.O_CREAT); os.close(fd); out["create_probe_failed"] = False
        except OSError: out["create_probe_failed"] = True
        try:
            with open(jp, "a") as f: f.write("")
            out["journal_append_probe_failed"] = False
        except OSError: out["journal_append_probe_failed"] = True
        def _pf(chain, mint, spot_index=None):
            pos = (L.open_positions() or {}).get(mint) or {}
            return float(pos.get("entry_price") or 0) * 2.0
        try: L.poll_exits(price_fn=_pf); out["poll_exc"] = None
        except Exception as e: out["poll_exc"] = type(e).__name__
    finally:
        os.chmod(ad, 0o755); os.chmod(jp, 0o644)
    out["refuse_reached"] = hits["refuse"]
    out["marker_exists"] = os.path.exists(os.path.join(ad, "unresolved_generation_refusals.jsonl"))
    evs = _jl(ROOTDIR)[0]
    out["refusal_journal_records"] = len([e for e in evs if e.get("event") == "settle_generation_refused"])
    out["credit_events"] = len([e for e in evs if e.get("event") == "credit"])
    print("RESULT " + _json.dumps(out, default=str), flush=True)
if MODE == "open": child_open(); sys.exit(0)
if MODE == "probe": child_probe(sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else ""); sys.exit(0)
if MODE == "fault": child_fault(); sys.exit(0)

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
try:
    rA = do_open(newroot("A"))
    check("A1 the real lane entry opened a bound position (%r)" % ({k: rA[k] for k in ("pa_pos_id", "pa_account_id", "pa_entry_op")},), bool(rA["pa_pos_id"] and rA["pa_account_id"] and rA["pa_entry_op"]))
    check("A2 registry primary and mirror exist after the real entry (%r %r)" % (rA["registry_primary"], rA["registry_mirror"]), rA["registry_primary"] is True and rA["registry_mirror"] is True)
    rootB = newroot("B"); do_open(rootB); pB = probe(rootB, "lane_first")
    check("B1 matching state, fresh process: lane recovery not blocked and public lane entry admitted (%r)" % pB, pB["lane_blocked"] is False and pB["lane_entry_ok"] is True)
    check("B2 matching state: direct account debit admitted after the lane restart (%r)" % pB.get("direct_why"), pB["direct_ok"] is True)
    rootC = newroot("C"); do_open(rootC); wipe(rootC); pC = probe(rootC, "direct_first")
    check("C1 after the generation transition a direct PA.try_debit is refused at ACCOUNT level, naming persisted lane evidence and generation-mismatch (%r)" % pC["direct_why"], pC["direct_ok"] is False and NEED in pC["direct_why"] and "generation-mismatch" in pC["direct_why"])
    check("C2 no debit event was written to the new generation's WAL by the refused direct debit (%r)" % pC["direct_new_debits"], pC["direct_new_debits"] == 0)
    check("C3 lane recovery is blocked and the public lane entry is refused (%r)" % ({k: pC[k] for k in ("lane_blocked", "lane_entry_ok", "lane_entry_reason")},), pC["lane_blocked"] is True and pC["lane_entry_ok"] is False)
    rootE = newroot("E"); do_open(rootE); wipe(rootE); pE = probe(rootE, "direct_first", "nullreg")
    check("E MUTANT (registry check nulled in the child): the same transition admits the direct debit, so C1 is the account-level check (%r)" % pE["direct_why"], pE["direct_ok"] is True)
    rootD = newroot("D"); do_open(rootD); wipe(rootD)
    rc, out, err = spawn(["fault", rootD]); fD = res(out)
    if fD is None: print("FAIL SETUP fault child (rc %s, %s)" % (rc, err[-300:])); raise SystemExit(2)
    if fD.get("skipped_root"):
        print("INFO D skipped: euid 0 (permission faults need non-root)")
    else:
        check("D1 the OS refused file creation and journal append during the fault window (%r)" % ({k: fD.get(k) for k in ("create_probe_failed", "journal_append_probe_failed")},), fD.get("create_probe_failed") is True and fD.get("journal_append_probe_failed") is True)
        check("D2 the real settle reached _refuse_generation (%r)" % fD.get("refuse_reached"), (fD.get("refuse_reached") or 0) >= 1)
        check("D3 neither recorder persisted anything: no marker file, no refusal journal record (%r)" % ({k: fD.get(k) for k in ("marker_exists", "refusal_journal_records", "credit_events")},), fD.get("marker_exists") is False and fD.get("refusal_journal_records") == 0 and fD.get("credit_events") == 0)
        pD = probe(rootD, "direct_first")
        check("D4 FRESH process after restored permissions: direct debit refused on persisted lane evidence (%r)" % pD["direct_why"], pD["direct_ok"] is False and NEED in pD["direct_why"] and "generation-mismatch" in pD["direct_why"])
        check("D5 FRESH process: public lane entry refused (%r)" % pD["lane_entry_reason"], pD["lane_entry_ok"] is False)
except _TO as e:
    print("FAIL BUDGET %s" % e); FAILS.append("budget")
except SystemExit:
    raise
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
