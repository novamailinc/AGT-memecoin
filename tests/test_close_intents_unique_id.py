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
check("other mint, same second: base id unchanged and unsuffixed (%s)" % d, "#" not in d.rsplit(":", 1)[-1] and d.endswith(":1795593430"))
print("fails: %d" % FAILS)
sys.exit(1 if FAILS else 0)
