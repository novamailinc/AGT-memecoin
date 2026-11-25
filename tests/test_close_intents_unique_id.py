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

def _raises(fn):
    try:
        fn()
    except ci.IntentNotDurable:
        return True
    return False

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

# FAIL CLOSED: an unwritable journal (path replaced by a directory: read and append both fail) must make begin() RAISE IntentNotDurable and freeze() return
# False, never hand back an id for an intent that is not on disk. The expected-fault logging (logger.exception inside _append) is silenced here so the
# native log carries no traceback of an INJECTED fault (the strict parser rejects any Traceback).
import shutil, logging
jp = ci.journal_path("lanex"); os.makedirs(jp)
class _Cap(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self); self.recs = []
    def emit(self, r):
        self.recs.append(r)
_cap = _Cap(); _lg = logging.getLogger(ci.logger.name); _prop = _lg.propagate; _lg.addHandler(_cap); _lg.propagate = False
try:
    try:
        ci.begin("lanex", "M", "p", OPS, dict(INFO)); _raised = False
    except ci.IntentNotDurable:
        _raised = True
    _fz = ci.freeze("lanex", "grad:M:p:1:abc", "x")
finally:
    _lg.removeHandler(_cap); _lg.propagate = _prop
_exp = [r for r in _cap.recs if r.getMessage().startswith("close_intents: append failed") and r.exc_info and r.exc_info[0] is IsADirectoryError]
check("the injected fault was logged EXACTLY as expected: %d records, all 'append failed' with IsADirectoryError, none other (%d total)" % (len(_exp), len(_cap.recs)), len(_exp) == 2 and len(_cap.recs) == 2)
check("unwritable journal: begin raises IntentNotDurable (no id for a non-durable intent) and freeze returns False (%r, %r)" % (_raised, _fz), _raised is True and _fz is False)
shutil.rmtree(jp)
check("healthy journal again: freeze returns True and begin works", ci.freeze("lanex", "grad:M:p:1:abc", "y") is True and bool(ci.begin("lanex", "M", "p", OPS, dict(INFO))))

# stable_id=True is idempotent per OPERATION KEY while every intent id stays UNIQUE: a retry finds the existing intent by the key and returns ITS id
st1 = ci.begin("lanes", "MS", "ps", OPS, dict(INFO), stable_id=True); st2 = ci.begin("lanes", "MS", "ps", OPS, dict(INFO), stable_id=True)
_ev = [e for e in ci._read(ci.journal_path("lanes"))[0] if e.get("ev") == "intent"]
check("stable_id=True: the retry returns the SAME unique id (%s) with ONE intent event (%d) carrying op_key lanes:MS:ps:1795593430" % (st1, len(_ev)),
      st1 == st2 and len(_ev) == 1 and _ev[0].get("op_key") == "lanes:MS:ps:1795593430" and st1 != "lanes:MS:ps:1795593430" and st1.startswith("lanes:MS:ps:1795593430:"))
check("intent_status: open before the freeze", ci.intent_status("lanes", st1) == "open")
check("a stable begin whose recorded PLAN differs is refused (raises, nothing appended, the old intent untouched)",
      (lambda: [ci.begin("lanes", "MS", "ps", [{"kind": "credit", "op_id": "op-OTHER", "amount": 9.0}], dict(INFO), stable_id=True)])
      and _raises(lambda: ci.begin("lanes", "MS", "ps", [{"kind": "credit", "op_id": "op-OTHER", "amount": 9.0}], dict(INFO), stable_id=True))
      and len([e for e in ci._read(ci.journal_path("lanes"))[0] if e.get("ev") == "intent"]) == 1)
check("freeze returns True, status frozen", ci.freeze("lanes", st1, "terminal") is True and ci.intent_status("lanes", st1) == "frozen")
st3 = ci.begin("lanes", "MS", "ps", OPS, dict(INFO), stable_id=True)
check("stable begin after a FREEZE returns the existing frozen handle and opens NO new intent (still one event, still terminal, not pending)",
      st3 == st1 and len([e for e in ci._read(ci.journal_path("lanes"))[0] if e.get("ev") == "intent"]) == 1 and st1 not in [x["intent_id"] for x in ci.pending("lanes")])
ci.mark_account_applied("lanes", st1); ci.mark_lane_applied("lanes", st1)
check("intent_status: applied after the terminal events; intent_status of an absent id is None", ci.intent_status("lanes", st1) == "applied" and ci.intent_status("lanes", "nope") is None)
# serialization: 8 threads begin the SAME operation concurrently -> ONE intent, one distinct id
import threading, subprocess
_ids = []; _l = threading.Lock()
def _w():
    i = ci.begin("lanec", "MC", "pc", OPS, dict(INFO), stable_id=True)
    with _l: _ids.append(i)
_th = [threading.Thread(target=_w) for _ in range(8)]
[t.start() for t in _th]; [t.join() for t in _th]
_n = len([e for e in ci._read(ci.journal_path("lanec"))[0] if e.get("ev") == "intent"])
check("8 concurrent stable begins (threads): ONE intent event (%d), one distinct id returned (%d of %d)" % (_n, len(set(_ids)), len(_ids)), _n == 1 and len(set(_ids)) == 1 and len(_ids) == 8)
# the same across PROCESSES (flock): 4 real subprocesses, same home
_code = ("import sys,os; sys.path.insert(0, %r); import daemon.launch_scanner.close_intents as ci; "
         "print(ci.begin('lanep','MP','pp',[{'kind':'credit','op_id':'op-1','amount':1.0}],{'close_ts':1795593430.2,'realized_usd':1.0},stable_id=True))" % REPO)
_ps = [subprocess.Popen([sys.executable, "-B", "-c", _code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ)) for _ in range(4)]
_outs = [p.communicate() for p in _ps]
_pids = {o[0].strip() for o in _outs}
_np = len([e for e in ci._read(ci.journal_path("lanep"))[0] if e.get("ev") == "intent"])
check("4 concurrent stable begins (separate processes): ONE intent event (%d), one distinct id (%d), all exit 0 (%r)" % (_np, len(_pids), [p.returncode for p in _ps]),
      _np == 1 and len(_pids) == 1 and all(p.returncode == 0 for p in _ps))
# stable_id on an unreadable journal fails closed
jp2 = ci.journal_path("lanet"); open(jp2, "w").write("{not json}\n{also not json}\n")
try:
    ci.begin("lanet", "MS", "ps", OPS, dict(INFO), stable_id=True); _sr = False
except ci.IntentNotDurable:
    _sr = True
check("stable_id on a torn journal fails closed (raises IntentNotDurable)", _sr)

# marker failure semantics: a lost marker is reported (False), nothing terminal is written without its evidence, and the intent stays pending for recovery
mk = ci.begin("lanem", "MM", "pm", OPS, dict(INFO))
_ra = ci._append; ci._append = lambda path, ev: False
try:
    _ma = ci.mark_account_applied("lanem", mk); _ml = ci.mark_lane_applied("lanem", mk)
finally:
    ci._append = _ra
check("marker appends that fail return False (%r, %r) and the intent is still PENDING for recovery (%r)" % (_ma, _ml, mk in [x["intent_id"] for x in ci.pending("lanem")]),
      _ma is False and _ml is False and mk in [x["intent_id"] for x in ci.pending("lanem")])
_okm = ci.mark_account_applied("lanem", mk) and ci.mark_lane_applied("lanem", mk)
check("healthy markers return True and the intent becomes terminal (applied)", _okm is True and ci.intent_status("lanem", mk) == "applied")

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
