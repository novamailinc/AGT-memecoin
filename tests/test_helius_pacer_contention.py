#!/usr/bin/env python3
"""Helius pacer contention tests (2026-11-22 root-cause fix).

Regression for the 15:03 UTC herd: after the quiet clear, 6 armed_watch
handshakes 429'd in one second on the discovery-key bucket while the
shared bucket held ~2 tokens. Cause: SharedPacer._transact gave up on
the flock after 5 x 10ms, and acquire() then fell back to a
per-process bucket — every contender granted ITSELF a full 2 tokens,
so the herd fired at once. The pacer serialized only when there was
no contention, which is exactly when serialization is not needed.

Covered here, against scratch temp homes only (FOREX_AGENT_HOME and
HOME are pointed at a temp dir before import, so the live quiet /
pacer / guard files are never read):
  * acquire() fails CLOSED (False) while another holder owns the
    pacer lock, instead of granting a token from a private bucket.
  * a 16-thread herd on one fresh bucket (capacity 2) is granted at
    most 2 tokens — no over-issue.
  * the longer lock wait lets a contender through once a brief
    holder releases the lock (serialization, not failure).
  * the first-run case (no shared state file yet) still falls back
    to the local bucket instead of blocking forever.

No network. No live files are edited. Plain script — run directly
with the venv python, not via pytest.
"""

import fcntl
import os
import subprocess
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

_TMP = tempfile.mkdtemp(prefix="pacer-contention-")
os.environ["FOREX_AGENT_HOME"] = _TMP
os.environ["HOME"] = _TMP

from daemon.launch_scanner import helius_ratelimit as hrl  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


BUCKET = "herd-test-bucket-00000001"


def fresh_pacer():
    home = tempfile.mkdtemp(dir=_TMP)
    return hrl.SharedPacer(home=home), home


# --- 1. Fail closed while the lock is held -------------------------
pacer, home = fresh_pacer()
# Seed the shared state file so the acquire path takes the
# contention branch (not the first-run local fallback).
pacer._write_state({"buckets": {BUCKET: hrl._pacer_default_bucket(
    time.time())}})
lock_path = os.path.join(home, "helius_pacer.lock")
holder = open(lock_path, "a+")
fcntl.flock(holder, fcntl.LOCK_EX)
try:
    got = pacer.acquire(BUCKET, wait_s=0)
    check("pacer: acquire fails closed while the lock is held",
          got is False)
finally:
    fcntl.flock(holder, fcntl.LOCK_UN)
    holder.close()
got = pacer.acquire(BUCKET, wait_s=0)
check("pacer: acquire succeeds once the lock is released",
      got is True)

# --- 2. Herd over-issue (MULTIPROCESS) -----------------------------
# Threads share one process-local bucket, so a threaded herd cannot
# reproduce the defect: the over-issue happens across PROCESSES,
# each with its own private fallback bucket. Spawn 8 separate
# interpreters that all acquire at once on one fresh bucket
# (capacity 2): the old code grants up to one token per process,
# the fixed code grants at most 2 in total.
pacer2, home2 = fresh_pacer()
worker_src = (
    "import os, sys, time\n"
    "BUCKET = 'herd-test-bucket-00000001'\n"
    "home, go, out, repo = sys.argv[1:5]\n"
    "sys.path.insert(0, repo)\n"
    "os.environ['FOREX_AGENT_HOME'] = home\n"
    "os.environ['HOME'] = home\n"
    "for _ in range(400):\n"
    "    if os.path.exists(go):\n"
    "        break\n"
    "    time.sleep(0.05)\n"
    "from daemon.launch_scanner import helius_ratelimit as hrl\n"
    "p = hrl.SharedPacer(home=home)\n"
    "got = p.acquire(BUCKET, wait_s=0)\n"
    "open(out, 'w').write('1' if got else '0')\n"
)
worker_path = os.path.join(home2, "herd_worker.py")
open(worker_path, "w").write(worker_src)
go_path = os.path.join(home2, "go")
procs = []
outs = []
for i in range(8):
    out_path = os.path.join(home2, "result-%d" % i)
    outs.append(out_path)
    procs.append(subprocess.Popen(
        [sys.executable, worker_path, home2, go_path, out_path, REPO],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
time.sleep(1.0)  # let every worker reach the barrier
open(go_path, "w").write("go")
for pr in procs:
    pr.wait(timeout=30)
granted = 0
for out_path in outs:
    try:
        granted += int(open(out_path).read().strip() or "0")
    except Exception:
        pass
check("pacer: 8-process herd is granted at most capacity (2) tokens",
      granted <= 2)
check("pacer: herd grants at least one token (liveness)",
      granted >= 1)

# --- 3. Brief holder serializes, does not fail the contender ------
pacer3, home3 = fresh_pacer()
pacer3._write_state({"buckets": {BUCKET: hrl._pacer_default_bucket(
    time.time())}})
lock_path3 = os.path.join(home3, "helius_pacer.lock")
released = threading.Event()


def _brief_holder():
    fh = open(lock_path3, "a+")
    fcntl.flock(fh, fcntl.LOCK_EX)
    time.sleep(0.3)
    fcntl.flock(fh, fcntl.LOCK_UN)
    fh.close()
    released.set()


holder_t = threading.Thread(target=_brief_holder)
holder_t.start()
time.sleep(0.05)  # let the holder take the lock first
got3 = pacer3.acquire(BUCKET, wait_s=5)
holder_t.join()
check("pacer: contender waits out a brief holder and is granted",
      got3 is True and released.is_set())

# --- 4. First run (no shared file) still uses the local bucket -----
pacer4, home4 = fresh_pacer()
# No _write_state call: the state file does not exist yet.
got4 = pacer4.acquire(BUCKET, wait_s=0)
check("pacer: first run without a shared file still grants a token",
      got4 is True)

print("FAILURES: %d" % len(fails))
for name in fails:
    print("  FAIL " + name)
sys.exit(1 if fails else 0)
