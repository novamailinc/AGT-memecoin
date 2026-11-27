"""UNEXECUTED DRAFT. Unit tests for daemon/grad_cycle.run_cycle with a FAKE grad module (no ledger, no network, no thread/process start).
Script style like the other grad tests: prints PASS/FAIL lines and a FAILS count. Real-ledger E2E and kill/restart are NOT covered here."""
import json, os, sys, tempfile, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from daemon import grad_cycle as GC
FAILS = 0
def check(name, ok):
    global FAILS
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: FAILS += 1
TMP = tempfile.mkdtemp(prefix="grad_cycle_")

def fake(health=None, demo=True, snap=None, calls=None, raise_exits=False):
    calls = calls if calls is not None else []
    g = types.SimpleNamespace(LEDGER_PATH=os.path.join(TMP, "led%d.json" % len(os.listdir(TMP))), calls=calls)
    g._demo_mode = lambda: demo
    g.poll_time_exits = lambda: (calls.append("time_exits") or [])
    def pe(price_fn=None):
        calls.append("exits")
        if raise_exits: raise RuntimeError("x")
        return []
    g.poll_exits = pe
    g.recon_health = lambda: health
    pa = types.SimpleNamespace(snapshot=lambda: snap)
    g._paper_account = lambda: pa
    return g
CLEAN = {"poll_aborts": {"count": 0}, "n_pending": 0, "n_settlement_pending": 0, "n_migration_blocked": 0, "items": []}
OK_SNAP = {"degraded": None, "lanes": {}}

os.environ.pop(GC.ENV_FLAG, None)
g = fake(CLEAN, snap=OK_SNAP); r = GC.run_cycle(g, scan_fn=lambda: g.calls.append("scan"), price_route="lane_default")
check("C1 disabled by default: nothing runs, no journal", r["status"] == "disabled" and g.calls == [] and not os.path.exists(g.LEDGER_PATH + ".cycle.json"))
os.environ[GC.ENV_FLAG] = "1"
g = fake(CLEAN, demo=False, snap=OK_SNAP); r = GC.run_cycle(g, scan_fn=lambda: g.calls.append("scan"), price_route="lane_default")
check("C2 refuses a non-DEMO lane: nothing runs", r["status"] == "refused:not_demo_mode" and g.calls == [])
g = fake(CLEAN, snap=OK_SNAP); r = GC.run_cycle(g, scan_fn=lambda: g.calls.append("scan"), price_route="lane_default")
check("C3 order time_exits -> exits -> scan when clean (%s)" % g.calls, g.calls == ["time_exits", "exits", "scan"] and r["status"] == "done")
j = json.load(open(g.LEDGER_PATH + ".cycle.json"))
check("C4 journal finished as done with steps", j["status"] == "done" and set(j["steps"]) == {"time_exits", "exits", "scan"})
for label, h, sn in (("ledger unreadable", None, OK_SNAP), ("pending exit", dict(CLEAN, n_pending=1), OK_SNAP), ("settlement pending", dict(CLEAN, n_settlement_pending=1), OK_SNAP),
                     ("migration blocked", dict(CLEAN, n_migration_blocked=1), OK_SNAP), ("account unreadable", CLEAN, None), ("account degraded", CLEAN, {"degraded": "x"})):
    g = fake(h, snap=sn); r = GC.run_cycle(g, scan_fn=lambda: g.calls.append("scan"), price_route="lane_default")
    check("C5 %s: exits STILL run, entry scan skipped with a reason (%s)" % (label, r["scan_skipped"]), g.calls == ["time_exits", "exits"] and r["scan_skipped"])
g = fake(CLEAN, snap=OK_SNAP, raise_exits=True); r = GC.run_cycle(g, scan_fn=lambda: g.calls.append("scan"), price_route="lane_default")
check("C6 exit step failure: time_exits ran, scan skipped, no raise", "scan" not in g.calls and r["scan_skipped"] == "exit_step_failed" and r["status"] == "done")
g = fake(CLEAN, snap=OK_SNAP); GC._write_journal(g, {"status": "running", "boot_id": "older", "cycle_seq": 7}, {})
r = GC.run_cycle(g, price_route="lane_default")
check("C7 a prior 'running' journal (interrupted cycle) records a durable fault and refuses entries, with no abort count and no earlier fault file", any(f["reason"]=="interrupted_prior_cycle" for f in r.get("faults", [])) and os.path.exists(g.LEDGER_PATH + ".cycle-fault.json") and g.calls[:2] == ["time_exits", "exits"])
g = fake(CLEAN, snap=OK_SNAP); open(g.LEDGER_PATH + ".cycle.json", "w").write("{not json")
r = GC.run_cycle(g, price_route="lane_default")
check("C8 unreadable journal records a durable fault and the exits still run", any(f["reason"]=="cycle_journal_unreadable" for f in r.get("faults", [])) and g.calls[:2] == ["time_exits", "exits"])
g = fake(CLEAN, snap=OK_SNAP); g.LEDGER_PATH = os.path.join(TMP, "nodir", "x.json")
r = GC.run_cycle(g, scan_fn=lambda: g.calls.append("scan"), price_route="lane_default")
check("C9 journal write failure: exits STILL run, NEW entries are refused (%s)" % r["scan_skipped"], r["scan_skipped"] in ("cycle_journal_unavailable", "cycle_lock_io_failure") and g.calls == ["time_exits", "exits"] and "journal_write_failure" in r)
# ---- fault model (fake grad; every case needs a durable cross-source proof; see clear_fault) ----
class _Lock:
    held = 0
    def __enter__(self): _Lock.held += 1; return self
    def __exit__(self, *a): _Lock.held -= 1; return False
def rich(snap=None, positions=None, closes=None, fills=None, aborts=0, journal_entries=None, lane="grad", start=10.0, cash=10.0):
    ab = {"poll_aborts": {"count": aborts}, "n_pending": 0, "n_settlement_pending": 0, "n_migration_blocked": 0}
    g = fake(ab, snap=None); g.ab = ab; g.LANE = lane
    g.state = {"positions": positions or {}, "closes": closes or []}
    g._read = lambda: g.state; g._ledger_lock = _Lock
    g.fills = fills if fills is not None else {}
    g._demo_filler = lambda: types.SimpleNamespace(_locked=lambda fn: fn({"fills": g.fills}))
    g.diagnose_position = lambda m: {"verdict": "consistent"}
    g.snap = snap if snap is not None else {"degraded": None, "start_balance": start, "cash": cash, "lanes": {lane: {"positions": []}}, "pending_sol": [], "holds": {}, "unresolved": [], "assumed_sold": []}
    g.entries = journal_entries if journal_entries is not None else []
    g.replay = {"reason": None}
    g.pa = types.SimpleNamespace(snapshot=lambda: g.snap, _journal_read=lambda: (g.entries, None, None), verify_replay=lambda: ((False, g.replay["reason"]) if g.replay["reason"] else ((None, g.replay["none"]) if g.replay.get("none") else (True, "match"))))
    g._paper_account = lambda: g.pa
    return g
def cyc(g, **kw):
    g.calls.clear(); return GC.run_cycle(g, scan_fn=lambda: g.calls.append("scan"), price_route="lane_default", **kw)
def abort_once(g):
    _p = g.poll_exits
    def pe(price_fn=None):
        r_ = _p(price_fn); g.ab["poll_aborts"]["count"] += 1; return r_
    g.poll_exits = pe; r_ = cyc(g); g.poll_exits = _p; return r_
GC._MEM.clear()
g = rich(); r1 = abort_once(g); c1 = list(g.calls); GC._MEM.clear(); r2 = cyc(g)
check("C10 abort with NO durable marker: this cycle refuses entries and the NEXT apparently clean cycle STILL does, even after the in-process flag is gone (restart); exits ran both (%s %s)" % (r1["scan_skipped"], r2["scan_skipped"]),
      "scan" not in c1 and r2["scan_skipped"].startswith("unresolved_fault") and g.calls == ["time_exits", "exits"] and os.path.exists(g.LEDGER_PATH + ".cycle-fault.json"))
GC.CLEAR_COMMIT_ENABLED = False; ok0, ev0 = GC.clear_fault(g)
check("C10c0 with the commit switch OFF (default) the evidence is gathered but NOTHING is committed: refused, fault stays, no clear record (%s)" % (ev0.get("blocked"),), (not ok0) and ev0["blocked"] == ["commit_disabled_no_common_writer_lock"] and os.path.exists(g.LEDGER_PATH + ".cycle-fault.json") and not GC._clear_records(g))
GC.CLEAR_COMMIT_ENABLED = True; ok, ev = GC.clear_fault(g)
check("C10c EMPTY lane with the evidence all clean clears with a durable clear record, only then entries resume (%s)" % (ev.get("blocked"),),
      ok and not os.path.exists(g.LEDGER_PATH + ".cycle-fault.json") and len(GC._clear_records(g)) == 1 and cyc(g)["status"] == "done" and g.calls[-1] == "scan")
GC.CLEAR_COMMIT_ENABLED = True
for label, mut, key in (
    ("account has an ORPHAN open position for the lane", lambda g: g.snap["lanes"]["grad"].__setitem__("positions", [{"pos_id": "P9"}]), "account_orphan_positions"),
    ("account has pending_sol for the lane", lambda g: g.snap.__setitem__("pending_sol", [{"lane": "grad"}]), "account_pending_sol_for_lane"),
    ("account has a hold for the lane", lambda g: g.snap.__setitem__("holds", {"grad:h1": {"lane": "grad"}}), "account_holds_for_lane"),
    ("account has an unresolved write-down for the lane", lambda g: g.snap.__setitem__("unresolved", [{"lane": "grad"}]), "account_unresolved_for_lane"),
    ("account has assumed_sold for the lane", lambda g: g.snap.__setitem__("assumed_sold", [{"lane": "grad"}]), "account_assumed_sold_for_lane"),
    ("the fill journal holds an exit fill that no lane position or close owns", lambda g: g.fills.__setitem__("grad:exit:MINTX:k1", {}), "fill_ownership"),
    ("the account's OWN genesis replay reports a divergence", lambda g: g.replay.__setitem__("reason", "cash mismatch vs journal"), "account_journal_consistency_failed"),
    ("the account replay cannot run (unevaluable)", lambda g: g.replay.__setitem__("none", "ledger_file_unreadable_or_missing"), "account_journal_consistency_unevaluable"),
    ("account WAL is TORN: verify_replay says journal_status:torn (None)", lambda g: g.replay.__setitem__("none", "journal_status:torn"), "account_journal_consistency_unevaluable"),
    ("account WAL has a sequence hole: verify_replay says journal_seq_violation (None)", lambda g: g.replay.__setitem__("none", "journal_seq_violation"), "account_journal_consistency_unevaluable"),
    ("the account module has NO verify_replay (refuse, never fall back to the mutating loader)", lambda g: delattr(g.pa, "verify_replay"), "account_journal_consistency_unevaluable"),
    ("an ENTRY fill has an account debit (still open on the account) and no lane owner", lambda g: (g.fills.__setitem__("grad:entry:MINTY:123", {}), g.entries.append({"event": "debit", "op_id": "grad:entry:MINTY:123", "pos_id": "PX"})), "fill_ownership"),
    ("an open entry intent whose op is DEBITED on the account, still open, with no lane owner", lambda g: (g.entries.append({"event": "debit", "op_id": "grad:entry:MINTZ:9:1", "pos_id": "PZ"}), setattr(g, "_demo_filler", lambda: types.SimpleNamespace(_locked=lambda fn: fn({"fills": g.fills, "intents": {"grad|MINTZ": "grad:entry:MINTZ:9:1"}})))), "fill_ownership"),
    ("the account journal is unreadable while an entry fill needs classifying", lambda g: (g.fills.__setitem__("grad:entry:MINTY:123", {}), setattr(g.pa, "_journal_read", lambda: ([], "torn", None))), "fill_ownership"),
    ("the fill journal has a key of an unknown kind for this lane", lambda g: g.fills.__setitem__("grad:weird:MINTY:1", {}), "fill_ownership"),
    ("an exit fill names an instance (pa_pos_id suffix) no position owns", lambda g: (g.state["closes"].append({"mint": "M2", "pa_pos_id": "P2"}), g.fills.__setitem__("grad:exit:M2:k:P9", {})), "fill_ownership"),
    ("a bare exit fill is ambiguous: the mint has two owned instances", lambda g: (g.state["closes"].extend([{"mint": "M2", "pa_pos_id": "P2"}, {"mint": "M2", "pa_pos_id": "P3"}]), g.fills.__setitem__("grad:exit:M2:k", {})), "fill_ownership"),
    ("a lane position has no pa_pos_id", lambda g: g.state["positions"].update({"M5": {}}), "lane_position_without_pa_pos_id"),
    ("a lane position is missing on the account", lambda g: g.state["positions"].update({"M5": {"pa_pos_id": "P5"}}), "lane_positions_missing_on_account"),
    ("an account position has no id", lambda g: g.snap["lanes"]["grad"].__setitem__("positions", [{"pos_id": None}]), "account_position_without_id"),
    ("an account pending_sol entry has NO lane key (unknown ownership)", lambda g: g.snap.__setitem__("pending_sol", [{"x": 1}]), "account_pending_sol_unknown_lane"),
    ("an account hold has NO lane key", lambda g: g.snap.__setitem__("holds", {"h": {"amount": 1}}), "account_holds_unknown_lane"),
    ("the account snapshot is unreadable", lambda g: setattr(g.pa, "snapshot", lambda: None), "account"),
    ("the fill journal is unreadable", lambda g: setattr(g, "_demo_filler", lambda: (_ for _ in ()).throw(OSError("x"))), "fill_journal"),
    ("an open lane position has legacy (evidence_unavailable) evidence", lambda g: (g.state["positions"].update({"M1": {"pa_pos_id": "P1"}}), g.snap["lanes"]["grad"].__setitem__("positions", [{"pos_id": "P1"}]), setattr(g, "diagnose_position", lambda m: {"verdict": "evidence_unavailable:G3"})), "position:M1"),
    ("an open position is only explained_residue (stored dust label is not repair proof)", lambda g: (g.state["positions"].update({"M1": {"pa_pos_id": "P1"}}), g.snap["lanes"]["grad"].__setitem__("positions", [{"pos_id": "P1"}]), setattr(g, "diagnose_position", lambda m: {"verdict": "explained_residue:stored_dust"})), "position:M1"),
    ):
    g = rich(); abort_once(g); mut(g); ok, ev = GC.clear_fault(g); r = cyc(g)
    check("C10d clear REFUSED: %s (%s)" % (label, ev.get("blocked")), (not ok) and any(key in b for b in ev["blocked"]) and os.path.exists(g.LEDGER_PATH + ".cycle-fault.json") and "scan" not in g.calls and not GC._clear_records(g))
g = rich(); abort_once(g); GC._write_journal(g, {"status": "running", "ts": 1, "boot_id": "x"}, {}); ok, ev = GC.clear_fault(g)
check("C10e clear refused while the cycle journal says running (%s)" % (ev.get("blocked"),), (not ok) and "cycle_running_or_interrupted" in ev["blocked"])
g = rich(); abort_once(g); os.mkdir(os.path.join(os.path.dirname(g.LEDGER_PATH), ".cycle-probe-%d" % os.getpid())); ok, ev = GC.clear_fault(g)
check("C10f failed storage I/O probe does NOT clear (%s)" % (ev.get("io_probe"),), (not ok) and "io_probe" in ev["blocked"])
os.rmdir(os.path.join(os.path.dirname(g.LEDGER_PATH), ".cycle-probe-%d" % os.getpid()))
g = rich(); abort_once(g); _s = g.pa.snapshot; _n = {"n": 0}
def _moving():
    _n["n"] += 1; d = json.loads(json.dumps(_s())); d["cash"] = 10.0 + _n["n"] * 0.0; d["marker"] = _n["n"]; return d
g.pa.snapshot = _moving; ok, ev = GC.clear_fault(g)
check("C10g state that changes between evidence read and commit does not clear (%s)" % (ev.get("blocked"),), (not ok) and "state_changed_during_evidence" in ev["blocked"])
g = rich(); abort_once(g); _Lock.held = 0; seen = []
_f = g.pa.snapshot; g.pa.snapshot = lambda: (seen.append(_Lock.held), _f())[1]; GC.clear_fault(g)
check("C10h evidence reads never run under the lane lock (self-deadlock rule); the commit does (held counts seen %s)" % (seen[:4],), seen and seen[0] == 0 and _Lock.held == 0)
open(g.LEDGER_PATH + ".cycle-fault.json", "w").write("{bad"); r5 = cyc(g)
check("C10i an unreadable fault record is unresolved and cannot be cleared (%s)" % r5["scan_skipped"], r5["scan_skipped"].startswith("unresolved_fault") and not GC.clear_fault(g)[0])
# restart with the fault FILE WRITE failing: the prior 'running' journal must still gate entries across boots
g = rich(); GC._MEM.clear(); g.LEDGER_PATH = g.LEDGER_PATH
GC._write_journal(g, {"status": "running", "boot_id": "prev-boot", "cycle_seq": 3, "ts": 1.0}, {})
_a = GC._atomic
def _fail_fault(path, rec):
    if path.endswith(".cycle-fault.json"): raise OSError("disk full")
    return _a(path, rec)
GC._atomic = _fail_fault; r6 = cyc(g); jr = GC._read_journal(g); GC._atomic = _a; GC._BOOT["id"] += "x"; r7 = cyc(g)
check("C10j fault FILE write fails after a restart found a prior 'running' cycle: entries refuse (%s), the prior running record is LEFT untouched (%s), the next boot still refuses (%s)" % (r6["scan_skipped"], jr.get("status"), r7["scan_skipped"]),
      r6["scan_skipped"] == "fault_not_durable" and jr.get("status") == "running" and jr.get("boot_id") == "prev-boot" and r7["scan_skipped"].startswith("unresolved_fault") and "scan" not in g.calls)
g = rich(); GC._MEM.clear(); r = cyc(g); GC._MEM.clear(); g.ab["poll_aborts"]["count"] += 2; r2 = cyc(g)
check("C10k an abort BETWEEN cycles (count above the last journaled end) faults the next cycle (%s)" % r2["scan_skipped"], r["scan_skipped"] is None and any(f["reason"] == "abort_between_cycles" for f in r2["faults"]) and "scan" not in g.calls)
g = rich(raise_exits=True) if False else rich(); _p = g.poll_exits; g.poll_exits = lambda price_fn=None: (_ for _ in ()).throw(RuntimeError("boom")); r = cyc(g); g.poll_exits = _p; r2 = cyc(g)
check("C10l an exit-step EXCEPTION records a durable fault, the next clean cycle still refuses entries", any(f["reason"] == "exit_step_exception" for f in r["faults"]) and r2["scan_skipped"].startswith("unresolved_fault"))
ga = rich(); gb = rich(); gb.LEDGER_PATH = os.path.join(TMP, "laneB.json"); abort_once(ga); rb = cyc(gb)
check("C10m the fault is keyed by ledger: lane A's fault does not block lane B, and clearing A leaves B's mirror alone", rb["scan_skipped"] is None and GC.fault_active(ga)[0] and not GC.fault_active(gb)[0])
g = rich(); abort_once(g); GC.clear_fault(g); GC._MEM[g.LEDGER_PATH] = {"since": 0.0, "file_written": True}
check("C10n cross-process: the fault file is removed with NO clear record naming it, so the in-process fault holds (%s)" % (GC.fault_active(g),), GC.fault_active(g)[0])
g = rich(); abort_once(g); st = GC.status(g, 60)
check("C10o status reports fault_unresolved with reason and age (%s)" % ({k: st.get(k) for k in ("state", "reason")},), st["state"] == "fault_unresolved" and st["reason"] == "poll_abort" and st["fault_age_s"] is not None)
GC.CLEAR_COMMIT_ENABLED = True
g = rich(); okn, evn = GC.clear_fault(g)
check("C10p clear with no fault recorded: refused 'no_fault', no clear record written (%s)" % (evn.get("blocked"),), (not okn) and evn["blocked"] == ["no_fault"] and not GC._clear_records(g))
g = rich(); abort_once(g); f1 = GC._read_fault(g); g.ab["poll_aborts"]["count"] += 1; abort_once(g); f2 = GC._read_fault(g)
check("C10q fault identity and age are PRESERVED across later faults: same fault_id and since, reasons appended (%s reasons)" % len(f2.get("reasons", [])), f2["fault_id"] == f1["fault_id"] and f2["since"] == f1["since"] and len(f2["reasons"]) >= 2)
g = rich(); abort_once(g); os.remove(g.LEDGER_PATH + ".cycle-fault.json"); GC._MEM.clear(); r = cyc(g)
check("C10r the fault FILE deleted AND the process restarted: the append-only fault log still holds the fault, entries refuse (%s)" % r["scan_skipped"], r["scan_skipped"] == "unresolved_fault:logged_fault_without_clear_record" and "scan" not in g.calls)
okc, evc = GC.clear_fault(g)
check("C10s a clear with the fault file gone but a logged fault succeeds on clean evidence and the clear record names the logged fault_id (%s)" % (evc.get("blocked"),), okc and not GC._open_logged_faults(g))
import fcntl
g = rich(); abort_once(g); _lk = open(g.LEDGER_PATH + ".cycle.lock", "w"); fcntl.flock(_lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
rb = cyc(g); okb, evb = GC.clear_fault(g); _lk.close()
check("C10t the per-ledger cycle lock: while held, run_cycle runs nothing (busy) and clear_fault refuses (%s, %s)" % (rb["status"], evb.get("blocked")), rb["status"].startswith("busy") and g.calls == [] and (not okb) and any("cycle_lock_unavailable" in b for b in evb["blocked"]))
GC.CLEAR_COMMIT_ENABLED = False
GC.CLEAR_COMMIT_ENABLED = True
g = rich(); abort_once(g); g.fills["grad:entry:MINTR:5"] = {}   # a REFUSED entry: fill journaled, NO account debit
okr, evr = GC.clear_fault(g)
check("C10u a refused entry (fill, no account debit) and a settled-on-account debit with no lane record are BENIGN, counted, and do not block the clear (%s %s)" % (evr.get("blocked"), evr.get("fill_ownership_notes")), okr and evr["fill_ownership_notes"]["entry_refused_no_debit"] == 1)
g = rich(); abort_once(g); g.fills["grad:entry:MINTS:6"] = {}; g.entries.extend([{"event": "debit", "op_id": "grad:entry:MINTS:6", "pos_id": "PS"}, {"event": "credit", "pos_id": "PS", "final": True}])
oks, evs = GC.clear_fault(g)
check("C10v a debit that the account already settled (final credit) with no lane record is benign and counted (%s)" % (evs.get("fill_ownership_notes"),), oks and evs["fill_ownership_notes"]["entry_settled_on_account_unowned"] == 1)
g = rich(); abort_once(g)
with open(g.LEDGER_PATH + ".cycle-fault-log.jsonl", "a") as fh: fh.write('{"fault_id": "x"')   # torn last line, no newline
os.remove(g.LEDGER_PATH + ".cycle-fault.json")
check("C10w a torn LAST fault-log line is tolerated: the earlier logged fault is still active and the clear does not say no_fault (%s)" % (GC.fault_active(g),), GC.fault_active(g)[0] and "no_fault" not in (GC.clear_fault(g)[1].get("blocked") or []))
g = rich(); open(g.LEDGER_PATH + ".cycle-fault-log.jsonl", "w").write("garbage line\n")
check("C10x a COMPLETE unparseable fault-log line is damage: active and reported as fault_log_unreadable, not no_fault", GC.fault_active(g)[0] and GC.clear_fault(g)[1].get("blocked") == ["fault_log_unreadable"])
GC.CLEAR_COMMIT_ENABLED = False
# exits-before-scans across lanes
order = []
ga = rich(); gb = rich(); gb.LEDGER_PATH = os.path.join(TMP, "laneB2.json")
ga.poll_exits = lambda price_fn=None: (order.append("A_exits") or []); gb.poll_exits = lambda price_fn=None: (order.append("B_exits") or [])
recs = GC.run_all([("A", ga, lambda: (order.append("A_scan") or {"x": 1})), ("B", gb, lambda: (order.append("B_scan") or {}))])
check("C15 run_all: EVERY lane's exits run before ANY scan (%s)" % order, order == ["A_exits", "B_exits", "A_scan", "B_scan"])
ga = rich(); gb = rich(); gb.LEDGER_PATH = os.path.join(TMP, "laneB3.json"); order.clear()
_pa = ga.poll_exits
def _slow_scan_then_abort(): order.append("A_scan"); gb.ab["poll_aborts"]["count"] += 1; return {}
recs = GC.run_all([("A", ga, _slow_scan_then_abort), ("B", gb, lambda: (order.append("B_scan") or {}))])
check("C16 an abort that happens between lane B's exit phase and its scan is faulted and B's scan is refused (%s, %s)" % (recs["B"].get("scan_skipped"), order), recs["B"]["scan_skipped"] == "abort_before_scan" and "B_scan" not in order and GC.fault_active(gb)[0])
GC.CLEAR_COMMIT_ENABLED = True
g = rich(); abort_once(g)
g._demo_filler = lambda: types.SimpleNamespace(_locked=lambda fn: fn({"fills": g.fills, "intents": {"grad|MA": "grad:entry:MA:1:1", "grad|MB": "grad:entry:MB:2:2"}}))
g.entries.append({"event": "debit_refused", "op_id": "grad:entry:MB:2:2"})
okd, evd = GC.clear_fault(g)
check("C10y routine DemoSkip leftovers do NOT block: an intent with no account event (fresh) and one with a durable debit_refused (stale) are benign and counted (%s %s)" % (evd.get("blocked"), evd.get("fill_ownership_notes")),
      okd and evd["fill_ownership_notes"].get("intent_fresh_no_account_event") == 1 and evd["fill_ownership_notes"].get("intent_stale_refused") == 1)
g = rich(); abort_once(g); g._demo_filler = lambda: types.SimpleNamespace(_locked=lambda fn: fn({"fills": g.fills, "intents": {"grad|MA": "grad:entry:MA:1:1"}}))
g.pa._journal_read = lambda: ([], "torn", None)
okd2, evd2 = GC.clear_fault(g)
check("C10y2 an intent cannot be classified when the account WAL is unreadable: blocks (%s)" % (evd2.get("blocked"),), (not okd2) and any("open_entry_intent_unclassifiable" in str(b) for b in evd2["blocked"]))
# raw account evidence is read BEFORE snapshot() (which heals on access)
g = rich(); abort_once(g); order_ = []
_vr = g.pa.verify_replay; _sn = g.pa.snapshot; _jr = g.pa._journal_read
g.pa.verify_replay = lambda: (order_.append("verify_replay") or _vr()); g.pa.snapshot = lambda: (order_.append("snapshot") or _sn()); g.pa._journal_read = lambda: (order_.append("wal_index") or _jr())
okz, evz = GC.clear_fault(g)
check("C10z verify_replay and the WAL index are read BEFORE any snapshot() (%s)" % order_[:4], order_.index("verify_replay") < order_.index("snapshot") and order_.index("wal_index") < order_.index("snapshot") and evz.get("account_evidence_order", "").startswith("verify_replay"))
# torn fault-log tail: second append must not poison the log
g = rich(); abort_once(g)
with open(g.LEDGER_PATH + ".cycle-fault-log.jsonl", "a") as fh: fh.write('{"fault_id": "frag')
os.remove(g.LEDGER_PATH + ".cycle-fault.json"); GC._MEM.clear()
okt, evt = GC.clear_fault(g)
check("C10w2 a torn tail is an ACTIVE unknown: the clear is blocked with fault_log_torn_tail (%s)" % (evt.get("blocked"),), (not okt) and "fault_log_torn_tail" in evt["blocked"])
GC._record_fault(g, {}, "second_fault_after_torn_tail")
_ids = GC._open_logged_faults(g)
check("C10w3 the NEXT append repairs the tail first: the log stays parseable, the fragment is preserved, and a marker fault keeps the unknown ACTIVE (%s)" % (_ids,),
      _ids is not None and GC._TORN_TAIL not in _ids and any(str(i).startswith("torn_tail_") for i in _ids) and len(_ids) >= 2 and "frag" in open(g.LEDGER_PATH + ".cycle-fault-log.jsonl.torn-tails.jsonl").read() and GC.fault_active(g)[0])
# deferred cycle: the journal stays running until the scan completes; the scan itself has a durable running record
GC.CLEAR_COMMIT_ENABLED = False
g = rich(); seen_ = {}
rec_ = GC.run_cycle(g, scan_fn=lambda: (seen_.update(during=dict(GC._read_journal(g))) or {}), price_route="lane_default", defer_scan=True)
j_after_exits = dict(GC._read_journal(g))
GC.scan_phase(g, lambda: (seen_.update(during=dict(GC._read_journal(g))) or {}), rec_)
j_end = dict(GC._read_journal(g))
check("C17 deferred: journal is running/awaiting_scan after the exits, running/scan DURING the scan, done after (%s, %s, %s)" % (j_after_exits.get("status"), (seen_.get("during") or {}).get("status"), j_end.get("status")),
      j_after_exits.get("status") == "running" and j_after_exits.get("phase") == "awaiting_scan" and (seen_.get("during") or {}).get("status") == "running" and (seen_["during"]).get("phase") == "scan" and j_end.get("status") == "done")
g = rich(); ran_ = []
rec_ = GC.run_cycle(g, scan_fn=lambda: ran_.append(1), price_route="lane_default", defer_scan=True)
_ws = GC._write_journal; GC._write_journal = lambda grad, r, out: False
try: GC.scan_phase(g, lambda: ran_.append(1), rec_)
finally: GC._write_journal = _ws
check("C17b if the scan's running record cannot be made durable the scan is REFUSED (%s)" % (rec_.get("scan_skipped"),), rec_.get("scan_skipped") == "scan_intent_not_durable" and ran_ == [])
GC.CLEAR_COMMIT_ENABLED = True
# torn tail with NO new fault: the next cycle repairs it (no deadlock), the marker is named by the clear, and the fault is then inactive
g = rich(); abort_once(g)
with open(g.LEDGER_PATH + ".cycle-fault-log.jsonl", "a") as fh: fh.write('{"fault_id": "frag2')
GC._MEM.clear(); cyc(g)
_idsr = GC._open_logged_faults(g)
check("C10w4 a torn tail with no new fault is repaired by the next cycle itself (%s)" % (_idsr,), _idsr is not None and GC._TORN_TAIL not in _idsr and any(str(i).startswith("torn_tail_") for i in _idsr))
okc, evc = GC.clear_fault(g)
_cl = GC._clear_records(g)
check("C10w5 the clear NAMES the torn_tail marker id and the fault is inactive afterwards (%s %s)" % (okc, evc.get("blocked")), okc and any(str(i).startswith("torn_tail_") for r_ in _cl for i in (r_.get("fault_ids") or [])) and not GC.fault_active(g)[0])
# crash INSIDE the repair, after the marker is written but before the truncate: the unknown must stay active
g = rich(); abort_once(g)
os.remove(g.LEDGER_PATH + ".cycle-fault.json"); GC._MEM.clear()
with open(g.LEDGER_PATH + ".cycle-fault-log.jsonl", "a") as fh: fh.write('{"fault_id": "frag3-' + "x" * 300)      # the fragment is LONGER than the marker
def _boom(point): raise RuntimeError("kill at " + point)
_hk = GC._REPAIR_HOOK; GC._REPAIR_HOOK = _boom
try: GC._repair_log_tail(g)
except Exception: pass
GC._REPAIR_HOOK = _hk
check("C10w6 a crash after the marker write and before the truncate leaves the unknown ACTIVE (not a clean log): (%s)" % (GC.fault_active(g),), GC.fault_active(g)[0] and "no_fault" not in (GC.clear_fault(g)[1].get("blocked") or []))
GC.CLEAR_COMMIT_ENABLED = False
# every scan_phase exit path finalizes truthfully
g = rich(); rec_ = GC.run_cycle(g, scan_fn=lambda: {}, price_route="lane_default", defer_scan=True)
g.ab["poll_aborts"]["count"] += 1
GC.scan_phase(g, lambda: {}, rec_)
check("C17c an abort between phases: journal finalized as done_scan_skipped (not a crash look-alike) (%s)" % (GC._read_journal(g).get("status"),), GC._read_journal(g).get("status") == "done_scan_skipped" and rec_["scan_skipped"] == "abort_before_scan")
g = rich(); rec_ = GC.run_cycle(g, scan_fn=lambda: {}, price_route="lane_default", defer_scan=True)
_sg = GC.scan_gate; GC.scan_gate = lambda grad, account=None: (_ for _ in ()).throw(RuntimeError("gate boom"))
try: GC.scan_phase(g, lambda: {}, rec_)
finally: GC.scan_gate = _sg
check("C17d an exception inside scan_phase is contained and finalizes the journal (%s, %s)" % (rec_.get("scan_skipped"), GC._read_journal(g).get("status")), str(rec_.get("scan_skipped")).startswith("scan_phase_exception") and GC._read_journal(g).get("status") == "done_scan_skipped")
g = rich(); rec_ = GC.run_cycle(g, scan_fn=lambda: {}, price_route="lane_default", defer_scan=True)
_at = GC._atomic; GC._atomic = lambda p, r: (_ for _ in ()).throw(OSError("disk full"))
try: GC.scan_phase(g, lambda: {}, rec_)
finally: GC._atomic = _at
check("C17e a failed FINAL journal write is recorded, and the journal stays running (fail closed) (%s)" % (rec_.get("journal_final_write_failure"),), rec_.get("journal_final_write_failure") and GC._read_journal(g).get("status") == "running")
GC.CLEAR_COMMIT_ENABLED = True
g = rich(); abort_once(g); g.fills["grad:entry:MD:7"] = {}
g.entries.extend([{"event": "debit", "op_id": "grad:entry:MD:7", "pos_id": "P1"}, {"event": "debit", "op_id": "grad:entry:MD:7", "pos_id": "P2"}])
okm, evm = GC.clear_fault(g)
check("C10v2 two debits for ONE op id block (the index must not keep only the last) (%s)" % (evm.get("blocked"),), (not okm) and "op_has_multiple_debits" in str(evm.get("blocked")))
GC.CLEAR_COMMIT_ENABLED = False
# a refused (done_scan_skipped) deferred cycle is NOT a crash signal: the next cycle runs clean, no interrupted_prior_cycle fault
g = rich(); rec_ = GC.run_cycle(g, scan_fn=lambda: {}, price_route="lane_default", defer_scan=True)
_sg2 = GC.scan_gate; GC.scan_gate = lambda grad, account=None: (False, "test_gate_refusal")
try: GC.scan_phase(g, lambda: {}, rec_)
finally: GC.scan_gate = _sg2
_jst = GC._read_journal(g).get("status"); GC._MEM.clear(); r_next = cyc(g)
check("C17f a done_scan_skipped journal (%s) does NOT fault the next cycle (only running does) (%s %s)" % (_jst, r_next.get("status"), r_next.get("scan_skipped")), _jst == "done_scan_skipped" and r_next["status"] == "done" and not os.path.exists(g.LEDGER_PATH + ".cycle-fault.json"))
g = rich(); GC._write_journal(g, {"status": "done", "ts": GC.time.time(), "steps": {"scan": {"ok": True, "feed_health": {"error": "cursor_unreadable"}}}}, {})
st_ = GC.status(g, 3600)
check("C17g a durable feed error is surfaced by status() as feed_error, never ok (%s)" % (st_.get("state"),), st_.get("state") == "feed_error")
GC._MEM.clear()
g = fake(dict(CLEAN, n_pending=1), snap=OK_SNAP); r3 = GC.run_cycle(g, scan_fn=lambda: g.calls.append("scan"), price_route="lane_default")
check("C11 an unresolved pending_exit keeps the scan refused across cycles (durable gate, not the in-memory counter): %s" % r3["scan_skipped"], "scan" not in g.calls and "n_pending" in str(r3["scan_skipped"]))
g = fake(CLEAN, snap=OK_SNAP); r = GC.run_cycle(g, price_route="lane_default")
check("S1 status ok within max_age; stale (UNKNOWN) when older; unknown with no journal",
      GC.status(g, 60)["state"] in ("ok", "scan_skipped") and GC.status(g, 60, now=r["ts"] + 3600)["state"] == "stale" and GC.status(fake(CLEAN, snap=OK_SNAP), 60)["state"] == "unknown")
GC._write_journal(g, {"status": "running", "ts": r["ts"], "boot_id": "x"}, {})
check("S2 status interrupted when the journal is still 'running'", GC.status(g, 60, now=r["ts"] + 1)["state"] == "interrupted")
g = fake(CLEAN, snap=OK_SNAP); r = GC.run_cycle(g, scan_fn=lambda: g.calls.append("scan"))
check("C12 no price route: price exits NOT run, cycle recorded degraded, no scan, status degraded (never healthy): %s %s" % (g.calls, r.get("degraded")),
      g.calls == ["time_exits"] and r.get("degraded") == "no_price_route" and r["scan_skipped"] == "no_price_route" and GC.status(g, 60)["state"] == "degraded")
g = fake(CLEAN, snap=OK_SNAP); r = GC.run_cycle(g, price_fn=lambda c, m, i: 1.0, scan_fn=lambda: g.calls.append("scan"))
check("C13 an explicit price_fn counts as a route", g.calls == ["time_exits", "exits", "scan"])
check("C14 the Daemon base is imported strictly (no stand-in class fallback in the source)", "_Daemon = object" not in open(GC.__file__.replace(".pyc", ".py")).read())
ok_l, unk = GC.selected_lanes({}); ok2, unk2 = GC.selected_lanes({GC.LANES_ENV: "grad_select, nope ,grad_raw"})
check("L1 lane selection is explicit: the default is NO lane, unknown names are reported, nothing is chosen for the user (%s %s)" % ((ok_l, unk), (ok2, unk2)), ok_l == [] and unk == [] and ok2 == ["grad_select", "grad_raw"] and unk2 == ["nope"])
os.environ.pop(GC.LANES_ENV, None); check("L2 main_entry refuses (rc 2) with the cycle flag on but no lane selected", GC.main_entry([]) == 2)
os.environ[GC.LANES_ENV] = "nope"; check("L3 main_entry refuses (rc 2) on an unknown lane", GC.main_entry([]) == 2); os.environ.pop(GC.LANES_ENV, None)
print("FAILS: %d" % FAILS)
sys.exit(1 if FAILS else 0)
