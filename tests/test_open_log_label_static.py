#!/usr/bin/env python3
"""Static source check (no product import, no execution of lane code): the OPEN log lines of momentum_lane.py and ceo.py must not label the lane NOTIONAL bankroll counter as cash; they must say NOTIONAL counter and name the shared paper account as the real cash, as grad.py and watcher_lane.py already do. Parses with ast; reads files only."""
import ast, os, sys
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "daemon", "launch_scanner")
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n, flush=True)
    if not c: FAILS.append(n)
def fmt_of(path, head):
    t = ast.parse(open(os.path.join(ROOT, path)).read())
    out = []
    for n in ast.walk(t):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "warning" and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str) and n.args[0].value.startswith(head):
            out.append(n.args[0].value)
    return out
for path, head in (("momentum_lane.py", "momentum OPEN"), ("ceo.py", "ceo OPEN"), ("grad.py", "grad OPEN"), ("watcher_lane.py", "watcher OPEN")):
    f = fmt_of(path, head)
    check("%s: exactly one %r log format found (%d)" % (path, head, len(f)), len(f) == 1)
    if len(f) != 1: continue
    check("%s: OPEN log does not call the lane counter cash (%r)" % (path, f[0]), "cash now" not in f[0])
    check("%s: OPEN log says NOTIONAL counter and names the shared paper account as real cash" % path, "NOTIONAL counter now" in f[0] and "real cash is the shared paper account" in f[0])
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
