#!/usr/bin/env python3
"""UNRUN draft. PER-MINT validation of Jupiter observed prices at grad's source boundary (_validate_jup_observed). Pure function, no network, no process or thread.
Why: a malformed entry for ONE mint used to raise inside the dict comprehension in _poll_exits, discarding every mint's Jupiter price (blast radius), and a NaN/inf/
negative/bool/str price could become q["px"] (a NaN price makes every stop comparison false). Run (producer wrapper only): python3 tests/test_grad_price_obs_validate.py"""
if __name__ != "__main__":
    import pytest
    pytest.skip("script-style test", allow_module_level=True)
import os, sys, math, subprocess, threading
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import tests._guard_fixture as GF
GF.block_network()   # BEFORE any repo import
STARTS = []
def _no(*a, **k):
    STARTS.append(1); raise RuntimeError("process/thread start forbidden")
subprocess.Popen = _no; subprocess.run = _no; threading.Thread.start = _no
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n)
    if not c: FAILS.append(n)
from daemon.launch_scanner import grad as G
V = G._validate_jup_observed
NOW = 1_700_000_000.0
good = V(["A", "B"], {"A": (1.5, NOW), "B": (2, NOW - 3)})
check("V1 valid entries pass through unchanged (prices %s, obs %s, rej %s)" % good, good == ({"A": 1.5, "B": 2.0}, {"A": NOW, "B": NOW - 3}, {}))
check("V1b an int price is returned as float", isinstance(good[0]["B"], float))
for lab, bad in (("nan", float("nan")), ("inf", float("inf")), ("neg_inf", float("-inf")), ("zero", 0.0), ("negative", -1.0), ("bool", True), ("false", False), ("str", "1.5"), ("none", None), ("list", [1.5]), ("dict", {"p": 1})):
    px, ob, rj = V(["A", "B"], {"A": (bad, NOW), "B": (2.0, NOW)})
    check("V2 %s price for ONE mint rejects only that mint (prices %s, rej %s)" % (lab, px, rj), "A" not in px and px.get("B") == 2.0 and list(rj.values()) == ["bad_price"])
for lab, bad in (("none", None), ("scalar", 1.5), ("short", (1.5,)), ("long", (1.5, NOW, 1)), ("str", "ab")):
    px, ob, rj = V(["A", "B"], {"A": bad, "B": (2.0, NOW)})
    check("V3 %s value shape rejects only that mint (prices %s, rej %s)" % (lab, px, rj), "A" not in px and px.get("B") == 2.0 and list(rj.values()) == ["bad_shape"])
px, ob, rj = V(["A"], {"A": (1.0, NOW), "ZZZ": (9.0, NOW)})
check("V4 an UNREQUESTED mint in the result is dropped, never used (%s %s)" % (px, rj), px == {"A": 1.0} and list(rj.values()) == ["unrequested_mint"])
for lab, raw in (("none", None), ("empty", {})):
    r = V(["A"], raw)
    check("V5 %s result -> nothing accepted, nothing rejected, no raise (%s)" % (lab, r), r == ({}, {}, {}))
r = V(["A"], [("A", (1.0, NOW))])
check("V5b a non-dict result is rejected as a whole and never raises (%s)" % (r,), r[0] == {} and r[2] == {"*": "not_a_dict"})
r = V(None, {"A": (1.0, NOW)})
check("V5c no requested mints -> every returned entry is unrequested (%s)" % (r,), r[0] == {} and list(r[2].values()) == ["unrequested_mint"])
class _Weird:
    def __iter__(self): raise RuntimeError("boom")
    def __len__(self): raise RuntimeError("boom")
r = V(["A", "B"], {"A": _Weird(), "B": (2.0, NOW)})
check("V6 a hostile object never raises and does not spoil the other mint (%s)" % (r,), r[0] == {"B": 2.0} and len(r[2]) == 1)
check("V7 the observation ts is passed through untouched for _obs_fields to judge (string stays, validated downstream): %s" % (V(["A"], {"A": (1.0, "yesterday")})[1],), V(["A"], {"A": (1.0, "yesterday")})[1] == {"A": "yesterday"})
check("V8 the reject counter exists and starts a plain dict counter (%s)" % (G._JUP_REJECTS,), isinstance(G._JUP_REJECTS.get("n"), int))
check("T9 no process/thread start attempted", STARTS == [])
print("FAILS: %d" % len(FAILS))
sys.exit(1 if FAILS else 0)
