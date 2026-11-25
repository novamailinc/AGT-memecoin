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
check("G17 static: the trader imports the wiring fail-soft and the entry thread consults entry_allowed", "except Exception:\n    _gtc = None" in src and "_gtc.entry_allowed(engine)" in src)
check("G18 static: push-price and ExitPoller paths still call engine.poll_exits directly (unjournaled by design)", src.count("engine.poll_exits(_armed_price_fn)") >= 2)
print("FAILS: %d" % FAILS)
sys.exit(1 if FAILS else 0)
