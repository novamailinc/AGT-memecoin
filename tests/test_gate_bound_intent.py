#!/usr/bin/env python3
"""bb69 REGRESSION: the recovery gate (_gate_intent) must accept the ops the writers now record (generation-binding fields) and must still reject unsafe ones.
Born from the V9c kill-after K0 run (NOT-OK): the gate's _OP_KEYS did not list expected_account_id/entry_op_id, so every recorded settle intent was frozen at recovery.
SOURCE ONLY, NOT RUN until cleared. One process, scratch home, real _gate_intent / _expected_op_id of momentum_lane and mirror (READ-ONLY functions), no network, no executor.
Predeclared (UNVERIFIED): POSITIVE bound op -> no problem naming binding or 'carries fields'. NEGATIVES: foreign generation, half-bound, non-string binding, unknown extra key ->
a freeze verdict that names the specific problem. All-None on an unbound intent -> no binding problem (the account refuses it visibly later)."""
if __name__ != "__main__":
    import pytest
    pytest.skip("script-style test", allow_module_level=True)
import os, sys, tempfile
ROOTDIR = tempfile.mkdtemp(prefix="genbind-gate-")
os.environ["FOREX_AGENT_HOME"] = os.path.join(ROOTDIR, "home"); os.makedirs(os.environ["FOREX_AGENT_HOME"], exist_ok=True)
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"; os.environ["PAPER_ACCOUNT_START"] = "10"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import tests._guard_fixture as G
G.block_network()
sys.path.insert(0, os.path.join(ROOT, "daemon", "launch_scanner"))
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n, flush=True)
    if not c: FAILS.append(n)
def ENTRY(lane): return "%s:entry:GateMint1:1700000000000" % lane
def intent(mod, lane, acct, op_extra):
    mint, pid = "GateMint1", "%s/GateMint1#1" % lane
    op = {"kind": "credit", "op_id": mod._expected_op_id(lane, "credit", "leg:a", mint, pid), "mint": mint, "amount": 1.1, "qty": 100.0, "fee": 0.0,
          "price_kind": "model", "pos_id": pid}
    op.update(op_extra)
    it = {"intent": {"lane": lane, "mint": mint, "pos_id": pid, "settle_ops": [op], "close_info": {"leg_suffix": "leg:a", "closed_reason": "tp", "realized_usd": 0.1},
                     **({"account_id": acct} if acct is not None else {})}, "account_applied": False}
    return it
from daemon.launch_scanner import momentum_lane as ML
from daemon.launch_scanner import mirror as MR
for name, mod, lane in (("momentum", ML, "momentum"), ("mirror", MR, next(iter(getattr(MR, "LANE_BY_CHAIN", {}).values()), "mirror"))):
    d = {"positions": {}, "closes": []}
    v, why, _ = mod._gate_intent(lane, intent(mod, lane, "acct-A", {"expected_account_id": "acct-A", "entry_op_id": ENTRY(lane)}), d, "ok")
    check("%s POSITIVE bound op: no binding or extra-field problem (verdict %s: %s)" % (name, v, why[:140]), "carries fields" not in why and "binding" not in why)
    v, why, _ = mod._gate_intent(lane, intent(mod, lane, "acct-A", {"expected_account_id": "acct-FOREIGN", "entry_op_id": ENTRY(lane)}), d, "ok")
    check("%s NEGATIVE foreign generation is a freeze naming the mismatch (%s: %s)" % (name, v, why[:140]), v == "freeze" and "differs from the intent's recorded account generation" in why)
    v, why, _ = mod._gate_intent(lane, intent(mod, lane, "acct-A", {"expected_account_id": "acct-A"}), d, "ok")
    check("%s NEGATIVE half-bound op (no entry_op_id) is a freeze (%s: %s)" % (name, v, why[:140]), v == "freeze" and "BOTH non-empty strings" in why)
    v, why, _ = mod._gate_intent(lane, intent(mod, lane, "acct-A", {"expected_account_id": 7, "entry_op_id": ["x"]}), d, "ok")
    check("%s NEGATIVE non-string binding is a freeze (%s: %s)" % (name, v, why[:140]), v == "freeze" and "BOTH non-empty strings" in why)
    v, why, _ = mod._gate_intent(lane, intent(mod, lane, "acct-A", {"expected_account_id": "acct-A", "entry_op_id": ENTRY(lane), "surprise": 1}), d, "ok")
    check("%s NEGATIVE an unknown extra key is still rejected (%s: %s)" % (name, v, why[:140]), v == "freeze" and "carries fields" in why and "surprise" in why)
    v, why, _ = mod._gate_intent(lane, intent(mod, lane, "acct-A", {"expected_account_id": None, "entry_op_id": None}), d, "ok")
    check("%s NEGATIVE op recorded unbound under a BOUND intent is a non-mutating HOLD naming the missing binding (%s: %s)" % (name, v, why[:140]), v == "hold" and "NO account-generation binding" in why)
    v, why, _ = mod._gate_intent(lane, intent(mod, lane, None, {"expected_account_id": None, "entry_op_id": None}), d, "ok")
    check("%s NEGATIVE unbound intent with all-None binding is a visible HOLD, never ok (%s: %s)" % (name, v, why[:140]), v == "hold" and "NO account-generation binding" in why)
    d2 = {"positions": {"GateMint1": {"lane": lane, "mint": "GateMint1", "pa_pos_id": "%s/GateMint1#1" % lane, "pa_account_id": "acct-A", "pa_entry_op": "%s:entry:GateMint1:5" % lane}}, "closes": []}
    v, why, _ = mod._gate_intent(lane, intent(mod, lane, "acct-A", {"expected_account_id": "acct-A", "entry_op_id": ENTRY(lane)}), d2, "ok")
    check("%s NEGATIVE binding contradicting the lane ledger's own record is a freeze (%s: %s)" % (name, v, why[:140]), v == "freeze" and "contradicts the lane ledger" in why)
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
