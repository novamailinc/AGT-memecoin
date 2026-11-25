#!/usr/bin/env python3
"""DIAGNOSTIC (bb61, NOT RUN until cleared): what does account EPOCH evidence actually prove for a REAL-SHAPE pending close intent? Lanes: MOMENTUM and MIRROR.
Purpose: distinguish safe no-op convergence of a never-debited orphan from hiding an account LOSS or duplicating accounting, WITHOUT choosing an owner economic contract.
Real parts: real lane entry (real paper_account.try_debit mints the real 'lane/mint#seq' pos_id), real close-intent writer (CI.begin through poll_exits), real separate-process SIGKILL right before PA.credit,
real recovery (entry-first: _enter triggers _recover_close_intents -> gate -> recover_lane -> real PA.credit / op_outcome / pos_ever_existed). Canned feed = engineering fixture.
LABELLED INJECTIONS (scratch root only, files of the paper account): none | wipe_all (ledger, .bak, journal removed = a brand-new account epoch) | journal_loss | ledger_loss | torn_mid (one corrupt record
mid-file) | stale_ledger (pre-entry ledger restored over ledger and .bak, journal intact) | journal_prefix (journal cut to the clean prefix BEFORE the debit, ledger intact). No production file is touched.
ASSERTIONS are only: setup/instrument facts, the healthy control (E0 converges once), and the no-duplicate-accounting invariant (at most one credit event for the op; no cash gained twice). The
convergence/freeze/hold CLASS of every loss scenario is printed as an OBSERVATION (OBS lines), NOT asserted: no owner contract is selected here. Hand predictions are UNVERIFIED.
Run: python3 -B tests/test_orphan_epoch_diag_momentum.py"""
if __name__ != "__main__":
    import pytest
    pytest.skip("script-style test", allow_module_level=True)
import sys, os
MODE = sys.argv[1] if len(sys.argv) > 1 else "parent"
import tempfile as _tf
ROOTDIR = sys.argv[2] if MODE != "parent" else _tf.mkdtemp(prefix="orphandiag-parent-unused-")
LANE_ARG = sys.argv[3] if MODE != "parent" else None
import logging as _logging, importlib, json as _json, signal as _signal, subprocess, time, hashlib, shutil
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
if LANE_ARG:
    if LANE_ARG == "momentum":
        from daemon.launch_scanner import momentum_lane as L
        LANE_NAME = L.LANE; assert LANE_NAME == "momentum", LANE_NAME
    else:
        from daemon.launch_scanner import mirror as L
        LANE_NAME = "degen_mirror"
class _Safe:
    def check_token(self, chain, mint, timeout=10): return {"passed": True, "score": 80, "flags": [], "fails": [], "unknown": False}
class _Veto:
    def veto_check(self, mint, chain, symbol="", safety_report=None, run_bundle=False, lane="unknown"):
        return {"passed": True, "vetoes": [], "flags": [], "details": {}}
def _tape():
    n = time.time()
    return {"fetched_at": n, "received_at": n, "dex": "pump", "usd_liquidity": 100000.0, "usd_market_cap": 500000.0, "chg_m5": 10.0, "buys_m5": 100, "sells_m5": 40,
            "vol_m5": 9000.0, "buys_h1": 400, "sells_h1": 150, "price_usd": 0.001, "pair_created_ms": (time.time() - 3600) * 1000, "tape_source": "dexscreener"}
MINT = "EpochDiagMint1"
def _enter(MINT=MINT):
    if LANE_ARG == "momentum":
        d = {"pool_address": "PoolOther", "mint": MINT, "symbol": "RUN", "name": "RUN", "chain": "solana", "source": "gecko-trending", "lane": "lowcap", "url": "",
             "chg_24h_pct": 55.0, "chg_1h_pct": 8.0, "vol_24h_usd": 250000.0, "liq_usd": 100000.0, "usd_market_cap": 500000.0}
        return L.consider_candidate(d, time.time())
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
    if LANE_ARG == "momentum":
        L.ensure_ledger(); L.quote_price = G.stamped_quote(L, lambda chain, mint, spot_index=None: 0.001)
    else:
        L._LOCK_PATH = os.path.join(led, LANE_ARG + ".lock"); L.quote_price = G.stamped_const(L, 0.01); L.RUNNER_CONFIRM_GAP_S = 0.0
    L._fresh_tape = lambda chain, mint: _tape()
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
    print("KILLHIT lane=%s at PA.credit" % LANE_ARG, flush=True)
    os.kill(os.getpid(), _signal.SIGKILL)
def raw_state(root, lane_name):
    """READ-ONLY raw file evidence (no account API call): account files, journal events, lane ledger, close-intent journal."""
    ad = os.path.join(root, "home", "paper_account"); out = {}
    def sha(p):
        try:
            with open(p, "rb") as f: return hashlib.sha256(f.read()).hexdigest()[:16]
        except OSError: return None
    out["files"] = {n: sha(os.path.join(ad, n)) for n in ("ledger.json", "ledger.json.bak", "events.jsonl")}
    evs = []; bad = 0
    try:
        with open(os.path.join(ad, "events.jsonl"), "rb") as f:
            for raw in f:
                s = raw.decode("utf-8", "replace").strip()
                if not s: continue
                try: evs.append(_json.loads(s))
                except ValueError: bad += 1
    except OSError: pass
    out["journal_lines"] = len(evs); out["journal_bad"] = bad
    out["journal_kinds"] = [e.get("event") for e in evs]
    out["debit_pos_ids"] = [e.get("pos_id") for e in evs if e.get("event") == "debit"]
    try:
        with open(os.path.join(ad, "ledger.json")) as f: led = _json.load(f)
        out["ledger"] = {"seq": led.get("seq"), "cash": led.get("cash"), "cash_spendable": led.get("cash_spendable"),
                         "open_keys": sorted(k for ln in (led.get("lanes") or {}).values() for k in ((ln or {}).get("open") or {}))}
    except Exception as e: out["ledger"] = "unreadable:%s" % type(e).__name__
    try:
        with open(os.path.join(root, "led", LANE_ARG + ".json")) as f: ld = _json.load(f)
        out["lane_ledger"] = {"positions": sorted((ld.get("positions") or {}).keys()), "closes": len(ld.get("closes") or []), "bankroll": ld.get("bankroll")}
    except Exception as e: out["lane_ledger"] = "unreadable:%s" % type(e).__name__
    try:
        ents, st, _t = CI._read(CI.journal_path(lane_name))
        out["intent_events"] = [e.get("ev") for e in ents]; out["intent_status"] = st
    except Exception as e: out["intent_events"] = "unreadable:%s" % type(e).__name__
    return out
def child_run(mult):
    PA = fresh(); cash(PA)
    _ad = os.path.join(ROOTDIR, "home", "paper_account")
    shutil.copy(os.path.join(_ad, "ledger.json"), os.path.join(ROOTDIR, "ledger.pre-entry.json"))   # scratch copies for the stale_ledger / pair_rollback injections
    if os.path.exists(os.path.join(_ad, "events.jsonl")): shutil.copy(os.path.join(_ad, "events.jsonl"), os.path.join(ROOTDIR, "events.pre-entry.jsonl"))
    r = _enter(); pos = (L.open_positions() or {}).get(MINT) or {}
    if not (r[0] and pos.get("pa_pos_id")):
        print("ENTRY-REFUSED lane=%s ok=%s reason=%r" % (LANE_ARG, bool(r[0]), r[1]), flush=True); sys.exit(5)
    print("ENTRY lane=%s ok=%s cash=%.6f nonsim=%s pa_pos_id=%s" % (LANE_ARG, bool(r[0]), cash(PA), (pos.get("execution") or {}).get("source") != "jupiter_quote_sim", pos.get("pa_pos_id")), flush=True)
    r2 = _enter("EpochDiagMint2"); p2 = (L.open_positions() or {}).get("EpochDiagMint2") or {}   # a SECOND real entry so a later account event follows the first debit (midchain scenario)
    if not (r2[0] and p2.get("pa_pos_id")): print("ENTRY-REFUSED second %r" % (r2[1],), flush=True); sys.exit(5)
    print("ENTRY2 cash=%.6f" % cash(PA), flush=True)
    _real = PA.credit
    def _kc(*a, **k):
        if a[1] == MINT: _die()
        return _real(*a, **k)
    PA.credit = _kc
    def _pf(chain, mint, spot_index=None):
        pos = (L.open_positions() or {}).get(mint) or {}
        return float(pos.get("entry_price") or 0) * (float(mult) if mint == MINT else 1.0)
    L.poll_exits(price_fn=_pf)
    print("CHILD SURVIVED (kill point not reached)", flush=True); sys.exit(7)
HITS = {"n": 0}; LOGS = []
class _H(_logging.Handler):
    def emit(self, rec):
        try: LOGS.append(rec.getMessage())
        except Exception: pass
def child_recover(entry_cash):
    pre = raw_state(ROOTDIR, LANE_NAME)
    ents0, st0, _t = CI._read(CI.journal_path(LANE_NAME))
    its = [e for e in ents0 if e.get("ev") == "intent"]
    op = (its[0].get("settle_ops") or [{}])[0] if its else {}
    op_id = op.get("op_id"); pid = its[0].get("pos_id") if its else None
    PA = fresh()
    _logging.getLogger().addHandler(_H()); _logging.getLogger().setLevel(_logging.INFO)
    cnt = {"poll_exits": 0}; _pe = L.poll_exits
    def _pe2(*a, **k):
        cnt["poll_exits"] += 1; return _pe(*a, **k)
    L.poll_exits = _pe2
    credit_calls = {"n": 0}; _cr = PA.credit
    def _crc(*a, **k):
        credit_calls["n"] += 1; return _cr(*a, **k)
    PA.credit = _crc
    e = _enter("EpochDiagNew")
    post = raw_state(ROOTDIR, LANE_NAME)
    gs = getattr(L, "_GATE_STATS", None)
    ents, st, _ = CI._read(CI.journal_path(LANE_NAME))
    succ = {x.get("supersedes") for x in ents if x.get("ev") == "intent" and x.get("supersedes")}
    fro = len([i for i, x in CI._state(ents).items() if x["intent"] is not None and not x["applied"] and x["frozen"] is not None and i not in succ])
    res = {"op_id": op_id, "pos_id": pid, "outcome": PA.op_outcome(op_id) if op_id else None,
           "pos_ever_existed": PA.pos_ever_existed(pos_id=pid, lane=LANE_NAME, mint=MINT), "credit_calls": credit_calls["n"],
           "credit_events": len([x for x in PA._journal_read()[0] if x.get("op_id") == op_id and x.get("event") == "credit"]),
           "orphan_events": [x.get("event") + ("/reason" if x.get("reason") else "") for x in PA._journal_read()[0] if x.get("op_id") == op_id and "orphan" in str(x.get("event"))],
           "degraded_events": [str(x.get("event")) for x in PA._journal_read()[0] if x.get("op_id") == op_id and "degraded" in str(x.get("event"))],
           "journal_status": PA._journal_read()[1], "intent_status": st, "pending": len(CI.pending(LANE_NAME)), "frozen_open": fro,
           "intent_applied": any(x.get("ev") == "applied" for x in ents), "entry_blocked": bool(L._ENTRY_BLOCKED), "entry_opened": bool(e[0]), "entry_try": repr(e)[:200],
           "cash": cash(PA), "entry_cash": entry_cash, "cnt": cnt, "gate_stats": dict(gs) if isinstance(gs, dict) else None,
           "gate_logs": {k: len([m for m in LOGS if "GATE verdict=%s" % k in m]) for k in ("ok", "freeze", "hold")},
           "recover_logs": [m[:240] for m in LOGS if "orphan" in m.lower() or "FROZEN" in m or "rejected by the gate" in m or "account re-apply" in m][:6]}
    print("PRE " + _json.dumps(pre, default=str), flush=True)
    print("POST " + _json.dumps(post, default=str), flush=True)
    print("RESULT " + _json.dumps(res, default=str), flush=True)
if MODE == "run": child_run(sys.argv[4]); sys.exit(0)
if MODE == "recover": child_recover(float(sys.argv[4])); sys.exit(0)

FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n, flush=True)
    if not c: FAILS.append(n)
def spawn(args):
    p = subprocess.run([sys.executable, "-B", os.path.abspath(__file__)] + args, capture_output=True, text=True, timeout=90, env=dict(os.environ))
    return p.returncode, p.stdout, p.stderr
def inject(root, mode):
    """LABELLED injection on the SCRATCH paper account files only."""
    ad = os.path.join(root, "home", "paper_account"); lj = os.path.join(ad, "ledger.json"); jj = os.path.join(ad, "events.jsonl")
    if mode == "none": return
    if mode == "wipe_all":
        for n in ("ledger.json", "ledger.json.bak", "events.jsonl"):
            if os.path.exists(os.path.join(ad, n)): os.remove(os.path.join(ad, n))
    elif mode == "journal_loss": os.remove(jj)
    elif mode == "ledger_loss":
        for n in ("ledger.json", "ledger.json.bak"): os.remove(os.path.join(ad, n))
    elif mode == "torn_mid":
        lines = open(jj).read().splitlines(keepends=True)
        with open(jj, "w") as f: f.writelines(lines[:-1] + ["{torn record\n"] + lines[-1:])
    elif mode == "stale_ledger":
        for n in ("ledger.json", "ledger.json.bak"): shutil.copy(os.path.join(root, "ledger.pre-entry.json"), os.path.join(ad, n))
    elif mode == "midchain":   # remove ONLY the debit record of the first position; the later debit and any later records stay (a seq hole in the middle of the chain)
        lines = open(jj).read().splitlines(keepends=True); k = min(i for i, l in enumerate(lines) if '"event": "debit"' in l)
        with open(jj, "w") as f: f.writelines(lines[:k] + lines[k + 1:])
    elif mode == "pair_rollback":   # BOTH ledger and journal restored to their pre-entry copies (a consistent clean older pair)
        for n in ("ledger.json", "ledger.json.bak"): shutil.copy(os.path.join(root, "ledger.pre-entry.json"), os.path.join(ad, n))
        pj = os.path.join(root, "events.pre-entry.jsonl")
        if os.path.exists(pj): shutil.copy(pj, jj)
        else: os.remove(jj)
    elif mode == "journal_prefix":
        lines = open(jj).read().splitlines(keepends=True); k = max(i for i, l in enumerate(lines) if '"event": "debit"' in l)
        with open(jj, "w") as f: f.writelines(lines[:k])
def classify(r):
    if r["credit_events"] == 1: return "CREDITED_ONCE"
    if r["intent_applied"] and r["credit_events"] == 0 and r["outcome"] == "orphan": return "CONVERGED_ORPHAN_NOOP_(no credit)"
    if r["frozen_open"] >= 1: return "FROZEN"
    if r["pending"] >= 1: return "HOLD_PENDING"
    return "OTHER"
SC = ("E0-CONTROL-healthy-no-injection", "E1-wipe_all-new-account-epoch", "E2-journal_loss-ledger-kept", "E3-ledger_loss-journal-kept", "E4-torn_mid-journal",
      "E5-stale_ledger-restore-journal-intact", "E6-journal_prefix-cut-before-debit-ledger-kept", "E7-midchain-first-debit-record-removed", "E8-pair_rollback-clean-older-ledger-and-journal")
for lane in ("momentum",):
    for tag0 in SC:
        mode = tag0.split("-", 1)[1].split("-")[0] if not tag0.startswith("E0") else "none"
        tag = "%s-%s" % (tag0, lane)
        root = _tf.mkdtemp(prefix="orphandiag-%s-" % tag)
        rc, out, err = spawn(["run", root, lane, "2.0"])
        _el = [l for l in out.splitlines() if l.startswith("ENTRY ")]
        ok_e = bool(_el) and "ok=True" in _el[0] and "nonsim=True" in _el[0] and "pa_pos_id=" + (lane if lane == "momentum" else "degen_mirror") + "/" in _el[0] and "Traceback" not in err
        check("%s: SETUP real entry opened with a real account pos_id (%s rc %d stderr %r)" % (tag, _el[:1], rc, err[-120:]), ok_e)
        check("%s: child KILLED by SIGKILL right before PA.credit (rc %d)" % (tag, rc), rc == -_signal.SIGKILL and "KILLHIT" in out and "SURVIVED" not in out)
        if not ok_e: continue
        _e2 = [l for l in out.splitlines() if l.startswith("ENTRY2 ")]
        check("%s: SETUP second real entry present (%s)" % (tag, _e2[:1]), bool(_e2))
        if not _e2: continue
        entry_cash = float(_e2[0].split("cash=")[1].split()[0])   # cash after BOTH entries, no credit applied yet
        os.environ["FOREX_AGENT_HOME"] = os.path.join(root, "home")
        lname = "momentum" if lane == "momentum" else "degen_mirror"
        before = None
        # raw evidence BEFORE the injection (parent reads files only): the account journal holds the debit for the real pos_id
        ad = os.path.join(root, "home", "paper_account")
        try: jk = [_json.loads(x).get("event") for x in open(os.path.join(ad, "events.jsonl")) if x.strip()]
        except OSError: jk = []
        pid = _el[0].split("pa_pos_id=")[1].split()[0]
        check("%s: BEFORE injection: the account journal holds the debit (kinds %r), the lane position is real (pos_id %s)" % (tag, jk, pid), "debit" in jk and pid.startswith(lname + "/" + MINT + "#"))
        inject(root, mode)
        rc2, out2, err2 = spawn(["recover", root, lane, "%.6f" % entry_cash])
        pl = [l for l in out2.splitlines() if l.startswith("PRE ")]; ql = [l for l in out2.splitlines() if l.startswith("POST ")]; rl = [l for l in out2.splitlines() if l.startswith("RESULT ")]
        check("%s: recovery process exited 0, no crash trace (rc %d, stderr tail %r)" % (tag, rc2, err2[-160:]), rc2 == 0 and pl and ql and rl and "Traceback" not in err2)
        if not (pl and ql and rl): continue
        pre = _json.loads(pl[0][4:]); post = _json.loads(ql[0][5:]); r = _json.loads(rl[0][7:])
        print("  RAW-PRE  %s %s" % (tag, _json.dumps(pre, default=str)), flush=True)
        print("  RAW-POST %s %s" % (tag, _json.dumps(post, default=str)), flush=True)
        print("  RAW-RESULT %s %s" % (tag, _json.dumps(r, default=str)), flush=True)
        check("%s: INSTRUMENT zero polls in the recovery process (%d)" % (tag, r["cnt"]["poll_exits"]), r["cnt"]["poll_exits"] == 0)
        check("%s: INVARIANT no duplicate accounting: at most one credit event for the op (%d) and PA.credit calls recorded (%d)" % (tag, r["credit_events"], r["credit_calls"]), r["credit_events"] <= 1)
        cl = classify(r)
        print("OBS %s: CLASS=%s outcome=%s pos_ever_existed=%s intent_applied=%s frozen_open=%d pending=%d entry_blocked=%s credit_calls=%d orphan_events=%s degraded_events=%s journal_status=%s" % (
            tag, cl, r["outcome"], r["pos_ever_existed"], r["intent_applied"], r["frozen_open"], r["pending"], r["entry_blocked"], r["credit_calls"], r["orphan_events"], r["degraded_events"], r["journal_status"]), flush=True)
        print("OBS %s: LOSS-HIDING-CANDIDATE=%s (the intent is terminal with no credit although the account journal held the debit BEFORE the injection)" % (tag, "YES" if (cl.startswith("CONVERGED_ORPHAN") and "debit" in jk) else "no"), flush=True)
        print("OBS %s: cash entry=%.6f post-recovery=%.6f; account debit pos_ids before=%r after=%r; lane ledger before=%r after=%r" % (tag, entry_cash, r["cash"], pre["debit_pos_ids"], post["debit_pos_ids"], pre["lane_ledger"], post["lane_ledger"]), flush=True)
        for m in r["recover_logs"]: print("OBS %s: LOG %s" % (tag, m), flush=True)
        if mode == "none":
            check("%s: CONTROL converges: the real credit applied exactly once, intent terminal, entries not blocked, new entry admitted (class %s)" % (tag, cl),
                  cl == "CREDITED_ONCE" and r["intent_applied"] and r["frozen_open"] == 0 and not r["entry_blocked"] and r["entry_opened"] and r["cash"] > entry_cash)
print("FAILS: %d" % len(FAILS))
sys.exit(1 if FAILS else 0)
