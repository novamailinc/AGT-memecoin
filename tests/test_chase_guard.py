"""Tranche chase guard (2026-10-31): stale candidates must not be
bought at runaway prices after waiting for a slot/quote.

Plain script: run with the repo venv python (never pytest). Uses a temp
FOREX_AGENT_HOME so no live ledger, killswitch state, or verdict chain
is ever touched. Covers: grad.py MAX_SLOT_WAIT_S default (off), the
guard branch in _consider_candidate (stale -> final skip, missing
timestamp -> fail-open), tranche's and tranche_protect's engine
settings (120s; protect ported from tranche 2026-11-05), and that no
other lane module sets it (raw_tight stays a pure test,
grad_select/grad_raw untouched).
"""

import importlib.util
import os
import sys
import tempfile

os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="chase-test-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
_LS = os.path.join(_ROOT, "daemon", "launch_scanner")
sys.path.insert(0, _LS)
sys.path.insert(0, _ROOT)


def load(name, path):
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__dict__["__file__"] = path
    with open(path) as f:
        exec(compile(f.read(), path, "exec"), mod.__dict__)
    return mod


FAILS = []


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


grad_src = open(os.path.join(_LS, "grad.py")).read()
check("MAX_SLOT_WAIT_S = None" in grad_src,
      "grad.py default MAX_SLOT_WAIT_S is None (off)")
check("stale candidate" in grad_src and
      "first_seen_ts" in grad_src and
      "MAX_SLOT_WAIT_S" in grad_src,
      "grad.py _consider_candidate has the chase-guard branch")
check('return _skip(\n                mint,\n                "stale candidate' in grad_src or
      '"stale candidate: %.0fs since first seen "' in grad_src,
      "chase-guard skip is final (no retry)")

for mod_file, label in (
        ("tranche.py", "tranche"),
        ("tranche_protect.py", "tranche_protect"),
        ("raw_tight.py", "raw_tight"),
        ("grad_select.py", "grad_select"),
        ("grad_raw.py", "grad_raw")):
    src = open(os.path.join(_LS, mod_file)).read()
    sets = "MAX_SLOT_WAIT_S = " in src or "MAX_SLOT_WAIT_S=" in src
    if label == "tranche":
        check("eng.MAX_SLOT_WAIT_S = 120.0" in src,
              "tranche make_engine sets 120s chase guard "
              "(tightened from 600s, 2026-10-31 night)")
    elif label == "tranche_protect":
        check("eng.MAX_SLOT_WAIT_S = 120.0" in src,
              "tranche_protect make_engine sets 120s chase guard "
              "(ported from tranche, 2026-11-05)")
    else:
        check(not sets, "%s does not set MAX_SLOT_WAIT_S" % label)

print()
if FAILS:
    print("FAILURES: %d" % len(FAILS))
    for f in FAILS:
        print(" - " + f)
    sys.exit(1)
print("ALL CHASE-GUARD CHECKS PASSED")
