#!/usr/bin/env python3
"""Regression for close_intents.begin() id uniqueness (2026-11-24).

The intent id base has one-second resolution (lane:mint:pos_id:int(close_ts)). A freeze followed by a NEW begin for the same position in the
same second used to reuse the id, so the new intent read as the old terminal (frozen) one and was never recovered. This test FORCES the same
second with an explicit close_ts (no sleeping, no clock dependence) against the REAL close_intents journal in a temp home. Engineering test of
journal logic only: no account, no network, no engine.
Run: python3 -B tests/test_close_intents_unique_id.py
"""
import os, sys, tempfile
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="ci-uniq-")
import daemon.launch_scanner.close_intents as ci

FAILS = 0
def check(name, cond):
    global FAILS
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS += 1

OPS = [{"kind": "credit", "op_id": "op-1", "amount": 1.0}]
INFO = {"close_ts": 1795593430.2, "realized_usd": 1.0, "entry_notional_usd": 1.0, "closed_reason": "t"}
a = ci.begin("grad", "MINT1", "grad/MINT1#1", OPS, dict(INFO))
ci.freeze("grad", a, "superseded by a changed settle plan")
b = ci.begin("grad", "MINT1", "grad/MINT1#1", OPS, dict(INFO, close_ts=1795593430.9))   # same integer second
c = ci.begin("grad", "MINT1", "grad/MINT1#1", OPS, dict(INFO))                          # same second again
check("same-second begin after a freeze gets a DIFFERENT id (%s vs %s)" % (a, b), a != b)
check("a third same-second begin is distinct from both (%s)" % c, len({a, b, c}) == 3)
pend = [x["intent_id"] for x in ci.pending("grad")]
check("the new intents are OPEN and the frozen one stays terminal (pending=%r)" % pend, a not in pend and b in pend and c in pend)
d = ci.begin("grad", "MINT2", "grad/MINT2#1", OPS, dict(INFO))
check("id keeps the readable base (lane:mint:pos:second) plus a suffix (%s)" % d, d.startswith("grad:MINT2:grad/MINT2#1:1795593430:") and len(d.rsplit(":", 1)[-1]) == 24)

# unreadable journal evidence must NOT restore the collision: replace the journal with a directory (read and append both fail)
import shutil
jp = ci.journal_path("lanex"); os.makedirs(jp)
u1 = ci.begin("lanex", "M", "p", OPS, dict(INFO)); u2 = ci.begin("lanex", "M", "p", OPS, dict(INFO))
check("unreadable journal: ids still distinct (%s vs %s)" % (u1[-8:], u2[-8:]), u1 != u2)
shutil.rmtree(jp)

# concurrency: 8 threads x 25 begins on the SAME position in the same second -> all ids distinct, every intent event recorded once
import threading
ids = []; lk = threading.Lock()
def work():
    for _ in range(25):
        x = ci.begin("lanec", "MC", "pc", OPS, dict(INFO))
        with lk:
            ids.append(x)
ts = [threading.Thread(target=work) for _ in range(8)]
[t_.start() for t_ in ts]; [t_.join() for t_ in ts]
ev = [e for e in ci._read(ci.journal_path("lanec"))[0] if e.get("ev") == "intent"]
check("8 threads x 25 same-second begins: %d ids, %d distinct, %d intent events, %d distinct in journal" % (len(ids), len(set(ids)), len(ev), len({e["intent_id"] for e in ev})),
      len(ids) == 200 and len(set(ids)) == 200 and len(ev) == 200 and len({e["intent_id"] for e in ev}) == 200)

# concurrency across PROCESSES (real begin callers can be separate processes): 4 children x 25 begins
import subprocess
code = ("import os,sys;sys.path.insert(0,%r);import daemon.launch_scanner.close_intents as ci;"
        "[ci.begin('lanep','MP','pp',[{'kind':'credit','op_id':'o','amount':1.0}],{'close_ts':1795593430.5}) for _ in range(25)]") % REPO
procs = [subprocess.Popen([sys.executable, "-B", "-c", code], env=dict(os.environ)) for _ in range(4)]
rcs = [p_.wait() for p_ in procs]
ev = [e for e in ci._read(ci.journal_path("lanep"))[0] if e.get("ev") == "intent"]
check("4 processes x 25 same-second begins: rcs %r, %d intent events, %d distinct ids, journal not torn" % (rcs, len(ev), len({e["intent_id"] for e in ev})),
      rcs == [0, 0, 0, 0] and len(ev) == 100 and len({e["intent_id"] for e in ev}) == 100 and ci._read(ci.journal_path("lanep"))[1] is None)
print("fails: %d" % FAILS)
sys.exit(1 if FAILS else 0)
