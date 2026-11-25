#!/usr/bin/env python3
"""bb84 (= bb83 + explicit target-path reachability instrumentation and direct write-down rows) REAL-CALLER frozen-hold harness, MOMENTUM, SOURCE ONLY (never run). The refused write-down and the refused leg credit are produced by the REAL poll_exits against a real generation mismatch (parent wipes the account files so the lane position is bound to an old generation); NO hand-written freezes except the unit child, which uses real begin/freeze and is labelled. Position age is set by editing the SCRATCH lane ledger (labelled fixture). Mutant: a labelled model of the BB81 pre-check (exact-op only)."""
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
    d["n_probe"] = len([e for e in ents if e.get("ev") == "probe"]); d["n_lines"] = len(ents)
    d["pending_exit"] = {k: pos.get(k) for k in ("pending_exit",)}["pending_exit"] if d["pos_open"] else None
    d["ev_kinds"] = sorted(set(e.get("ev") for e in ents))
    d["pending_unfrozen"] = len(CI.pending(LANE_NAME)); d["frozen_ids"] = CI.frozen_unresolved(jp)[0]; d["frozen_unresolved"] = len(d["frozen_ids"])
    evs = _jl(ROOTDIR)[0]
    d["credit"] = len([e for e in evs if e.get("event") == "credit"]); d["debit"] = len([e for e in evs if e.get("event") == "debit"])
    d["wal_events"] = sorted(set(e.get("event") for e in evs))
    mp = os.path.join(_acct_dir(ROOTDIR), "unresolved_generation_refusals.jsonl")
    d["marker_records"] = len([1 for ln in open(mp) if ln.strip()]) if os.path.exists(mp) else 0
    fp = os.path.join(os.environ.get("FOREX_AGENT_HOME", "."), "demo_fill_journal.json")   # the REAL quote-sim fill journal (demo_fill.py), read from disk
    try: fb = open(fp, "rb").read()
    except OSError: fb = None
    d["fj_exists"] = fb is not None; d["fj_sha"] = hashlib.sha256(fb or b"").hexdigest()[:16]; d["fj_len"] = len(fb or b"")
    try: _fj = _json.loads(fb) if fb else None
    except ValueError: _fj = "unparsable"
    d["fj_n"] = (len(_fj) if isinstance(_fj, (dict, list)) else (None if _fj is None else -1))
    d["fj_keys"] = sorted(str(k) for k in _fj)[:12] if isinstance(_fj, dict) else None
    return d
def _pf2(mult):
    def f(chain, mint, spot_index=None):
        pos = (L.open_positions() or {}).get(mint) or {}
        return float(pos.get("entry_price") or 0) * mult
    return f
def _pf0(chain, mint, spot_index=None): return 0.0
def _age():
    with L._ledger_lock():
        d = L._read(); d["positions"][MINT]["entry_ts"] = time.time() - 26 * 3600; L._write(d)
def _pos(): return (L.open_positions() or {}).get(MINT) or {}
GATE = []; WD = {"n": 0}
def _instrument():
    """TARGET-PATH EVIDENCE: wrap the REAL _intent_gate (log key, whether the exit meta classifies as a write-down, and the real return) and count REAL _pa_write_down calls. The wrappers call the real functions unchanged."""
    og = L._intent_gate; ow = L._pa_write_down
    def g(pos, key):
        r = og(pos, key); m = pos.get("_exit_meta") or {}
        GATE.append({"key": str(key), "wd": L._pkind(m.get("reason"), m.get("pkind")) == "write_down_zero", "ret": bool(r)}); return r
    def w(pos, reason):
        WD["n"] += 1; return ow(pos, reason)
    L._intent_gate = g; L._pa_write_down = w
def _mark(): return {"gate": [dict(x) for x in GATE], "wd_calls": WD["n"]}
def _mutant_bb81():
    def f(pos, key):
        _all = CI.frozen_for_op(LANE_NAME, pos["mint"], pos.get("pa_pos_id"), "%s:%s:%s:%s" % (LANE_NAME, key if str(key).startswith("remaining:") else "leg:%s" % key, pos["mint"], pos.get("pa_pos_id")))
        _wd = CI.frozen_for_op(LANE_NAME, pos["mint"], pos.get("pa_pos_id"), "%s:writedown:%s:%s" % (LANE_NAME, pos["mint"], pos.get("pa_pos_id")))
        return None if _all is None or _wd is None else [(i, w) for i, w in list(_all) + list(_wd)]
    L._frozen_overlap = f
def child_wdref(variant=""):
    """REAL refused write-down: poll_exits on a >24h position with no quote, against a stale generation. Then more real cycles and real direct close calls."""
    PA = fresh(); cash(PA); out = {}; _instrument()
    _age(); s0 = _snap(PA); out["s0"] = s0
    L.poll_exits(price_fn=_pf0); out["s1"] = _snap(PA); out["m1"] = _mark()
    L.poll_exits(price_fn=_pf0); out["s2"] = _snap(PA); out["m2"] = _mark()
    for _i in range(3): L.poll_exits(price_fn=_pf0)
    out["s2b"] = _snap(PA)
    pos = _pos(); r0 = float(pos.get("realized_usd") or 0)
    out["ret_remaining_wd"] = L._close_remaining(pos, 0.0, "time_stop_24h_unquotable")
    out["ret_remaining_px"] = L._close_remaining(pos, 0.002, "hard_stop_-50pct")
    out["ret_leg_a"] = L._close_leg(pos, "a", 0.002, "leg_a_2x")
    out["realized_unchanged"] = float(pos.get("realized_usd") or 0) == r0; out["legs_after"] = {k: bool((pos.get("legs") or {}).get(k, {}).get("done")) for k in ("a", "b", "c")}
    out["s3"] = _snap(PA)
    try: e1 = _enter(MINT); out["entry_same_mint"] = [bool(e1[0]), str(e1[1])[:160]]
    except Exception as ex: out["entry_same_mint"] = [False, "EXC %s" % type(ex).__name__]
    try: e2 = _enter(MINT2); out["entry_other_mint"] = [bool(e2[0]), str(e2[1])[:160]]
    except Exception as ex: out["entry_other_mint"] = [False, "EXC %s" % type(ex).__name__]
    try: r = PA.try_debit(LANE_NAME, "DirectProbeMint", 1.0, 1000.0, "probe:direct:wd"); out["direct"] = [bool(r[0]), str(r[1])[:240]]
    except Exception as ex: out["direct"] = [False, "EXC %s" % type(ex).__name__]
    out["s4"] = _snap(PA)
    print("RESULT " + _json.dumps(out, default=str), flush=True)
def child_legwd(variant=""):
    """REAL refused leg-a credit (2x poll vs stale generation), THEN a real write-down poll and a real remaining close on the same position."""
    PA = fresh(); cash(PA); out = {}; _instrument()
    L.poll_exits(price_fn=_pf2(2.0)); out["s1"] = _snap(PA); out["m1"] = _mark()
    if variant == "mutant": _mutant_bb81()
    _age(); L.poll_exits(price_fn=_pf0); out["s2"] = _snap(PA); out["m2"] = _mark()
    pos = _pos(); out["ret_wd"] = L._close_remaining(pos, 0.0, "time_stop_24h_unquotable"); out["s2w"] = _snap(PA); out["m2w"] = _mark()
    out["ret_remaining"] = L._close_remaining(pos, 0.002, "hard_stop_-50pct"); out["s3"] = _snap(PA); out["m3"] = _mark()
    print("RESULT " + _json.dumps(out, default=str), flush=True)
def child_unit2():
    """Real begin/freeze (labelled hand-built intents) read through the REAL _frozen_overlap on a real entered position."""
    PA = fresh(); cash(PA); _enter(MINT); pos = _pos(); pid = pos["pa_pos_id"]; out = {}
    def op(s, p=pid): return "momentum:%s:%s:%s" % (s, MINT, p)
    def frz(s, p=pid):
        i = CI.begin(LANE_NAME, MINT, p, [{"kind": "credit", "op_id": op(s, p), "amount": 1.0, "qty": 1.0}], {"close_ts": time.time(), "leg_suffix": s}); CI.freeze(LANE_NAME, i, "gen refused"); return i
    out["clean_a"] = L._frozen_overlap(pos, "a") == []
    frz("leg:a")
    ids = lambda k, p=None: [x[0] for x in (L._frozen_overlap(p or pos, k) or [])]
    out["a_held"] = len(ids("a")) == 1; out["b_free"] = ids("b") == [] and ids("c") == []
    out["remaining_held"] = len(ids("remaining:bc")) == 1
    other = dict(pos, pa_pos_id=pid + "_NEWGEN"); out["newgen_free"] = L._frozen_overlap(other, "a") == [] and L._frozen_overlap(other, "remaining:abc") == []
    pw = dict(pos, _exit_meta={"reason": "time_stop_24h_unquotable", "pkind": None}); out["writedown_held"] = len(ids("b", pw)) == 1
    out["a_still_free_of_b_only"] = ids("b") == []
    jp = CI.journal_path(LANE_NAME); b4 = os.path.getsize(jp); L._frozen_overlap(pos, "a"); out["readonly"] = os.path.getsize(jp) == b4
    with open(jp, "ab") as f: f.write(b"{garbage\n")
    out["unreadable_none"] = L._frozen_overlap(pos, "a") is None
    print("RESULT " + _json.dumps(out), flush=True)
def child_unit3():
    """frozen REMAINING close of legs b,c holds legs b and c and any span, not leg a."""
    PA = fresh(); cash(PA); _enter(MINT); pos = _pos(); pid = pos["pa_pos_id"]; out = {}
    i = CI.begin(LANE_NAME, MINT, pid, [{"kind": "credit", "op_id": "momentum:remaining:bc:%s:%s" % (MINT, pid), "amount": 1.0, "qty": 1.0}], {"close_ts": time.time(), "leg_suffix": "remaining:bc"}); CI.freeze(LANE_NAME, i, "gen refused")
    g = lambda k: len(L._frozen_overlap(pos, k) or [])
    out["b_held"] = g("b") == 1; out["c_held"] = g("c") == 1; out["a_free"] = g("a") == 0; out["span_held"] = g("remaining:c") == 1
    print("RESULT " + _json.dumps(out), flush=True)
if MODE == "open": child_open(); sys.exit(0)
if MODE == "wdref": child_wdref(sys.argv[3] if len(sys.argv) > 3 else ""); sys.exit(0)
if MODE == "legwd": child_legwd(sys.argv[3] if len(sys.argv) > 3 else ""); sys.exit(0)
if MODE == "unit2": child_unit2(); sys.exit(0)
if MODE == "unit3": child_unit3(); sys.exit(0)
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
def run_child(mode, variant=""):
    root = newroot("C"); do_open(root); wipe(root)
    rc, out, err = spawn([mode, root] + ([variant] if variant else [])); r = res(out)
    if r is None: print("FAIL SETUP %s child (rc %s, %s)" % (mode, rc, err[-300:])); raise SystemExit(2)
    return r
def run_unit(mode):
    rc, out, err = spawn([mode, newroot("U")]); r = res(out)
    if r is None: print("FAIL SETUP %s (rc %s, %s)" % (mode, rc, err[-300:])); raise SystemExit(2)
    return r
KEYS = ("credit", "debit", "intent_ops", "pending_unfrozen", "frozen_unresolved", "journal_sha", "n_lines", "pos_open")
try:
    w = run_child("wdref"); a0, a1, a2, a2b, a3 = w["s0"], w["s1"], w["s2"], w["s2b"], w["s3"]
    wdop = [o for o in a1["intent_ops"] if ":writedown:" in o]
    check("C1 REAL refused write-down: the real poll_exits time-stop write-down against a stale generation leaves ONE frozen write-down obligation (op id lane:writedown:mint:pos), the position open with no leg done, no credit or debit (%r)" % ({k: a1[k] for k in ("intent_ops", "frozen_unresolved", "pending_unfrozen", "pos_open", "legs_done", "credit", "debit")},), len(a1["intent_ops"]) == 1 and len(wdop) == 1 and a1["frozen_unresolved"] == 1 and a1["pending_unfrozen"] == 0 and a1["pos_open"] is True and a1["legs_done"] == {"a": False, "b": False, "c": False} and a1["credit"] == a0["credit"] and a1["debit"] == a0["debit"])
    check("C1b TARGET PATH REACHED: in the first real cycle the REAL write-down caller ran (_pa_write_down calls %r) behind a gate call classified as write-down that was allowed (gate log %r)" % (w["m1"]["wd_calls"], w["m1"]["gate"]), w["m1"]["wd_calls"] == 1 and any(x["wd"] and x["ret"] for x in w["m1"]["gate"]))
    pe = a2.get("pending_exit") or {}
    check("C2b TARGET PATH REACHED in the held cycle: the second real cycle reached a write-down-classified gate call that returned False and made no further _pa_write_down call (calls %r, new gate calls %r)" % (w["m2"]["wd_calls"], w["m2"]["gate"][len(w["m1"]["gate"]):]), w["m2"]["wd_calls"] == 1 and any(x["wd"] and not x["ret"] for x in w["m2"]["gate"][len(w["m1"]["gate"]):]))
    check("C2 the next real cycles hold under the real cause (%r), with no new intent and no journal line after the first held cycle (lines %r -> %r -> %r)" % (pe.get("why"), a1["n_lines"], a2["n_lines"], a2b["n_lines"]), pe.get("why") == "frozen_intent_unresolved" and a2["intent_ops"] == a1["intent_ops"] and a2b["journal_sha"] == a2["journal_sha"] and a2b["n_lines"] == a2["n_lines"])
    check("C3 REAL CALLERS after the freeze: a direct write-down close, a remaining close at a quote and a leg-a close each return 0.0, leave every leg open and realized unchanged, add NO intent, NO journal line and NO account credit or debit (%r)" % ({k: w[k] for k in ("ret_remaining_wd", "ret_remaining_px", "ret_leg_a", "realized_unchanged", "legs_after")},), w["ret_remaining_wd"] == 0.0 and w["ret_remaining_px"] == 0.0 and w["ret_leg_a"] == 0.0 and w["realized_unchanged"] is True and w["legs_after"] == {"a": False, "b": False, "c": False} and a3["journal_sha"] == a2b["journal_sha"] and a3["intent_ops"] == a1["intent_ops"] and a3["credit"] == a1["credit"] and a3["debit"] == a1["debit"])
    check("C4 NO REBINDING: the lane position keeps its original generation through every cycle (%r vs %r)" % (a0["pa_account_id"], a3["pa_account_id"]), a0["pa_account_id"] is not None and a3["pa_account_id"] == a0["pa_account_id"] and a3["pa_pos_id"] == a0["pa_pos_id"])
    g = run_child("legwd"); b1, b2, b3 = g["s1"], g["s2"], g["s3"]
    check("X1 REAL frozen leg credit (2x poll, stale generation): one frozen leg-a obligation, leg a open (%r)" % ({k: b1[k] for k in ("intent_ops", "frozen_unresolved", "pos_open", "legs_done")},), len(b1["intent_ops"]) == 1 and ":leg:a:" in b1["intent_ops"][0] and b1["frozen_unresolved"] == 1 and b1["pos_open"] is True and b1["legs_done"]["a"] is False)
    check("X2 CROSS-FAMILY: the later REAL write-down poll opens NO write-down intent and makes no account op while the leg-a credit is frozen (ops %r -> %r, credit %r -> %r, debit %r -> %r, why %r)" % (b1["intent_ops"], b2["intent_ops"], b1["credit"], b2["credit"], b1["debit"], b2["debit"], (b2.get("pending_exit") or {}).get("why")), b2["intent_ops"] == b1["intent_ops"] and b2["credit"] == b1["credit"] and b2["debit"] == b1["debit"] and b2["pos_open"] is True and b2["legs_done"] == b1["legs_done"])
    check("X3 CROSS-FAMILY: a real remaining close (spans leg a) is held too: returns 0.0, no new intent, journal unchanged (ret %r, lines %r -> %r)" % (g["ret_remaining"], b2["n_lines"], b3["n_lines"]), g["ret_remaining"] == 0.0 and b3["intent_ops"] == b1["intent_ops"] and b3["journal_sha"] == b2["journal_sha"] and b3["credit"] == b1["credit"])
    FJK = ("fj_exists", "fj_sha", "fj_len", "fj_n")
    check("C5 MEASURED NO-SALE (write-down family): the real quote-sim FILL JOURNAL on disk is byte-identical across every real cycle and direct close call after the freeze (s1 %r, s2b %r, s3 %r; before the first cycle %r)" % ({k: a1[k] for k in FJK}, {k: a2b[k] for k in FJK}, {k: a3[k] for k in FJK}, {k: a0[k] for k in FJK}), all(a1[k] == a2[k] == a2b[k] == a3[k] for k in FJK))
    check("C6 PUBLIC RE-ENTRY stays refused while the obligation is unresolved (this is NOT authorization of re-entry): the lane entry for the SAME mint and for another mint and a direct account debit are all refused, one position, debit count unchanged (%r)" % ({k: w[k] for k in ("entry_same_mint", "entry_other_mint", "direct")},), w["entry_same_mint"][0] is False and w["entry_other_mint"][0] is False and w["direct"][0] is False and w["s4"]["debit"] == a1["debit"] and w["s4"]["n_positions"] == 1 and w["s4"]["fj_sha"] == a1["fj_sha"])
    check("X4 MEASURED NO-SALE (cross-family): after the first real 2x sale of leg a, the fill journal is byte-identical through the later write-down poll and the real remaining close (after sale %r, after write-down poll %r, after remaining close %r)" % ({k: b1[k] for k in FJK}, {k: b2[k] for k in FJK}, {k: b3[k] for k in FJK}), all(b1[k] == b2[k] == b3[k] for k in FJK))
    check("X5 the first real 2x cycle DID write the fill journal (positive control: the journal fingerprint is capable of changing, so X4/C5 are not vacuous) (%r)" % ({k: b1[k] for k in FJK},), b1["fj_exists"] is True and b1["fj_len"] > 0 and (b1["fj_n"] or 0) >= 1)
    g1, g2, g2w = g["m1"], g["m2"], g["m2w"]
    check("X2a REACHABILITY RECORD: the later write-down POLL may be intercepted by the journal resume of the already-sold leg a (so it may NOT reach the write-down path; X2b is the explicit target-path row); gate calls observed during that poll %r, _pa_write_down calls during it %r (safety condition: none)" % (g2["gate"][len(g1["gate"]):], g2["wd_calls"]), g2["wd_calls"] == 0)
    check("X2b EXPLICIT TARGET PATH: a direct REAL _close_remaining write-down close on the position with the frozen leg-a obligation reaches a write-down-classified gate call that returns False, returns 0.0, never reaches _pa_write_down (calls %r), opens no intent (ops %r -> %r), no account op, fill journal unchanged (%r -> %r); new gate calls %r" % (g2w["wd_calls"], b2["intent_ops"], g["s2w"]["intent_ops"], b2["fj_sha"], g["s2w"]["fj_sha"], g2w["gate"][len(g2["gate"]):]), g["ret_wd"] == 0.0 and g2w["wd_calls"] == 0 and any(x["wd"] and not x["ret"] for x in g2w["gate"][len(g2["gate"]):]) and g["s2w"]["intent_ops"] == b1["intent_ops"] and g["s2w"]["credit"] == b1["credit"] and g["s2w"]["debit"] == b1["debit"] and g["s2w"]["fj_sha"] == b1["fj_sha"] and g["s2w"]["pos_open"] is True)
    u = run_unit("unit2")
    check("U1 overlap rule on a real journal: frozen leg a holds leg a and any spanning close, NOT legs b/c, NOT a new generation of the same mint (%r)" % ({k: u[k] for k in ("clean_a", "a_held", "b_free", "remaining_held", "newgen_free")},), all(u[k] is True for k in ("clean_a", "a_held", "b_free", "remaining_held", "newgen_free")))
    check("U2 a write-down close is held by any frozen intent of the position, the helper is read-only, and an unreadable journal returns None (%r)" % ({k: u[k] for k in ("writedown_held", "readonly", "unreadable_none")},), all(u[k] is True for k in ("writedown_held", "readonly", "unreadable_none")))
    u3 = run_unit("unit3")
    check("U3 a frozen REMAINING close of legs b,c holds legs b and c and any span but not leg a (%r)" % (u3,), all(u3[k] is True for k in ("b_held", "c_held", "a_free", "span_held")))
    gm = run_child("legwd", "mutant"); m1, m2 = gm["s1"], gm["s2"]
    mw = gm["s2w"]; mm = gm["m2w"]
    check("XM2 MUTANT, EXPLICIT TARGET PATH: under the labelled BB81-style pre-check the same direct real write-down close is NOT held: it reaches _pa_write_down (calls %r) or opens an intent/account op (ops %r -> %r), so X2b is a real negative control" % (mm["wd_calls"], m1["intent_ops"], mw["intent_ops"]), mm["wd_calls"] >= 1 or mw["intent_ops"] != m1["intent_ops"] or mw["credit"] != m1["credit"] or mw["debit"] != m1["debit"])
    check("XM MUTANT (labelled model of the BB81 exact-op pre-check): the same real write-down poll after a frozen leg DOES open a write-down intent or account op (ops %r -> %r, credit %r -> %r), so X2 would fail" % (m1["intent_ops"], m2["intent_ops"], m1["credit"], m2["credit"]), m2["intent_ops"] != m1["intent_ops"] or m2["credit"] != m1["credit"] or m2["debit"] != m1["debit"] or m2["pos_open"] is False)
except _TO as e:
    print("FAIL BUDGET %s" % e); FAILS.append("budget")
except SystemExit:
    raise
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
