#!/usr/bin/env python3
"""Event identity tests: program field + recognition latency (2026-11-23).

Request section 5 requires every discovery event to carry program
identity and the bot to measure latency from the earliest observable
on-chain event to recognition. LogicalEvent now stores ``program``
(from the winning decoder event's program_id, never guessed) and
``recognition_latency_s`` (observation receipt minus block_time; raw
value, None when block_time is unknown - never a fabricated zero).

Plain script, run directly with the venv python (never pytest).
Fixtures: real on-chain transactions under tests/data/intelligence/.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.intelligence.events import EventNormalizer
from daemon.intelligence.events.event_models import LogicalEvent

DATA = os.path.join(os.path.dirname(__file__), "data", "intelligence")

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def load_fixture(name):
    with open(os.path.join(DATA, name)) as f:
        wrap = json.load(f)
    return wrap.get("transaction") or wrap.get("rpc_result") or wrap


def obs(provider, tx, observed_at=None):
    return {"provider": provider, "kind": "getTransaction", "tx": tx,
            "commitment": "confirmed",
            "observed_at": observed_at or time.time()}


n = EventNormalizer()

# 1. Program identity comes from the decoder's program_id.
tx = load_fixture("atx_create.json")
ev = n.normalize_observation(obs("alchemy", tx))
check("create event carries program identity", ev.program is not None)
check("program provenance noted", "program" in ev.provenance)
print("     program = %s" % ev.program)

# 2. Recognition latency = observed_at - block_time, raw.
bt = tx.get("blockTime")
ev2 = n.normalize_observation(obs("alchemy", tx, observed_at=bt + 7.5))
check("recognition latency computed", ev2.recognition_latency_s == 7.5)
check("latency provenance noted",
      "recognition_latency_s" in ev2.provenance)

# 3. Negative latency (clock skew) is kept raw - it IS the evidence.
ev3 = n.normalize_observation(obs("alchemy", tx, observed_at=bt - 2.0))
check("negative latency kept raw", ev3.recognition_latency_s == -2.0)

# 4. Unknown block_time -> None, never zero.
tx_nobt = json.loads(json.dumps(tx))
tx_nobt["blockTime"] = None
ev4 = n.normalize_observation(obs("alchemy", tx_nobt))
check("missing block_time -> latency None",
      ev4.recognition_latency_s is None)

# 5. Serialization round-trip preserves both fields.
d = ev2.to_dict()
back = LogicalEvent.from_dict(d)
check("round-trip program", back.program == ev2.program)
check("round-trip latency",
      back.recognition_latency_s == ev2.recognition_latency_s)

# 6. Old records (no new keys) deserialize with None, not a crash.
d_old = ev.to_dict()
del d_old["program"]
del d_old["recognition_latency_s"]
back_old = LogicalEvent.from_dict(d_old)
check("legacy record program None", back_old.program is None)
check("legacy record latency None",
      back_old.recognition_latency_s is None)

# 7. Merge: earliest recognition wins (later observation with an
#    earlier relative receipt lowers it; a slower one never raises).
ev_a = n.normalize_observation(obs("alchemy", tx, observed_at=bt + 10))
ev_b = n.normalize_observation(obs("helius", tx, observed_at=bt + 4))
merged = n.merge_into(ev_a, obs("helius", tx, observed_at=bt + 4))
check("merge keeps earliest recognition",
      merged.recognition_latency_s == 4)
merged2 = n.merge_into(merged, obs("chainstack", tx,
                                   observed_at=bt + 30))
check("later observation never raises latency",
      merged2.recognition_latency_s == 4)

# 8. Merge fills an unknown program and never silently overwrites a
#    known one.
ev_c = n.normalize_observation(obs("alchemy", tx_nobt))
ev_c.program = None
ev_c.provenance.pop("program", None)
m3 = n.merge_into(ev_c, obs("helius", tx, observed_at=bt + 1))
check("merge fills unknown program", m3.program == ev.program)
m3.program = "OtherProgram1111111111111111111111111111111"
m4 = n.merge_into(m3, obs("chainstack", tx, observed_at=bt + 2))
check("known program never overwritten",
      m4.program == "OtherProgram1111111111111111111111111111111")
check("program conflict recorded as disagreement",
      any(d.get("field") == "program" for d in m4.disagreements))

print()
if FAILS:
    print("FAILURES: %d" % len(FAILS))
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("ALL EVENT-IDENTITY TESTS PASSED")
