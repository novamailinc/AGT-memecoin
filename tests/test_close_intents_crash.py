#!/usr/bin/env python3
"""Cross-ledger process-death tests for the close-intent journal
(2026-11-22, Stage C).

The paper-account crash suite (test_paper_account_crash.py) covers
death inside ONE ledger's write path. This suite covers death
BETWEEN the two ledgers a close spans: the shared account and the
lane ledger. Each scenario runs a scripted close in a CHILD
process hard-killed (os._exit) at one boundary, then a FRESH
process runs close_intents.recover_lane with the real account and
a minimal lane ledger, and must reach exactly one converged state:

  kill_after_intent     intent journaled, nothing applied ->
                        recovery applies BOTH sides exactly once
  kill_after_account    account credited + account_applied, lane
                        ledger never written -> recovery re-books
                        the lane close from the durable payload;
                        the account credit must NOT double
                        (op_id dedupe)
  kill_after_lane       both writes done, lane_applied/applied
                        events lost -> recovery marks the intent
                        applied without re-settling anything
  torn_tail             crash mid-append -> the torn record is
                        truncated and recovery proceeds
  recover_twice         a second recovery pass is a no-op

The lane side here is a minimal stand-in ledger (closes list +
bankroll, atomic tmp+rename) - the lane wiring commits hook the
real lane ledgers to the same journal API.

Run: python3 tests/test_close_intents_crash.py
"""
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


CHILD = r'''
import json, os, sys
mode, home = sys.argv[1], sys.argv[2]
os.environ["FOREX_AGENT_HOME"] = home
os.environ["PAPER_ACCOUNT_START"] = "10"
sys.path.insert(0, os.environ["AGT_REPO_ROOT"])
import tests._guard_fixture as _g; _g.block_network(); _g.set_limits_env()  # mandatory-guard migration (disclosed): blocker before pa import; gates on + TEST-FIXTURE limits
import daemon.launch_scanner.paper_account as pa
import daemon.launch_scanner.close_intents as ci

LANE = "grad"
_ok, _why, PID = pa.try_debit(LANE, "M", 2.0, qty=100.0,
                              op_id="grad:buy:M:1")
assert _ok, _why

SETTLE = [{"kind": "credit", "op_id": "grad:close:M:p1",
           "amount": 3.0, "qty": 100.0, "pos_id": PID,
           "price_kind": "current_price_simulated_fill"}]
CLOSE = {"realized_usd": 3.0, "entry_notional_usd": 2.0,
         "closed_reason": "take_profit", "close_ts": 1000.0}
LANE_LEDGER = os.path.join(home, "lane_ledger.json")

def lane_write(rec):
    d = {"closes": [], "bankroll": 8.0}
    if os.path.exists(LANE_LEDGER):
        d = json.load(open(LANE_LEDGER))
    d["closes"].append(rec)
    d["bankroll"] = round(d["bankroll"] + rec["realized_usd"], 6)
    tmp = LANE_LEDGER + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, LANE_LEDGER)

iid = ci.begin(LANE, "M", PID, SETTLE, CLOSE)
if mode == "kill_after_intent":
    os._exit(1)
ok, why = pa.credit(LANE, "M", 3.0, "grad:close:M:p1",
                    pos_id=PID, qty=100.0,
                    price_kind="current_price_simulated_fill")
assert ok, why
ci.mark_account_applied(LANE, iid)
if mode == "kill_after_account":
    os._exit(1)
lane_write({"mint": "M", "pos_id": PID, "realized_usd": 3.0,
            "closed_reason": "take_profit"})
if mode == "kill_after_lane":
    os._exit(1)
ci.mark_lane_applied(LANE, iid)
'''

RECOVER = r'''
import json, os, sys
home = sys.argv[1]
os.environ["FOREX_AGENT_HOME"] = home
os.environ["PAPER_ACCOUNT_START"] = "10"
sys.path.insert(0, os.environ["AGT_REPO_ROOT"])
import tests._guard_fixture as _g; _g.block_network(); _g.set_limits_env()  # mandatory-guard migration (disclosed): blocker before pa import; gates on + TEST-FIXTURE limits
import daemon.launch_scanner.paper_account as pa
import daemon.launch_scanner.close_intents as ci

LANE = "grad"
LANE_LEDGER = os.path.join(home, "lane_ledger.json")

def account_apply(op):
    if op["kind"] == "credit":
        return pa.credit(LANE, "M", op["amount"], op["op_id"],
                         pos_id=op.get("pos_id"), qty=op.get("qty"),
                         price_kind=op.get("price_kind"))
    return pa.write_down(LANE, "M", op["op_id"],
                         pos_id=op.get("pos_id"), qty=op.get("qty"),
                         reason=op.get("reason") or "recovered")

def lane_has_close(intent):
    if not os.path.exists(LANE_LEDGER):
        return False
    d = json.load(open(LANE_LEDGER))
    return any(c.get("pos_id") == intent.get("pos_id")
               for c in d["closes"])

def lane_apply(intent):
    info = intent["close_info"]
    d = {"closes": [], "bankroll": 8.0}
    if os.path.exists(LANE_LEDGER):
        d = json.load(open(LANE_LEDGER))
    d["closes"].append({"mint": intent["mint"],
                        "pos_id": intent["pos_id"],
                        "realized_usd": info["realized_usd"],
                        "closed_reason": info["closed_reason"]})
    d["bankroll"] = round(d["bankroll"] + info["realized_usd"], 6)
    tmp = LANE_LEDGER + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, LANE_LEDGER)
    return True

passes = int(sys.argv[2])
frozen = []
for _ in range(passes):
    frozen = ci.recover_lane(LANE, account_apply, lane_has_close,
                             lane_apply)
snap = pa.snapshot()
view = snap["view"] if "view" in snap else snap
lane = json.load(open(LANE_LEDGER)) if os.path.exists(LANE_LEDGER) \
    else {"closes": [], "bankroll": None}
print(json.dumps({
    "frozen": frozen,
    "pending": [p["intent_id"] for p in ci.pending(LANE)],
    "cash": snap["cash"],
    "lane_closes": len(lane["closes"]),
    "lane_bankroll": lane["bankroll"],
}))
'''


def run(script, *args):
    env = dict(os.environ)
    env["AGT_REPO_ROOT"] = REPO
    return subprocess.run([sys.executable, "-c", script] + list(args),
                          capture_output=True, text=True, env=env,
                          timeout=60)


def scenario(mode, passes=1):
    home = tempfile.mkdtemp(prefix="ci-crash-")
    child = run(CHILD, mode, home)
    check("%s: child killed at boundary" % mode, child.returncode != 0)
    rec = run(RECOVER, home, str(passes))
    if rec.returncode != 0:
        check("%s: recovery ran" % mode, False)
        print(rec.stderr[-2000:])
        return
    return json.loads(rec.stdout.strip().splitlines()[-1])


# kill after INTENT: recovery applies both sides exactly once.
r = scenario("kill_after_intent")
if r:
    check("kill_after_intent: nothing frozen", r["frozen"] == [])
    check("kill_after_intent: nothing pending", r["pending"] == [])
    check("kill_after_intent: account credited once (8+3)",
          abs(r["cash"] - 11.0) < 1e-9)
    check("kill_after_intent: lane close booked once",
          r["lane_closes"] == 1)
    check("kill_after_intent: lane bankroll 8+3",
          abs(r["lane_bankroll"] - 11.0) < 1e-9)

# kill after ACCOUNT_APPLIED: lane side re-booked from the payload,
# account credit not doubled.
r = scenario("kill_after_account")
if r:
    check("kill_after_account: nothing frozen", r["frozen"] == [])
    check("kill_after_account: account NOT double-credited",
          abs(r["cash"] - 11.0) < 1e-9)
    check("kill_after_account: lane close booked once",
          r["lane_closes"] == 1)

# kill after the lane write but before lane_applied: recovery only
# completes the journal.
r = scenario("kill_after_lane")
if r:
    check("kill_after_lane: nothing frozen", r["frozen"] == [])
    check("kill_after_lane: account not re-credited",
          abs(r["cash"] - 11.0) < 1e-9)
    check("kill_after_lane: lane close still once",
          r["lane_closes"] == 1)

# torn tail: a half-written record is truncated and recovery works.
home = tempfile.mkdtemp(prefix="ci-crash-")
child = run(CHILD, "kill_after_intent", home)
check("torn_tail: child killed", child.returncode != 0)
with open(os.path.join(home, "close_intents_grad.jsonl"), "ab") as f:
    f.write(b'{"ev": "account_applied", "intent_id": "grad:M:p')
rec = run(RECOVER, home, "1")
if rec.returncode == 0:
    r = json.loads(rec.stdout.strip().splitlines()[-1])
    check("torn_tail: recovered past the torn record",
          r["frozen"] == [] and r["pending"] == [])
    check("torn_tail: account credited once",
          abs(r["cash"] - 11.0) < 1e-9)
else:
    check("torn_tail: recovery ran", False)
    print(rec.stderr[-2000:])

# recovery is idempotent: a second pass settles nothing again.
r = scenario("kill_after_account", passes=2)
if r:
    check("recover_twice: account not double-credited",
          abs(r["cash"] - 11.0) < 1e-9)
    check("recover_twice: lane close still once",
          r["lane_closes"] == 1)

print()
if FAILS:
    print("FAILURES: %d" % len(FAILS))
    for f in FAILS:
        print(" - " + f)
    sys.exit(1)
print("ALL CLOSE-INTENT CRASH TESTS PASSED")
