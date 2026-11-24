#!/usr/bin/env python3
"""Section 4.5 provider-health decision gate (2026-11-23).

Operational memory informs the decision engine: with
PROVIDER_HEALTH_GATE_ENABLED=1 (default OFF, owner-activated), every
shared-account entry debit consults the durable acquisition
provider-health record first. Incomplete enabled config, missing or
stale records, and any listed provider not "ok" all fail CLOSED -
the bot stops treating uncertain market information as reliable.
The gate never raises and never touches exits.

Run: python3 tests/test_provider_health_gate.py
"""
import importlib
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests._guard_fixture as _gfb; _gfb.block_network()  # no network in tests (mandatory-guard migration, disclosed)

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)
        raise AssertionError(name)


def fresh(home, enabled=None, providers=None, mode=None):
    os.environ["FOREX_AGENT_HOME"] = home
    os.environ["PAPER_ACCOUNT_START"] = "10.0"
    # mandatory-guard migration (disclosed): account gates can no longer be
    # OFF; fixture limits are installed below so only the provider gate varies.
    if enabled:
        os.environ["PROVIDER_HEALTH_GATE_ENABLED"] = "1"
    else:
        os.environ.pop("PROVIDER_HEALTH_GATE_ENABLED", None)
    if providers is None:
        os.environ.pop("PROVIDER_HEALTH_GATE_PROVIDERS", None)
    else:
        os.environ["PROVIDER_HEALTH_GATE_PROVIDERS"] = providers
    if mode is None:
        os.environ.pop("PROVIDER_HEALTH_GATE_MODE", None)
    else:
        os.environ["PROVIDER_HEALTH_GATE_MODE"] = mode
    import daemon.launch_scanner.paper_account as pa
    import tests._guard_fixture as _gf
    return _gf.install(importlib.reload(pa))


def write_health(home, statuses, age_s=0.0):
    """Write a durable health file whose providers land on the given
    statuses. statuses: {name: "ok"|"degraded"|"down"|"unconfigured"}."""
    providers = {}
    for name, st in statuses.items():
        rec = {"configured": st != "unconfigured",
               "calls": 10, "errors": 0, "rpc_429s": 0,
               "wss_handshake_429s": 0, "timeouts": 0,
               "monthly_quota_down": False}
        if st == "degraded":
            rec["errors"] = 3  # error_rate 0.3 >= 0.2 -> degraded
        elif st == "down":
            rec["errors"] = 10  # calls >= 5 and error_rate 1.0 -> down
        providers[name] = rec
    path = os.path.join(home, "acquisition_provider_health.json")
    with open(path, "w") as fh:
        json.dump({"providers": providers,
                   "ts": time.time() - age_s}, fh)


def debit(pa, op="op1"):
    return pa.try_debit("watcher", "Mint111", 1.0, 100.0, op)


# ---------------- gate OFF by default: no behavior change ------------
home = tempfile.mkdtemp(prefix="phgate-off-")
pa = fresh(home)
ok, why, pid = debit(pa)
check("gate disabled (default): entry unaffected", ok and pid)

# ---------------- enabled, no provider list: fail closed -------------
home = tempfile.mkdtemp(prefix="phgate-nocfg-")
pa = fresh(home, enabled=True, providers=None)
ok, why, pid = debit(pa)
check("enabled with no provider list fails closed", not ok
      and "incomplete configuration" in why)

# ---------------- enabled, no health file: fail closed ---------------
home = tempfile.mkdtemp(prefix="phgate-nofile-")
pa = fresh(home, enabled=True, providers="alchemy")
ok, why, pid = debit(pa)
check("enabled with no health record fails closed (unknown)",
      not ok and "no durable provider-health record" in why)

# ---------------- corrupt health file: fail closed, no raise ---------
home = tempfile.mkdtemp(prefix="phgate-corrupt-")
with open(os.path.join(home, "acquisition_provider_health.json"),
          "w") as fh:
    fh.write("{not json")
pa = fresh(home, enabled=True, providers="alchemy")
ok, why, pid = debit(pa)
check("corrupt health record fails closed, never raises",
      not ok and "no durable provider-health record" in why)

# ---------------- stale health file: fail closed ---------------------
home = tempfile.mkdtemp(prefix="phgate-stale-")
write_health(home, {"alchemy": "ok"}, age_s=3600)
pa = fresh(home, enabled=True, providers="alchemy")
ok, why, pid = debit(pa)
check("stale health record fails closed", not ok and "stale" in why)

# ---------------- provider ok: entry allowed -------------------------
home = tempfile.mkdtemp(prefix="phgate-ok-")
write_health(home, {"alchemy": "ok", "chainstack": "ok"})
pa = fresh(home, enabled=True, providers="alchemy, chainstack")
ok, why, pid = debit(pa)
check("listed providers ok: entry allowed", ok and pid)

# ---------------- provider degraded: fail closed ---------------------
home = tempfile.mkdtemp(prefix="phgate-deg-")
write_health(home, {"alchemy": "degraded"})
pa = fresh(home, enabled=True, providers="alchemy")
ok, why, pid = debit(pa)
check("degraded provider blocks entry (uncertain != reliable)",
      not ok and "alchemy degraded" in why)

# ---------------- provider down: fail closed -------------------------
home = tempfile.mkdtemp(prefix="phgate-down-")
write_health(home, {"alchemy": "down"})
pa = fresh(home, enabled=True, providers="alchemy")
ok, why, pid = debit(pa)
check("down provider blocks entry", not ok and "alchemy down" in why)

# ---------------- provider unconfigured: fail closed -----------------
home = tempfile.mkdtemp(prefix="phgate-uncfg-")
write_health(home, {"alchemy": "unconfigured"})
pa = fresh(home, enabled=True, providers="alchemy")
ok, why, pid = debit(pa)
check("unconfigured provider blocks entry",
      not ok and "alchemy unconfigured" in why)

# ---------------- provider missing from record: fail closed ----------
home = tempfile.mkdtemp(prefix="phgate-missing-")
write_health(home, {"alchemy": "ok"})
pa = fresh(home, enabled=True, providers="alchemy, helius")
ok, why, pid = debit(pa)
check("listed provider absent from record fails closed",
      not ok and "no health record for provider helius" in why)

# ---------------- unlisted provider down: entry still allowed --------
home = tempfile.mkdtemp(prefix="phgate-unlisted-")
write_health(home, {"alchemy": "ok", "helius": "down"})
pa = fresh(home, enabled=True, providers="alchemy")
ok, why, pid = debit(pa)
check("unlisted provider health does not gate", ok and pid)

# ---------------- all_down mode: fallback-chain semantics ------------
# The core distinction: a primary outage absorbed by a healthy
# fallback is NOT missing reliable data.
home = tempfile.mkdtemp(prefix="phgate-ad-fallback-")
write_health(home, {"alchemy": "down", "helius": "ok"})
pa = fresh(home, enabled=True, providers="alchemy, helius",
           mode="all_down")
ok, why, pid = debit(pa)
check("all_down: primary down + healthy fallback allows entry",
      ok and pid)

home = tempfile.mkdtemp(prefix="phgate-ad-all-")
write_health(home, {"alchemy": "down", "helius": "down"})
pa = fresh(home, enabled=True, providers="alchemy, helius",
           mode="all_down")
ok, why, pid = debit(pa)
check("all_down: every provider dark refuses with states",
      not ok and "no named provider known-ok" in why
      and "alchemy=down" in why and "helius=down" in why)

home = tempfile.mkdtemp(prefix="phgate-ad-norec-")
write_health(home, {"alchemy": "ok"})
pa = fresh(home, enabled=True, providers="alchemy, helius",
           mode="all_down")
ok, why, pid = debit(pa)
check("all_down: known-ok primary covers a no-record fallback",
      ok and pid)

home = tempfile.mkdtemp(prefix="phgate-ad-none-")
write_health(home, {"alchemy": "down"})
pa = fresh(home, enabled=True, providers="alchemy, helius",
           mode="all_down")
ok, why, pid = debit(pa)
check("all_down: down + unknown-record refuses (unknown != ok)",
      not ok and "helius=no-record" in why)

home = tempfile.mkdtemp(prefix="phgate-ad-stale-")
write_health(home, {"alchemy": "ok", "helius": "ok"}, age_s=3600)
pa = fresh(home, enabled=True, providers="alchemy, helius",
           mode="all_down")
ok, why, pid = debit(pa)
check("all_down: stale record still fails closed (freshness kept)",
      not ok and "stale" in why)

home = tempfile.mkdtemp(prefix="phgate-ad-corrupt-")
with open(os.path.join(home, "acquisition_provider_health.json"),
          "w") as fh:
    fh.write("{not json")
pa = fresh(home, enabled=True, providers="alchemy", mode="all_down")
ok, why, pid = debit(pa)
check("all_down: unreadable record fails closed",
      not ok and "no durable provider-health record" in why)

home = tempfile.mkdtemp(prefix="phgate-badmode-")
write_health(home, {"alchemy": "ok"})
pa = fresh(home, enabled=True, providers="alchemy", mode="weird")
ok, why, pid = debit(pa)
check("unknown mode fails closed as incomplete configuration",
      not ok and "unknown PROVIDER_HEALTH_GATE_MODE" in why)

# Default mode keeps the original any-down semantics: primary down
# refuses even with a healthy fallback. Documents the distinction.
home = tempfile.mkdtemp(prefix="phgate-anydown-")
write_health(home, {"alchemy": "down", "helius": "ok"})
pa = fresh(home, enabled=True, providers="alchemy, helius")
ok, why, pid = debit(pa)
check("default any_down: primary down refuses despite fallback",
      not ok and "alchemy down" in why)

# ---------------- exits never pass through the gate ------------------
home = tempfile.mkdtemp(prefix="phgate-exit-")
write_health(home, {"alchemy": "ok"})
pa = fresh(home, enabled=True, providers="alchemy")
ok, why, pid = debit(pa)
assert ok, "setup debit failed"
write_health(home, {"alchemy": "down"})
res = pa.credit("watcher", "Mint111", 1.5, "op-exit", pos_id=pid)
check("credit (exit) unaffected when provider goes down",
      res[0] is True)

print()
if FAILS:
    print("FAILURES: %d" % len(FAILS))
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("ALL PROVIDER-HEALTH-GATE TESTS PASSED")
