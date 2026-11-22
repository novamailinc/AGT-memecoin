#!/usr/bin/env python3
"""Store.journal_add retries 'database is locked' (2026-10-31).

Daemon state persists via journal_add; the hourly backup holds a shared
lock for tens of seconds, and 27 persist failures were logged on
2026-10-30 alone (fail-soft, but lossy + noisy). journal_add now
retries a bounded number of times on locked errors only.
No network. Uses temp DBs.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os
import sqlite3
import sys
import tempfile

REPO = _REPO_ROOT
sys.path.insert(0, REPO)

from storage.store import Store

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


def _store():
    d = tempfile.mkdtemp(prefix="store_retry_")
    return Store(path=os.path.join(d, "test.db"))


# 1. happy path still works
s = _store()
rid = s.journal_add({"kind": "test", "symbol": "x"})
check("retry: happy path returns row id", isinstance(rid, int) and rid >= 1)
rows = s.journal_query(kind="test")
check("retry: happy path row readable",
      len(rows) == 1 and rows[0].get("symbol") == "x")

# sqlite3.Connection attributes are read-only, so fault injection goes
# through a delegating wrapper swapped in after construction.
class FlakyConn:
    def __init__(self, real, fail_n, exc):
        self._real = real
        self._fail_n = fail_n
        self._exc = exc
        self.calls = 0

    def execute(self, sql, params=()):
        self.calls += 1
        if self.calls <= self._fail_n:
            raise self._exc
        return self._real.execute(sql, params)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    def close(self):
        return self._real.close()


# 2. transient locked error on first attempt -> succeeds on retry
s2 = _store()
w2 = FlakyConn(s2._conn, 1, sqlite3.OperationalError("database is locked"))
s2._conn = w2
rid2 = s2.journal_add({"kind": "test2", "symbol": "y"})
check("retry: transient lock succeeds", isinstance(rid2, int))
check("retry: transient lock took 2 attempts", w2.calls == 2)
# restore the real connection for the read-back (wrapper delegates
# reads fine, but keep the assertion on the real path)
s2._conn = w2._real
rows2 = s2.journal_query(kind="test2")
check("retry: transient lock row present exactly once", len(rows2) == 1)

# 3. persistent locked error -> raises after bounded attempts, no hang
s3 = _store()
w3 = FlakyConn(s3._conn, 999, sqlite3.OperationalError("database is locked"))
s3._conn = w3
try:
    s3.journal_add({"kind": "test3", "symbol": "z"})
    check("retry: persistent lock raises", False)
except sqlite3.OperationalError:
    check("retry: persistent lock raises", True)
check("retry: persistent lock bounded attempts", w3.calls == 3)

# 4. non-lock errors raise immediately (no retry)
s4 = _store()
w4 = FlakyConn(s4._conn, 999,
               sqlite3.OperationalError("no such table: journal_missing"))
s4._conn = w4
try:
    s4.journal_add({"kind": "test4", "symbol": "w"})
    check("retry: non-lock error raises", False)
except sqlite3.OperationalError:
    check("retry: non-lock error raises", True)
check("retry: non-lock error not retried", w4.calls == 1)

print()
if FAILURES:
    print("FAILURES:", FAILURES)
    sys.exit(1)
print("ALL STORE JOURNAL RETRY TESTS PASSED")
