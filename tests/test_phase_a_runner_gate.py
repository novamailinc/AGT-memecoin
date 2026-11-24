#!/usr/bin/env python3
"""Phase A regression (P2-5 runner gate, 2026-11-22): the test
runner must fail on any suite that executes zero checks — the
test_lifecycle.py hole (plain run exited 0 having run nothing).
Drives scripts/run-test-suites against fixture suites in a temp
dir: a silent 0-check suite must fail the run, a 1-check suite
must pass it, and a pytest-style suite (def test_, no top-level
checks) must be picked up by the pytest fallback and pass.
"""
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(REPO, "scripts", "run-test-suites")

_PASSED = 0


def check(name, cond):
    global _PASSED
    if not cond:
        raise AssertionError("FAIL: %s" % name)
    _PASSED += 1
    print("PASS: %s" % name)


_ZERO_SUITE = '''#!/usr/bin/env python3
"""Fixture: a suite that defines checks but runs none of them."""
import sys


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise AssertionError("FAIL: " + name)


# The bug: exits without calling any check().
sys.exit(0)
'''

_OK_SUITE = '''#!/usr/bin/env python3
"""Fixture: one plain check."""


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise AssertionError("FAIL: " + name)


check("fixture check ran", True)
print("ALL 1 CHECKS PASSED")
'''

_PYTEST_SUITE = '''#!/usr/bin/env python3
"""Fixture: pytest-style, no top-level checks."""


def test_fixture_passes():
    assert 1 + 1 == 2
'''

_HELPER_SUITE = '''#!/usr/bin/env python3
"""Fixture: a helper module named test_*.py (no test content)."""


class FakeThing:
    pass
'''

_RETIRED_SUITE = '''#!/usr/bin/env python3
"""Fixture: a retired suite (announces RETIRED, exits 0)."""
import sys


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise AssertionError("FAIL: " + name)


print("RETIRED 2026-10-23: fixture lane killed - suite retired.")
sys.exit(0)
'''


def _run_runner(tmpdir):
    proc = subprocess.run(
        [sys.executable, RUNNER, "--dir", tmpdir, "--timeout", "60"],
        capture_output=True, text=True, timeout=180, cwd=REPO)
    return proc.returncode, proc.stdout + proc.stderr


def _write(tmpdir, name, content):
    path = os.path.join(tmpdir, name)
    with open(path, "w") as fh:
        fh.write(content)
    return path


def main():
    # 1. A silent 0-check suite fails the run and is named.
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, "test_zero.py", _ZERO_SUITE)
        rc, out = _run_runner(tmp)
        check("runner fails on a 0-check suite", rc != 0)
        check("runner names the 0-check suite",
              "test_zero.py" in out and "PASS=0" in out)

    # 2. A 1-check suite passes the run (and is really run, not skipped).
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, "test_ok.py", _OK_SUITE)
        rc, out = _run_runner(tmp)
        check("runner passes a 1-check suite",
              rc == 0 and "PASS=1" in out and "SKIP" not in out)

    # 3. A pytest-style suite is picked up by the fallback.
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, "test_pytest_style.py", _PYTEST_SUITE)
        rc, out = _run_runner(tmp)
        check("runner passes a pytest-style suite via fallback",
              rc == 0 and "[pytest]" in out and "PASS=1" in out)

    # 4. A helper module (no test content) is skipped, not failed.
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, "test_helper.py", _HELPER_SUITE)
        rc, out = _run_runner(tmp)
        check("runner skips a helper module instead of failing it",
              rc == 0 and "SKIP test_helper.py" in out)

    # 5. A retired suite (announces RETIRED, exits 0) is skipped.
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, "test_retired.py", _RETIRED_SUITE)
        rc, out = _run_runner(tmp)
        check("runner skips a retired suite instead of failing it",
              rc == 0 and "SKIP test_retired.py" in out)

    print("ALL %d CHECKS PASSED" % _PASSED)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
