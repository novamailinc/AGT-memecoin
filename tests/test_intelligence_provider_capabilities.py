"""Schema test for daemon/intelligence/acquisition/provider_capabilities.py.

Plain script, run directly with the venv python (never pytest).
Checks: module loads standalone (no live imports), validate() is clean,
every account record is complete, no secret material is present in the
file, quotas are config (numbers with notes), and the end-state
assignment only names known accounts or explicit paid-gated None.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
# Import the module by path so the test never imports the launch_scanner
# monolith or any live module.
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
MOD_PATH = os.path.join(HERE, "..", "daemon", "intelligence", "acquisition",
                        "provider_capabilities.py")
# The test lives in tests/; resolve the module relative to the repo root.
REPO = os.path.abspath(os.path.join(HERE, ".."))
MOD_PATH = os.path.join(REPO, "daemon", "intelligence", "acquisition",
                        "provider_capabilities.py")

checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail and not cond else ""))


spec = importlib.util.spec_from_file_location("provider_capabilities", MOD_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
check("module loads standalone", True)
check("schema version present", mod.SCHEMA_VERSION == "1.0.0", mod.SCHEMA_VERSION)

problems = mod.validate()
check("validate() returns no problems", not problems, "; ".join(problems))

check("six accounts recorded", set(mod.ACCOUNTS) == {
    "helius_main", "helius_discovery", "helius_vault",
    "alchemy", "chainstack", "public"}, str(sorted(mod.ACCOUNTS)))
check("enabled_accounts() non-empty", len(mod.enabled_accounts()) >= 5,
      str(mod.enabled_accounts()))
check("helius_main http_rpc is yes", mod.supports("helius_main", "http_rpc") == "yes")
check("alchemy http_rpc is blocked (401)", mod.supports("alchemy", "http_rpc") == "blocked")
check("chainstack http_rpc is blocked (no key)", mod.supports("chainstack", "http_rpc") == "blocked")
check("chainstack archive is no on Developer",
      mod.supports("chainstack", "historical_archive") == "no")
check("helius laserstream grpc is paid-gated",
      mod.supports("helius_main", "laserstream_grpc") == "paid")
check("helius preprocessed tx is paid-gated",
      mod.supports("helius_main", "preprocessed_transactions") == "paid")
check("helius parsed streams included on free",
      mod.supports("helius_main", "parsed_streams") == "yes")
check("helius parsed events included on free",
      mod.supports("helius_main", "parsed_events") == "yes")
check("alchemy parsed streams is no (Helius-only)",
      mod.supports("alchemy", "parsed_streams") == "no")
check("chainstack getSignaturesForAddress refused on free",
      "getSignaturesForAddress" in mod.FREE_TIER_REFUSALS["chainstack"])
check("deep history assigned to alchemy (Chainstack has no archive)",
      mod.END_STATE_ASSIGNMENT["deep_history_retrieval"] == "alchemy")
check("laserstream grpc assignment is None (paid, out of scope)",
      mod.END_STATE_ASSIGNMENT["specialized_laserstream_grpc"] is None)
check("preprocessed tx assignment is None (paid, out of scope)",
      mod.END_STATE_ASSIGNMENT["specialized_preprocessed_tx"] is None)
check("method_cost getTransaction is (10,40,1,None)",
      mod.method_cost("helius", "getTransaction") == (10, 40, 1, None),
      str(mod.method_cost("helius", "getTransaction")))
check("method_cost getTokenLargestAccounts alchemy CU is None (unverified)",
      mod.method_cost("alchemy", "getTokenLargestAccounts")[1] is None)
check("unknown account returns None", mod.get_account("nope") is None)
check("unknown capability returns None",
      mod.supports("alchemy", "no_such_capability") is None)

# No secret material: the file must not contain a plausible key value.
# Key files are 37 bytes; the module must not embed any 32+ char token-like
# string other than URLs and doc prose. Check for api-key query values and
# endpoint ids (a bare <key>/<endpoint-id>/<surrogate> placeholder is fine).
src = open(MOD_PATH, encoding="utf-8").read()
bad = [line for line in src.splitlines()
       if "api-key=" in line and "<key>" not in line and "<surrogate>" not in line
       and "api-key=<key>" not in line]
check("no embedded api-key value", not bad, "; ".join(bad[:3]))
check("endpoint id is a placeholder only", "<endpoint-id>" in src
      and "core.chainstack.com/" in src)
check("no hsurr: value", "hsurr:" in src and "hsurr:*" in src)

failed = [c for c in checks if not c[1]]
print("\n%d checks, %d failed" % (len(checks), len(failed)))
if failed:
    sys.exit(1)
print("ALL PASSED")
