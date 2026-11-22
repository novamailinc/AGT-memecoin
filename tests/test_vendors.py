#!/usr/bin/env python3
"""Declared vendor fallback chains (vendors.py).

Covers the CHAINS declaration: documented order for all six
needs, fail contracts, annotate() stamping (no clobber of an
existing same-need stamp, never raises on bad inputs),
chain_for() returning a copy, and validate() on the shipped
chains plus deliberately broken chains.

No network, no live ledgers. Plain script — run directly with
the venv python, not pytest.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import copy
import os
import sys

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import vendors as V  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


EXPECTED_ORDER = {
    "curve_reads": ["curve_cache", "helius_rpc", "public_rpc"],
    # LIVE order, reconciled 2026-11-21 (see vendors.py): entry
    # quotes are DexScreener pairs -> tape -> one DS retry;
    # exit prices start at the caller price_fn; copycat reads
    # the local tally first; canonical verification never uses GT.
    "quotes": ["dex_screener", "tape_fallback", "dex_screener_retry"],
    "exit_prices": ["caller_price_fn", "jupiter_v3",
                    "helius_reserves", "ds_gt", "f2_last_real_price"],
    "copycat_tally": ["local_recorder", "dex_screener"],
    "canonical_pool": ["migration_event", "recorder_migration_match",
                       "chain_fallback"],
    "deployer_launches": ["local_recorder",
                          "curve_oldest_tx_creator"],
}

EXPECTED_FAIL = {
    "curve_reads": "closed",
    "quotes": "open",
    "exit_prices": "closed",
    "copycat_tally": "closed",
    "canonical_pool": "closed",
    "deployer_launches": "closed",
}

# ---------------- chain_for: documented order ----------------------
for need, order in EXPECTED_ORDER.items():
    got = [s["source"] for s in V.chain_for(need)]
    check("chain_for %s order is %s" % (need, order), got == order)

# Every step carries the documented shape.
for need in EXPECTED_ORDER:
    ok = True
    for step in V.chain_for(need):
        if set(step) != {"source", "cost", "timeout_s", "fail"}:
            ok = False
        if step["cost"] not in ("free", "rpc"):
            ok = False
        if not isinstance(step["timeout_s"], (int, float)) \
                or step["timeout_s"] < 0:
            ok = False
    check("chain_for %s steps are well-formed" % need, ok)

# Unknown need: empty chain, never raises.
check("chain_for unknown need returns []",
      V.chain_for("no_such_need") == [])
check("chain_for None returns []", V.chain_for(None) == [])

# ---------------- fail contracts -----------------------------------
for need, contract in EXPECTED_FAIL.items():
    check("fail_contract %s is %s" % (need, contract),
          V.fail_contract(need) == contract)
    steps_ok = all(s["fail"] == contract
                   for s in V.chain_for(need))
    check("chain %s every step fail == %s" % (need, contract),
          steps_ok)
check("fail_contract unknown need fails closed",
      V.fail_contract("no_such_need") == "closed")
check("fail_contract None fails closed",
      V.fail_contract(None) == "closed")

# ---------------- annotate -----------------------------------------
# vendor_chain is keyed by need (reconciled 2026-11-21): the four
# select needs coexist in one candidate's values dict.
vals = {}
out = V.annotate(vals, "curve_reads", "helius_rpc")
check("annotate returns the same dict", out is vals)
check("annotate stamps vendor_chain keyed by need",
      vals.get("vendor_chain") == {
          "curve_reads": {
              "chain": ["curve_cache", "helius_rpc", "public_rpc"],
              "used": "helius_rpc"}})

# Same need: an existing stamp is never overwritten.
vals2 = {"vendor_chain": {"quotes":
                          {"chain": ["dex_screener", "tape_fallback",
                                     "dex_screener_retry"],
                           "used": "dex_screener"}}}
V.annotate(vals2, "quotes", "tape_fallback")
check("annotate does not clobber same-need stamp",
      vals2["vendor_chain"]["quotes"]["used"] == "dex_screener")

# Different need: a new stamp coexists, the old one survives.
vals3 = {"vendor_chain": {"quotes":
                          {"chain": ["dex_screener"],
                           "used": "dex_screener"}}}
V.annotate(vals3, "curve_reads", "public_rpc")
check("annotate keeps a different-need stamp alongside",
      vals3["vendor_chain"]["quotes"]["used"] == "dex_screener"
      and vals3["vendor_chain"]["curve_reads"]["used"] == "public_rpc")

# Never raises on None / empty / non-dict inputs.
for bad in (None, {}, [], "not-a-dict", 0):
    try:
        r = V.annotate(bad, "quotes", "dex_screener")
        ok = True
    except Exception:
        ok = False
    check("annotate never raises on %r" % (bad,), ok)
check("annotate None returns None",
      V.annotate(None, "quotes", "dex_screener") is None)
check("annotate empty dict stamps",
      V.annotate({}, "exit_prices", "f2_last_real_price")
      ["vendor_chain"]["exit_prices"]["used"] == "f2_last_real_price")
try:
    V.annotate({}, None, None)
    ok_none = True
except Exception:
    ok_none = False
check("annotate never raises on None need/source", ok_none)

# ---------------- chain_for returns a copy -------------------------
first = V.chain_for("quotes")
first.append({"source": "forged", "cost": "free", "timeout_s": 1,
              "fail": "open"})
first[0]["source"] = "forged"
first[0]["timeout_s"] = 999
check("chain_for list mutation does not change CHAINS",
      [s["source"] for s in V.chain_for("quotes")]
      == EXPECTED_ORDER["quotes"])
check("chain_for step-dict mutation does not change CHAINS",
      V.chain_for("quotes")[0] == {
          "source": "dex_screener", "cost": "free", "timeout_s": 12,
          "fail": "open"})
check("chain_for returns a new list each call",
      V.chain_for("quotes") is not V.chain_for("quotes"))
check("CHAINS itself still validates after mutation attempts",
      V.validate() == [])

# ---------------- validate -----------------------------------------
check("validate() returns [] on shipped CHAINS",
      V.validate() == [])
check("validate(CHAINS) returns []",
      V.validate(V.CHAINS) == [])

broken = copy.deepcopy(V.CHAINS)
broken["quotes"] = []
problems = V.validate(broken)
check("validate flags an empty chain",
      len(problems) >= 1
      and any("quotes" in p and "empty" in p for p in problems))

broken = copy.deepcopy(V.CHAINS)
broken["curve_reads"].append(
    {"source": "helius_rpc", "cost": "rpc", "timeout_s": 12,
     "fail": "closed"})
problems = V.validate(broken)
check("validate flags a duplicate source",
      any("curve_reads" in p and "duplicate" in p
          and "helius_rpc" in p for p in problems))

broken = copy.deepcopy(V.CHAINS)
broken["copycat_tally"][0]["fail"] = "open"
problems = V.validate(broken)
check("validate flags a wrong fail contract",
      any("copycat_tally" in p and "local_recorder" in p
          and "open" in p for p in problems))

broken = copy.deepcopy(V.CHAINS)
broken["exit_prices"][1]["fail"] = "open"
problems = V.validate(broken)
check("validate flags a wrong fail on exit_prices",
      any("exit_prices" in p and "jupiter_v3" in p
          for p in problems))

broken = copy.deepcopy(V.CHAINS)
broken["mystery_need"] = [
    {"source": "x", "cost": "free", "timeout_s": 1,
     "fail": "closed"}]
problems = V.validate(broken)
check("validate flags an unknown need",
      any("mystery_need" in p for p in problems))

check("validate never raises on garbage input",
      isinstance(V.validate("not-a-dict"), list)
      and len(V.validate("not-a-dict")) >= 1)
check("validate never raises on None input (means shipped)",
      V.validate(None) == [])

print("")
if fails:
    print("FAILURES: %d — %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("ALL VENDOR TESTS PASSED")
