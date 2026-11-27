"""STATIC census (SOURCE ONLY, no import of project code, no execution): every PRODUCTION call to account credit/write_down passes BOTH
expected_account_id and entry_op_id, and every production try_debit call site is followed, in the same function, by an entry_binding call
(the thread-local capture is same-thread). A site that cannot be proven fails. Negative control: a synthetic unbound call is detected."""
import ast, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ["daemon/launch_scanner/momentum_lane.py", "daemon/launch_scanner/mirror.py", "daemon/launch_scanner/ceo.py", "daemon/launch_scanner/watcher_lane.py",
         "daemon/launch_scanner/grad.py", "broker/memecoin/shared_capital.py"]
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n)
    if not c: FAILS.append(n)
def unbound_settle_calls(src):
    bad = []; n = 0
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("credit", "write_down"):
            n += 1
            kw = {k.arg for k in node.keywords}
            if not ({"expected_account_id", "entry_op_id"} <= kw): bad.append(node.lineno)
    return n, bad
def entry_sites_without_binding(src):
    bad = []; n = 0
    for fn in ast.walk(ast.parse(src)):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)]
            td = [c for c in calls if c.func.attr == "try_debit"]
            eb = [c for c in calls if c.func.attr == "entry_binding"]
            if td:
                n += len(td)
                if not eb or min(c.lineno for c in eb) < min(c.lineno for c in td): bad.append((fn.name, td[0].lineno))
    return n, bad
tot = 0
for f in FILES:
    src = open(os.path.join(ROOT, f)).read()
    n, bad = unbound_settle_calls(src); tot += n
    check("%s: %d settle call(s), all bound (unbound at %s)" % (f, n, bad), not bad)
    n2, bad2 = entry_sites_without_binding(src)
    check("%s: %d try_debit site(s), each followed by entry_binding (bad %s)" % (f, n2, bad2), not bad2)
check("census saw production settle sites (non-vacuous, %d)" % tot, tot >= 20)
n, bad = unbound_settle_calls("def f(pa):\n    pa.credit('l','m',1.0,'op')\n    pa.write_down('l','m','op2')\n")
check("NEGATIVE CONTROL: synthetic unbound calls detected", n == 2 and len(bad) == 2)
n, bad = entry_sites_without_binding("def g(pa):\n    pa.try_debit('l','m',1,1,'op')\n")
check("NEGATIVE CONTROL: try_debit without entry_binding detected", n == 1 and len(bad) == 1)
print("RESULT: %d failure(s)" % len(FAILS)); sys.exit(1 if FAILS else 0)
