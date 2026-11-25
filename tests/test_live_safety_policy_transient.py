"""Pins retry (transient) semantics of core.live_safety_policy.live_safety_decision when the base safety says not ok.
SYNTHETIC reports. compile-checked only; not executed by the author."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from core.live_safety_policy import live_safety_decision as D
F = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n)
    if not c: F.append(n)
def base(rep, ok=False, why="x", tr=False): return lambda: (ok, why, rep, tr)
r = D(base({"passed": False, "fails": ["f"]}))
check("hard fail with no unknown key is refused and NOT transient", r[0] is False and r[3] is False)
r = D(base({"passed": False, "fails": ["f"], "unknown": False}))
check("hard fail with unknown False is refused and NOT transient", r[0] is False and r[3] is False)
r = D(base({"unknown": True}, why="safety unknown"))
check("explicit unknown True is refused and transient", r[0] is False and r[3] is True and "unknown" in r[1])
r = D(base({"passed": False, "unknown": "yes"}))
check("non-bool unknown value is treated as unknown (transient)", r[0] is False and r[3] is True)
r = D(base({"passed": False, "unknown": None}))
check("unknown None present is treated as unknown (transient)", r[0] is False and r[3] is True)
check("why text of base is preserved", D(base({"passed": False, "fails": ["mint authority live"]}, why="mint authority live"))[1] == "mint authority live")
r = D(lambda: (_ for _ in ()).throw(RuntimeError("x")))
check("exception refuses and is transient", r[0] is False and r[3] is True and r[2] is None)
r = D(base(None, ok=True))
check("ok True with no report refused, transient", r[0] is False and r[3] is True)
print("FAILS: %d" % len(F)); sys.exit(1 if F else 0)
