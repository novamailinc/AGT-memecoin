#!/usr/bin/env python3
"""Slippage-gate tests (2026-11-23).

Locks the risk_controls.check_slippage contract: fills.py's declared
MAX_SLIPPAGE_PCT is enforced at the fill whenever liquidity is known,
AGT_RISK_MAX_SLIPPAGE_PCT can only TIGHTEN it, a set-but-invalid value
or a set-but-unknown-liquidity input fails CLOSED, and every entry
lane wires the gate at its fill. Entry/account gating (daily loss,
drawdown, exposure) belongs to paper_account's owner-config gates and
is intentionally not duplicated here.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from daemon.launch_scanner import risk_controls as RC  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---------- declared cap always enforced when liquidity is known ----------
ok, why, cap = RC.check_slippage(1.0, 3.0, env={})
check("declared fills cap enforced with env unset "
      "(slippage >= 25% blocks)", not ok and cap == 25.0)
ok, why, cap = RC.check_slippage(1.0, 10000.0, env={})
check("deep liquidity passes the declared cap", ok and cap == 25.0)
ok, why, cap = RC.check_slippage(1.0, 4.0, env={})
check("exactly at the declared cap blocks (>= cap)", not ok)

# ---------- env tighten-only ----------
env = {"AGT_RISK_MAX_SLIPPAGE_PCT": "1"}
ok, why, cap = RC.check_slippage(0.10, 30.0, env=env)
check("env cap tightens the declared cap", cap == 1.0)
ok, why, cap = RC.check_slippage(0.10, 5.0, env=env)
check("tightened env cap blocks what the declared cap would allow",
      not ok and "slippage" in why)
env = {"AGT_RISK_MAX_SLIPPAGE_PCT": "50"}
ok, why, cap = RC.check_slippage(0.10, 30.0, env=env)
check("env cannot loosen the declared cap", cap == 25.0)
ok, why, cap = RC.check_slippage(1.0, 3.0, env=env)
check("looser env value: declared cap still blocks", not ok)

# ---------- fail closed on invalid config / unknown input ----------
for bad in ("abc", "0", "-1", "nan"):
    ok, why, cap = RC.check_slippage(
        0.10, 30.0, env={"AGT_RISK_MAX_SLIPPAGE_PCT": bad})
    check("invalid env %r fails closed" % bad,
          not ok and "invalid" in why)
env = {"AGT_RISK_MAX_SLIPPAGE_PCT": "5"}
ok, why, cap = RC.check_slippage(1.0, None, env=env)
check("unknown liquidity with env set fails closed",
      not ok and "liquidity unknown" in why)
ok, why, cap = RC.check_slippage(1.0, 0, env=env)
check("zero liquidity with env set fails closed", not ok)

# ---------- unset env + unknown liquidity passes ----------
ok, why, cap = RC.check_slippage(1.0, None, env={})
check("unknown liquidity with env unset passes "
      "(lane's own handling stands)", ok)

# ---------- never raises on garbage ----------
try:
    RC.check_slippage("x", "y", env={})
    RC.check_slippage(None, None, env={"AGT_RISK_MAX_SLIPPAGE_PCT": None})
    check("slippage gate never raises on garbage", True)
except Exception as e:
    check("slippage gate never raises on garbage (%r)" % e, False)

# ---------- lane wiring lock: every entry lane calls the gate ----------
for lane in ("grad", "watcher_lane", "ceo", "momentum_lane", "mirror"):
    src = open(os.path.join(REPO, "daemon", "launch_scanner",
                            lane + ".py")).read()
    check("%s wires check_slippage at the fill" % lane,
          "_risk.check_slippage(" in src)
    check("%s imports risk_controls" % lane,
          "import risk_controls as _risk" in src)
    check("%s does not duplicate paper_account entry gates" % lane,
          "_risk.check_entry(" not in src)

# ---------- verified-tape liquidity beats card claims at the fill ----
# ceo/mirror fed the slippage gate and honest_entry the CARD's claimed
# liquidity; a claim can be absent (fail-open) or inflated (diluted
# gate). Both lanes now compute the fill liquidity from their own
# gate-verified tape (_LAST_GATE_TAPE) BEFORE the gate call, falling
# back to the claim only when the verified figure is absent
# (2026-11-23, integrator fix on the merged patch).
for lane in ("ceo", "mirror"):
    src = open(os.path.join(REPO, "daemon", "launch_scanner",
                            lane + ".py")).read()
    i_gate = src.find('gate_liq = float((_LAST_GATE_TAPE or {})'
                      '.get("usd_liquidity")')
    check("%s fill prefers gate-verified tape liquidity" % lane,
          i_gate >= 0)
    if i_gate >= 0:
        check("%s verified liquidity precedes the slippage call" % lane,
              src.find("_risk.check_slippage(", i_gate) > i_gate)

print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL RISK-CONTROL TESTS PASSED")
