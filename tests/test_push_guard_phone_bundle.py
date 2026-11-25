"""Packaging guard for tools/phone-test (SOURCE ONLY, WRITTEN NOT RUN; no network, no push, imports push_repo for its pure helpers). run: python3 tests/test_push_guard_phone_bundle.py"""
import os, re, sys, importlib.util, subprocess
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("push_repo", os.path.join(ROOT, "tools", "github-deploy", "push_repo.py"))
pr = importlib.util.module_from_spec(spec); spec.loader.exec_module(pr)
FAILS = 0
def check(n, ok, d=""):
    global FAILS
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else " | " + str(d)), flush=True); FAILS += (not ok)
E = "/x/phone-test/bundle:tools/phone-test"
check("default: --extra into tools/phone-test refused", pr.guard_extras([E], {}) is not None)
check("default: nested dest tools/phone-test/agent refused", pr.guard_extras(["/x:tools/phone-test/agent"], {}) is not None)
check("default: other extras allowed", pr.guard_extras(["/x:tools/scripts/a.py", "/y:tools/phone-testing-notes"], {}) is None)
check("explicit allow env permits (opt-in only)", pr.guard_extras([E], {pr.ALLOW_UNQUALIFIED_ENV: "1"}) is None)
check("allow env must be exactly 1", pr.guard_extras([E], {pr.ALLOW_UNQUALIFIED_ENV: "yes"}) is not None)
d, c, k = pr.guard_mirror(["tools/phone-test/a.py", "tools/other/b.py"], ["tools/phone-test/z.py", "tools/n.py"], {})
check("mirror: remote phone-test never deleted/overwritten; others unaffected", d == ["tools/other/b.py"] and c == ["tools/n.py"] and k == 1, (d, c, k))
d, c, k = pr.guard_mirror(["tools/phone-test/a.py"], [], {pr.ALLOW_UNQUALIFIED_ENV: "1"})
check("mirror: with explicit allow nothing is filtered", d == ["tools/phone-test/a.py"] and k == 0)
sh = open(os.path.join(ROOT, "tools", "github-deploy", "push_all.sh")).read()
check("push_all.sh no longer passes the phone-test extra", "phone-test/bundle" not in sh and "tools/phone-test\"" not in sh)
check("push_all.sh continuation lines intact (no comment inside the exec command)", not re.search(r"\\\n\s*#", sh))
pm = open(os.path.join(ROOT, "tools", "phone-test", "broker", "memecoin", "execution", "mainnet_executor.py")).read()
print("INFO tools/phone-test mainnet_executor.py still has the UNREPAIRED ratio-as-percent compare (no quote_price_impact_pct): %s; guard keeps it out of packaging, it is NOT fixed" % ("quote_price_impact_pct" not in pm))
check("phone-test copy is untouched by this change (still lacks the helper)", "quote_price_impact_pct" not in pm)
print("FAILS: %d" % FAILS); sys.exit(1 if FAILS else 0)
