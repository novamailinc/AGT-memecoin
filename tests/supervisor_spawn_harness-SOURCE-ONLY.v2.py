#!/usr/bin/env python3
"""SOURCE ONLY, UNEXECUTED (written, not run). NEVER RUN without main's explicit clearance for this exact commit and blob.
SV1 + SV2 of the supervisor containment proposal. Target: scripts/supervisor (git blob bb8277ef8f05d0a3c2ed5317e616cfb6163b7d9d), loaded by path with sys.dont_write_bytecode (no pyc), never executed as a script.
SV1 (pure logic, injected clock, no spawn): RestartPolicy / Daemon.on_death / Daemon.tick exact delays and gating from the file's own constants.
SV2 (controlled real spawn): the REAL Supervisor class (class-level ROSTER injection: Supervisor(roster=[one Spec]), DISCLOSED: this is NOT the stock main() and not start.sh/systemd) owns ONE stand-in child
(stdlib-only script written by this harness into the scratch home) through real subprocess.Popen(start_new_session=True), real SIGKILL deaths (rc -9, fsynced sentinel written before each kill), real backoff-ladder
respawns (1 s then 2 s), environment/cwd/session propagation, state file and log file. Real wall-clock delays (about 5 s total).
OWNED-PROCESS CLEANUP: the supervisor starts children in their own session, so killing the driver's process group does NOT reach them. Cleanup is by exact owned pids only: the driver kills/waits its own Popen objects;
the parent then proves every recorded stand-in pid is absent, and only if one is still present AND its /proc cmdline contains this run's unique stand-in path does the parent SIGKILL that exact pid. No name or pattern kills, no other process signalled.
Containment: FOREX_AGENT_HOME, FOREX_SUPERVISOR_PROC_DIR (empty scratch dir: adoption can find nothing) and every file are inside a scratch home; network blocked by the wrapper namespace (the stand-in opens no sockets);
no repo module is imported by the parent; the supervisor file is read, hashed and exec'd by a child only. No pytest, no installs, no deploy.
CLAIM LIMITS: Supervisor class with an injected one-entry roster, one stand-in, SIGKILL, one machine. Not the stock supervisor main(), not start.sh, not systemd, not the real grad-trader (SV3), not adoption of live
processes (scratch proc dir), not the 600 s stable window or 300 s storm hold in wall time (SV1 covers them with an injected clock only)."""
import importlib.util, json, os, signal, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
def _load(n, f):
    sp = importlib.util.spec_from_file_location(n, os.path.join(HERE, f)); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m); return m
V5 = _load("v5mod", "real_supported_loop_entry_recovery_bare_harness-SOURCE-ONLY.v5.py")   # imports run nothing (main guarded); gives ONE shared helper chain (V5.B) and the pinned prelude strings
B = V5.B; check = B.check; FAILS = B.FAILS
SUP_BLOB = "bb8277ef8f05d0a3c2ed5317e616cfb6163b7d9d"

LOADER = r'''
import sys, os, json, time, hashlib
sys.dont_write_bytecode = True
home, name = sys.argv[1], sys.argv[2]; repo = os.environ["AGT_REPO_ROOT"]
os.environ["FOREX_AGENT_HOME"] = home
os.makedirs(os.path.join(home, "proc"), exist_ok=True)
os.environ["FOREX_SUPERVISOR_PROC_DIR"] = os.path.join(home, "proc")
os.environ["SV_PROBE_FLAG"] = "sv-flag-1"
import importlib.machinery, importlib.util
_p = os.path.join(repo, "scripts", "supervisor")
_raw = open(_p, "rb").read(); BLOB = hashlib.sha1(b"blob %d\0" % len(_raw) + _raw).hexdigest()
_ld = importlib.machinery.SourceFileLoader("sup_mod", _p); _sp = importlib.util.spec_from_loader("sup_mod", _ld); S = importlib.util.module_from_spec(_sp); _ld.exec_module(S)
CONSTS = {"TICK_S": S.TICK_S, "LADDER": list(S.BACKOFF_LADDER), "STABLE_S": S.STABLE_S, "STORM_MAX": S.STORM_MAX_STARTS, "STORM_WINDOW": S.STORM_WINDOW_S, "STORM_HOLD": S.STORM_HOLD_S}
'''

SV1 = LOADER + r'''
R = {"blob": BLOB, "consts": CONSTS, "home": S.HOME, "proc_dir": S.PROC_DIR, "no_pyc_flag": sys.dont_write_bytecode}
P = S.RestartPolicy(); R["delays"] = [P.delay() for _ in range(7)]
P.note_start(1.0); P.note_stable(); R["after_stable"] = [P.delay(), P.starts]
P = S.RestartPolicy()
for i in range(5): P.note_start(100.0 + i)
R["storm5"] = [P.hold_until, P.allowed(105.0)]
P.note_start(105.0)
R["storm6"] = [P.hold_until, P.allowed(105.0), P.allowed(105.0 + S.STORM_HOLD_S - 0.1), P.allowed(105.0 + S.STORM_HOLD_S), P.in_hold(105.0 + 1.0)]
P = S.RestartPolicy(); P.note_start(100.0); P.note_start(100.0 + S.STORM_WINDOW_S + 1.0); R["window_prune"] = len(P.starts)
D = S.Daemon(S.Spec("x", ["x"], "x")); D.state = "running"; D.started_at = 10.0; D.on_death(11.0, -9)
R["d1"] = [D.state, D.restart_at, D.last_exit, D.restarts, D.owned, D.pid]
D.state = "running"; D.started_at = 13.0; D.on_death(14.0, -9); R["d2"] = [D.state, D.restart_at, D.restarts]
D.state = "running"; D.started_at = 20.0; D.on_death(20.0 + S.STABLE_S, -9); R["d3_stable_reset"] = [D.state, D.restart_at, D.restarts]
D = S.Daemon(S.Spec("y", ["y"], "y")); sp = []; D.spawn = lambda now: sp.append(now); D.state = "backoff"; D.restart_at = 50.0
D.tick(49.9); a = list(sp); D.tick(50.0); R["tick_gate"] = [a, list(sp), D.state]
D = S.Daemon(S.Spec("z", ["z"], "z")); sp2 = []; D.spawn = lambda now: sp2.append(now)
for t in range(6): D.policy.note_start(float(t))
D.state = "running"; D.started_at = 5.0; D.on_death(6.0, -9); R["storm_daemon"] = [D.state, D.restart_at, D.storm_alerted]
D.tick(304.9); b = list(sp2); D.tick(305.0); R["storm_tick"] = [b, list(sp2)]
D = S.Daemon(S.Spec("g", ["g"], "g")); D.state = "adopted"; D.owned = False; D.pid = 2 ** 22 - 3; D.started_at = 1.0; D.tick(2.0); R["gone_state"] = [D.state, D.last_exit, D.restarts]
print(json.dumps(R))
'''

STANDIN = r'''
import os, sys, json, time, signal
home = sys.argv[1]; p = os.path.join(home, "standin.starts"); n = 0
try: n = len(open(p).read().splitlines())
except FileNotFoundError: pass
inc = n + 1
rec = {"inc": inc, "pid": os.getpid(), "ts": time.time(), "cwd": os.getcwd(), "FOREX_AGENT_HOME": os.environ.get("FOREX_AGENT_HOME"), "SV_PROBE_FLAG": os.environ.get("SV_PROBE_FLAG"), "sid_is_own": os.getsid(0) == os.getpid(), "ppid": os.getppid()}
with open(p, "a") as f: f.write(json.dumps(rec) + "\n"); f.flush(); os.fsync(f.fileno())
print("standin incarnation", inc, flush=True)
if inc < 3:
    with open(os.path.join(home, "standin.killing.%d" % inc), "w") as f: f.write(json.dumps({"pid": os.getpid()})); f.flush(); os.fsync(f.fileno())
    os.kill(os.getpid(), signal.SIGKILL)
while True: time.sleep(0.2)
'''

SV2 = LOADER + r'''
standin = os.path.join(home, "standin.py")
spec = S.Spec("sv-standin", [sys.executable, standin, home], standin)
sup = S.Supervisor(roster=[spec]); t0 = time.time(); sup.adopt_existing(t0)
log = []; last = None; end = t0 + float(sys.argv[3]); stopped = "deadline"
while time.time() < end:
    now = time.time(); sup.tick(now); sup.write_state(now)
    d = sup.daemons["sv-standin"]; cur = (d.state, d.pid, d.restarts, d.last_exit, d.owned)
    if cur != last: log.append([round(now - t0, 3)] + list(cur)); last = cur
    if d.restarts >= 2 and d.state == "running" and d.started_at and now - d.started_at >= 1.5: stopped = "third incarnation stable 1.5s"; break
    time.sleep(0.1)
final = sup.state_dict(time.time()); owned_pids = []; cleaned = []; LEAVE = len(sys.argv) > 4 and sys.argv[4] == "leave"
for d in sup.daemons.values():
    if d.owned and d.proc is not None:
        owned_pids.append(d.proc.pid)
        if d.proc.poll() is None and not LEAVE:
            d.proc.kill(); d.proc.wait(timeout=5); cleaned.append(d.proc.pid)
print(json.dumps({"log": log, "stopped": stopped, "consts": CONSTS, "root": S.ROOT, "home": S.HOME, "proc_dir": S.PROC_DIR, "blob": BLOB, "final": final, "owned_pids": owned_pids, "cleaned": cleaned, "driver_pid": os.getpid()}))
'''

def pid_state(pid, marker, env_marker=None):
    """-> 'absent' | 'zombie' (dead, awaiting its reaper) | 'present-owned' (cmdline contains marker, and when given /proc environ carries env_marker) | 'present-other'. Never signals."""
    try: os.kill(pid, 0)
    except ProcessLookupError: return "absent"
    except PermissionError: return "present-other"
    try:
        stat = open("/proc/%d/stat" % pid).read(); st = stat[stat.rindex(")") + 2:][:1]
        if st == "Z": return "zombie"
        cmd = open("/proc/%d/cmdline" % pid, "rb").read().replace(b"\0", b" ").decode("utf8", "replace")
        envok = True if env_marker is None else (env_marker.encode() in open("/proc/%d/environ" % pid, "rb").read())
    except Exception: return "absent"
    return "present-owned" if (marker in cmd and envok) else "present-other"

def reap_owned(home, marker):
    """Last-resort cleanup by EXACT recorded pid and only when the live cmdline carries this run's unique stand-in path."""
    out = []
    try: recs = [json.loads(l) for l in open(os.path.join(home, "standin.starts")).read().splitlines() if l.strip()]
    except Exception: recs = []
    for r in recs:
        if pid_state(r["pid"], marker) == "present-owned":
            try: os.kill(r["pid"], signal.SIGKILL); out.append(r["pid"])
            except Exception: pass
    return out

def sv1():
    h = B.newhome(); p = B.spawn(SV1, h, "sv1")
    r = B.finish(p, 20.0)
    check("SV1 driver exited 0 and produced a result (rc %s)" % p.returncode, p.returncode == 0 and isinstance(r, dict))
    if not isinstance(r, dict): return
    check("SV1 target identity: scripts/supervisor blob %s == %s; loaded by path with sys.dont_write_bytecode %s; HOME %s and PROC_DIR %s are inside the scratch home" % (r["blob"], SUP_BLOB, r["no_pyc_flag"], r["home"] == h, r["proc_dir"] == os.path.join(h, "proc")),
          r["blob"] == SUP_BLOB and r["no_pyc_flag"] is True and r["home"] == h and r["proc_dir"] == os.path.join(h, "proc"))
    c = r["consts"]
    check("SV1 constants (pinned source) TICK_S %s == 1.0, ladder %s == [1,2,5,15,60], STABLE_S %s == 600, storm max %s == 6 in %s s, hold %s == 300" % (c["TICK_S"], c["LADDER"], c["STABLE_S"], c["STORM_MAX"], c["STORM_WINDOW"], c["STORM_HOLD"]),
          c["TICK_S"] == 1.0 and c["LADDER"] == [1.0, 2.0, 5.0, 15.0, 60.0] and c["STABLE_S"] == 600.0 and c["STORM_MAX"] == 6 and c["STORM_WINDOW"] == 600.0 and c["STORM_HOLD"] == 300.0)
    check("SV1 backoff ladder delays %s == [1,2,5,15,60,60,60] (capped at the last rung); note_stable resets: next delay and starts %s == [1.0, []]" % (r["delays"], r["after_stable"]), r["delays"] == [1.0, 2.0, 5.0, 15.0, 60.0, 60.0, 60.0] and r["after_stable"] == [1.0, []])
    check("SV1 storm guard: 5 starts do not hold (hold_until %s == 0.0, allowed %s); the 6th within the window holds until start+300 (%s == 405.0), not allowed at 105 (%s) or 404.9 (%s), allowed at 405 (%s), in_hold at 106 (%s); a start older than the window is pruned (starts kept %s == 1)" %
          (r["storm5"][0], r["storm5"][1], r["storm6"][0], r["storm6"][1], r["storm6"][2], r["storm6"][3], r["storm6"][4], r["window_prune"]),
          r["storm5"] == [0.0, True] and r["storm6"] == [405.0, False, False, True, True] and r["window_prune"] == 1)
    check("SV1 on_death with a SIGKILL exit (-9): state/restart_at/last_exit/restarts/owned/pid %s == ['backoff', 12.0, -9, 1, False, 0]; second death restart_at %s == 16.0 (2 s rung), restarts 2; a death after STABLE_S resets the ladder: %s == ['backoff', 621.0, 3]" % (r["d1"], r["d2"][1:], r["d3_stable_reset"]),
          r["d1"] == ["backoff", 12.0, -9, 1, False, 0] and r["d2"] == ["backoff", 16.0, 2] and r["d3_stable_reset"] == ["backoff", 621.0, 3])
    check("SV1 tick gating (empty scratch proc dir, injected spawn): no spawn before restart_at (calls %s == []), exactly one at restart_at (%s == [50.0])" % (r["tick_gate"][0], r["tick_gate"][1]), r["tick_gate"][0] == [] and r["tick_gate"][1] == [50.0])
    check("SV1 storm via Daemon: on_death after 6 starts gives %s == ['storm-hold', 305.0, True]; tick at 304.9 spawns nothing (%s), at 305.0 exactly once (%s)" % (r["storm_daemon"], r["storm_tick"][0], r["storm_tick"][1]),
          r["storm_daemon"] == ["storm-hold", 305.0, True] and r["storm_tick"] == [[], [305.0]])
    check("SV1 an adopted (not owned) daemon whose pid is gone is detected as death with exit 'gone' (state/last_exit/restarts %s == ['backoff', 'gone', 1])" % (r["gone_state"],), r["gone_state"] == ["backoff", "gone", 1])

def sv2():
    h = B.newhome(); sp = os.path.join(h, "standin.py"); open(sp, "w").write(STANDIN)
    p = B.spawn(SV2, h, "sv2", 25)
    try:
        r = B.finish(p, 40.0)
        starts = []
        try: starts = [json.loads(l) for l in open(os.path.join(h, "standin.starts")).read().splitlines() if l.strip()]
        except Exception: pass
        check("SV2 driver (real Supervisor class, one injected Spec) exited 0 with a result (rc %s, stopped: %s)" % (p.returncode, (r or {}).get("stopped")), p.returncode == 0 and isinstance(r, dict) and r.get("stopped") == "third incarnation stable 1.5s")
        if not isinstance(r, dict): return
        check("SV2 target identity and containment: supervisor blob %s == %s, HOME %s and PROC_DIR %s inside the scratch home, ROOT %s == the repo root %s" % (r["blob"], SUP_BLOB, r["home"] == h, r["proc_dir"] == os.path.join(h, "proc"), os.path.realpath(r["root"]) == os.path.realpath(B.REPO), os.path.realpath(r["root"]) == os.path.realpath(B.REPO)),
              r["blob"] == SUP_BLOB and r["home"] == h and r["proc_dir"] == os.path.join(h, "proc") and os.path.realpath(r["root"]) == os.path.realpath(B.REPO))
        pids = [x["pid"] for x in starts]
        k1 = os.path.exists(os.path.join(h, "standin.killing.1")); k2 = os.path.exists(os.path.join(h, "standin.killing.2")); k3 = os.path.exists(os.path.join(h, "standin.killing.3"))
        check("SV2 three stand-in incarnations ran (file read directly): incs %s == [1,2,3], pids distinct %s; fsynced kill sentinels for 1 and 2 present (%s, %s), none for 3 (%s)" % ([x["inc"] for x in starts], len(set(pids)) == 3, k1, k2, k3),
              [x["inc"] for x in starts] == [1, 2, 3] and len(set(pids)) == 3 and k1 and k2 and not k3)
        seq = []
        for e in r["log"]:
            if not seq or seq[-1] != e[1]: seq.append(e[1])
        bo = [e for e in r["log"] if e[1] == "backoff"]
        check("SV2 supervisor state sequence %s == ['running','backoff','running','backoff','running'] (never adopted/storm-hold), backoff entries carry last_exit %s == [-9, -9] and restarts %s == [1, 2], all owned" % (seq, [e[4] for e in bo], [e[3] for e in bo]),
              seq == ["running", "backoff", "running", "backoff", "running"] and [e[4] for e in bo] == [-9, -9] and [e[3] for e in bo] == [1, 2] and all(e[5] for e in r["log"] if e[1] == "running"))
        g1 = starts[1]["ts"] - starts[0]["ts"]; g2 = starts[2]["ts"] - starts[1]["ts"]
        print("INFO SV2 MEASURED start gaps (s): %.3f, %.3f; state log %s" % (g1, g2, r["log"]))
        check("SV2 real ladder delays: gap start1->start2 %.2f s is >= 1.0 (first rung) and <= 3.5, gap start2->start3 %.2f s is >= 2.0 (second rung) and <= 4.5 (the upper bounds are measurement tolerance for tick and scheduling, not a production claim)" % (g1, g2),
              1.0 <= g1 <= 3.5 and 2.0 <= g2 <= 4.5)
        probe = all(x["SV_PROBE_FLAG"] == "sv-flag-1" and x["FOREX_AGENT_HOME"] == h for x in starts)
        check("SV2 spawn environment: every incarnation saw the driver's environment variable (%s) and FOREX_AGENT_HOME == scratch home; cwd == repo root (%s); own session (%s); parent pid == driver pid %s (%s)" %
              (probe, all(os.path.realpath(x["cwd"]) == os.path.realpath(B.REPO) for x in starts), all(x["sid_is_own"] for x in starts), r["driver_pid"], all(x["ppid"] == r["driver_pid"] for x in starts)),
              probe and all(os.path.realpath(x["cwd"]) == os.path.realpath(B.REPO) for x in starts) and all(x["sid_is_own"] for x in starts) and all(x["ppid"] == r["driver_pid"] for x in starts))
        try: st = json.load(open(os.path.join(h, "supervisor_state.json")))["daemons"]["sv-standin"]
        except Exception as e: st = {"err": repr(e)}
        try: lg = open(os.path.join(h, "log", "sv-standin.log")).read()
        except Exception as e: lg = ""
        check("SV2 state file read directly: restarts %s == 2, state %s == running, owned %s, pid %s == the third incarnation pid %s; child log has all three incarnation lines (%s)" % (st.get("restarts"), st.get("state"), st.get("owned"), st.get("pid"), pids[2] if len(pids) == 3 else None, all(("standin incarnation %d" % i) in lg for i in (1, 2, 3))),
              st.get("restarts") == 2 and st.get("state") == "running" and st.get("owned") is True and len(pids) == 3 and st.get("pid") == pids[2] and all(("standin incarnation %d" % i) in lg for i in (1, 2, 3)))
        check("SV2 owned-pid cleanup by the driver: it killed and reaped only its own live child (cleaned %s == [third pid %s]); owned pids it tracked %s" % (r["cleaned"], pids[2] if len(pids) == 3 else None, r["owned_pids"]), len(pids) == 3 and r["cleaned"] == [pids[2]] and pids[2] in r["owned_pids"])
        time.sleep(0.2)
        states = [pid_state(x, sp) for x in pids]
        check("SV2 every recorded stand-in pid is absent after the driver exit (explicit /proc and kill-0 probe: %s)" % (states,), states == ["absent"] * 3)
    finally:
        left = reap_owned(h, sp)
        if left: print("INFO SV2 parent last-resort cleanup killed exact owned pids %s (cmdline carried the unique stand-in path)" % (left,))


# ---------------- SV5: the supervisor exits, its child keeps running (source: "supervisor stopping (children keep running)") ----------------
def sv5():
    h = B.newhome(); sp = os.path.join(h, "standin.py"); open(sp, "w").write(STANDIN)
    p = B.spawn(SV2, h, "sv5", 25, "leave")
    try:
        r = B.finish(p, 40.0)
        try: starts = [json.loads(l) for l in open(os.path.join(h, "standin.starts")).read().splitlines() if l.strip()]
        except Exception: starts = []
        check("SV5 driver exited 0 (supervisor loop ended, cleanup of the child deliberately SKIPPED: leave mode) with a result (rc %s) and 3 incarnations" % p.returncode, p.returncode == 0 and isinstance(r, dict) and len(starts) == 3 and r.get("cleaned") == [])
        if not (isinstance(r, dict) and len(starts) == 3): return
        pid = starts[2]["pid"]; time.sleep(0.3)
        st = pid_state(pid, sp)
        check("SV5 after the supervisor process has exited, its last child (pid %s) is STILL RUNNING (state %s == present-owned: cmdline carries this run's unique stand-in path)" % (pid, st), st == "present-owned" and p.poll() is not None)
        if st == "present-owned":
            os.kill(pid, signal.SIGKILL)       # exact owned pid, verified by the unique cmdline path just above; nothing else is signalled
            time.sleep(0.3); st2 = pid_state(pid, sp)
            check("SV5 exact-pid cleanup: after SIGKILL of that one pid its state is %s (absent, or zombie awaiting the namespace init to reap it)" % st2, st2 in ("absent", "zombie"))
    finally:
        left = reap_owned(h, sp)
        if left: print("INFO SV5 last-resort cleanup killed exact owned pids %s" % (left,))

# ---------------- SV3: REAL scripts/grad-trader under the REAL Supervisor class, scratch-only sitecustomize shim ----------------
# DISCLOSED: (1) class-level roster injection (Supervisor(roster=[Spec])), not stock main/start.sh/systemd; (2) the Spec argv uses the contained interpreter sys.executable instead of the roster's literal "python3";
# (3) the fixture prelude (bare grad import, fake pumpportal/killswitch, fixture price and scan seams, SIGKILL seam, bounded loop, final snapshot) is delivered by a sitecustomize module in a scratch dir, put on PYTHONPATH
# ONLY by the driver AFTER its own start so the supervisor's Popen env carries it into the child and nothing else loads it; the pinned scripts/grad-trader is untouched and runs as __main__ via its own path.
# The shim is the V5 prelude text with sys.argv replaced by a per-incarnation argv built from env (replacement count asserted). Incarnation 1: SIGKILL at the entry seam in pass 0 (rc -9 seen by the supervisor);
# incarnation 2 (respawned after the 1 s ladder rung): 5 passes with the 3x production-seam quote, then final snapshot and idle until the driver kills its own child. A loop pass is counted only when time.sleep is
# called from a frame whose file is the real scripts/grad-trader (same pass semantics as the V5 M.time shim).
SHIM_HEAD = """
import os, sys, json
_cfgs = os.environ.get("SV3_CFG")
if _cfgs:
    _cfg = json.loads(_cfgs); home = _cfg["home"]
    _ip = os.path.join(home, "sv3.inc")
    try: _n = len([l for l in open(_ip).read().splitlines() if l.strip()])
    except FileNotFoundError: _n = 0
    _inc = _n + 1
    with open(_ip, "a") as _f: _f.write(json.dumps({"inc": _inc, "pid": os.getpid()}) + "\\n"); _f.flush(); os.fsync(_f.fileno())
    _c = _cfg["inc"][str(min(_inc, 2))]
    _A = [None, home, "sv3i%d" % _inc, _cfg["ei"], json.dumps(_c["sched"]), _cfg["cyc"], str(_c["kill"]), str(_c["npass"]), str(_c["mult"]), _c["role"]]
"""
SHIM_TAIL = r"""
    import types
    PKG_BEFORE_SCRIPT = PKGN in sys.modules; SYSGRAD_BEFORE = sys.modules.get("grad") is GR
    class _FakeFeed:
        def start(self): pass
        def drain(self): return []
    sys.modules["pumpportal"] = types.SimpleNamespace(PumpPortalFeed=_FakeFeed)
    sys.modules["killswitch"] = types.SimpleNamespace(check_lane=lambda lane: {"lane": lane})
    _SCRIPT = os.path.realpath(os.path.join(os.environ["AGT_REPO_ROOT"], "scripts", "grad-trader"))
    _real_sleep = time.sleep; _SEEN = [False]
    def _ident2():
        _M = sys.modules.get("__main__")
        return {"bare_grad_in_sysmodules_before_script": SYSGRAD_BEFORE, "no_pkg_grad_before_script": not PKG_BEFORE_SCRIPT, "no_pkg_grad_at_boot": not PKG_AT_BOOT, "no_pkg_grad_after_script_import": PKGN not in sys.modules,
                "GR_name_is_grad": GR.__name__ == "grad", "script_grad_is_GR": getattr(_M, "_grad", None) is GR, "script_gtc_is_T": getattr(_M, "_gtc", None) is T, "script_flag_on": getattr(_M, "_FLAG_ON", None), "has_grad": getattr(_M, "_HAS_GRAD", None),
                "main_is_real_script": os.path.realpath(os.path.join(os.getcwd(), getattr(_M, "__file__", "") or "")) == _SCRIPT}
    def _finish():
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf): _final(ROLE, "shim-passes-done", {"ident2": _ident2(), "supervised": True})
        _p = os.path.join(home, "final.%s.json" % name)
        with open(_p + ".tmp", "w") as _f: _f.write(buf.getvalue()); _f.flush(); os.fsync(_f.fileno())
        os.replace(_p + ".tmp", _p)
        while True: _real_sleep(0.2)
    def _sleep(s):
        f = sys._getframe(1)
        if os.path.realpath(os.path.join(os.getcwd(), f.f_code.co_filename)) == _SCRIPT:
            if not _SEEN[0]:
                _SEEN[0] = True; token("SCRIPT_LOOP_SEEN", file=f.f_code.co_filename, cwd=os.getcwd(), argv=list(sys.argv), pid=os.getpid())
            _snap(); PASSNO[0] += 1
            if PASSNO[0] >= NPASS: _finish()
            return None
        return _real_sleep(s)
    time.sleep = _sleep
    token("SHIM_LOADED", mode=list(T.mode_info(GR)), live_ident_pre_main=_live_ident(), inc=_inc, pid=os.getpid())
"""
def build_shim():
    body = V5.COMMON
    n = body.count("sys.argv"); assert n >= 8, n
    body = body.replace("sys.argv", "_A")
    ind = "\n".join(("    " + l if l.strip() else l) for l in body.splitlines())      # whole prelude under `if _cfgs:` so the module is inert unless the driver exported SV3_CFG
    return SHIM_HEAD + ind + "\n" + SHIM_TAIL, n

DRIVER3 = LOADER + r'''
cfg = sys.argv[4]; shim = os.path.join(home, "shim")
spec = S.Spec("grad-trader", [sys.executable, "scripts/grad-trader"], "scripts/grad-trader")
sup = S.Supervisor(roster=[spec]); t0 = time.time(); sup.adopt_existing(t0)
os.environ["SV3_CFG"] = cfg; os.environ["PYTHONPATH"] = shim; os.environ["PYTHONDONTWRITEBYTECODE"] = "1"      # set AFTER this process started: only the supervisor's children inherit them
log = []; last = None; end = t0 + float(sys.argv[3]); stopped = "deadline"; fin = os.path.join(home, "final.sv3i2.json"); seen = None
while time.time() < end:
    now = time.time(); sup.tick(now); sup.write_state(now)
    d = sup.daemons["grad-trader"]; cur = (d.state, d.pid, d.restarts, d.last_exit, d.owned)
    if cur != last: log.append([round(now - t0, 3)] + list(cur)); last = cur
    if os.path.exists(fin) and d.state == "running":
        seen = seen or now
        if now - seen >= 0.5: stopped = "final snapshot of incarnation 2 present"; break
    time.sleep(0.1)
final = sup.state_dict(time.time()); owned_pids = []; cleaned = []
for d in sup.daemons.values():
    if d.owned and d.proc is not None:
        owned_pids.append(d.proc.pid)
        if d.proc.poll() is None:
            d.proc.kill(); d.proc.wait(timeout=5); cleaned.append(d.proc.pid)
print(json.dumps({"log": log, "stopped": stopped, "root": S.ROOT, "home": S.HOME, "blob": BLOB, "final": final, "owned_pids": owned_pids, "cleaned": cleaned, "driver_pid": os.getpid()}))
'''

def tokfile(h, name, tag): return V5.tokfile(h, name, tag)

def sv3(lab, cyc):
    h = B.newhome(); shimdir = os.path.join(h, "shim"); os.makedirs(shimdir)
    shim, nrep = build_shim(); open(os.path.join(shimdir, "sitecustomize.py"), "w").write(shim)
    try: compile(shim, "sitecustomize.py", "exec")
    except SyntaxError as e: raise B.Stop("shim does not compile: %r" % (e,))
    cfg = {"home": h, "ei": "1", "cyc": cyc, "inc": {"1": {"sched": {"0": [1]}, "kill": 0, "npass": 6, "mult": 1.0, "role": "kill"}, "2": {"sched": {"1": [9]}, "kill": -1, "npass": 5, "mult": 3.0, "role": "restart"}}}
    marker_env = "FOREX_AGENT_HOME=" + h; gt = os.path.join("scripts", "grad-trader")
    p = B.spawn(DRIVER3, h, "sv3d", 70, json.dumps(cfg))
    try:
        r = B.finish(p, 90.0)
        check("%s driver (real Supervisor class owning the real scripts/grad-trader; shim built from %d replaced sys.argv sites, asserted >= 8) exited 0 and ended because incarnation 2 wrote its final snapshot (rc %s, stopped %s)" % (lab, nrep, p.returncode, (r or {}).get("stopped")),
              p.returncode == 0 and isinstance(r, dict) and r.get("stopped") == "final snapshot of incarnation 2 present")
        if not isinstance(r, dict): return
        check("%s target identity: supervisor blob %s == %s; supervisor HOME == scratch home (%s); ROOT == repo root (%s)" % (lab, r["blob"], SUP_BLOB, r["home"] == h, os.path.realpath(r["root"]) == os.path.realpath(B.REPO)), r["blob"] == SUP_BLOB and r["home"] == h and os.path.realpath(r["root"]) == os.path.realpath(B.REPO))
        seq = []
        for e in r["log"]:
            if not seq or seq[-1] != e[1]: seq.append(e[1])
        bo = [e for e in r["log"] if e[1] == "backoff"]; run = [e for e in r["log"] if e[1] == "running"]
        try: incs = [json.loads(l) for l in open(os.path.join(h, "sv3.inc")).read().splitlines() if l.strip()]
        except Exception: incs = []
        check("%s SUPERVISED RESTART: state sequence %s == ['running','backoff','running'], the backoff entry carries last_exit %s == [-9] and restarts %s == [1], two shim incarnations only (%d == 2, no third spawn), distinct pids (%s), supervisor-owned" % (lab, seq, [e[4] for e in bo], [e[3] for e in bo], len(incs), len(set(x["pid"] for x in incs)) == 2),
              seq == ["running", "backoff", "running"] and [e[4] for e in bo] == [-9] and [e[3] for e in bo] == [1] and len(incs) == 2 and len(set(x["pid"] for x in incs)) == 2 and all(e[5] for e in run))
        t1_ = tokfile(h, "sv3i1", "SHIM_LOADED"); t2_ = tokfile(h, "sv3i2", "SHIM_LOADED"); ls1 = tokfile(h, "sv3i1", "SEAM_REACHED"); sv = tokfile(h, "sv3i1", "SURVIVED"); seen = tokfile(h, "sv3i2", "SCRIPT_LOOP_SEEN")
        pm2 = (t2_ or {}).get("live_ident_pre_main") or {}
        check("%s SHIM + SCRIPT EVIDENCE (tokens read directly): shim loaded in both incarnations (%s, %s) before the script (pre-main registered_is_GR %s, package loaded %s), incarnation 1 reached the entry seam (%s) and never survived it (SURVIVED absent: %s), incarnation 2 ran the real scripts/grad-trader loop (file %s, cwd == repo root %s, argv %s)" %
              (lab, t1_ is not None, t2_ is not None, pm2.get("registered_is_GR"), pm2.get("pkg_grad_loaded"), ls1 is not None, sv is None, (seen or {}).get("file"), os.path.realpath((seen or {}).get("cwd", "")) == os.path.realpath(B.REPO), (seen or {}).get("argv")),
              t1_ is not None and t2_ is not None and ls1 is not None and sv is None and seen is not None and pm2.get("registered_is_GR") is False and pm2.get("pkg_grad_loaded") is False
              and os.path.realpath(seen.get("cwd", "")) == os.path.realpath(B.REPO) and (seen.get("argv") or [""])[0].endswith(gt))
        fin = None
        try: fin = json.load(open(os.path.join(h, "final.sv3i2.json")))
        except Exception as e: print("INFO %s final snapshot unreadable: %r" % (lab, e))
        i2 = ((fin or {}).get("extra") or {}).get("ident2") or {}
        check("%s final snapshot of incarnation 2 present and parsed; script identity ident2 (%s): every flag true, except script_flag_on which must equal the cycle flag (%s)" % (lab, i2, cyc == "1"),
              isinstance(fin, dict) and bool(i2) and all(bool(v) for k, v in i2.items() if k != "script_flag_on") and i2.get("script_flag_on") == (cyc == "1"))
        if not isinstance(fin, dict): return
        pid_ = V5.common_pre(lab, "1", ls1, fin, cyc)
        passes = fin["passes"]; a = fin["after"]
        print("INFO %s MEASURED per pass: lane %s, credits for the killed pos_id %s, scan calls %s, fault reasons %s; candidate considerations %s" % (lab, [x["lane"] for x in passes], V5.cum(passes, pid_), [x["scan_calls"] for x in passes], [x["fault_reasons"] for x in passes], fin["calls"]["cand"]))
        adopt_pass = V5.recovery_and_management(lab, fin, pid_)
        if cyc == "0":
            fresh = [x for x in fin["calls"]["cand"] if x[1] == V5.FRESH_MINT]; opened = [x for x in fresh if x[2]]
            check("%s ENTRY PERMISSION (legacy loop, no latch; MEASURED not endorsed): no cycle fault (%s), no clear log (%s); the fresh candidate %s was admitted exactly once (%s) only after the adoption pass %s; debits are the killed + the fresh pos_id (%s)" % (lab, [x["fault_reasons"] for x in passes], fin["clear_log_exists"], V5.FRESH_MINT, fresh, adopt_pass, V5.deb_pos(a, "Mint")),
                  all(x["fault_reasons"] == [] for x in passes) and fin["clear_log_exists"] is False and fin["clear_enabled"] is False and len(opened) == 1 and adopt_pass is not None and opened[0][0] >= adopt_pass and len(V5.deb_pos(a, "Mint")) == 2 and len(V5.deb_pos(a, V5.FRESH_MINT)) == 1)
        else:
            V5.entry_permission_latch(lab, fin, pid_)
        print("INFO %s CLAIM LIMIT: injected-startup fixture (sitecustomize shim, class-level roster, contained interpreter), not stock start.sh/systemd; one SIGKILL at the entry seam; cadence/max-hold/close not exercised; flags set by the shim config, not by any deployment file" % lab)
        time.sleep(0.2)
        states = [pid_state(x["pid"], gt, marker_env) for x in incs]
        check("%s OWNED-PROCESS CLEANUP: the driver killed and reaped only its own live child (cleaned %s == [incarnation 2 pid %s]); every recorded grad-trader pid is absent after the driver exit (kill-0 + /proc probe %s)" % (lab, r["cleaned"], incs[1]["pid"] if len(incs) == 2 else None, states),
              len(incs) == 2 and r["cleaned"] == [incs[1]["pid"]] and states == ["absent", "absent"])
    finally:
        out = []
        try: recs = [json.loads(l) for l in open(os.path.join(h, "sv3.inc")).read().splitlines() if l.strip()]
        except Exception: recs = []
        for x in recs:
            if pid_state(x["pid"], gt, marker_env) == "present-owned":
                try: os.kill(x["pid"], signal.SIGKILL); out.append(x["pid"])
                except Exception: pass
        if out: print("INFO %s parent last-resort cleanup killed exact owned pids %s (cmdline carries scripts/grad-trader AND /proc environ carries this scratch FOREX_AGENT_HOME)" % (lab, out))


def main():
    try:
        for n, f in (("SV1", sv1), ("SV2", sv2), ("SV5", sv5), ("SV3a", lambda: sv3("SV3a", "0")), ("SV3b", lambda: sv3("SV3b", "1"))): B.scen(n, f)
    finally: B.reap_all()
    alive = [p.pid for p in B.KIDS if p.poll() is None]
    check("all harness-spawned children reaped (still alive: %s), total %d children spawned by the harness (stand-ins are the supervisor's, proven absent above)" % (alive, len(B.KIDS)), not alive)
    check("no bytecode written under the repo (scripts/__pycache__ exists: %s, tests/__pycache__ exists: %s)" % (os.path.exists(os.path.join(B.REPO, "scripts", "__pycache__")), os.path.exists(os.path.join(B.REPO, "tests", "__pycache__"))),
          not os.path.exists(os.path.join(B.REPO, "scripts", "__pycache__")) and not os.path.exists(os.path.join(B.REPO, "tests", "__pycache__")))
    print("elapsed %.1fs of 90s budget; FAILS: %d" % (90.0 - B.left(), len(FAILS))); return 1 if FAILS else 0
if __name__ == "__main__":
    sys.exit(main())
