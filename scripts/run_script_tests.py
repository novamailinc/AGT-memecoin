#!/usr/bin/env python3
"""Run the script-style tests (plain __main__ scripts, not pytest modules).

About half of tests/ are plain scripts: module-level checks that print
PASS/FAIL lines and end in sys.exit(1 if fails else 0). pytest cannot
collect them (importing the module runs the whole script and the
module-level sys.exit kills the pytest session), so tests/conftest.py
excludes them from pytest collection and this runner executes each one
as a subprocess instead. Every script still runs; a nonzero exit fails
this runner, so nothing is masked.

Usage: python3 scripts/run_script_tests.py
"""

import ast
import glob
import os
import subprocess
import shutil
import tempfile
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PER_TEST_TIMEOUT_S = 600


from test_discovery import is_script_test


def main():
    if os.environ.get("AGT_CONTAINED") != "1":
        print("REFUSING: run via scripts/contained.sh (networkless namespace); coverage UNQUALIFIED")
        return 2
    selected = sys.argv[1:]
    scripts = sorted(p for p in glob.glob(os.path.join(REPO, "tests", "test_*.py"))
                     if is_script_test(p)
                     and (not selected or os.path.basename(p) in selected
                          or os.path.relpath(p, REPO) in selected
                          or p in selected))
    if not scripts:
        print("no script-style tests found")
        return 0
    base_env = dict(os.environ)
    base_env["PYTHONPATH"] = REPO + os.pathsep + base_env.get("PYTHONPATH", "")
    failures = []
    for path in scripts:
        rel = os.path.relpath(path, REPO)
        print("=== %s ===" % rel, flush=True)
        # Repo modules default FOREX_AGENT_HOME to the original dev VM's
        # home and create it at import time, so every script gets an
        # isolated scratch home (also keeps tests off any real state).
        env = dict(base_env)
        scratch_home = tempfile.mkdtemp(prefix="agt-test-home-")
        env["FOREX_AGENT_HOME"] = scratch_home
        try:
            proc = subprocess.run([sys.executable, path], cwd=REPO, env=env,
                                  capture_output=True, text=True,
                                  timeout=PER_TEST_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            print("TIMEOUT after %ds" % PER_TEST_TIMEOUT_S)
            failures.append(rel + " (timeout)")
            shutil.rmtree(scratch_home, ignore_errors=True)
            continue
        finally:
            shutil.rmtree(scratch_home, ignore_errors=True)
        out = proc.stdout + proc.stderr
        # Show the FAIL lines and the tail unconditionally so CI logs
        # carry the diagnosis; keep passing scripts to a one-line summary.
        fail_lines = [l for l in out.splitlines() if l.startswith("FAIL")]
        if proc.returncode == 0:
            tail = out.strip().splitlines()[-1] if out.strip() else ""
            print("ok (%s)" % tail)
        else:
            print(out if len(out) < 4000 else "\n".join(
                fail_lines + ["...", *out.strip().splitlines()[-20:]]))
            failures.append(rel)
    print("\n%d/%d script tests passed" % (len(scripts) - len(failures),
                                           len(scripts)))
    if failures:
        print("failing:")
        for f in failures:
            print("  " + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
