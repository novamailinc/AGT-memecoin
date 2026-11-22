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
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PER_TEST_TIMEOUT_S = 600


def _collectable(node):
    """pytest can only run a test whose params are all optional."""
    args = node.args
    params = args.posonlyargs + args.args + args.kwonlyargs
    n_defaults = len(args.defaults) + len(args.kw_defaults or [])
    if params and params[0].arg == "self":
        params = params[1:]
    return len(params) <= n_defaults


def is_script_test(path):
    """A test file is script-style when pytest can collect nothing in it:
    no test functions or Test classes, or test functions that take
    required arguments (a __main__ driver passes those by hand)."""
    try:
        tree = ast.parse(open(path).read())
    except (SyntaxError, OSError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name.startswith("test_") and _collectable(node):
            return False
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            return False
    return True


def main():
    selected = sys.argv[1:]
    scripts = sorted(p for p in glob.glob(os.path.join(REPO, "tests", "test_*.py"))
                     if is_script_test(p)
                     and (not selected or os.path.basename(p) in selected
                          or os.path.relpath(p, REPO) in selected
                          or p in selected))
    if not scripts:
        print("no script-style tests found")
        return 0
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO + os.pathsep + env.get("PYTHONPATH", "")
    failures = []
    for path in scripts:
        rel = os.path.relpath(path, REPO)
        print("=== %s ===" % rel, flush=True)
        try:
            proc = subprocess.run([sys.executable, path], cwd=REPO, env=env,
                                  capture_output=True, text=True,
                                  timeout=PER_TEST_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            print("TIMEOUT after %ds" % PER_TEST_TIMEOUT_S)
            failures.append(rel + " (timeout)")
            continue
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
