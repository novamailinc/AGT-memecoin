"""Tranche-protect + raw-tight test lanes (2026-10-31, CEO diagnosis).

Plain script: run with the repo venv python (never pytest). Uses a temp
FOREX_AGENT_HOME so no live ledger, killswitch state, or verdict chain
is ever touched. Covers: grad.py _effective_stop (default / protect /
override), both variant modules' engine flags + lane constants, and
killswitch + supervisor + watchdog registration for both lanes.
"""

import importlib.util
import os
import sys
import tempfile

os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="variants-test-")

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


# ---- grad.py _effective_stop ----
G = load("grad_mod", os.path.join(_LS, "grad.py"))
check(G.HARD_STOP_MULT == 0.50, "grad default HARD_STOP_MULT 0.50")
check(G.PROTECT_AFTER_A is False, "grad default PROTECT_AFTER_A False")
pos_open = {"legs": {"a": {"done": False}, "b": {"done": False}}}
pos_banked = {"legs": {"a": {"done": True}, "b": {"done": False}}}
check(G._effective_stop(pos_open) == (0.50, "hard_stop_-50pct"),
      "default stop: 0.50 / hard_stop_-50pct (leg A open)")
check(G._effective_stop(pos_banked) == (0.50, "hard_stop_-50pct"),
      "default stop unchanged after leg A banks (protect off)")
check(G._effective_stop({}) == (0.50, "hard_stop_-50pct"),
      "default stop on malformed position (fail-safe)")
G.PROTECT_AFTER_A = True
check(G._effective_stop(pos_banked) == (1.0, "hard_stop_protect_be"),
      "protect on + leg A banked: 1.0 / hard_stop_protect_be")
check(G._effective_stop(pos_open) == (0.50, "hard_stop_-50pct"),
      "protect on + leg A open: still -50%")
G.PROTECT_AFTER_A = False
G.HARD_STOP_MULT = 0.75
check(G._effective_stop(pos_open) == (0.75, "hard_stop_-25pct"),
      "override 0.75: hard_stop_-25pct")
check(G._effective_stop(pos_banked) == (0.75, "hard_stop_-25pct"),
      "override 0.75 wins over banked leg A (protect off)")

# ---- tranche_protect module ----
TP = load("tranche_protect_mod", os.path.join(_LS, "tranche_protect.py"))
check(TP.LANE == "tranche_protect", "tranche_protect LANE")
check(TP.LEDGER_PATH.endswith("tranche_protect_ledger.json"),
      "tranche_protect ledger path")
check(TP.LEG_A_FRAC == 0.25 and TP.LEG_A_TARGET == 1.5,
      "tranche_protect keeps tranche legs 25%@1.5x")
check(TP.NOTIONAL_USD == 25.00 and TP.BANKROLL_START == 500.00,
      "tranche_protect keeps $25 stake / $500 bankroll")
eng = TP.make_engine()
check(eng.LANE == "tranche_protect", "tranche_protect engine lane")
check(eng.PROTECT_AFTER_A is True, "tranche_protect engine PROTECT_AFTER_A")
check(eng.HARD_STOP_MULT == 0.50, "tranche_protect engine stop still 0.50 base")
check(eng.LEDGER_PATH.endswith("tranche_protect_ledger.json"),
      "tranche_protect engine ledger path")
pre = TP._preregistration()
check(pre["lane"] == "tranche_protect", "tranche_protect prereg lane")
check("tranche_protect" in pre["rule"], "tranche_protect prereg rule names lane")
check("PROTECT" in pre["terms"]["ladder"], "tranche_protect prereg ladder names protect")

# ---- raw_tight module ----
RT = load("raw_tight_mod", os.path.join(_LS, "raw_tight.py"))
check(RT.LANE == "raw_tight", "raw_tight LANE")
check(RT.LEDGER_PATH.endswith("raw_tight_ledger.json"), "raw_tight ledger path")
check(RT.NOTIONAL_USD == 25.00 and RT.BANKROLL_START == 100.00,
      "raw_tight keeps raw $25 stake / $100 bankroll")
eng2 = RT.make_engine()
check(eng2.LANE == "raw_tight", "raw_tight engine lane")
check(eng2.HARD_STOP_MULT == 0.75, "raw_tight engine HARD_STOP_MULT 0.75")
check(eng2.PROTECT_AFTER_A is False, "raw_tight engine protect off")
pre2 = RT._preregistration()
check(pre2["lane"] == "raw_tight", "raw_tight prereg lane")
check("-25%" in pre2["terms"]["ladder"], "raw_tight prereg ladder names -25%")

# ---- killswitch registration ----
KS = load("killswitch_mod", os.path.join(_LS, "killswitch.py"))
for lane in ("tranche_protect", "raw_tight"):
    cfg = KS.CFG.get(lane)
    check(cfg == {"window": 100, "min_trades": 100, "halt": True},
          "killswitch lane %s {100,100}" % lane)
check(KS.CFG.get("tranche") == {"window": 100, "min_trades": 100, "halt": True},
      "live tranche killswitch untouched")
check(KS.CFG.get("grad_raw") == {"window": 100, "min_trades": 100, "halt": True},
      "live grad_raw killswitch untouched")

# ---- supervisor roster + watchdog stanzas (text checks) ----
with open(os.path.join(_ROOT, "scripts", "supervisor")) as f:
    sup = f.read()
check('tranche-protect-trader' in sup, "supervisor roster has tranche-protect-trader")
check('raw-tight-trader' in sup, "supervisor roster has raw-tight-trader")
with open(os.path.join(_ROOT, "scripts", "forex-watchdog")) as f:
    wd = f.read()
check('scripts/tranche-protect-trader' in wd, "watchdog stanza tranche-protect-trader")
check('scripts/raw-tight-trader' in wd, "watchdog stanza raw-tight-trader")

print()
if FAILS:
    print("FAILURES: %d" % len(FAILS))
    for f in FAILS:
        print(" - " + f)
    sys.exit(1)
print("ALL VARIANT CHECKS PASSED")
