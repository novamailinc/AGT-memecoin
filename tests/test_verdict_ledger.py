#!/usr/bin/env python3
"""Tamper-evident verdict ledger + veto-event journal tests
(2026-10-23). Plain script — run with the venv python, not pytest.

Covers daemon/launch_scanner/verdict_ledger.py: hash-chained appends
verify OK, genesis/seq/linkage, tampering with a middle record is
reported BROKEN at the right seq, payload canonicalization is
key-order stable, append is fail-soft on an unwritable home; and
daemon/launch_scanner/veto.py's evaluation journal: veto_check
writes one veto_events.jsonl record per evaluation (pass AND fail),
fail-soft when the journal path is broken. Veto fixtures follow
tests/test_veto_log.py (synthetic RugCheck report, run_bundle=False,
no network).
"""
import json
import os
import sys
import tempfile

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

_ORIG_HOME = os.environ.get("FOREX_AGENT_HOME")
HOME1 = tempfile.mkdtemp(prefix="verdict-ledger-home-")
os.environ["FOREX_AGENT_HOME"] = HOME1

from daemon.launch_scanner import verdict_ledger as VL  # noqa: E402
from daemon.launch_scanner import veto as V  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


CHAIN = os.path.join(HOME1, "verdict_ledger", "events.jsonl")

# ---------------- empty chain ----------------
check("empty: missing chain verifies (True, 0, None)",
      VL.verify() == (True, 0, None))
check("empty: head_hash is GENESIS", VL.head_hash() == "GENESIS")
check("empty: ledger_path follows FOREX_AGENT_HOME",
      VL.ledger_path() == CHAIN)

# ---------------- appends + chain integrity ----------------
check("append: close record 1 written",
      VL.append("grad", "close", {"mint": "M1pump", "pnl_usd": -0.5,
                                  "realized_mult": 0.5,
                                  "closed_reason": "hard_stop_-50pct",
                                  "closes_count": 1}) is True)
check("append: close record 2 written",
      VL.append("grad", "close", {"mint": "M2pump", "pnl_usd": 1.25,
                                  "realized_mult": 2.25,
                                  "closed_reason": "ladder_complete",
                                  "closes_count": 2}) is True)
check("append: verdict record written",
      VL.append("ceo", "verdict", {"closes": 15, "expectancy": -0.31,
                                   "decision": "HALT"}) is True)
check("append: checkpoint record written (explicit path)",
      VL.append("grad_raw", "checkpoint", {"closes": 40},
                path=CHAIN) is True)
check("append: chain file exists at the documented path",
      os.path.isfile(CHAIN))

recs = VL.read_records()
check("chain: 4 records in order, seq 1..4",
      len(recs) == 4 and [r["seq"] for r in recs] == [1, 2, 3, 4])
check("chain: genesis prev_hash on seq 1, links after",
      recs[0]["prev_hash"] == "GENESIS"
      and all(recs[i]["prev_hash"] == recs[i - 1]["hash"]
              for i in range(1, 4)))
check("chain: record shape {seq,ts,lane,kind,payload,prev_hash,hash}",
      all(set(r.keys()) == {"seq", "ts", "lane", "kind", "payload",
                            "prev_hash", "hash"} for r in recs)
      and recs[0]["lane"] == "grad" and recs[0]["kind"] == "close"
      and recs[2]["payload"]["expectancy"] == -0.31)
ok, n, bad = VL.verify()
check("chain: verify OK with 4 records, no bad seq",
      ok is True and n == 4 and bad is None)
check("chain: head_hash is the last record's hash",
      VL.head_hash() == recs[-1]["hash"])

# ---------------- canonicalization stability ----------------
check("canonical: key order never changes the canonical JSON",
      VL._canonical({"b": 1, "a": {"y": 2, "x": 1}})
      == VL._canonical({"a": {"x": 1, "y": 2}, "b": 1}))
# A record's stored hash recomputes from its parsed form even when
# the payload dict is rebuilt in a different key order.
r0 = recs[0]
reordered = {"payload": dict(reversed(list(r0["payload"].items()))),
             "kind": r0["kind"], "lane": r0["lane"], "ts": r0["ts"],
             "seq": r0["seq"], "prev_hash": r0["prev_hash"]}
check("canonical: hash stable under payload key reordering",
      VL._record_hash(r0["prev_hash"], reordered) == r0["hash"])
# Appending the same payload built in two key orders yields records
# whose hashes verify (round-trip through the file).
VL.append("grad", "close", {"a": 1, "b": 2, "mint": "ORDpump"})
VL.append("grad", "close", {"mint": "ORDpump", "b": 2, "a": 1})
ok, n, bad = VL.verify()
check("canonical: reordered-payload appends still verify OK",
      ok is True and n == 6 and bad is None)

# ---------------- tamper evidence ----------------
with open(CHAIN) as f:
    lines = f.read().splitlines()
tampered = json.loads(lines[2])          # middle record, seq 3
tampered["payload"]["expectancy"] = 9.99  # rewrite history
lines[2] = json.dumps(tampered)
with open(CHAIN, "w") as f:
    f.write("\n".join(lines) + "\n")
ok, n, bad = VL.verify()
check("tamper: middle-record edit -> BROKEN at seq 3",
      ok is False and n == 6 and bad == 3)
check("tamper: records still readable after tamper",
      len(VL.read_records()) == 6)

# Link tamper on a fresh chain: rewriting seq 2's prev_hash breaks
# at seq 2 even if its own hash were recomputed naively.
HOME2 = tempfile.mkdtemp(prefix="verdict-ledger-home2-")
os.environ["FOREX_AGENT_HOME"] = HOME2
VL.append("grad", "close", {"mint": "A", "pnl_usd": 0.1})
VL.append("grad", "close", {"mint": "B", "pnl_usd": 0.2})
chain2 = VL.ledger_path()
with open(chain2) as f:
    lines2 = f.read().splitlines()
rec2 = json.loads(lines2[1])
rec2["prev_hash"] = "FORGED"
lines2[1] = json.dumps(rec2)
with open(chain2, "w") as f:
    f.write("\n".join(lines2) + "\n")
ok, n, bad = VL.verify()
check("tamper: forged prev_hash -> BROKEN at seq 2",
      ok is False and n == 2 and bad == 2)

# ---------------- fail-soft append ----------------
os.environ["FOREX_AGENT_HOME"] = "/proc/definitely-not-writable-xyz"
check("fail-soft: append to unwritable home returns False, no raise",
      VL.append("grad", "close", {"mint": "X"}) is False)
check("fail-soft: verify on unwritable/missing home is empty-OK",
      VL.verify() == (True, 0, None))
os.environ["FOREX_AGENT_HOME"] = HOME1
ok, n, bad = VL.verify()
check("fail-soft: original chain untouched by the failed append",
      n == 6 and ok is False and bad == 3)  # still the tampered state

# ---------------- veto-event journal ----------------
HOME3 = tempfile.mkdtemp(prefix="veto-events-home-")
os.environ["FOREX_AGENT_HOME"] = HOME3
EVENTS = os.path.join(HOME3, "veto_events.jsonl")
MINT = "VetoEventProbeMint11111111111111111111111111"
# top-10 = 10 x 9% = 90% > TOP10_MAX_PCT (40%) -> concentration veto
RUG_REPORT = {"topHolders": [{"pct": 9.0}] * 10}


def _event_lines():
    if not os.path.exists(EVENTS):
        return []
    with open(EVENTS) as f:
        return [ln for ln in f.read().splitlines() if ln.strip()]


check("veto journal: no file before any evaluation",
      not os.path.exists(EVENTS))
res = V.veto_check(MINT, "solana", "PROBE", safety_report=RUG_REPORT,
                   run_bundle=False, lane="mirror")
check("veto journal: fixture veto still fails the check",
      res["passed"] is False)
lines = _event_lines()
check("veto journal: one evaluation record landed", len(lines) == 1)
ev = json.loads(lines[0]) if lines else {}
check("veto journal: record carries ts/mint/chain/passed/vetoes",
      isinstance(ev.get("ts"), float) and ev.get("mint") == MINT
      and ev.get("chain") == "solana" and ev.get("passed") is False
      and any("concentration:top10" in v for v in ev.get("vetoes", [])))
check("veto journal: source_lane recorded from the caller",
      ev.get("source_lane") == "mirror")

res = V.veto_check(MINT, "solana", "PROBE", safety_report=None,
                   run_bundle=False, lane="ceo")
lines = _event_lines()
ev2 = json.loads(lines[1]) if len(lines) == 2 else {}
check("veto journal: PASSING evaluation is journaled too "
      "(reachability is provable, unlike veto_log)",
      res["passed"] is True and len(lines) == 2
      and ev2.get("passed") is True and ev2.get("vetoes") == []
      and ev2.get("source_lane") == "ceo")
check("veto journal: veto_log still only records the veto",
      len(open(os.path.join(HOME3, "veto_log.jsonl")).readlines()) == 1)

# Fail-soft: a broken journal path must not change the veto result.
_orig_events_path = V._veto_events_path
V._veto_events_path = lambda: os.path.join(  # noqa: E731
    HOME3, "no-such-dir", "x.jsonl")
try:
    res = V.veto_check(MINT, "solana", "PROBE",
                       safety_report=RUG_REPORT, run_bundle=False,
                       lane="mirror")
    check("veto journal: write failure does not break the veto",
          res["passed"] is False)
finally:
    V._veto_events_path = _orig_events_path
check("veto journal: failed write added no record",
      len(_event_lines()) == 2)

# ---------------- restore env ----------------
if _ORIG_HOME is None:
    os.environ.pop("FOREX_AGENT_HOME", None)
else:
    os.environ["FOREX_AGENT_HOME"] = _ORIG_HOME

print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("all verdict ledger tests passed")
