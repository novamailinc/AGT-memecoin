#!/usr/bin/env python3
"""Regression for the close-intent journal TAIL handling (2026-11-25), REAL close_intents module in a temp home. Engineering test of journal logic only:
no account, no network, no engine. Run: python3 -B tests/test_close_intents_tail.py
Covers: (1) a provable torn tail is truncated BEFORE the next append (no merged half line), a complete-but-unterminated last record gets a newline, (2) genuine
MID-FILE corruption is never healed: append refused, journal_writable False, begin raises IntentNotDurable, recover_lane freezes (returns the pending ids) and the
file bytes are unchanged, (3) begin RESUMES a pending intent for the same op ids (never a second intent), (4) 4 processes x 20 concurrent appends/begins with a
torn tail present leave one clean journal and exactly the expected single intent."""
import os, sys, tempfile, subprocess, json, hashlib
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
HOME = tempfile.mkdtemp(prefix="ci-tail-")
os.environ["FOREX_AGENT_HOME"] = HOME
import daemon.launch_scanner.close_intents as ci

FAILS = 0
def check(name, cond):
    global FAILS
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS += 1

def raises(fn):
    try:
        fn()
    except ci.IntentNotDurable:
        return True
    return False

OPS = [{"kind": "credit", "op_id": "tail:leg:a:M:p#1", "amount": 1.0}]
INFO = {"close_ts": 1795593430.2, "realized_usd": 1.0, "entry_notional_usd": 1.0, "closed_reason": "t", "leg_suffix": "a"}
def raw(lane): return open(ci.journal_path(lane), "rb").read()
def put(lane, data):
    os.makedirs(HOME, exist_ok=True); open(ci.journal_path(lane), "wb").write(data)

# (1a) provable torn tail: truncated before the append
ci.journal_writable("t1")
put("t1", raw("t1") + b'{"ev": "intent", "intent_id": "half')
check("setup t1: journal reads torn_tail", ci._read(ci.journal_path("t1"))[1] == "torn_tail")
ok = ci.journal_writable("t1")
ents, st, _ = ci._read(ci.journal_path("t1"))
check("t1: probe after a torn tail succeeds (%r), journal clean (%r), no merged line: %d probe rows and no 'half' text" % (ok, st, len(ents)),
      ok is True and st is None and len(ents) == 2 and b"half" not in raw("t1"))
# (1b) complete record without its newline
put("t1b", json.dumps({"ev": "probe", "lane": "t1b"}).encode())
ok = ci._append(ci.journal_path("t1b"), {"ev": "probe", "lane": "t1b"})
ents, st, _ = ci._read(ci.journal_path("t1b"))
check("t1b: complete unterminated last record keeps its data and gets a newline (ok %r, status %r, rows %d)" % (ok, st, len(ents)), ok and st is None and len(ents) == 2)
# (2) genuine mid-file corruption, with a PENDING intent journaled before the garbage (so recover_lane has something real to freeze)
ci.begin("t2", "M", "p#1", OPS, dict(INFO))
pend_id = [x["intent_id"] for x in ci.pending("t2")]
good = json.dumps({"ev": "probe", "lane": "t2"}).encode() + b"\n"
put("t2", raw("t2") + b"{garbage not json\n" + good)       # newline-TERMINATED garbage in the middle
before = hashlib.sha256(raw("t2")).hexdigest()
check("setup t2: one pending intent %r and status torn (mid-file, newline-terminated)" % pend_id, len(pend_id) == 1 and ci._read(ci.journal_path("t2"))[1] == "torn")
r1 = ci._append(ci.journal_path("t2"), {"ev": "probe", "lane": "t2"})
r2 = ci.journal_writable("t2")
r3 = raises(lambda: ci.begin("t2", "M", "p#1", OPS, dict(INFO)))
r3b = ci.mark_account_applied("t2", pend_id[0])
r4 = ci.recover_lane("t2", lambda op: (True, "x"), lambda i: False, lambda i: True)
after = hashlib.sha256(raw("t2")).hexdigest()
check("t2 mid-file corruption: append refused %r, journal_writable %r, begin raises %r, marker refused %r, recover_lane returns the pending id %r (want %r), bytes UNCHANGED %r"
      % (r1, r2, r3, r3b, r4, pend_id, before == after), r1 is False and r2 is False and r3 is True and r3b is False and r4 == pend_id and before == after)
# t2b: mid-file corruption AND an unterminated torn tail
base = raw("t2")
put("t2b", base + b'{"ev": "half')
b2 = hashlib.sha256(raw("t2b")).hexdigest()
r5 = ci._append(ci.journal_path("t2b"), {"ev": "probe"})
r6 = ci.recover_lane("t2b", lambda op: (True, "x"), lambda i: False, lambda i: True)
check("t2b mid-file corruption plus torn tail: append refused %r, recover returns the pending id %r, bytes unchanged %r" % (r5, r6, hashlib.sha256(raw("t2b")).hexdigest() == b2),
      r5 is False and r6 == pend_id and hashlib.sha256(raw("t2b")).hexdigest() == b2)
# negative control: a RAW append WITHOUT the repair merges into the half line. ONE raw append leaves one unparseable LAST record (still torn_tail); a SECOND raw append
# (what the next writer would do) turns the merged line into MID-FILE garbage (status torn). Both states are asserted, proving the guard is what prevents the merge.
put("t2c", json.dumps({"ev": "probe"}).encode() + b'\n{"ev": "half')
with open(ci.journal_path("t2c"), "a") as f: f.write(json.dumps({"ev": "probe"}) + "\n")
st_one = ci._read(ci.journal_path("t2c"))[1]
with open(ci.journal_path("t2c"), "a") as f: f.write(json.dumps({"ev": "probe"}) + "\n")
st_two = ci._read(ci.journal_path("t2c"))[1]
check("t2c negative control: one raw append onto a half line gives %r (want torn_tail), a second raw append gives %r (want torn, mid-file)" % (st_one, st_two),
      st_one == "torn_tail" and st_two == "torn")

# (3) resume
a = ci.begin("t3", "M", "p#1", OPS, dict(INFO))
b = ci.begin("t3", "M", "p#1", OPS, dict(INFO, close_ts=1795599999.9))
check("t3 begin with a different close_ts RESUMES the pending intent (%s == %s), one intent row" % (a, b),
      a == b and len([e for e in ci._read(ci.journal_path("t3"))[0] if e.get("ev") == "intent"]) == 1)
c = raises(lambda: ci.begin("t3", "M", "p#1", [dict(OPS[0], amount=2.0)], dict(INFO)))
check("t3 same op ids with a DIFFERENT plan refuses", c)
e1 = ci.begin("t3", "M", "p#1", [dict(OPS[0], op_id="tail:leg:c:M:p#1")], dict(INFO, leg_suffix="c"))
check("t3 DIFFERENT op ids for the same position open a NEW intent (%s != %s)" % (e1, a), e1 != a)
ci.mark_account_applied("t3", a); ci.mark_account_applied("t3", a); ci.mark_account_applied("t3", a)
n_aa = len([e for e in ci._read(ci.journal_path("t3"))[0] if e.get("ev") == "account_applied" and e.get("intent_id") == a])
n_aa_other = len([e for e in ci._read(ci.journal_path("t3"))[0] if e.get("ev") == "account_applied" and e.get("intent_id") == e1])
ci.mark_account_applied("t3", e1)
n_aa_other2 = len([e for e in ci._read(ci.journal_path("t3"))[0] if e.get("ev") == "account_applied" and e.get("intent_id") == e1])
check("t3 repeated mark_account_applied writes ONE event (%d); a different intent still appends (%d -> %d)" % (n_aa, n_aa_other, n_aa_other2), n_aa == 1 and n_aa_other == 0 and n_aa_other2 == 1)
ci.mark_lane_applied("t3", a); ci.mark_lane_applied("t3", a)
evs = [e["ev"] for e in ci._read(ci.journal_path("t3"))[0] if e.get("intent_id") == a]
check("t3 mark_lane_applied twice: lifecycle %r (terminal applied exactly once)" % evs, evs == ["intent", "account_applied", "lane_applied", "applied"])
ci.freeze("t3", e1, "test freeze")
fm = ci.mark_lane_applied("t3", e1)
evs1 = [e["ev"] for e in ci._read(ci.journal_path("t3"))[0] if e.get("intent_id") == e1]
check("t3 a FROZEN intent refuses markers (returned %r), lifecycle stays %r" % (fm, evs1), fm is False and evs1 == ["intent", "account_applied", "frozen"])

d = ci.begin("t3", "M", "p#1", OPS, dict(INFO, close_ts=1795600000.1))
check("t3 after the terminal applied a new begin is a new intent (%s != %s)" % (d, a), d != a)
# (4) concurrency with a torn tail present
ci.journal_writable("t4")
put("t4", raw("t4") + b'{"ev": "inten')
CHILD = ("import os,sys\nsys.path.insert(0,%r)\nos.environ['FOREX_AGENT_HOME']=%r\nimport daemon.launch_scanner.close_intents as ci\n"
         "OPS=%r\nINFO=%r\nfor i in range(20):\n    ci.journal_writable('t4')\n    ci.begin('t4','M','p#1',OPS,dict(INFO,close_ts=1795600100.0+i))\n") % (REPO, HOME, OPS, INFO)
ps = [subprocess.Popen([sys.executable, "-B", "-c", CHILD]) for _ in range(4)]
rcs = [p.wait(timeout=60) for p in ps]
ents, st, _ = ci._read(ci.journal_path("t4"))
n_int = len([e for e in ents if e.get("ev") == "intent"])
check("t4 4 procs x 20 begins+probes over a torn tail: rcs %r, journal status %r, %d intent rows (want 1: all resumed), half text gone %r" % (rcs, st, n_int, b"inten" not in raw("t4").replace(b"intent", b"")),
      rcs == [0, 0, 0, 0] and st is None and n_int == 1)
print("fails: %d" % FAILS)
sys.exit(1 if FAILS else 0)
