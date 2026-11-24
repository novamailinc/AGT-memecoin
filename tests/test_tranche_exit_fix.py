"""Tranche exit fix (2026-10-31 night, user order): live tranche ports
the tranche_protect breakeven stop, so a banked position can no longer
retrace to the -50% hard stop.

Plain script: run with the repo venv python (never pytest). Uses a temp
FOREX_AGENT_HOME so no live ledger, killswitch state, or verdict chain
is ever touched. Covers: grad.py PROTECT_AFTER_A default (off),
tranche's engine setting (on), protect's setting (on, unchanged), and
that raw_tight / grad_select / grad_raw do not set it (their exits
stay exactly as their verdicts assume).
"""

import os
import sys

os.environ["FOREX_AGENT_HOME"] = "/tmp/tranche-exit-fix-test"

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
_LS = os.path.join(_ROOT, "daemon", "launch_scanner")

FAILS = []


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


grad_src = open(os.path.join(_LS, "grad.py")).read()
check("PROTECT_AFTER_A = False" in grad_src,
      "grad.py default PROTECT_AFTER_A is False (off)")
check("hard_stop_protect_be" in grad_src,
      "_effective_stop has the breakeven reason")

for mod_file, label, want in (
        ("tranche.py", "tranche", True),
        ("tranche_protect.py", "tranche_protect", True),
        ("raw_tight.py", "raw_tight", False),
        ("grad_select.py", "grad_select", False),
        ("grad_raw.py", "grad_raw", False)):
    src = open(os.path.join(_LS, mod_file)).read()
    sets = "eng.PROTECT_AFTER_A = True" in src
    if want:
        check(sets, "%s sets PROTECT_AFTER_A on" % label)
    else:
        check(not sets, "%s does not set PROTECT_AFTER_A" % label)

print()
if FAILS:
    print("FAILURES: %d" % len(FAILS))
    for f in FAILS:
        print(" - " + f)
    sys.exit(1)
print("ALL TRANCHE-EXIT-FIX CHECKS PASSED")
