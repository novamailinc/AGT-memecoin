"""Resident supervisor — restart policy, storm guard, death handling.
Loads scripts/supervisor as a module; spawns nothing real."""

import importlib.util
import os
import sys

_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts",
                     "supervisor")
_spec = importlib.util.spec_from_loader("supervisor_mod",
                                        loader=None)
S = importlib.util.module_from_spec(_spec)
S.__dict__["__file__"] = _PATH
with open(_PATH) as f:
    exec(compile(f.read(), _PATH, "exec"), S.__dict__)

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# roster sanity: every daemon the watchdog used to guard is present
names = {s.name for s in S.ROSTER}
for want in ("launch_scanner",
             "ceo-trader", "mirror-exits", "realtime-sync",
             "locked-liq-scan", "challenge-trader", "momentum-trader",
             "watcher-trader", "grad-select-trader",
             "tranche-trader", "devnet-demo-trader",
             "watcher-v2-trader", "moon-trader",
             "tranche-protect-trader", "live10-demo-mirror",
             "raw-tight-trader", "degen-peak-tracker"):
    check("sup: roster has %s" % want, want in names)
check("sup: roster has no smart-select-trader",
      "smart-select-trader" not in names)
# 25 + tranche-protect-trader + raw-tight-trader (2026-10-31 test lanes)
# = 27. Mimic family (mimic, mimic-edge, mimic-edge2) REMOVED
# 2026-11-02 on the user's permanent-kill order (unprofitable).
# + live10-demo-mirror (2026-11-02 $10 split across live lanes;
# paper-live-trader superseded same day, 0 trades — roster stays 28).
check("sup: roster size 28", len(S.ROSTER) == 28)

# backoff ladder: 1,2,5,15,60,60...
p = S.RestartPolicy()
seq = [p.delay() for _ in range(6)]
check("sup: ladder sequence", seq == [1.0, 2.0, 5.0, 15.0, 60.0, 60.0])
p.note_stable()
check("sup: stable resets ladder", p.delay() == 1.0)

# storm guard: 6 starts inside the window -> hold
p2 = S.RestartPolicy()
t = 1000.0
for i in range(6):
    p2.note_start(t + i * 10)
check("sup: storm hold set", p2.in_hold(t + 60))
check("sup: not allowed during hold", not p2.allowed(t + 60))
check("sup: allowed after hold", p2.allowed(t + 60 + S.STORM_HOLD_S + 1))
# starts spread beyond the window do not storm
p3 = S.RestartPolicy()
for i in range(6):
    p3.note_start(1000.0 + i * (S.STORM_WINDOW_S + 1))
check("sup: spread starts no storm", not p3.in_hold(9000.0))

# daemon death handling: short run -> backoff restart scheduled
spec = S.Spec("dummy", ["python3", "scripts/nope"], "scripts/nope")
d = S.Daemon(spec)
d.state = "running"
d.started_at = 100.0
d.on_death(105.0, 1)
check("sup: death -> backoff", d.state == "backoff")
check("sup: restart scheduled at +1s", d.restart_at == 106.0)
check("sup: restart counted", d.restarts == 1)
check("sup: exit recorded", d.last_exit == 1)

# long stable run resets the ladder before scheduling
d2 = S.Daemon(spec)
d2.policy.ladder_idx = 3
d2.state = "running"
d2.started_at = 0.0
d2.on_death(S.STABLE_S + 10.0, 0)
check("sup: stable run resets ladder", d2.restart_at == S.STABLE_S + 11.0)

# adopted death converts to a scheduled spawn
d3 = S.Daemon(spec)
d3.adopt(4242, 50.0)
check("sup: adopt state", d3.state == "adopted" and d3.pid == 4242)
d3.on_death(55.0, "gone")
check("sup: adopted death -> backoff", d3.state == "backoff")

# state dict shape
sup = S.Supervisor(roster=[spec])
sd = sup.state_dict(1000.0)
check("sup: state has ts + daemons",
      sd["ts"] == 1000.0 and "dummy" in sd["daemons"])

# find_running: venv python must match (devnet-demo-trader runs under
# …/.venv/bin/python, and the old python3-only check meant the
# supervisor could never adopt it, spawning a duplicate each restart)
import tempfile
_fake_proc = tempfile.mkdtemp(prefix="fakeproc_")


def _fake_pid(pid, argv):
    d = os.path.join(_fake_proc, str(pid))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "cmdline"), "wb") as f:
        f.write("\0".join(argv).encode() + b"\0")


_fake_pid(1111, ["/home/hatch/workspace/Forex-agent/.venv/bin/python",
                 "scripts/devnet-demo-trader"])
_fake_pid(2222, ["python3", "scripts/grad-select-trader"])
_fake_pid(3333, ["/bin/bash", "--norc", "-c",
                 "ps aux | grep scripts/devnet-demo-trader"])
_fake_pid(4444, ["python3", "scripts/other-daemon"])
check("sup: find_running adopts venv trader",
      S.find_running("scripts/devnet-demo-trader",
                     proc_dir=_fake_proc) == [1111])
check("sup: find_running adopts system python3 trader",
      S.find_running("scripts/grad-select-trader",
                     proc_dir=_fake_proc) == [2222])
check("sup: find_running ignores shell mentioning token",
      3333 not in S.find_running("scripts/devnet-demo-trader",
                                 proc_dir=_fake_proc))
check("sup: find_running ignores other tokens",
      S.find_running("scripts/nope", proc_dir=_fake_proc) == [])

# adopt-before-spawn (2026-10-31): a daemon in backoff/storm-hold/init
# must adopt a matching process that appeared since death instead of
# spawning a second instance. spawn is stubbed; find_running is stubbed
# so no real process is touched.
_real_find_running = S.find_running


def _stub_find(pids):
    def _f(token, proc_dir="/proc"):
        return list(pids)
    return _f


def _stub_spawn(recorder):
    def _spawn(now):
        recorder.append(now)
    return _spawn


_now = 1000000.0
_d = S.Daemon(S._script("test-adopt"))
_d.state = "backoff"
_d.restart_at = 0
_spawned = []
_d.spawn = _stub_spawn(_spawned)
S.find_running = _stub_find([4242])
_d.tick(_now)
check("sup: tick adopts live match instead of spawning",
      _d.state == "adopted" and _d.pid == 4242 and _spawned == [])
S.find_running = _real_find_running

_d2 = S.Daemon(S._script("test-spawn"))
_d2.state = "backoff"
_d2.restart_at = 0
_spawned2 = []
_d2.spawn = _stub_spawn(_spawned2)
S.find_running = _stub_find([])
_d2.tick(_now)
check("sup: tick spawns when no match exists", _spawned2 == [_now])
S.find_running = _real_find_running

_d3 = S.Daemon(S._script("test-hold"))
_d3.state = "storm-hold"
_d3.restart_at = 0
_d3.policy.hold_until = _now + 600.0
_spawned3 = []
_d3.spawn = _stub_spawn(_spawned3)
S.find_running = _stub_find([4242])
_d3.tick(_now)
check("sup: storm-hold still gates adopt/spawn",
      _d3.state == "storm-hold" and _spawned3 == [])
S.find_running = _real_find_running

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL SUPERVISOR TESTS PASSED")
