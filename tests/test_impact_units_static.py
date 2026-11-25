"""Static (AST only, imports no project code) checks for the impact-unit change. SOURCE ONLY, WRITTEN NOT RUN. run: python3 tests/test_impact_units_static.py"""
import ast, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = 0
def check(n, ok, d=""):
    global FAILS
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else " | " + str(d)), flush=True); FAILS += (not ok)
for rel in ("broker/memecoin/execution/sim_executor.py", "broker/memecoin/execution/mainnet_executor.py"):
    t = ast.parse(open(os.path.join(ROOT, rel)).read())
    sites = 0
    for fn in [n for n in ast.walk(t) if isinstance(n, ast.FunctionDef)]:
        keys = [n for n in ast.walk(fn) if isinstance(n, ast.Constant) and n.value == "price_impact_raw"]
        if not keys: continue
        assigned = sorted(n.lineno for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == "quote" and isinstance(n.ctx, ast.Store))
        sites += len(keys)
        check("%s %s: `quote` assigned before every price_impact_raw site" % (rel, fn.name), bool(assigned) and all(assigned[0] < k.lineno for k in keys), (assigned, [k.lineno for k in keys]))
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "quote_price_impact_pct"]
        check("%s %s: impact parsed via quote_price_impact_pct" % (rel, fn.name), len(calls) == 1, len(calls))
        check("%s %s: no remaining raw float(quote[priceImpactPct])" % (rel, fn.name), "float(quote.get(\"priceImpactPct\"" not in ast.get_source_segment(open(os.path.join(ROOT, rel)).read(), fn))
    check("%s: result sites found (sim 1, mainnet 2)" % rel, sites == (1 if "sim_" in rel else 2), sites)
print("FAILS: %d" % FAILS); sys.exit(1 if FAILS else 0)
