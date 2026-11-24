#!/usr/bin/env python3
"""Phase 2 acquisition tests — request_scheduler.py.

Plain script, no network. Covers (§11): shared quota accounting
(N processes, one provider budget — no joint exhaustion), priority
scheduling (entry/exit-critical served before background research
under contention), background auto-shed, 429 backoff (doubling,
single-probe recovery), monthly-quota vs throughput 429, CU/RU
accounting.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os
import sys
import tempfile
import time

REPO = _REPO_ROOT
sys.path.insert(0, REPO)

from daemon.intelligence.acquisition import request_scheduler as S

fails = []
total = [0]


def check(name, cond):
    total[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


HOME = tempfile.mkdtemp(prefix="acq-sched-")
clock = Clock()

# ---------------- shared quota: two instances, one budget --------
q1 = S.SharedQuota("alchemy", home=HOME, now=clock)
q2 = S.SharedQuota("alchemy", home=HOME, now=clock)
granted = 0
for q in (q1, q2, q1, q2, q1, q2, q1, q2, q1, q2, q1):
    g, _, _ = q.acquire(S.PRIORITY_CRITICAL)
    granted += int(g)
check("shared bucket: burst 10 across two instances, 11th refused",
      granted == 10)
g, wait, reason = q1.acquire(S.PRIORITY_CRITICAL)
check("11th token refused with wait reason", not g and reason == "wait")
check("wait is refill time for 1 token (0.2s at 5/s)",
      abs(wait - 0.2) < 1e-9)

# refill: 1s -> 5 tokens back
clock.advance(1.0)
g, _, _ = q1.acquire(S.PRIORITY_CRITICAL)
check("refill restores tokens after 1s", g)

# ---------------- priority: background sheds, critical waits ------
HOME2 = tempfile.mkdtemp(prefix="acq-sched2-")
clock2 = Clock()
qa = S.SharedQuota("chainstack", home=HOME2, now=clock2)
for _ in range(5):  # drain burst 5
    qa.acquire(S.PRIORITY_CRITICAL)
g, _, reason = qa.acquire(S.PRIORITY_BACKGROUND)
check("background sheds when bucket empty (never queues)",
      not g and reason == "shed")
g, wait, reason = qa.acquire(S.PRIORITY_CRITICAL)
check("critical reports a wait instead of shedding",
      not g and reason == "wait" and wait > 0)
st = qa.status()
check("shed counted in state", st.get("shed", 0) == 1)
# background acquire_wait never sleeps
slept = []
g, waited, reason = qa.acquire_wait(S.PRIORITY_BACKGROUND,
                                    sleep=lambda s: slept.append(s))
check("background acquire_wait returns at once without sleeping",
      not g and waited == 0.0 and slept == [])
# critical acquire_wait waits up to its max and then fails fast
g, waited, reason = qa.acquire_wait(
    S.PRIORITY_CRITICAL, max_wait=0.6, sleep=lambda s: clock2.advance(s))
check("critical acquire_wait honours max_wait and gets a token",
      g and waited <= 0.6)

# ---------------- 429 backoff --------------------------------------
HOME3 = tempfile.mkdtemp(prefix="acq-sched3-")
clock3 = Clock()
qb = S.SharedQuota("alchemy", home=HOME3, now=clock3)
r = qb.report_429("throughput")
check("throughput 429 engages backoff", r == "backoff")
g, wait, reason = qb.acquire(S.PRIORITY_CRITICAL)
check("bucket in backoff refuses with backoff reason",
      not g and reason == "backoff" and wait > 0)
g, _, reason = qb.acquire(S.PRIORITY_BACKGROUND)
check("background sheds during backoff", not g and reason == "shed_backoff")
# second 429 while in backoff doubles it
clock3.advance(5.0)
qb.report_429("throughput")
st = qb.status()
check("backoff doubles on repeat 429 (30 -> 60)",
      abs(st.get("backoff_s", 0) - 60.0) < 1e-9)
# success clears backoff fleet-wide at once
qb.report_success()
st = qb.status()
check("success clears backoff", st.get("backoff_remaining_s", 1) == 0.0)
g, _, _ = qb.acquire(S.PRIORITY_CRITICAL)
check("token granted after backoff cleared", g)

# monthly-quota 429: provider down, never retried
qb.report_429("monthly_quota")
g, _, reason = qb.acquire(S.PRIORITY_CRITICAL)
check("monthly-quota 429 marks provider down (quota_down)",
      not g and reason == "quota_down")
st = qb.status()
check("monthly_quota_down flag set", st.get("monthly_quota_down") is True)
qb.reset_quota_flag()
g, _, _ = qb.acquire(S.PRIORITY_CRITICAL)
check("operator reset clears quota-down flag", g)

# wait hint honoured: hint longer than base wins
qb.report_429("throughput", wait_hint_s=120.0)
st = qb.status()
check("wait hint (120s) honoured over 30s base",
      abs(st.get("backoff_s", 0) - 120.0) < 1e-9)
qb.report_success()

# ---------------- CU / RU accounting -------------------------------
qb.record_cost(cu=40)
qb.record_cost(cu=40, ru=0)
st = qb.status()
check("CU accounting sums (80 CU)", st.get("cu_spent") == 80)
qc = S.SharedQuota("chainstack", home=HOME3, now=clock3)
qc.record_cost(ru=2)
check("RU accounting sums", qc.status().get("ru_spent") == 2)

# ---------------- public rate --------------------------------------
qp = S.SharedQuota("public", home=HOME3, now=clock3)
g1, _, _ = qp.acquire(S.PRIORITY_CRITICAL)
g2, _, reason2 = qp.acquire(S.PRIORITY_CRITICAL)
check("public: 1 call/2s fleet-wide (burst 1)",
      g1 and not g2 and reason2 == "wait")

# helius has no scheduler bucket by design
try:
    S.SharedQuota("helius", home=HOME3)
    check("helius raises (paced by helius_ratelimit, never here)", False)
except ValueError:
    check("helius raises (paced by helius_ratelimit, never here)", True)

# cross-process: separate process honours the same budget file
import subprocess
code = (
    "import sys; sys.path.insert(0, %r);"
    "from daemon.intelligence.acquisition.request_scheduler import SharedQuota;"
    "q = SharedQuota('public', home=%r);"
    "print(q.acquire('critical')[0])" % (REPO, HOME3))
# public bucket was drained above and clock is frozen in-process, but
# the subprocess uses real time — state file's `updated` is frozen
# at clock3 time (old), so refill gives it tokens; just check the
# subprocess runs and shares the file (calls counter increments).
before = S.SharedQuota("public", home=HOME3, now=clock3).status()["calls"]
proc = subprocess.run([sys.executable, "-c", code],
                      capture_output=True, text=True)
after = S.SharedQuota("public", home=HOME3, now=clock3).status()["calls"]
check("second process shares the same quota file",
      proc.returncode == 0 and proc.stdout.strip() == "True"
      and after == before + 1)

print("\n%d checks, %d failed" % (total[0], len(fails)))
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("ALL PASS")
