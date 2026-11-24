#!/usr/bin/env python3
"""Kill-switch path-layout and permission regression tests.

Regressions covered (d4e65aa incident, CI run 45137710348):

1. Independent repointing: callers and tests may repoint STATE_DIR,
   STATE_FILE, SHADOW_FILE and LOCK_FILE INDEPENDENTLY (e.g. repoint
   STATE_DIR/STATE_FILE but leave LOCK_FILE at its default). Every
   state operation must work under every such layout. Before the
   0b38871+ fix, _ensure_state_dir() only created STATE_DIR, so a
   LOCK_FILE in an uncreated directory made os.open(LOCK_FILE) raise
   ENOENT, and halt_lane/check_lane reported state_error.

2. Permission failures stay fail-closed: with an unwritable state
   location, is_halted() must be True, gate() must be False, and
   check_lane/halt_lane/reenable/status must report state_error
   without raising. The emergency stop must never silently open up
   because its storage is broken.

3. Bare import survives an unwritable home (the d4e65aa lazy-init
   contract): importing killswitch with FOREX_AGENT_HOME pointing
   into an unwritable directory must succeed, with the failure
   deferred to first state use.

Plain script — run directly with the venv python, not pytest.
Uses only temp dirs; the live killswitch state is untouched.
"""
import importlib
import json
import os
import stat
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMPD = tempfile.mkdtemp(prefix="ks-paths-")
os.environ["FOREX_AGENT_HOME"] = os.path.join(TMPD, "home")
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))

import killswitch

# Bind the production storage latch path (storage.store.DEFAULT_DB_PATH
# is resolved at storage.store import time) to a temp file BEFORE any
# is_halted call, so the human-halt checks below - and every earlier
# exercise's implicit latch read - never touch the repo's real
# storage/local.db.
sys.path.insert(0, REPO)
import importlib as _il
import storage.store as _store_mod
os.environ["FOREX_AGENT_STORAGE"] = os.path.join(
    TMPD, "storage", "local.db")
os.makedirs(os.path.dirname(os.environ["FOREX_AGENT_STORAGE"]))
_il.reload(_store_mod)

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def fresh_dir(tag):
    """A directory that does NOT exist yet."""
    return os.path.join(TMPD, tag, "sub")


def reload_with(state_dir=None, state_file=None, shadow_file=None,
                lock_file=None):
    importlib.reload(killswitch)
    if state_dir is not None:
        killswitch.STATE_DIR = state_dir
    if state_file is not None:
        killswitch.STATE_FILE = state_file
    if shadow_file is not None:
        killswitch.SHADOW_FILE = shadow_file
    if lock_file is not None:
        killswitch.LOCK_FILE = lock_file
    return killswitch


def exercise(ks, lane):
    """Run the full mutating surface against the current layout."""
    r = ks.halt_lane(lane, "regression test")
    if r["action"] != "HALTED":
        return "halt_lane: %s" % (r,)
    if not ks.is_halted(lane):
        return "is_halted after halt"
    if ks.gate(lane):
        return "gate after halt"
    ks.record_trade(lane, -0.01)
    r = ks.check_lane(lane)
    if r["action"] != "halted":
        return "check_lane: %s" % (r,)
    st = ks.status()
    if not st.get(lane, {}).get("halted"):
        return "status: %s" % (st.get(lane),)
    r = ks.reenable(lane, "regression test")
    if r["action"] != "reenabled":
        return "reenable: %s" % (r,)
    if ks.is_halted(lane):
        return "still halted after reenable"
    return None


# 1a. Default layout (module reads FOREX_AGENT_HOME at import).
ks = reload_with()
err = exercise(ks, "main")
check("default layout: full halt/record/check/reenable cycle", err is None)

# 1b. Repoint ONLY STATE_DIR (SHADOW/LOCK keep defaults under the
#     import-time STATE_DIR) — the exact test_grad.py pattern that
#     failed CI at d4e65aa.
ks = reload_with(state_dir=fresh_dir("b"),
                 state_file=os.path.join(fresh_dir("b"), "state.json"))
err = exercise(ks, "main")
check("STATE_DIR+STATE_FILE repointed, LOCK_FILE default", err is None)

# 1c. Every path constant in its OWN fresh nonexistent directory.
d1, d2, d3 = fresh_dir("c1"), fresh_dir("c2"), fresh_dir("c3")
ks = reload_with(state_dir=fresh_dir("c0"),
                 state_file=os.path.join(d1, "state.json"),
                 shadow_file=os.path.join(d2, "state.json.bak"),
                 lock_file=os.path.join(d3, "state.lock"))
err = exercise(ks, "main")
check("each path constant in its own fresh directory", err is None)
check("state file written at repointed STATE_FILE",
      os.path.isfile(os.path.join(d1, "state.json")))
check("shadow written at repointed SHADOW_FILE",
      os.path.isfile(os.path.join(d2, "state.json.bak")))
check("lock created at repointed LOCK_FILE",
      os.path.isfile(os.path.join(d3, "state.lock")))

# 1d. Only LOCK_FILE repointed to a fresh dir.
ld = fresh_dir("d")
ks = reload_with(lock_file=os.path.join(ld, "state.lock"))
err = exercise(ks, "main")
check("only LOCK_FILE repointed", err is None)

# 1e. Only SHADOW_FILE repointed to a fresh dir.
sd = fresh_dir("e")
ks = reload_with(shadow_file=os.path.join(sd, "state.json.bak"))
err = exercise(ks, "main")
check("only SHADOW_FILE repointed", err is None)

# 2. Permission failure: every path inside an unwritable directory.
# DISCLOSED TEST EDIT (CI run 146): the contained CI runner maps the user to root inside its user
# namespace, and root ignores chmod r-x, so the directory stayed writable and the fail-closed
# assertions saw a working store. When chmod cannot make a directory unwritable (euid 0), the
# unwritable location is a path BENEATH a regular file (ENOTDIR on every write). The assertions
# are unchanged; the production code is unchanged.
_ROOT = (os.geteuid() == 0)
if _ROOT:
    _blocker = os.path.join(TMPD, "readonly-blocker")
    open(_blocker, "w").close()
    ro = os.path.join(_blocker, "readonly")
else:
    ro = os.path.join(TMPD, "readonly")
    os.makedirs(ro)
ks = reload_with(state_dir=ro,
                 state_file=os.path.join(ro, "state.json"),
                 shadow_file=os.path.join(ro, "state.json.bak"),
                 lock_file=os.path.join(ro, "state.lock"))
if not _ROOT:
    os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)  # r-x: no writes
try:
    raised = False
    try:
        halted = ks.is_halted("main")
        gate = ks.gate("main")
        chk = ks.check_lane("main")
        hl = ks.halt_lane("main", "should fail")
        re = ks.reenable("main", "should fail")
        st = ks.status()
        ks.record_trade("main", -1.0)
    except Exception:
        raised = True
    check("unwritable state: nothing raises", not raised)
    check("unwritable state: is_halted True (fail closed)", halted)
    check("unwritable state: gate False (fail closed)", not gate)
    check("unwritable state: check_lane state_error",
          chk.get("action") == "state_error")
    check("unwritable state: halt_lane state_error",
          hl.get("action") == "state_error")
    check("unwritable state: reenable state_error",
          re.get("action") == "state_error")
    check("unwritable state: status state_error",
          st.get("state_error") is True)
finally:
    if not _ROOT:
        os.chmod(ro, stat.S_IRWXU)

# 3. Bare import survives an unwritable FOREX_AGENT_HOME.
if _ROOT:
    rohome = os.path.join(_blocker, "rohome")   # beneath a regular file: unwritable even for root
else:
    rohome = os.path.join(TMPD, "rohome")
    os.makedirs(rohome)
    os.chmod(rohome, stat.S_IRUSR | stat.S_IXUSR)
try:
    p = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); import killswitch; "
         "print('IMPORT_OK')"
         % os.path.join(REPO, "daemon", "launch_scanner")],
        env={"PATH": os.environ.get("PATH", ""),
             "FOREX_AGENT_HOME": os.path.join(rohome, "agent")},
        capture_output=True, text=True, timeout=60)
    check("bare import with unwritable home survives",
          "IMPORT_OK" in p.stdout)
finally:
    if not _ROOT:
        os.chmod(rohome, stat.S_IRWXU)

# 4. Global human emergency stop (Telegram /halt -> storage
#    kill_switch latch) halts every lane on top of per-lane state.
#    Regression for the 2026-11-23 validator findings: /halt engaged
#    the storage latch while grad.scan_once kept opening positions,
#    and the first fix read HOME/forex.db - a path no production
#    /halt writes. The latch path is resolved exactly as the
#    production writer: storage.store.DEFAULT_DB_PATH, i.e.
#    $FOREX_AGENT_STORAGE else <repo>/storage/local.db, bound at
#    storage.store import time.
PROD_DB = os.environ["FOREX_AGENT_STORAGE"]

hh = fresh_dir("hh")
ks = reload_with(state_dir=hh,
                 state_file=os.path.join(hh, "state.json"),
                 shadow_file=os.path.join(hh, "state.json.bak"),
                 lock_file=os.path.join(hh, "state.lock"))
store = _store_mod.Store()  # exactly what the production backend builds
check("test setup: store path is the production resolver path",
      os.path.realpath(str(store.path)) == os.path.realpath(PROD_DB))
store.set_kill_switch(True, "telegram")  # what /halt persists
check("human halt: is_halted True with clean lane state",
      ks.is_halted("main"))
check("human halt: gate False", not ks.gate("main"))
check("human halt: applies to every lane",
      ks.is_halted("grad") and ks.is_halted("watcher")
      and ks.is_halted("degen"))
# Exit capability preserved: recording a close still works while the
# latch is engaged (exits never consult is_halted).
ks.record_trade("main", -0.01)
check("human halt: exits/closes still recordable",
      os.path.isfile(os.path.join(hh, "state.json")))
# /resume releases the global latch; the lane is tradeable again
# (its own per-lane halt state untouched).
store.set_kill_switch(False, "regression-probe")
check("resume: lane tradeable again", not ks.is_halted("main"))
check("resume: gate open", ks.gate("main"))

# 5. Unreadable latch fails CLOSED (a stop command we cannot read
#    is treated as given): replace the DB file with a directory.
store_path = str(store.path)
del store
os.unlink(PROD_DB)
os.makedirs(PROD_DB)
try:
    check("unreadable latch: is_halted True", ks.is_halted("main"))
    check("unreadable latch: gate False", not ks.gate("main"))
finally:
    os.rmdir(PROD_DB)

# 6. Fresh environment (latch DB does not exist) reads as NOT
#    engaged, and the read must NOT create the production DB.
NEW_DB = os.path.join(TMPD, "storage", "fresh", "local.db")
os.makedirs(os.path.dirname(NEW_DB))
os.environ["FOREX_AGENT_STORAGE"] = NEW_DB
importlib.reload(_store_mod)
check("no latch DB: not halted", not ks.is_halted("main"))
check("no latch DB: read does not create the DB",
      not os.path.exists(NEW_DB))

print()
if fails:
    print("FAILURES: %d" % len(fails))
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("ALL KILLSWITCH-PATH TESTS PASSED")
