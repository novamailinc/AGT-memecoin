#!/usr/bin/env python3
"""Phase 2 regression tests — Token-2022 extension safety, fail-closed
review (directive §7.2).

Rule: an extension this parser cannot verify — unknown name,
malformed entry, unreadable state, non-list extensions data — must
NEVER silently pass. It produces an explicit REVIEW flag (score -5,
``passed`` unchanged); the known trap names still BLOCK via fails.
Fixtures: recognized-safe, recognized-risky, unknown, malformed-TLV,
and incomplete-account cases.

Also covers the verdict cache: an unknown verdict (null/incomplete
read) is NEVER cached — it must not block later retrieval of
confirmed data — while a reached verdict is still cached.

RPC is mocked at the module's _rpc seam. Plain script — run
directly with the venv python, not pytest.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "daemon", "launch_scanner"))
import instant_safety as ins  # noqa: E402

FAILS = []
TOTAL = [0]


def check(name, cond):
    TOTAL[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


WALLET = "Fn9n3ftUzjhcmoLwXbTqJdATzamCp3xPEu4LmLSS8ELr"  # on-curve
MINT = "AKuvHXF6H2ajMQhEPn3GpEq9FbDQixg2X27jHNbtpump"
SUPPLY = 1_000_000_000  # raw units
ONE_HOLDER = [("AcctA", 40_000_000, WALLET)]


def make_rpc(holders=(), extensions=None, owner=None, counter=None):
    """holders: list of (token_account, raw_amount, owner)."""
    def _rpc(method, params, timeout):
        if counter is not None:
            counter[0] += 1
        if method == "getAccountInfo" and params[0] == MINT:
            info = {"mintAuthority": None, "freezeAuthority": None,
                    "supply": str(SUPPLY), "decimals": 6}
            if extensions is not None:
                info["extensions"] = extensions
            return {"value": {
                "owner": owner or ins.TOKEN2022_PROGRAM,
                "data": {"parsed": {"info": info}}}}
        if method == "getTokenLargestAccounts":
            return {"value": [
                {"address": a, "amount": str(amt), "decimals": 6,
                 "uiAmount": amt / 1e6} for a, amt, _o in holders]}
        if method == "getMultipleAccounts":
            by_addr = {a: o for a, _amt, o in holders}
            return {"value": [
                {"data": {"parsed": {"info": {"owner": by_addr[a]}}}}
                if a in by_addr else None for a in params[0]]}
        return None
    return _rpc


def run(name, rpc):
    ins._cache.clear()
    orig = ins._rpc
    ins._rpc = rpc
    try:
        rep = ins.check(MINT, timeout=6)
    finally:
        ins._rpc = orig
    print("--- %s: passed=%s score=%s unknown=%s fails=%s flags=%s"
          % (name, rep.get("passed"), rep.get("score"),
             rep.get("unknown"), rep.get("fails"), rep.get("flags")))
    return rep


def ext(name, state=None):
    return {"extension": name, "state": state or {}}


# --- recognized-safe: no flag, verdict unchanged ---------------------
rep = run("safe extensions", make_rpc(
    holders=ONE_HOLDER,
    extensions=[ext("metadataPointer", {"metadataAddress": WALLET}),
                ext("immutableOwner"),
                ext("groupPointer", {"groupAddress": WALLET})]))
check("recognized-safe extensions pass clean (score 95)",
      rep["passed"] and rep["score"] == 95
      and not any("unrecognized" in f or "unreadable" in f
                  for f in rep["flags"]))
check("recognized-safe names still recorded",
      rep["instant"]["token2022_extensions"]
      == ["metadataPointer", "immutableOwner", "groupPointer"])

# --- recognized-risky: still BLOCKs (control) ------------------------
rep = run("risky extension control", make_rpc(
    holders=ONE_HOLDER,
    extensions=[ext("transferHook", {"programId": WALLET})]))
check("recognized trap still fails", not rep["passed"]
      and any("transfer hook" in f for f in rep["fails"]))

# --- unknown: REVIEW flag, never a silent pass, never a fail ---------
rep = run("interest-bearing extension", make_rpc(
    holders=ONE_HOLDER,
    extensions=[ext("interestBearingMint",
                    {"rateAuthority": WALLET})]))
check("interest-bearing named and flagged for review (score 90)",
      rep["passed"] and rep["score"] == 90
      and any("interest-bearing" in f and "review" in f
              for f in rep["flags"]))
rep = run("named review extensions", make_rpc(
    holders=ONE_HOLDER,
    extensions=[ext("memoTransfer"), ext("pausable"),
                ext("scaledUiAmount"),
                ext("interestBearingConfig")]))
check("memo/pausable/scaled/interest named, not generic "
      "unrecognized",
      rep["passed"]
      and any("memo-gated" in f for f in rep["flags"])
      and any("pausable" in f.lower() for f in rep["flags"])
      and any("scaled UI amount" in f for f in rep["flags"])
      and any("interest-bearing" in f for f in rep["flags"])
      and not any("unrecognized" in f for f in rep["flags"]))
check("named review extensions keep REVIEW class (flag, pass)",
      rep["passed"] and not rep["fails"]
      and rep["instant"]["token2022_extensions"]
      == ["memoTransfer", "pausable", "scaledUiAmount",
          "interestBearingConfig"])
rep = run("unknown future extension", make_rpc(
    holders=ONE_HOLDER,
    extensions=[ext("quantumTransfer", {})]))
check("unknown future extension flagged, still passes (REVIEW)",
      rep["passed"] and rep["score"] == 90
      and any("quantumTransfer" in f and "unrecognized" in f
              for f in rep["flags"]))
check("unknown extension name still recorded",
      "quantumTransfer" in rep["instant"]["token2022_extensions"])

# --- malformed-TLV entries: REVIEW flag, parseable trap still fails --
rep = run("malformed TLV entries", make_rpc(
    holders=ONE_HOLDER,
    extensions=["junk", None, {"state": {}},
                {"extension": 7},
                {"extension": "transferHook", "state": "junk"}]))
check("malformed entries flagged for review, hook still fails",
      not rep["passed"]
      and any("transfer hook" in f for f in rep["fails"])
      and any("entry unreadable" in f for f in rep["flags"])
      and rep["score"] == 65)

# --- incomplete account: extensions data not a list ------------------
rep = run("extensions not a list", make_rpc(
    holders=ONE_HOLDER, extensions="junk"))
check("non-list extensions flagged for review (score 90)",
      rep["passed"] and rep["score"] == 90
      and any("extensions data unreadable" in f
              for f in rep["flags"]))

# --- partially decoded: fee / default-state unreadable ---------------
rep = run("transfer fee unreadable", make_rpc(
    holders=ONE_HOLDER,
    extensions=[ext("transferFeeConfig", {})]))
check("transferFeeConfig with no parseable bps flagged (score 90)",
      rep["passed"] and rep["score"] == 90
      and any("transfer fee unreadable" in f for f in rep["flags"]))
rep = run("default state unreadable", make_rpc(
    holders=ONE_HOLDER,
    extensions=[ext("defaultAccountState", {})]))
check("defaultAccountState with no state flagged (score 90)",
      rep["passed"] and rep["score"] == 90
      and any("default account state unreadable" in f
              for f in rep["flags"]))

# --- unknown verdicts are NEVER cached -------------------------------
# Dead RPC -> unknown; then a good RPC with NO cache clear must
# reach a verdict (the unknown did not block the confirmed read).
ins._cache.clear()
orig = ins._rpc
try:
    ins._rpc = lambda m, p, t: None
    rep1 = ins.check(MINT, timeout=6)
    check("dead RPC returns unknown", rep1["unknown"] is True)
    check("unknown verdict is not cached",
          MINT not in ins._cache)
    counter = [0]
    ins._rpc = make_rpc(holders=ONE_HOLDER,
                        extensions=[ext("metadataPointer", {})],
                        counter=counter)
    rep2 = ins.check(MINT, timeout=6)
    check("confirmed data retrievable after an unknown (not blocked)",
          rep2["unknown"] is False and rep2["passed"] is True)
    # ... and the reached verdict IS cached (no further RPC calls).
    calls_after_reach = counter[0]
    rep3 = ins.check(MINT, timeout=6)
    check("reached verdict is cached (no repeat RPC)",
          rep3["unknown"] is False
          and counter[0] == calls_after_reach)
finally:
    ins._rpc = orig
    ins._cache.clear()

print("\n%d checks, %d failed" % (TOTAL[0], len(FAILS)))
if FAILS:
    print("FAILED: %s" % FAILS)
    sys.exit(1)
print("ALL PASS")
