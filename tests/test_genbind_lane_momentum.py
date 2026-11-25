#!/usr/bin/env python3
"""bb68 LANE-LEVEL generation-binding proof, MOMENTUM, SOURCE ONLY (NOT RUN until main clears an exact HEAD/blob/script). Derived from the BB66 diagnostic
harness (same REAL entry, REAL SIGKILL before PA.credit, REAL recovery, labelled scratch-file injections; canned feed = engineering fixture) with PREDECLARED
expectations added per scenario (hand-written, UNVERIFIED):
  none (E0): the real credit applies EXACTLY once for the op and the pid, entries not blocked, cash up.
  STRONG SET wipe_all (E1), ledger_loss (E3), torn_mid (E4), pair_rollback (E8), reuse (E9): NO credit or write_down is journaled for the intent op or the old pid during recovery
    (reused pos_id never credited), recovery does not mark the intent applied with a silent orphan no-op, and a fresh entry attempt is NOT opened (entries blocked or account degraded).
  OBSERVE-ONLY (no expectation, labelled): journal_loss, stale_ledger, trail_cut, midchain.
Failures are reported as findings; no scenario is rerun and no expectation is relaxed to go green."""
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
def _die():
    print("KILLHIT lane=%s at PA.credit target" % LANE_ARG, flush=True)
    os.kill(os.getpid(), _signal.SIGKILL)
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
def _sha(p):
    try:
        with open(p, "rb") as f: return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError: return None
def raw_state(root, lane_name):
    """READ-ONLY raw file evidence (no account API call)."""
    os.environ["FOREX_AGENT_HOME"] = os.path.join(root, "home")   # read-only evidence of THIS scratch root (the close-intent journal path follows this env)
    ad = os.path.join(root, "home", "paper_account"); out = {}
    out["files"] = {n: _sha(os.path.join(ad, n)) for n in ("ledger.json", "ledger.json.bak", "events.jsonl")}
    evs, bad, ex = _jl(root)
    out["journal_lines"] = len(evs); out["journal_bad"] = bad; out["journal_exists"] = ex
    out["journal_kinds"] = [e.get("event") for e in evs]
    out["debit_pos_ids"] = [e.get("pos_id") for e in evs if e.get("event") == "debit"]
    out["create_account_ids"] = [e.get("account_id") for e in evs if e.get("event") == "create"]
    out["seqs"] = [e.get("event_id") for e in evs if e.get("event_id")]
    try:
        with open(os.path.join(ad, "ledger.json")) as f: led = _json.load(f)
        out["ledger"] = {"account_id": led.get("account_id"), "seq": led.get("seq"), "cash": led.get("cash"), "cash_spendable": led.get("cash_spendable"),
                         "open_keys": sorted(k for ln in (led.get("lanes") or {}).values() for k in ((ln or {}).get("open") or {}))}
    except Exception as e: out["ledger"] = "unreadable:%s" % type(e).__name__
    try:
        with open(os.path.join(root, "led", LANE_ARG + ".json")) as f: ld = _json.load(f)
        out["lane_ledger"] = {"positions": sorted((ld.get("positions") or {}).keys()), "closes": len(ld.get("closes") or []), "bankroll": ld.get("bankroll"),
                              "pos_ids": {m: p.get("pa_pos_id") for m, p in (ld.get("positions") or {}).items()}}
    except Exception as e: out["lane_ledger"] = "unreadable:%s" % type(e).__name__
    try:
        ents, st, _t = CI._read(CI.journal_path(lane_name))
        out["intent_events"] = [e.get("ev") for e in ents]; out["intent_status"] = st
    except Exception as e: out["intent_events"] = "unreadable:%s" % type(e).__name__
    return out
def child_run(mult, order):
    """order=after: target entered first, second entered after it (midchain shape). order=before: second entered first, target LAST (trailing shape)."""
    PA = fresh(); cash(PA)   # real API load creates the virgin account
    ad = os.path.join(ROOTDIR, "home", "paper_account")
    def _snap_pre_target():
        shutil.copy(os.path.join(ad, "ledger.json"), os.path.join(ROOTDIR, "ledger.pre-target.json"))
        if os.path.exists(os.path.join(ad, "events.jsonl")): shutil.copy(os.path.join(ad, "events.jsonl"), os.path.join(ROOTDIR, "events.pre-target.jsonl"))
    def _ent(m):
        r = _enter(m); p = (L.open_positions() or {}).get(m) or {}
        if not (r[0] and p.get("pa_pos_id")):
            print("ENTRY-REFUSED lane=%s mint=%s ok=%s reason=%r" % (LANE_ARG, m, bool(r[0]), r[1]), flush=True); sys.exit(5)
        return p
    if order == "before":
        _ent(MINT2); print("ENTRY2 cash=%s" % cash_str(cash(PA)), flush=True)
        _snap_pre_target(); pos = _ent(MINT)
    else:
        _snap_pre_target(); pos = _ent(MINT)
    print("ENTRY lane=%s mint=%s nonsim=%s pa_pos_id=%s" % (LANE_ARG, MINT, (pos.get("execution") or {}).get("source") != "jupiter_quote_sim", pos.get("pa_pos_id")), flush=True)
    if order != "before":
        _ent(MINT2); print("ENTRY2 cash=%s" % cash_str(cash(PA)), flush=True)
    print("ENTRYCASH %s" % cash_str(cash(PA)), flush=True)
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
def child_recover(entry_cash, reuse_stake, reuse_qty):
    pre = raw_state(ROOTDIR, LANE_NAME)
    ents0, st0, _t = CI._read(CI.journal_path(LANE_NAME))
    its = [e for e in ents0 if e.get("ev") == "intent"]
    op = (its[0].get("settle_ops") or [{}])[0] if its else {}
    op_id = op.get("op_id"); pid = its[0].get("pos_id") if its else None
    PA = fresh()
    _logging.getLogger().addHandler(_H()); _logging.getLogger().setLevel(_logging.INFO)
    reuse = None
    if reuse_stake > 0:   # LABELLED injection (reuse): a REAL account debit after the epoch loss, same lane/mint/stake/qty
        _ok, _why, _p2 = PA.try_debit(LANE_NAME, MINT, reuse_stake, reuse_qty, "epochdiag-reuse-op")
        reuse = {"ok": bool(_ok), "why": str(_why)[:120], "new_pos_id": _p2, "same_as_intent_pos_id": _p2 == pid}
    pre2 = raw_state(ROOTDIR, LANE_NAME)   # state just before recovery (after any labelled reuse debit, after the account loaded)
    jn0 = None   # set after the API load, below
    cnt = {"poll_exits": 0, "credit_calls": 0}; _pe = L.poll_exits
    def _pe2(*a, **k):
        cnt["poll_exits"] += 1; return _pe(*a, **k)
    L.poll_exits = _pe2
    _cr = PA.credit
    def _crc(*a, **k):
        cnt["credit_calls"] += 1; return _cr(*a, **k)
    PA.credit = _crc
    cash_before = cash(PA)                 # real API load of the (possibly lost/recreated) account happens here
    pre3 = raw_state(ROOTDIR, LANE_NAME)   # raw state AFTER the API load and BEFORE recovery: pre2 -> pre3 exposes load-time replay/creation
    jn0 = pre3["journal_lines"]
    L._RECOVERY_DONE = False
    blocked = L._recover_close_intents()
    cash_after = cash(PA)
    post = raw_state(ROOTDIR, LANE_NAME)          # RECOVERY-ONLY state: no fresh entry has happened
    evs, _b, _e = _jl(ROOTDIR)
    new_evs = evs[jn0:] if len(evs) >= jn0 else []
    gs = getattr(L, "_GATE_STATS", None)
    ents, st, _ = CI._read(CI.journal_path(LANE_NAME))
    succ = {x.get("supersedes") for x in ents if x.get("ev") == "intent" and x.get("supersedes")}
    fro = len([i for i, x in CI._state(ents).items() if x["intent"] is not None and not x["applied"] and x["frozen"] is not None and i not in succ])
    res = {"op_id": op_id, "pos_id": pid, "blocked_returned": blocked, "outcome": PA.op_outcome(op_id) if op_id else None,
           "pos_ever_existed": PA.pos_ever_existed(pos_id=pid, lane=LANE_NAME, mint=MINT), "credit_calls": cnt["credit_calls"], "cnt": cnt,
           "new_event_kinds": [str(x.get("event")) + ("/reason" if x.get("reason") else "") for x in new_evs],
           "new_credit_events_for_op": len([x for x in new_evs if x.get("op_id") == op_id and x.get("event") == "credit"]),
           "new_debit_events": len([x for x in new_evs if x.get("event") == "debit"]),
           "new_credit_net_sum": round(sum(float(x.get("net") or 0.0) for x in new_evs if x.get("event") == "credit"), 6),
           "all_credit_events_for_op": len([x for x in evs if x.get("op_id") == op_id and x.get("event") == "credit"]),
           "new_settle_events_pid_or_mint": len([x for x in new_evs if x.get("event") in ("credit", "write_down") and (x.get("pos_id") == pid or x.get("mint") == MINT)]),
           "all_settle_events_pid_or_mint": len([x for x in evs if x.get("event") in ("credit", "write_down") and (x.get("pos_id") == pid or x.get("mint") == MINT)]),
           "new_write_down_events": len([x for x in new_evs if x.get("event") == "write_down"]),
           "new_credit_events_detail": [{"op_id": x.get("op_id"), "pos_id": x.get("pos_id"), "mint": x.get("mint"), "net": x.get("net"), "proceeds": x.get("proceeds"), "fee": x.get("fee"), "qty": x.get("qty")} for x in new_evs if x.get("event") == "credit"],
           "orphan_events": [str(x.get("event")) + ("/reason" if x.get("reason") else "") for x in evs if x.get("op_id") == op_id and "orphan" in str(x.get("event"))],
           "degraded_events": [str(x.get("event")) for x in evs if x.get("op_id") == op_id and "degraded" in str(x.get("event"))],
           "journal_status": PA._journal_read()[1], "intent_status": st, "pending": len(CI.pending(LANE_NAME)), "frozen_open": fro,
           "intent_applied": any(x.get("ev") == "applied" for x in ents), "entry_blocked": bool(L._ENTRY_BLOCKED),
           "cash_before_recovery": cash_before, "cash_after_recovery": cash_after,
           "raw_cash_pre3": (pre3["ledger"] or {}).get("cash") if isinstance(pre3["ledger"], dict) else None, "raw_cash_post": (post["ledger"] or {}).get("cash") if isinstance(post["ledger"], dict) else None,
           "raw_seq_pre2": (pre2["ledger"] or {}).get("seq") if isinstance(pre2["ledger"], dict) else None, "raw_seq_pre3": (pre3["ledger"] or {}).get("seq") if isinstance(pre3["ledger"], dict) else None,
           "raw_cash_pre2": (pre2["ledger"] or {}).get("cash") if isinstance(pre2["ledger"], dict) else None, "pre3_journal_lines": pre3["journal_lines"], "entry_cash": entry_cash, "reuse": reuse,
           "gate_stats": dict(gs) if isinstance(gs, dict) else None,
           "gate_logs": {k: len([m for m in LOGS if "GATE verdict=%s" % k in m]) for k in ("ok", "freeze", "hold")},
           "recover_logs": [m[:240] for m in LOGS if "orphan" in m.lower() or "FROZEN" in m or "rejected by the gate" in m or "account re-apply" in m][:6],
           "lane_closes_delta": (post["lane_ledger"]["closes"] - pre3["lane_ledger"]["closes"]) if isinstance(post["lane_ledger"], dict) and isinstance(pre3["lane_ledger"], dict) else None}
    print("PRE " + _json.dumps(pre, default=str), flush=True)
    print("PRE2 " + _json.dumps(pre2, default=str), flush=True)
    print("PRE3 " + _json.dumps(pre3, default=str), flush=True)
    print("POST " + _json.dumps(post, default=str), flush=True)
    print("RESULT " + _json.dumps(res, default=str), flush=True)
    # SEPARATE LATER STEP (cash here is NOT used for any reconciliation): a fresh entry attempt shows whether entries are blocked
    e = _enter("EpochDiagNew")
    print("ENTRYTRY " + _json.dumps({"opened": bool(e[0]), "entry_blocked": bool(L._ENTRY_BLOCKED), "try": repr(e)[:200]}, default=str), flush=True)
if MODE == "lane": print("LANE=%s" % LANE_NAME); sys.exit(0)
if MODE == "run": child_run(sys.argv[4], sys.argv[3]); sys.exit(0)
if MODE == "recover": child_recover(float(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5])); sys.exit(0)

FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n, flush=True)
    if not c: FAILS.append(n)
class _TO(Exception): pass
def spawn(args):
    left = BUDGET_S - (time.time() - T0)
    if left < 8: raise _TO("budget exhausted (%.0fs of %.0fs used)" % (time.time() - T0, BUDGET_S))
    try:
        p = subprocess.run([sys.executable, "-B", os.path.abspath(__file__)] + args, capture_output=True, text=True, timeout=min(25.0, left), env=dict(os.environ))
    except subprocess.TimeoutExpired:
        raise _TO("spawn timeout %r" % (args[:2],))
    return p.returncode, p.stdout, p.stderr
def inject(root, mode):
    """LABELLED injection on the SCRATCH paper account files only."""
    ad = os.path.join(root, "home", "paper_account"); lj = os.path.join(ad, "ledger.json"); jj = os.path.join(ad, "events.jsonl")
    if mode == "none": return
    if mode in ("wipe_all", "reuse"):
        for n in ("ledger.json", "ledger.json.bak", "events.jsonl"):
            if os.path.exists(os.path.join(ad, n)): os.remove(os.path.join(ad, n))
    elif mode == "journal_loss": os.remove(jj)
    elif mode == "ledger_loss":
        for n in ("ledger.json", "ledger.json.bak"): os.remove(os.path.join(ad, n))
    elif mode == "torn_mid":
        lines = open(jj).read().splitlines(keepends=True)
        with open(jj, "w") as f: f.writelines(lines[:-1] + ["{torn record\n"] + lines[-1:])
    elif mode == "stale_ledger":
        for n in ("ledger.json", "ledger.json.bak"): shutil.copy(os.path.join(root, "ledger.pre-target.json"), os.path.join(ad, n))
    elif mode == "trail_cut":   # target is the LAST debit (order=before): cut at the clean line boundary just before the TARGET debit, ledger kept
        lines = open(jj).read().splitlines(keepends=True); k = [i for i, l in enumerate(lines) if '"event": "debit"' in l and MINT + '#' in l.replace(MINT2, "")][0]
        with open(jj, "w") as f: f.writelines(lines[:k])
    elif mode == "midchain":    # target is the FIRST debit (order=after): ONLY the target debit line removed; the later debit stays
        lines = open(jj).read().splitlines(keepends=True); k = [i for i, l in enumerate(lines) if '"event": "debit"' in l and (MINT + "#") in l][0]
        with open(jj, "w") as f: f.writelines(lines[:k] + lines[k + 1:])
    elif mode == "pair_rollback":   # pre-target ledger, bak AND journal restored together (a consistent clean older pair)
        for n in ("ledger.json", "ledger.json.bak"): shutil.copy(os.path.join(root, "ledger.pre-target.json"), os.path.join(ad, n))
        pj = os.path.join(root, "events.pre-target.jsonl")
        if os.path.exists(pj): shutil.copy(pj, jj)
        elif os.path.exists(jj): os.remove(jj)
def verify_injection(root, mode, pre_raw):
    """The label must match the files BEFORE recovery runs (hashes/existence, no account API)."""
    ad = os.path.join(root, "home", "paper_account"); post = raw_state(root, "x"); f = post["files"]; pf = pre_raw["files"]
    pre_t = _sha(os.path.join(root, "ledger.pre-target.json")); pre_j = _sha(os.path.join(root, "events.pre-target.jsonl"))
    if mode == "none": return f == pf
    if mode in ("wipe_all", "reuse"): return all(v is None for v in f.values())
    if mode == "journal_loss": return f["events.jsonl"] is None and f["ledger.json"] == pf["ledger.json"] and f["ledger.json.bak"] == pf["ledger.json.bak"]
    if mode == "ledger_loss": return f["ledger.json"] is None and f["ledger.json.bak"] is None and f["events.jsonl"] == pf["events.jsonl"]
    if mode == "torn_mid": return post["journal_bad"] == 1 and f["ledger.json"] == pf["ledger.json"] and post["journal_lines"] == pre_raw["journal_lines"]
    if mode == "stale_ledger": return f["ledger.json"] == pre_t and f["ledger.json.bak"] == pre_t and f["events.jsonl"] == pf["events.jsonl"] and pre_t != pf["ledger.json"]
    if mode == "trail_cut": return post["journal_lines"] < pre_raw["journal_lines"] and f["ledger.json"] == pf["ledger.json"] and post["journal_bad"] == 0
    if mode == "midchain": return post["journal_lines"] == pre_raw["journal_lines"] - 1 and f["ledger.json"] == pf["ledger.json"] and post["journal_bad"] == 0
    if mode == "pair_rollback":
        _ev, _bad, _ex = _jl(root); _cr = [e for e in _ev if e.get("event") == "create"]
        _seqs = [int(str(e.get("event_id")).split("#")[1]) for e in _ev if str(e.get("event_id", "")).startswith("ev#")]
        _lg = post["ledger"] if isinstance(post["ledger"], dict) else {}
        consistent = _ex and _bad == 0 and len(_cr) == 1 and _cr[0].get("account_id") == _lg.get("account_id") and bool(_seqs) and _lg.get("seq") == max(_seqs)
        print("  E8-PAIR-CONSISTENCY journal_exists=%s bad=%s creates=%d create_account_id==ledger=%s ledger_seq=%s last_journal_seq=%s" % (_ex, _bad, len(_cr), (_cr[0].get("account_id") == _lg.get("account_id")) if _cr else None, _lg.get("seq"), max(_seqs) if _seqs else None), flush=True)
        return f["ledger.json"] == pre_t and f["ledger.json.bak"] == pre_t and f["events.jsonl"] == pre_j and consistent
    return False
def classify(r):
    if r["new_credit_events_for_op"] == 1: return "CREDITED_ONCE"
    if r["intent_applied"] and r["new_credit_events_for_op"] == 0 and r["outcome"] == "orphan": return "CONVERGED_ORPHAN_NOOP_(no credit)"
    if r["frozen_open"] >= 1: return "FROZEN"
    if r["pending"] >= 1: return "HOLD_PENDING"
    return "OTHER"
COMPLETED = {"n": 0}
SC = (("E0-CONTROL-healthy-no-injection", "none", "after"), ("E1-wipe_all-new-account-generation", "wipe_all", "after"), ("E2-journal_loss-ledger-kept", "journal_loss", "after"),
      ("E3-ledger_loss-ledger-and-bak-removed-journal-kept", "ledger_loss", "after"), ("E4-torn_mid-journal", "torn_mid", "after"), ("E5-stale_ledger-restore-journal-intact", "stale_ledger", "after"),
      ("E6-trail_cut-target-is-last-debit-cut-before-it-ledger-kept", "trail_cut", "before"), ("E7-midchain-target-debit-record-removed-later-debit-stays", "midchain", "after"),
      ("E8-pair_rollback-consistent-older-ledger-bak-and-journal", "pair_rollback", "before"), ("E9-reuse-wipe_all-then-real-debit-same-lane-mint", "reuse", "after"))
try:
    rc0, out0, err0 = spawn(["lane", ROOTDIR])
    lname = out0.strip().split("LANE=")[-1] if "LANE=" in out0 else ""
    check("SETUP actual final lane name derived from the module: %r (stderr %r)" % (lname, err0[-80:]), bool(lname) and rc0 == 0 and lname == "momentum")
except _TO as ex:
    check("SETUP lane derivation: %s" % ex, False); lname = ""
for tag, mode, order in (SC if lname else ()):
    tag = "%s-%s" % (tag, LANE_ARG)
    try:
        root = _tf.mkdtemp(prefix="genbind-%s-" % tag[:40])
        rc, out, err = spawn(["run", root, order, "2.0"])
        _el = [l for l in out.splitlines() if l.startswith("ENTRY ")]; _e2 = [l for l in out.splitlines() if l.startswith("ENTRY2 ")]; _ec = [l for l in out.splitlines() if l.startswith("ENTRYCASH ")]
        ok_e = bool(_el) and bool(_e2) and bool(_ec) and "nonsim=True" in _el[0] and ("pa_pos_id=%s/%s#" % (lname, MINT)) in _el[0] and "Traceback" not in err
        check("%s: SETUP both real entries opened, target pos_id real (%s | %s rc %d stderr %r)" % (tag, _el[:1], _e2[:1], rc, err[-120:]), ok_e)
        check("%s: SETUP API cash after both entries is numeric (%s); a None snapshot would be recorded, not crash" % (tag, _ec[:1]), bool(_ec) and _ec[0].split()[1] != "None")
        check("%s: child KILLED by SIGKILL right before PA.credit of the TARGET (rc %d)" % (tag, rc), rc == -_signal.SIGKILL and "KILLHIT" in out and "SURVIVED" not in out)
        if not (ok_e and rc == -_signal.SIGKILL): continue
        _ecs = _ec[0].split()[1]; entry_cash = float(_ecs) if _ecs != "None" else None; pid = _el[0].split("pa_pos_id=")[1].split()[0]
        before = raw_state(root, lname)   # pid-specific proof of a complete, clean setup BEFORE the injection (raw files; no account API)
        evs, bad, _ex = _jl(root)
        tdeb = [e for e in evs if e.get("event") == "debit" and e.get("pos_id") == pid]
        led = before["ledger"] if isinstance(before["ledger"], dict) else {}
        cr = [e for e in evs if e.get("event") == "create"]
        pend = CI.pending(lname)   # env of this scratch root was set by raw_state above
        _pop = ((pend[0].get("intent") or {}).get("settle_ops") or [{}])[0].get("op_id") if len(pend) == 1 else None
        prior = [e for e in evs if e.get("event") not in ("create", "debit") and (e.get("pos_id") == pid or e.get("mint") == MINT or (_pop is not None and e.get("op_id") == _pop))]
        _it = (pend[0].get("intent") or {}) if len(pend) == 1 else {}
        _ie = before["intent_events"]
        clauses = [("one_pending_intent", len(pend) == 1), ("not_account_applied", len(pend) == 1 and not pend[0].get("account_applied")),
                   ("intent_pos_id_is_target", _it.get("pos_id") == pid), ("intent_mint_is_target", _it.get("mint") == MINT), ("settle_op_id_present", _pop is not None),
                   ("no_prior_settle_or_other_event_for_pid_mint_op", not prior), ("exactly_one_target_debit", len(tdeb) == 1), ("account_journal_clean", bad == 0),
                   ("exactly_one_create", len(cr) == 1), ("create_account_id_equals_ledger", len(cr) == 1 and cr[0].get("account_id") == led.get("account_id")),
                   ("target_pid_in_debit_pos_ids", pid in before["debit_pos_ids"]), ("lane_position_pa_pos_id_equals_pid", isinstance(before["lane_ledger"], dict) and before["lane_ledger"]["pos_ids"].get(MINT) == pid),
                   ("intent_journal_exactly_one_intent", isinstance(_ie, list) and _ie.count("intent") == 1), ("intent_journal_only_probe_or_intent", isinstance(_ie, list) and all(x in ("probe", "intent") for x in _ie)),
                   ("intent_journal_status_clean", before["intent_status"] is None), ("second_debit_present", any(("%s#" % MINT2) in str(x) for x in before["debit_pos_ids"]))]
        proof = all(v for _k, v in clauses)
        print("  PROOF-CLAUSES %s %s" % (tag, " ".join("%s=%s" % (k, v) for k, v in clauses)), flush=True)
        check("%s: BEFORE injection pid-specific proof: exactly one debit for %s, journal clean (close-intent journal holds exactly one intent record, only probe records besides it: any other event kind fails the proof), one create event whose account_id equals the ledger's, lane position pos_id equals it, intent journaled, second debit present (debits %r, account_id %s, pending intents %d, prior events for pid/mint/op %r)" % (tag, pid, before["debit_pos_ids"], str(led.get("account_id"))[:8], len(pend), [e.get("event") for e in prior]), proof)
        if not proof: continue
        inject(root, mode)
        check("%s: INJECTION-VERIFIED the files match the label %r before recovery (%r)" % (tag, mode, {k: v for k, v in raw_state(root, lname)["files"].items()}), verify_injection(root, mode, before))
        stake = float(tdeb[0].get("stake") or 0.0) if mode == "reuse" else 0.0; qty = float(tdeb[0].get("qty") or 0.0) if mode == "reuse" else 0.0
        rc2, out2, err2 = spawn(["recover", root, ("%.6f" % entry_cash) if entry_cash is not None else "-1", "%.6f" % stake, "%.6f" % qty])
    except _TO as ex:
        check("%s: BOUNDED run: %s" % (tag, ex), False); continue
    g = lambda p: [l for l in out2.splitlines() if l.startswith(p)]
    pl, p2l, ql, rl, el = g("PRE "), g("PRE2 "), g("POST "), g("RESULT "), g("ENTRYTRY ")
    check("%s: recovery process exited 0, no crash trace (rc %d, stderr tail %r)" % (tag, rc2, err2[-160:]), rc2 == 0 and pl and p2l and ql and rl and el and "Traceback" not in err2)
    if not (pl and p2l and ql and rl): continue
    pre = _json.loads(pl[0][4:]); pre2 = _json.loads(p2l[0][5:]); post = _json.loads(ql[0][5:]); r = _json.loads(rl[0][7:])
    print("  RAW-BEFORE-INJECTION %s %s" % (tag, _json.dumps(before, default=str)), flush=True)
    print("  RAW-PRE %s %s" % (tag, _json.dumps(pre, default=str)), flush=True)
    print("  RAW-PRE2 %s %s" % (tag, _json.dumps(pre2, default=str)), flush=True)
    print("  RAW-POST %s %s" % (tag, _json.dumps(post, default=str)), flush=True)
    print("  RAW-RESULT %s %s" % (tag, _json.dumps(r, default=str)), flush=True)
    print("  RAW-ENTRYTRY %s %s" % (tag, el[0][9:] if el else None), flush=True)
    check("%s: INSTRUMENT zero polls in the recovery process (%d) and the gate/recovery path was reached (gate stats %r)" % (tag, r["cnt"]["poll_exits"], r["gate_stats"]), r["cnt"]["poll_exits"] == 0 and r["gate_stats"] is not None and sum(r["gate_stats"].values()) >= 1)
    _rb, _ra = r["raw_cash_pre3"], r["raw_cash_post"]
    _api_ok = r["cash_before_recovery"] is not None and r["cash_after_recovery"] is not None
    _raw_ok = isinstance(_rb, (int, float)) and isinstance(_ra, (int, float))
    delta = round(float(_ra) - float(_rb), 6) if _raw_ok else (round(float(r["cash_after_recovery"]) - float(r["cash_before_recovery"]), 6) if _api_ok else None)
    print("OBS %s: CASH SOURCES raw-ledger pre-load(pre2)=%s post-load(pre3)=%s post-recovery=%s; API before=%s after=%s; raw seq pre2=%s pre3=%s; journal lines pre2=%d pre3=%d (load-time replay/creation visible as pre2->pre3 change); delta basis=%s" % (
        tag, r["raw_cash_pre2"], _rb, _ra, r["cash_before_recovery"], r["cash_after_recovery"], r["raw_seq_pre2"], r["raw_seq_pre3"], pre2["journal_lines"], r["pre3_journal_lines"], "raw-ledger" if _raw_ok else ("api" if _api_ok else "UNMEASURED")), flush=True)
    check("%s: INVARIANT event-level reconciliation (counts and amounts only, not conservation): credit events for the op <=1 (%d new, %d all), credit+write_down events for the pid or mint <=1 (%d new, %d all), write_down new %d, no new debit in recovery (%d), cash delta %s == net sum of credit events journaled during recovery %.6f, lane close delta %r <=1" % (
          tag, r["new_credit_events_for_op"], r["all_credit_events_for_op"], r["new_settle_events_pid_or_mint"], r["all_settle_events_pid_or_mint"], r["new_write_down_events"], r["new_debit_events"], delta, r["new_credit_net_sum"], r["lane_closes_delta"]),
          r["new_credit_events_for_op"] <= 1 and r["all_credit_events_for_op"] <= 1 and r["new_settle_events_pid_or_mint"] <= 1 and r["all_settle_events_pid_or_mint"] <= 1 and r["new_debit_events"] == 0 and delta is not None and abs(delta - r["new_credit_net_sum"]) < 1e-6 and r["lane_closes_delta"] is not None and r["lane_closes_delta"] <= 1)
    cl = classify(r)
    try: _et = _json.loads(el[0][9:]) if el else {}
    except ValueError: _et = {}
    if mode == "none":
        check("%s: PREDECLARED E0 credits exactly once (op %d, pid-or-mint %d), entries not blocked, cash up (delta %s)" % (tag, r["new_credit_events_for_op"], r["new_settle_events_pid_or_mint"], delta),
              r["new_credit_events_for_op"] == 1 and r["new_settle_events_pid_or_mint"] == 1 and not r["entry_blocked"] and delta is not None and delta > 0)
    if mode in ("wipe_all", "ledger_loss", "torn_mid", "pair_rollback", "reuse"):
        check("%s: PREDECLARED no credit/write_down for the old op or pid in recovery (new credit for op %d, new settle pid/mint %d, write_down %d)" % (tag, r["new_credit_events_for_op"], r["new_settle_events_pid_or_mint"], r["new_write_down_events"]),
              r["new_credit_events_for_op"] == 0 and r["new_settle_events_pid_or_mint"] == 0 and r["new_write_down_events"] == 0)
        check("%s: PREDECLARED no silent orphan convergence (class %s) and the intent is not marked applied without a credit" % (tag, cl), not cl.startswith("CONVERGED_ORPHAN") and not (r["intent_applied"] and r["new_credit_events_for_op"] == 0))
        check("%s: PREDECLARED a fresh entry is NOT opened afterwards (entry try %r, entry_blocked %s)" % (tag, _et, r["entry_blocked"]), _et.get("opened") is False)
    gen_b = (before["ledger"] or {}).get("account_id") if isinstance(before["ledger"], dict) else None
    gen_a = (post["ledger"] or {}).get("account_id") if isinstance(post["ledger"], dict) else None
    print("OBS %s: CLASS=%s blocked_returned=%s outcome=%s pos_ever_existed=%s intent_applied=%s frozen_open=%d pending=%d entry_blocked=%s credit_calls=%d new_event_kinds=%s orphan_events=%s degraded_events=%s journal_status=%s" % (
        tag, cl, r["blocked_returned"], r["outcome"], r["pos_ever_existed"], r["intent_applied"], r["frozen_open"], r["pending"], r["entry_blocked"], r["credit_calls"], r["new_event_kinds"], r["orphan_events"], r["degraded_events"], r["journal_status"]), flush=True)
    print("OBS %s: DEBIT-WAS-PRESENT-BEFORE-INJECTION=yes (proven above); LOSS-HIDING-CANDIDATE=%s (intent terminal with no credit and outcome orphan)" % (tag, "YES" if cl.startswith("CONVERGED_ORPHAN") else "no"), flush=True)
    print("OBS %s: CANDIDATE-EVIDENCE ONLY (not proof): account_id before=%s after=%s changed=%s; ledger seq before=%s after=%s; pos_id of intent=%s reuse=%s" % (
        tag, str(gen_b)[:8], str(gen_a)[:8], gen_b != gen_a, (before["ledger"] or {}).get("seq") if isinstance(before["ledger"], dict) else None, (post["ledger"] or {}).get("seq") if isinstance(post["ledger"], dict) else None, pid, r["reuse"]), flush=True)
    print("OBS %s: cash entry=%s before-recovery=%s after-recovery=%s; lane ledger before=%r after=%r" % (tag, entry_cash, r["cash_before_recovery"], r["cash_after_recovery"], pre2["lane_ledger"], post["lane_ledger"]), flush=True)
    print("OBS %s: event-specific credit proceeds during recovery: %r; new write_down events %d" % (tag, r["new_credit_events_detail"], r["new_write_down_events"]), flush=True)
    for m in r["recover_logs"]: print("OBS %s: LOG %s" % (tag, m), flush=True)
    if mode == "reuse":
        ru = r["reuse"] or {}
        _old_op = tdeb[0].get("op_id"); _new_op = "epochdiag-reuse-op"
        check("%s: E9 REAL reuse debit succeeded (ok %r, why %r, new pos_id %r)" % (tag, ru.get("ok"), ru.get("why"), ru.get("new_pos_id")), ru.get("ok") is True and bool(ru.get("new_pos_id")))
        _col = ru.get("new_pos_id") == pid
        _cred_pids = [d.get("pos_id") for d in r["new_credit_events_detail"]]
        print("OBS %s: E9 %s: intent pos_id=%s new account pos_id=%s; old entry op_id=%s new debit op_id=%s; old account_id=%s new account_id=%s; intent settle op=%s; credits journaled during recovery on pos_ids %r (new pos_id credited=%s)" % (
            tag, "COLLISION" if _col else "NO-COLLISION", pid, ru.get("new_pos_id"), _old_op, _new_op, str(gen_b)[:8], str(gen_a)[:8], r["op_id"], _cred_pids, ru.get("new_pos_id") in _cred_pids), flush=True)
    COMPLETED["n"] += 1
print("OBS no positive safe-pre-epoch (never-debited) control exists in this diagnostic; the original orphan purpose stays separate coverage. Elapsed %.1fs of the %.0fs budget." % (time.time() - T0, BUDGET_S))
check("COMPLETE: %d of %d scenarios produced a full RESULT within the budget (lane %r derived)" % (COMPLETED["n"], len(SC), lname), lname != "" and COMPLETED["n"] == len(SC) and (time.time() - T0) < BUDGET_S + 5)
print("FAILS: %d" % len(FAILS))
sys.exit(1 if FAILS else 0)
