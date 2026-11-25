"""UNEXECUTED DRAFT. Tests for daemon/grad_trader_cycle (R1/R2 wiring of scripts/grad-select-trader) with a FAKE engine. No ledger beyond temp files, no
network, no thread/process start. Script style: PASS/FAIL lines and a FAILS count. NOT covered: the running trader process, threads, kill/restart."""
import fcntl, json, os, re, sys, tempfile, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from daemon import grad_cycle as GC
from daemon import grad_trader_cycle as T
FAILS = 0
def check(name, ok):
    global FAILS
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: FAILS += 1
TMP = tempfile.mkdtemp(prefix="gtc_")
PF = lambda c, m, i=None: 1.0
CLEAN = {"poll_aborts": {"count": 0}, "n_pending": 0, "n_settlement_pending": 0, "n_migration_blocked": 0}
OK_SNAP = {"degraded": None, "lanes": {}}
def fake(demo=True, closes_t=0, closes_e=0, raise_exits=False):
    calls = []
    g = types.SimpleNamespace(LEDGER_PATH=os.path.join(TMP, "led%d.json" % len(os.listdir(TMP))), calls=calls)
    g._demo_mode = lambda: demo
    g._POLL_ABORTS = {"count": 0}
    g.LANE = "lane_%d" % len(os.listdir(TMP))
    g.poll_time_exits = lambda: (calls.append("time_exits") or [1] * closes_t)
    def pe(price_fn=None):
        calls.append("exits")
        if raise_exits: raise RuntimeError("x")
        return [1] * closes_e
    g.poll_exits = pe
    g.recon_health = lambda: CLEAN
    pa = types.SimpleNamespace(snapshot=lambda: OK_SNAP)
    g._paper_account = lambda: pa
    return g
def legacy_for(g):
    def _l():
        g.calls.append("LEGACY"); return {"opened": 9}, 7
    return _l
def scan_for(g, res=None, boom=False):
    def _s():
        g.calls.append("scan")
        if boom: raise RuntimeError("scan boom")
        return res if res is not None else {"stream": 3, "opened": 1, "select_rejected": 0, "select_reasons": {}}
    return _s

os.environ.pop(GC.ENV_FLAG, None)
g = fake(); res, n, rec = T.main_cycle(g, PF, scan_for(g), legacy_for(g))
check("G1 flag off: ONLY the legacy callable runs, result passed through, no journal", g.calls == ["LEGACY"] and res == {"opened": 9} and n == 7 and rec is None and not os.path.exists(g.LEDGER_PATH + ".cycle.json"))
check("G2 flag off: entry_allowed is (True, None) and startup_report inactive", T.entry_allowed(g) == (True, None) and T.startup_report(g) == {"active": False})
os.environ[GC.ENV_FLAG] = "1"
g = fake(demo=False); res, n, rec = T.main_cycle(g, PF, scan_for(g), legacy_for(g))
check("G3 flag on but NON-DEMO engine: legacy only, nothing journaled", g.calls == ["LEGACY"] and rec is None and T.entry_allowed(g) == (True, None))
g = fake(closes_t=1, closes_e=2); res, n, rec = T.main_cycle(g, PF, scan_for(g), legacy_for(g))
check("G4 on: order time_exits -> exits -> scan (EXITS BEFORE SCAN), legacy not used (%s)" % g.calls, g.calls == ["time_exits", "exits", "scan"])
check("G5 on: scan result is the scan's own dict, close count sums both exit steps (3), record done", res.get("opened") == 1 and res.get("stream") == 3 and n == 3 and rec["status"] == "done")
j = json.load(open(g.LEDGER_PATH + ".cycle.json"))
check("G6 on: journal finished done with all three steps", j["status"] == "done" and set(j["steps"]) == {"time_exits", "exits", "scan"})
g = fake(); lk = open(GC._cycle_lock_path(g), "w"); fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
res, n, rec = T.main_cycle(g, PF, scan_for(g), legacy_for(g)); fcntl.flock(lk, fcntl.LOCK_UN); lk.close()
check("G7 cycle lock held elsewhere: exits STILL run directly, scan NOT run, skipped reason visible (%s)" % g.calls, g.calls == ["time_exits", "exits"] and res.get("skipped", "").startswith("busy") and res["opened"] == 0)
g = fake(); GC._write_journal(g, {"status": "running", "boot_id": "older", "cycle_seq": 7}, {})
res, n, rec = T.main_cycle(g, PF, scan_for(g), legacy_for(g))
check("G8 interrupted prior cycle (fault): exits run, scan refused with a reason, entry_allowed False (%s)" % g.calls, g.calls == ["time_exits", "exits"] and res.get("skipped") and not T.entry_allowed(g)[0])
check("G9 startup_report shows the active fault, read-only", T.startup_report(g)["active"] is True and T.startup_report(g)["fault_active"] is True)
g = fake(raise_exits=True); res, n, rec = T.main_cycle(g, PF, scan_for(g), legacy_for(g))
check("G10 exit step failure: scan skipped, no raise, count 0", "scan" not in g.calls and n == 0 and res.get("skipped"))
g = fake(); res, n, rec = T.main_cycle(g, PF, scan_for(g, boom=True), legacy_for(g))
check("G11 scan exception does not propagate; exits ran; skipped/empty result", g.calls == ["time_exits", "exits", "scan"] and isinstance(res, dict))
g = fake(); _o = GC.run_cycle
def _raise(*a, **k): raise RuntimeError("cycle boom")
GC.run_cycle = _raise
try: res, n, rec = T.main_cycle(g, PF, scan_for(g), legacy_for(g))
finally: GC.run_cycle = _o
check("G12 run_cycle raising: exits STILL run directly, scan skipped (%s)" % g.calls, g.calls == ["time_exits", "exits"] and res.get("skipped"))
g = fake(); ok, why = T.entry_allowed(g)
check("G13 on, clean state: entry_allowed True", ok is True)
g = fake(); g.recon_health = lambda: dict(CLEAN, n_pending=1)
check("G14 on, pending exit marker: entry_allowed False but exits are not consulted", T.entry_allowed(g)[0] is False)
g = fake(); g._paper_account = lambda: (_ for _ in ()).throw(RuntimeError("x"))
check("G15 on, scan gate raising: entry refused (fail closed), no raise", T.entry_allowed(g)[0] is False)
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "grad-select-trader")).read()
m = re.search(r"def _legacy\(\):(.*?)return r, len\(c\)", src, re.S)
check("G16 static: the trader's legacy path keeps the ORIGINAL order scan_once -> poll_time_exits -> poll_exits", bool(m) and m.group(1).index("scan_once") < m.group(1).index("poll_time_exits") < m.group(1).index("poll_exits"))
check("G17 static: the trader imports the wiring fail-soft", "except Exception:\n    _gtc = None" in src)
check("G18 static: push-price and ExitPoller paths still call engine.poll_exits directly (unjournaled by design)", src.count("engine.poll_exits(_armed_price_fn)") >= 2)
# ---- R1 refinements: exits-before-scan qualification, fail-closed mode, journaled entry scan, commit-time fault check ----
os.environ[GC.ENV_FLAG] = "1"
g = fake(); st = {"closed": False}
g.poll_exits = lambda price_fn=None: (g.calls.append("exits") or st.__setitem__("closed", True) or [1])
seen = {}
def _s():
    seen["closed_before_scan"] = st["closed"]; return {"stream": 1, "opened": 0, "select_rejected": 0, "select_reasons": {}}
res, n, rec = T.main_cycle(g, PF, _s, legacy_for(g))
check("G19 qualification: a position closed by the exit step is already closed when the scan runs (capacity and cash freed before entries; same ledger lock serialises both)", seen.get("closed_before_scan") is True and n == 1)
g = fake(); g.poll_exits = lambda price_fn=None: (_ for _ in ()).throw(RuntimeError("x"))
res, n, rec = T.main_cycle(g, PF, scan_for(g), legacy_for(g))
check("G20 qualification: a failing or slow exit never waits behind a scan (scan is not even started after an exit failure)", "scan" not in g.calls)
g = fake(); g._demo_mode = lambda: (_ for _ in ()).throw(RuntimeError("state"))
res, n, rec = T.main_cycle(g, PF, scan_for(g), legacy_for(g))
check("G21 flag on, DEMO state unreadable: FAIL CLOSED, exits run directly, NO scan, NO legacy ungated path (%s)" % g.calls, g.calls == ["time_exits", "exits"] and res.get("skipped") == "flag_on_cycle_unavailable" and T.entry_allowed(g) == (False, "flag_on_cycle_unavailable") and T.mode(g) == "refuse")
_orig = T._gc
def _brk(): raise ImportError("grad_cycle broken")
T._gc = _brk
try:
    g = fake(); res, n, rec = T.main_cycle(g, PF, scan_for(g), legacy_for(g)); ea = T.entry_allowed(g); es = T.entry_scan(g, scan_for(g))
finally:
    T._gc = _orig
check("G22 flag on, grad_cycle import broken: no entries from main loop, entry gate or entry thread (%s)" % g.calls, "scan" not in g.calls and "LEGACY" not in g.calls and ea[0] is False and es[0] is False)
os.environ.pop(GC.ENV_FLAG, None)
g = fake(); ran, why, r_ = T.entry_scan(g, scan_for(g))
check("G23 flag off: entry_scan is the plain scan, no journal", ran is True and g.calls == ["scan"] and not os.path.exists(g.LEDGER_PATH + ".cycle.json"))
os.environ[GC.ENV_FLAG] = "1"
g = fake(); ran, why, r_ = T.entry_scan(g, scan_for(g)); j = json.load(open(g.LEDGER_PATH + ".cycle.json"))
ej = json.load(open(g.LEDGER_PATH + ".entry-scan.json"))
check("G24 (v3) flag on: entry-thread scan runs journaled in ITS OWN file (entry-scan.json done) and never touches cycle.json", ran is True and g.calls == ["scan"] and ej["status"] == "done" and not os.path.exists(g.LEDGER_PATH + ".cycle.json") and r_.get("opened") == 1)
g = fake(); lk = open(g.LEDGER_PATH + ".entry-scan.lock", "w"); fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
ran, why, r_ = T.entry_scan(g, scan_for(g)); fcntl.flock(lk, fcntl.LOCK_UN); lk.close()
check("G25 (v3) entry scan with the ENTRY lock held elsewhere: skipped, not run (%s)" % why, ran is False and g.calls == [] and why == "entry_scan_busy")
g = fake(); lk = open(GC._cycle_lock_path(g), "w"); fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
ran, why, r_ = T.entry_scan(g, scan_for(g)); fcntl.flock(lk, fcntl.LOCK_UN); lk.close()
check("G25b (v3) the main CYCLE lock held does not block the entry thread (it never takes it)", ran is True and g.calls == ["scan"])
g = fake(); GC._write_journal(g, {"status": "running", "boot_id": "older", "cycle_seq": 7}, {}); GC.run_cycle(g, price_fn=PF)
ran, why, r_ = T.entry_scan(g, scan_for(g))
check("G26 entry scan under an active fault: refused", ran is False and "scan" not in g.calls)
check("G27 commit_refusal: flag off None", (os.environ.pop(GC.ENV_FLAG), T.commit_refusal("lane_x"))[1] is None)
os.environ[GC.ENV_FLAG] = "1"
os.environ.pop(GC.LANES_ENV, None)
g = fake(); g.LANE = "lane_unreg"
check("G28 (v3) commit_refusal: lane NOT named in AGT_GRAD_CYCLE_LANES and unregistered: None; registered clean lane: None", T.commit_refusal("lane_unreg") is None and (T.register(g), T.commit_refusal("lane_unreg"))[1] is None)
g2 = fake(); g2.LANE = "lane_fault"; T.register(g2); GC._write_journal(g2, {"status": "running", "boot_id": "older", "cycle_seq": 7}, {}); GC.run_cycle(g2, price_fn=PF)
check("G29 commit_refusal: registered lane with a fault refuses", "unresolved" in (T.commit_refusal("lane_fault") or ""))
_fa = GC.fault_active
GC.fault_active = lambda grad: (_ for _ in ()).throw(RuntimeError("io"))
try: cr = T.commit_refusal("lane_fault")
finally: GC.fault_active = _fa
check("G30 commit_refusal: fault state unreadable refuses (fail closed)", cr is not None and "unreadable" in cr)
# entry_guard integration with a stub paper_account so the earlier mandatory checks pass
import daemon.launch_scanner as _LS
from daemon.launch_scanner import entry_guard as EG
_stub = types.SimpleNamespace(gates_enabled=lambda: True, max_daily_loss_usd=lambda: 1.0, max_drawdown_pct=lambda: 1.0, max_exposure_pct=lambda: 1.0)
_had = getattr(_LS, "paper_account", None); _had_m = sys.modules.get("daemon.launch_scanner.paper_account")
_LS.paper_account = _stub; sys.modules["daemon.launch_scanner.paper_account"] = _stub
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
try:
    os.environ.pop(GC.ENV_FLAG, None)
    r_off = EG.mandatory_entry_refusal("lane_fault")
    os.environ[GC.ENV_FLAG] = "1"
    r_fault = EG.mandatory_entry_refusal("lane_fault"); r_clean = EG.mandatory_entry_refusal("lane_unreg")
    _gt = sys.modules.get("daemon.grad_trader_cycle"); _attr = getattr(sys.modules["daemon"], "grad_trader_cycle")
    sys.modules["daemon.grad_trader_cycle"] = None; delattr(sys.modules["daemon"], "grad_trader_cycle")
    try: r_imp = EG.mandatory_entry_refusal("lane_unreg")
    finally:
        sys.modules["daemon.grad_trader_cycle"] = _gt; setattr(sys.modules["daemon"], "grad_trader_cycle", _attr)
finally:
    os.environ.pop("PAPER_ACCOUNT_ENABLED", None)
    if _had is not None: _LS.paper_account = _had
    else: delattr(_LS, "paper_account")
    if _had_m is not None: sys.modules["daemon.launch_scanner.paper_account"] = _had_m
    else: sys.modules.pop("daemon.launch_scanner.paper_account", None)
check("G31 entry_guard: flag off passes; flag on refuses a faulted registered lane; unregistered passes; broken import refuses", r_off is None and r_fault and "unresolved" in r_fault and r_clean is None and r_imp and "unavailable" in r_imp)
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "grad-select-trader")).read()
check("G32 static: trader entry thread goes through entry_scan and refuses when flag on and the module is missing", "_gtc.entry_scan(engine, _fresh)" in src and "cycle flag on, wiring missing" in src and "_gtc.register(engine)" in src)
# ---- v3 additions (UNEXECUTED) ----
def _lane_env(v):
    if v is None: os.environ.pop(GC.LANES_ENV, None)
    else: os.environ[GC.LANES_ENV] = v
os.environ[GC.ENV_FLAG] = "1"; _lane_env(None)
# G33 two-writer interleaving, deterministic, single process: main's deferred cycle leaves cycle.json running/awaiting_scan; an entry scan then runs and
# finishes; the main loop 'dies' (scan_phase never called). The entry scan must NOT have overwritten the main record, so the next cycle faults.
g = fake(); rec = GC.run_cycle(g, price_fn=PF, scan_fn=scan_for(g), defer_scan=True)
j0 = json.load(open(g.LEDGER_PATH + ".cycle.json")); ran, why, r_ = T.entry_scan(g, scan_for(g)); j1 = json.load(open(g.LEDGER_PATH + ".cycle.json"))
rec2 = GC.run_cycle(g, price_fn=PF, scan_fn=scan_for(g), defer_scan=True)
check("G33 two-writer interleave: entry scan between main's deferred cycle and its death leaves cycle.json running; next cycle records interrupted_prior_cycle (%s/%s)" % (j0.get("status"), j0.get("phase")),
      j0.get("status") == "running" and j1 == j0 and ran is True and any(f.get("reason") == "interrupted_prior_cycle" for f in rec2.get("faults", [])))
# G34 reverse direction: a dead entry scan (record running, lock free) is found by BOTH the entry thread and the main loop's check
g = fake(); T.entry_scan(g, scan_for(g)); ejp = g.LEDGER_PATH + ".entry-scan.json"; ej = json.load(open(ejp)); ej["status"] = "running"; json.dump(ej, open(ejp, "w"))
ok_main, why_main = T._entry_check(g); act, _w = GC.fault_active(g); ran, why, _r = T.entry_scan(g, scan_for(g))
check("G34 dead entry scan (running + free lock): main-side check records interrupted_entry_scan, fault active, entries refused (%s)" % why_main, ok_main is False and why_main == "interrupted_entry_scan" and act and ran is False and g.calls.count("scan") == 1)
# G35 a LIVE entry scan (lock held) is not a fault
g = fake(); T.entry_scan(g, scan_for(g)); ej = json.load(open(g.LEDGER_PATH + ".entry-scan.json")); ej["status"] = "running"
json.dump(ej, open(g.LEDGER_PATH + ".entry-scan.json", "w")); lk = open(g.LEDGER_PATH + ".entry-scan.lock", "w"); fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
ok_live, why_live = T._entry_check(g); fcntl.flock(lk, fcntl.LOCK_UN); lk.close()
check("G35 a running entry record with the lock HELD (live scan) is not a fault", ok_live is True and GC.fault_active(g)[0] is False)
# G36 unreadable entry journal is a fault, fail closed
g = fake(); open(g.LEDGER_PATH + ".entry-scan.json", "w").write("{not json")
ran, why, _r = T.entry_scan(g, scan_for(g))
check("G36 unreadable entry journal: durable fault, scan refused (%s)" % why, ran is False and why == "entry_scan_journal_unreadable" and GC.fault_active(g)[0] and "scan" not in g.calls)
# G37 abort baseline: count above the baseline refuses at commit even with NO fault file yet; advancing only via a completed cycle
g = fake(); g.LANE = "lane_abort"; T.register(g)
before = T.commit_refusal("lane_abort"); g._POLL_ABORTS["count"] = 1; after = T.commit_refusal("lane_abort")
check("G37 abort baseline: clean None; one abort after registration refuses with no fault file written (%s)" % after, before is None and after and "above acknowledged baseline" in after and not os.path.exists(GC._fault_path(g)))
# G38 the abort hook (called by grad.py inside the ledger lock) makes the fault durable at once
g = fake(); g.LANE = "lane_hook"; T.register(g); check("G38a register installs the hook flag on", callable(getattr(g, "_ABORT_HOOK", None)))
g._ABORT_HOOK("mintX", "boom"); fr = GC._read_fault(g)
check("G38 abort hook writes a durable poll_abort_live fault and the append-only log record", fr and fr.get("reason") == "poll_abort_live" and GC._open_logged_faults(g))
# G39 baseline advances only after a completed cycle and only past a durable fault
g = fake(); g.LANE = "lane_adv"; T.register(g); g._POLL_ABORTS["count"] = 2
rec = {"status": "done", "aborts_end": 2, "fault_write_failure": "x"}; T._advance_baseline(g, rec); stuck = T._BASELINE["lane_adv"]
rec = {"status": "done", "aborts_end": 2}; T._advance_baseline(g, rec)
check("G39 baseline not advanced when the fault write failed; advanced after a completed cycle", stuck == 0 and T._BASELINE["lane_adv"] == 2)
# G40 unreadable abort counter refuses
g = fake(); g.LANE = "lane_nocount"; T.register(g); del g._POLL_ABORTS
check("G40 unreadable abort counter refuses fail-closed", "unreadable" in (T.commit_refusal("lane_nocount") or ""))
# G41 selected_lanes semantics: named + unregistered (known or unknown to grad_cycle) refuses; named + registered clean None; not named None
_lane_env("lane_named_unreg,grad_select"); T._ENGINES.pop("grad_select", None)
r_unk = T.commit_refusal("lane_named_unreg"); r_known = T.commit_refusal("grad_select"); r_other = T.commit_refusal("lane_not_named")
_lane_env(None)
check("G41 lane named in AGT_GRAD_CYCLE_LANES but unregistered refuses (known and unknown names); a lane not named is untouched", r_unk and "selected" in r_unk and r_known and "selected" in r_known and r_other is None)
# G42 startup_report modes
os.environ.pop(GC.ENV_FLAG, None); sr_off = T.startup_report(fake())
os.environ[GC.ENV_FLAG] = "1"; sr_nd = T.startup_report(fake(demo=False)); sr_on = T.startup_report(fake()); T._gc = _brk
try: sr_ref = T.startup_report(fake())
finally: T._gc = _orig
check("G42 startup_report distinguishes flag_off, off_non_demo, on, refuse", sr_off.get("detail") == "flag_off" and sr_nd.get("detail") == "off_non_demo" and sr_on.get("mode") == "on" and sr_on.get("active") is True and sr_ref.get("mode") == "refuse")
# G43 static: the entry thread path never imports/uses the cycle lock or the cycle journal; post-scan flag is not part of this source
gsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "daemon", "grad_trader_cycle.py")).read()
i = gsrc.index("def entry_scan("); j2 = gsrc.index("# ---------------------------------------------------------- main loop")
check("G43 static: entry_scan never uses scan_phase, the cycle lock or cycle.json; no post-scan flag in this source", all(x not in gsrc[i:j2] for x in ("scan_phase", "_cycle_lock_path", "_journal_path", "cycle.json")) and "POST_SCAN" not in gsrc)
print("FAILS: %d" % FAILS)
sys.exit(1 if FAILS else 0)
