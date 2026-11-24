#!/usr/bin/env python3
"""Plain-script tests for the devnet-demo-trader singleton lock.

Run with the venv python from the repo root:
    ../.venv/bin/python tests/test_devnet_demo_lock.py
No network, no production state: the lock is exercised in a temp dir
only, never against the live trader's lock file.
"""

import importlib.util
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))

from importlib.machinery import SourceFileLoader
_loader = SourceFileLoader(
    "devnet_demo_trader_lock",
    os.path.join(_HERE, "..", "scripts", "devnet-demo-trader"))
_spec = importlib.util.spec_from_loader("devnet_demo_trader_lock", _loader)
mod = importlib.util.module_from_spec(_spec)
_loader.exec_module(mod)

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


_tmp = tempfile.mkdtemp(prefix="demo_lock_")
_lock = os.path.join(_tmp, "devnet-demo-trader.lock")

fd1 = mod.acquire_singleton_lock(_lock)
check("lock: first acquire holds", fd1 is not None)
check("lock: exit code is distinct (42)",
      mod.SINGLETON_LOCK_EXIT_CODE == 42)

fd2 = mod.acquire_singleton_lock(_lock)
check("lock: second acquire refused while held", fd2 is None)

os.close(fd1)
fd3 = mod.acquire_singleton_lock(_lock)
check("lock: re-acquire succeeds after holder closes", fd3 is not None)
if fd3 is not None:
    os.close(fd3)

check("lock: file mode is 0600",
      (os.stat(_lock).st_mode & 0o777) == 0o600)

print()
if FAILURES:
    print("FAILURES:", FAILURES)
    sys.exit(1)
print("ALL DEVNET DEMO LOCK TESTS PASS")
