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

# ---- normalization / bypass rows (pure helpers)
for bad in ("./tools/phone-test/x", "tools//phone-test", "tools\\phone-test\\x", "/tools/phone-test", "TOOLS/Phone-Test/x", "tools/phone-test/../phone-test/a"):
    check("normalized dest refused: %r" % bad, pr.guard_extras(["/s:" + bad], {}) is not None, bad)
check("dest with '..' refused (unresolvable)", pr.guard_extras(["/s:tools/a/../phone-test"], {}) is not None)
check("dest escaping with '..' refused", pr.guard_extras(["/s:../x"], {}) is not None)
d2, c2, k2 = pr.guard_mirror(["./tools//phone-test/a.py", "TOOLS/phone-test/b.py", "tools/other.py"], ["tools\\phone-test\\c.py"], {})
check("mirror lists normalized the same way", d2 == ["tools/other.py"] and c2 == [] and k2 == 2, (d2, c2, k2))
import tempfile
src = tempfile.mkdtemp(prefix="pgsrc-"); os.makedirs(os.path.join(src, "phone-test"))
open(os.path.join(src, "phone-test", "a.py"), "w").write("x")
for parent in ("tools", ".", ""):
    check("parent-of-bundle dest %r with a source holding phone-test/ refused" % parent, pr.guard_extras([src + ":" + parent], {}) is not None, parent)
src2 = tempfile.mkdtemp(prefix="pgsrc2-"); open(os.path.join(src2, "a.py"), "w").write("x")
check("parent dest with a source WITHOUT phone-test allowed", pr.guard_extras([src2 + ":tools"], {}) is None)

# ---- drive the REAL main() (tree building and uploads) with a stub api/remote; no network, no token, nothing pushed
import io, contextlib
def run_main(argv, env_allow=False):
    root = tempfile.mkdtemp(prefix="pgroot-")
    os.makedirs(os.path.join(root, "tools", "phone-test")); os.makedirs(os.path.join(root, "tools", "scripts"))
    for p, t in (("tools/phone-test/old.py", "old"), ("tools/scripts/ok.py", "ok"), ("tools/scripts/new.py", "new")):
        open(os.path.join(root, p), "w").write(t)
    rec = {"blobs": [], "trees": []}
    remote = {"tools/phone-test/old.py": ("deadbeef", "100644"), "tools/phone-test/remote_only.py": ("cafe", "100644"), "stale.py": ("beef", "100644"),
              "tools/scripts/ok.py": (pr.blob_sha(b"ok"), "100644")}
    def fake_api(method, path, payload=None):
        if path == "/user": return {"login": "olamoreA"}
        if path.endswith("/git/blobs"): rec["blobs"].append(payload); return {"sha": "b%d" % len(rec["blobs"])}
        if path.endswith("/git/trees"): rec["trees"].append(payload); return {"sha": "t1"}
        if path.endswith("/git/commits"): return {"sha": "c1" * 10}
        if "/git/refs" in path: return {}
        if path == "/user/repos": raise RuntimeError("HTTP 422 exists")
        raise AssertionError("unexpected api call " + method + " " + path)
    pr.api = fake_api; pr.remote_tree = lambda o, r, b: (dict(remote), "basetree", "parentcommit")
    old_argv, old_env = sys.argv, dict(os.environ)
    os.environ.pop(pr.ALLOW_UNQUALIFIED_ENV, None)
    if env_allow: os.environ[pr.ALLOW_UNQUALIFIED_ENV] = "1"
    sys.argv = ["push_repo.py", root] + argv
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = pr.main()
    finally:
        sys.argv = old_argv; os.environ.clear(); os.environ.update(old_env)
    return rc, out.getvalue(), rec
rc, out, rec = run_main([])
paths = [e["path"] for t in rec["trees"] for e in t["tree"]]
check("main(): rc 0 and a tree was created", rc == 0 and len(rec["trees"]) == 1, (rc, out[-200:]))
check("main(): NO tools/phone-test path in the created tree (neither changed nor deleted)", not any("phone-test" in p for p in paths), paths)
check("main(): changed file and the stale deletion still go through", "tools/scripts/new.py" in paths and "stale.py" in paths and any(e["path"] == "stale.py" and e["sha"] is None for e in rec["trees"][0]["tree"]), paths)
import base64
check("main(): no phone-test blob content uploaded (only 'new')", [base64.b64decode(b["content"]) for b in rec["blobs"]] == [b"new"], [base64.b64decode(b["content"]) for b in rec["blobs"]])
check("main(): NOTICE says not a complete phone release", "NOT a complete phone release" in out and "2 remote paths preserved" in out, out[-300:])
rc, out, rec = run_main(["--dry-run"])
check("main() dry-run lists no phone-test path", "phone-test/" not in out.replace("tools/phone-test is NOT", "") and rc == 0, out[-300:])
rc, out, rec = run_main([], env_allow=True)
paths = [e["path"] for t in rec["trees"] for e in t["tree"]]
check("main() WITH the explicit env allow does include it (the bypass is only the explicit env; not an authorization to ship, nothing is pushed here: stub api)", "tools/phone-test/old.py" in paths, paths)
pm = open(os.path.join(ROOT, "tools", "phone-test", "broker", "memecoin", "execution", "mainnet_executor.py")).read()
print("INFO tools/phone-test mainnet_executor.py still has the UNREPAIRED ratio-as-percent compare (no quote_price_impact_pct): %s; guard keeps it out of packaging, it is NOT fixed" % ("quote_price_impact_pct" not in pm))
check("phone-test copy is untouched by this change (still lacks the helper)", "quote_price_impact_pct" not in pm)
print("FAILS: %d" % FAILS); sys.exit(1 if FAILS else 0)
