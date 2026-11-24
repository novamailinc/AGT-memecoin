#!/usr/bin/env python3
"""Independent process-death tests at the durable boundaries
(2026-11-22, quarantine design v4).

Not a module reload: each scenario runs the mutation in a CHILD
process that is hard-killed (os._exit, no cleanup, no atexit) at a
chosen point inside the write path, then a FRESH process loads the
account and must reach exactly one consistent state via the WAL:

  kill_after_journal   the intent is journaled, the state never
                       saved -> replay applies the mutation exactly
                       once
  kill_after_live      the live ledger is written, the shadow never
                       is -> the live file + journal agree; the
                       stale shadow must not resurrect anything
  kill_mid_live_write  the live file is torn mid-write -> the
                       shadow (one mutation behind) + journal replay
                       converge to the applied state

Run: python3 tests/test_paper_account_crash.py
"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


CHILD = r'''
import importlib, os, sys, time
mode = sys.argv[1]
home = sys.argv[2]
os.environ["FOREX_AGENT_HOME"] = home
os.environ["PAPER_ACCOUNT_START"] = "10"
sys.path.insert(0, os.environ["AGT_REPO_ROOT"])
import daemon.launch_scanner.paper_account as pa

if mode == "kill_after_journal":
    def die(led):
        os._exit(1)   # journaled, never saved
    pa._save_locked = die
    pa.try_debit("watcher", "M", 2.0, qty=100.0,
                 op_id="watcher:buy:M:1")
elif mode == "kill_after_live":
    orig = pa._atomic_write
    def die_after_live(path, data):
        if path.endswith(".bak"):
            os._exit(1)   # live written, shadow stale
        orig(path, data)
    pa._atomic_write = die_after_live
    pa.try_debit("watcher", "M", 2.0, qty=100.0,
                 op_id="watcher:buy:M:1")
elif mode == "kill_mid_live_write":
    orig = pa._atomic_write
    def die_mid(path, data):
        if not path.endswith(".bak"):
            with open(path, "w") as f:
                f.write(json.dumps(data)[:50])   # torn
            os._exit(1)
        orig(path, data)
    import json as _json
    globals()["json"] = _json
    pa._atomic_write = die_mid
    pa.try_debit("watcher", "M", 2.0, qty=100.0,
                 op_id="watcher:buy:M:1")
'''

VERIFY = r'''
import os, sys
home, expect_cash = sys.argv[1], float(sys.argv[2])
os.environ["FOREX_AGENT_HOME"] = home
os.environ["PAPER_ACCOUNT_START"] = "10"
sys.path.insert(0, os.environ["AGT_REPO_ROOT"])
import daemon.launch_scanner.paper_account as pa
snap = pa.snapshot()
pos = snap["lanes"].get("watcher", {}).get("positions", [])
print("SNAP cash=%.6f spendable=%.6f open=%d degraded=%s"
      % (snap["cash"], snap["cash_spendable"], len(pos),
         snap.get("degraded")))
'''


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(code, *args):
    env = dict(os.environ, AGT_REPO_ROOT=REPO)
    return subprocess.run([sys.executable, "-c", code] + list(args),
                          capture_output=True, text=True, timeout=60,
                          env=env)


def seed(home):
    """A settled entry+exit in one healthy process."""
    r = run('''
import os, sys
os.environ["FOREX_AGENT_HOME"] = sys.argv[1]
os.environ["PAPER_ACCOUNT_START"] = "10"
sys.path.insert(0, os.environ["AGT_REPO_ROOT"])
import daemon.launch_scanner.paper_account as pa
pa.try_debit("watcher", "S", 1.0, qty=50.0, op_id="watcher:buy:S:1")
pa.credit("watcher", "S", 1.5, "watcher:sell:S:1", qty=50.0,
          price_kind="current_price_simulated_fill")
''', home)
    return r.returncode == 0


def main():
    for mode in ("kill_after_journal", "kill_after_live",
                 "kill_mid_live_write"):
        home = tempfile.mkdtemp()
        check("%s: seed ok" % mode, seed(home))
        child = run(CHILD, mode, home)
        check("%s: child died at the boundary" % mode,
              child.returncode != 0)
        ver = run(VERIFY, home, "8.0")
        line = [l for l in ver.stdout.splitlines()
                if l.startswith("SNAP ")]
        check("%s: verifier loaded" % mode, bool(line))
        if line:
            # seed: 10 - 1 + 1.5 = 10.5; debit M: -2.0 -> 8.5
            ok = ("cash=8.500000" in line[0]
                  and "spendable=8.500000" in line[0]
                  and "open=1" in line[0]
                  and "degraded=None" in line[0])
            check("%s: recovered exactly once, consistent, not "
                  "degraded (%s)" % (mode, line[0]), ok)

    print()
    if FAILS:
        print("FAILURES:", FAILS)
        sys.exit(1)
    print("ALL OK")


main()
