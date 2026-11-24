#!/usr/bin/env python3
"""Pidfile rollout (2026-11-16, user order "fix everything").

Bug: the supervisor was the only duplicate protection for the bot's
daemons — no daemon called acquire_pidfile, so a manual spawn could
race the supervisor's adopt-before-spawn check and run a second
instance (~1 min of duplicate traders on 2026-11-16, zero harm,
quota dead). The helper already existed (daemon/common.py) with an
atomic O_EXCL claim and stale reclamation, but nothing used it.

Fix: every roster daemon now claims its pidfile at startup.
  * module daemons (6): Daemon.run_forever() already acquired; it
    now passes the cmdline token and exits 1 (was 0) on a duplicate.
  * script daemons (22): guard_singleton(name, token) is the first
    statement of main() — a duplicate exits 1 before any work.
  * helper hardening: with a token, a recorded pid counts as live
    only when its cmdline contains the token — the 2026-11-16
    telegram incident (stale pidfile held a pid the kernel had
    recycled to an unrelated process, blocking every respawn) can
    no longer happen.

Plain script: run with the repo venv python from the repo root.
No network, no live state: every check runs under a temp
FOREX_AGENT_HOME, and decoy processes are killed in finally
blocks. Real scripts are only ever executed as subprocesses
against a planted live-owner pidfile, so they exit at the guard
before doing any work.
"""
import ast
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = sys.executable

FAILURES = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name
          + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _tmp_home():
    d = tempfile.mkdtemp(prefix="pidfile-test-")
    os.environ["FOREX_AGENT_HOME"] = d
    return d


def _plant_pidfile(home, name, pid):
    run = os.path.join(home, "run")
    os.makedirs(run, exist_ok=True)
    with open(os.path.join(run, name + ".pid"), "w") as fh:
        fh.write(str(pid))


def _start_decoy(token):
    """A live process whose cmdline contains ``token`` (bash exec -a
    sets argv[0]). Returns the Popen object; caller must kill it."""
    return subprocess.Popen(
        ["bash", "-c", 'exec -a "%s" sleep 60' % token],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _run_driver(code, home, timeout=20):
    env = dict(os.environ)
    env["FOREX_AGENT_HOME"] = home
    # Keep any Store() the driver constructs off the repo's own DB.
    env["FOREX_AGENT_STORAGE"] = os.path.join(home, "test-store.db")
    env["PYTHONPATH"] = REPO
    return subprocess.run(
        [VENV_PY, "-c", code], env=env, cwd=REPO, timeout=timeout,
        capture_output=True, text=True)


# ---------------------------------------------------------------------------
# (a) second holder refused
# ---------------------------------------------------------------------------
def test_second_holder_refused():
    from daemon.common import acquire_pidfile, release_pidfile
    home = _tmp_home()
    # Same process, second claim: refused (own pid is alive in file).
    check("acquire first claim wins",
          acquire_pidfile("dup-a") is True)
    check("acquire second claim same process refused",
          acquire_pidfile("dup-a") is False)
    release_pidfile("dup-a")
    check("acquire after release wins again",
          acquire_pidfile("dup-a") is True)
    release_pidfile("dup-a")
    # Separate process, live holder: refused.
    holder = subprocess.Popen(
        [VENV_PY, "-c",
         "import os,sys,time; sys.path.insert(0, %r); "
         "from daemon.common import acquire_pidfile; "
         "assert acquire_pidfile('dup-b'); time.sleep(30)" % REPO],
        env={**os.environ, "FOREX_AGENT_HOME": home,
             "PYTHONPATH": REPO},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.0)
        check("acquire refused while another process holds it",
              acquire_pidfile("dup-b") is False)
        # guard_singleton in a fresh process: exit code 1, clear line.
        # (token 'dup-b': the holder's -c cmdline contains it, so the
        # holder counts as a live owner.)
        r = _run_driver(
            "import sys; sys.path.insert(0, %r); "
            "from daemon.common import guard_singleton; "
            "guard_singleton('dup-b', token='dup-b')" % REPO,
            home)
        check("guard_singleton duplicate exits 1",
              r.returncode == 1, "rc=%s out=%s" % (r.returncode, r.stderr[-200:]))
        check("guard_singleton duplicate logs already running",
              "already running" in r.stderr)
    finally:
        holder.kill()
        holder.wait()
    # Racing acquirers: exactly one winner out of six. The winner
    # must HOLD the pidfile (sleep) while the others attempt — a
    # winner that exits at once leaves a stale file the next racer
    # legitimately reclaims, which would read as extra winners.
    home2 = _tmp_home()
    code = ("import sys, time; sys.path.insert(0, %r); "
            "from daemon.common import acquire_pidfile; "
            "won = acquire_pidfile('race-x'); time.sleep(5); print(won)"
            % REPO)
    procs = [subprocess.Popen(
                 [VENV_PY, "-c", code],
                 env={**os.environ, "FOREX_AGENT_HOME": home2,
                      "PYTHONPATH": REPO},
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 text=True)
             for _ in range(6)]
    wins = 0
    for p in procs:
        out, _ = p.communicate(timeout=20)
        wins += out.strip() == "True"
    check("racing acquirers produce exactly one winner",
          wins == 1, "winners=%d" % wins)


# ---------------------------------------------------------------------------
# (b) stale pidfile does not block
# ---------------------------------------------------------------------------
def test_stale_pidfile_reclaimed():
    from daemon.common import acquire_pidfile, read_pid, release_pidfile
    home = _tmp_home()
    # Dead pid: reclaimed at once.
    dead = subprocess.Popen(["true"])
    dead.wait()
    _plant_pidfile(home, "stale-a", dead.pid)
    check("dead pid in pidfile is stale",
          read_pid("stale-a") is None)
    check("acquire reclaims a dead pidfile",
          acquire_pidfile("stale-a") is True)
    release_pidfile("stale-a")
    # PID-reuse case (2026-11-16 telegram incident): a LIVE pid whose
    # cmdline is NOT our daemon. With a token it must not block; the
    # token-less check still (correctly) refuses.
    decoy = _start_decoy("some-unrelated-process")
    try:
        _plant_pidfile(home, "stale-b", decoy.pid)
        check("live pid without token counts as alive (old behaviour)",
              read_pid("stale-b") is not None)
        check("token check rejects a recycled pid (not our daemon)",
              read_pid("stale-b", token="scripts/grad-select-trader") is None)
        check("acquire with token reclaims a recycled pidfile",
              acquire_pidfile("stale-b",
                              token="scripts/grad-select-trader") is True)
        release_pidfile("stale-b")
        _plant_pidfile(home, "stale-c", decoy.pid)
        check("acquire without token still refuses a live pid",
              acquire_pidfile("stale-c") is False)
    finally:
        decoy.kill()
        decoy.wait()
    # Empty pidfile (crashed mid-claim): reclaimed after the grace poll.
    home2 = _tmp_home()
    run = os.path.join(home2, "run")
    os.makedirs(run, exist_ok=True)
    open(os.path.join(run, "stale-d.pid"), "w").close()
    t0 = time.monotonic()
    ok = acquire_pidfile("stale-d") is True
    check("acquire reclaims an empty pidfile",
          ok, "took %.2fs" % (time.monotonic() - t0))
    release_pidfile("stale-d")


# ---------------------------------------------------------------------------
# run_forever: duplicate exits 1, normal path still works
# ---------------------------------------------------------------------------
def test_run_forever_duplicate_exits_1():
    home = _tmp_home()
    decoy = _start_decoy("python3 -m daemon.health_monitor")
    try:
        _plant_pidfile(home, "health_monitor", decoy.pid)
        # NOTE: compound statements (class) cannot follow ';' on one
        # line under `python -c` — build the driver as real lines.
        code = "\n".join([
            "import sys",
            "sys.path.insert(0, %r)" % REPO,
            "from daemon.common import Daemon",
            "class T(Daemon):",
            "    name = 'health_monitor'",
            "    def run_once(self):",
            "        self._stop = True",
            "rc = T().run_forever()",
            "print('RC', rc)",
            "sys.exit(rc)",
        ])
        r = _run_driver(code, home)
        check("run_forever duplicate exits 1",
              r.returncode == 1, "rc=%s out=%s" % (r.returncode, r.stderr[-200:]))
        check("run_forever duplicate logs already running",
              "already running" in r.stderr)
    finally:
        decoy.kill()
        decoy.wait()
    home2 = _tmp_home()
    code = "\n".join([
        "import sys",
        "sys.path.insert(0, %r)" % REPO,
        "from daemon.common import Daemon, read_pid",
        "class T(Daemon):",
        "    name = 'health_monitor'",
        "    def run_once(self):",
        "        self._stop = True",
        "rc = T().run_forever()",
        "print('RC', rc, 'PIDFILE', read_pid('health_monitor'))",
        "sys.exit(rc)",
    ])
    r = _run_driver(code, home2)
    check("run_forever normal path returns 0",
          r.returncode == 0, "rc=%s out=%s" % (r.returncode, r.stderr[-300:]))
    check("run_forever releases pidfile on clean exit",
          "PIDFILE None" in r.stdout)


# ---------------------------------------------------------------------------
# (c) wiring: every roster daemon's startup claims its pidfile
# ---------------------------------------------------------------------------
ROSTER_MODULES = ["market_monitor", "signal_monitor", "position_monitor",
                  "health_monitor", "telegram", "launch_scanner"]
ROSTER_SCRIPTS = [
    "degen-peak-tracker", "base-runner-feed", "eth-runner-feed",
    "locked-liq-scan", "hl-listing-watch", "mirror-exits",
    "ceo-trader", "grad-trader", "grad-raw-trader", "momentum-trader",
    "grad-select-trader", "tranche-trader", "tranche-protect-trader",
    "live10-demo-mirror", "raw-tight-trader", "devnet-demo-trader",
    "watcher-trader", "watcher-v2-trader", "moon-trader",
    "realtime-sync", "position-tracker", "challenge-trader",
]


def _main_first_statements(path, n=6):
    tree = ast.parse(open(path).read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node.body[:n]
    return []


def _calls_guard(stmts, name, token):
    for st in stmts:
        for node in ast.walk(st):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name) and f.id == "guard_singleton":
                    args = [a.value for a in node.args
                            if isinstance(a, ast.Constant)]
                    kw = {k.arg: k.value.value for k in node.keywords
                          if isinstance(k.value, ast.Constant)}
                    if args and args[0] == name and kw.get("token") == token:
                        return True
    return False


def test_wiring_static():
    common = open(os.path.join(REPO, "daemon", "common.py")).read()
    check("run_forever acquires with daemon token",
          'acquire_pidfile(self.name, token="daemon." + self.name)' in common)
    check("run_forever duplicate returns 1",
          "return 1\n        logger.warning(\"daemon %s: started" in common)
    for name in ROSTER_MODULES:
        init = os.path.join(REPO, "daemon", name, "__init__.py")
        src = open(init).read()
        check("module daemon %s sets name" % name,
              ('name = "%s"' % name) in src)
        check("module daemon %s main_entry uses common.main" % name,
              "return main(lambda:" in src)
    for name in ROSTER_SCRIPTS:
        path = os.path.join(REPO, "scripts", name)
        stmts = _main_first_statements(path)
        check("script %s guards first in main()" % name,
              _calls_guard(stmts, name, "scripts/" + name),
              "guard call not among first statements of main()")
    # Roster coverage: the supervisor roster matches exactly the set
    # covered above (no daemon left unguarded, none invented).
    import re as _re
    sup = open(os.path.join(REPO, "scripts", "supervisor")).read()
    roster_mods = _re.findall(r'_module\("([^"]+)"\)', sup)
    roster_scripts = _re.findall(r'_script\("([^"]+)"\)', sup)
    roster_scripts.append("devnet-demo-trader")  # Spec(...) form
    check("roster module set matches guarded set",
          sorted(roster_mods) == sorted(ROSTER_MODULES),
          "roster=%s" % sorted(roster_mods))
    check("roster script set matches guarded set",
          sorted(roster_scripts) == sorted(ROSTER_SCRIPTS),
          "roster=%s" % sorted(roster_scripts))


def test_wiring_behavioral():
    # Real scripts, run as subprocesses against a planted live owner:
    # they must exit 1 at the guard before doing any work.
    for name in ["grad-select-trader", "base-runner-feed",
                 "live10-demo-mirror"]:
        home = _tmp_home()
        decoy = _start_decoy("python3 scripts/" + name)
        try:
            _plant_pidfile(home, name, decoy.pid)
            env = dict(os.environ)
            env["FOREX_AGENT_HOME"] = home
            env["FOREX_AGENT_STORAGE"] = os.path.join(home, "test-store.db")
            r = subprocess.run(
                [VENV_PY, "scripts/" + name], cwd=REPO, env=env,
                timeout=25, capture_output=True, text=True)
            check("script %s duplicate exits 1" % name,
                  r.returncode == 1,
                  "rc=%s out=%s" % (r.returncode, r.stderr[-200:]))
            check("script %s duplicate logs already running" % name,
                  "already running" in r.stderr)
        finally:
            decoy.kill()
            decoy.wait()


if __name__ == "__main__":
    test_second_holder_refused()
    test_stale_pidfile_reclaimed()
    test_run_forever_duplicate_exits_1()
    test_wiring_static()
    test_wiring_behavioral()
    if FAILURES:
        print("FAILED: %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
        sys.exit(1)
    print("ALL PIDFILE ROLLOUT TESTS PASSED")


if __name__ != "__main__":
    from tests.recorded_checks import recorded_check_guard
