#!/usr/bin/env python3
"""SOURCE ONLY. NEVER RUN WITHOUT main's explicit clearance and a review of this exact blob.
It spawns CHILD processes that are hard-killed (os._exit), which the current run authorization does NOT permit.

Scope (grad_cycle fault-log torn-tail repair, the C10w6 family). Each scenario: a child seeds a fault log, arms _REPAIR_HOOK to os._exit at one crash
point inside _repair_log_tail, then triggers the repair through _record_fault; a FRESH process then inspects the durable files and must find:
  O1 fault_active is True (an unknown fault never reads clean),
  O2 the original fragment bytes are preserved in <log>.torn-tails.jsonl, OR the fragment is still physically in the log (kill before preserve),
  O3 every complete log line parses, and the earlier known fault_id is still open,
  O4 a LATER _record_fault in the fresh process succeeds, the log is append-safe afterwards (ends in newline, all lines parse), and nothing was cleared,
  O5 the ledger file and the cycle journal are byte-identical before and after (the repair never touches accounting state).
Scenarios: S1 fragment SHORTER than the marker killed after marker/before truncate; S2 fragment LONGER than the marker, same kill point;
S3 kill at the new seam after_preserve_before_marker (fragment preserved, marker not yet written); S0 control with no kill. NOT built yet: S4 partial-marker write (Python has no hook inside one write(); needs a synthesized torn state, which would NOT be a real mid-write kill).
CLAIM LIMITS (read before citing any result): evidence class is an INSTRUMENTED ABRUPT PROCESS EXIT (os._exit at a test seam), not an arbitrary external SIGKILL.
It covers the lock-FREE caller fault-log layer only (_record_fault -> _repair_log_tail with a bare fake grad); it does NOT exercise the cycle lock, a locked repair,
or any accounting state (O5 only shows the ledger file bytes were untouched). NOT covered: a kill between the preserve write and its fsync (the torn-tails append
is flushed/fsynced inside one with-block; there is no seam before the fsync), the partial-marker window (no hook inside one write(), only a synthesized state
could stand in, which would not be a real mid-write exit), and torn-page/power-loss behavior.
NOT covered (known gaps, to build next): the position-opening step, the accounting oracle (cash + marks conservation across a kill between the fill journal and
the account credit), and the fault-WRITE-failure variant (the fault-file atomic write failing while the log append succeeds)."""
import json, os, subprocess, sys, tempfile, hashlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []
def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: FAILS.append(name)

PRE = r'''
import os, sys, json, types, time
sys.path.insert(0, os.environ["AGT_REPO_ROOT"])
import tests._guard_fixture as _g; _g.block_network()   # BEFORE any repo import
from daemon import grad_cycle as GC
home = sys.argv[1]
grad = types.SimpleNamespace(LEDGER_PATH=os.path.join(home, "led.json"))
'''
CHILD = PRE + r'''
point = sys.argv[2]
if point != "none":
    def hook(p):
        if p == point: os._exit(1)
    GC._REPAIR_HOOK = hook
out = {"cycle_seq": 2, "ts": time.time()}
GC._record_fault(grad, out, "harness_second_fault")
os._exit(0)
'''
VERIFY = PRE + r'''
log = GC._fault_log_path(grad)
raw = open(log, "rb").read() if os.path.exists(log) else b""
active, why = GC.fault_active(grad)
torn = log + ".torn-tails.jsonl"
tt = open(torn).read() if os.path.exists(torn) else ""
complete = raw.split(b"\n")[:-1] if raw else []
ok_lines = True
for ln in complete:
    try: json.loads(ln)
    except Exception: ok_lines = False
opened = GC._open_logged_faults(grad)
before_led = sys.argv[2]
out = {"cycle_seq": 3, "ts": time.time()}
w = GC._record_fault(grad, out, "harness_verify_fault")
raw2 = open(log, "rb").read()
ok2 = raw2.endswith(b"\n") and all(json.loads(l) for l in raw2.split(b"\n")[:-1])
print(json.dumps({"active": active, "why": why, "torn_has_fragment": "FRAG" in tt, "frag_in_log": b"FRAG" in raw, "complete_lines_parse": ok_lines,
                  "open_ids": opened if opened is None else [str(i)[:14] for i in opened], "later_write_ok": bool(w), "log_append_safe": ok2,
                  "led_sha": hashlib.sha256(open(grad.LEDGER_PATH, "rb").read()).hexdigest() if os.path.exists(grad.LEDGER_PATH) else None}))
'''
def py(code, *args):
    env = dict(os.environ, AGT_REPO_ROOT=REPO)
    return subprocess.run([sys.executable, "-c", code] + list(args), capture_output=True, text=True, timeout=60, env=env)

def seed(home, frag):
    led = os.path.join(home, "led.json"); open(led, "w").write('{"positions": {}}')
    return led
def log_path(home):
    r = py(PRE + "print(GC._fault_log_path(grad))", home); return r.stdout.strip()

def scenario(name, frag, point, expect_kill):
    home = tempfile.mkdtemp(prefix="gc_crash_"); led = seed(home, frag)
    lp = log_path(home); known = json.dumps({"fault_id": "known_1", "since": 1.0, "entry": {"reason": "seed"}}) + "\n"
    with open(lp, "wb") as fh: fh.write(known.encode() + frag)
    sha0 = hashlib.sha256(open(led, "rb").read()).hexdigest()
    c = py(CHILD, home, point)
    check("%s child exit as expected (%s)" % (name, c.returncode), (c.returncode == 1) == expect_kill)
    v = py(VERIFY, home, sha0)
    try: j = json.loads(v.stdout.strip().splitlines()[-1])
    except Exception: j = {}; print(v.stdout, v.stderr)
    check("%s O1 fault active, never clean (%s)" % (name, j.get("why")), j.get("active") is True)
    check("%s O2 fragment preserved in torn-tails or still in the log" % name, j.get("torn_has_fragment") or j.get("frag_in_log"))
    check("%s O3 complete lines parse and known_1 still open (%s)" % (name, j.get("open_ids")), j.get("complete_lines_parse") and isinstance(j.get("open_ids"), list) and any(i.startswith("known_1") for i in j["open_ids"]))
    check("%s O4 later fault write ok and log append-safe" % name, j.get("later_write_ok") and j.get("log_append_safe"))
    check("%s O5 ledger untouched" % name, j.get("led_sha") == sha0)

def main():
    short = b'{"fault_id": "FRAG'                                  # shorter than the marker
    long_ = b'{"fault_id": "FRAG' + b"x" * 600                      # longer than the marker
    scenario("S1 short fragment, kill after marker/before truncate", short, "after_marker_before_truncate", True)
    scenario("S2 long fragment, kill after marker/before truncate", long_, "after_marker_before_truncate", True)
    scenario("S3 kill after preserve, before marker", short, "after_preserve_before_marker", True)
    scenario("S0 control: no kill, repair completes", short, "none", False)
    print("FAILS: %d" % len(FAILS)); return 1 if FAILS else 0
if __name__ == "__main__":
    sys.exit(main())
