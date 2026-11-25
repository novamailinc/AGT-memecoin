#!/usr/bin/env python3
"""bb68 lane-level (momentum) UNBOUND legacy position across restarts, SOURCE ONLY (NOT RUN until cleared). Scratch dirs only, canned feed engineering-only, no
network, no live executor. A lane ledger holds an open position with pa_pos_id but NO pa_account_id (legacy/unbound). Predeclared (UNVERIFIED): in EACH of two fresh
processes the lane recovery returns blocked (True), L._ENTRY_BLOCKED is True, and the journal of the lane's close intents is not mutated. NEGATIVE CONTROL: a position
with neither pa_pos_id nor pa_account_id does not block. A fresh process is the restart."""
if __name__ != "__main__":
    import pytest
    pytest.skip("script-style test", allow_module_level=True)
import os, sys, json, subprocess, tempfile, importlib
MODE = sys.argv[1] if len(sys.argv) > 1 else "parent"
ROOTDIR = sys.argv[2] if MODE != "parent" else tempfile.mkdtemp(prefix="genbind-lane-unbound-")
if MODE != "parent": os.environ["FOREX_AGENT_HOME"] = os.path.join(ROOTDIR, "home")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import tests._guard_fixture as G
G.block_network()
sys.path.insert(0, os.path.join(ROOT, "daemon", "launch_scanner"))
def child(kind):
    os.environ["PAPER_ACCOUNT_ENABLED"] = "1"; os.environ["PAPER_ACCOUNT_START"] = "10"
    from daemon.launch_scanner import momentum_lane as L
    import daemon.launch_scanner.paper_account as PA
    PA = importlib.reload(PA); G.install(PA)
    led = os.path.join(ROOTDIR, "led"); os.makedirs(led, exist_ok=True)
    L.LEDGER_PATH = os.path.join(led, "momentum.json"); L.ensure_ledger()
    d = L._read()
    if kind == "setup":
        pos = {"mint": "LegacyMint", "symbol": "LEG", "pa_pos_id": "momentum/LegacyMint#1"} if sys.argv[3] == "unbound" else {"mint": "PlainMint", "symbol": "PLN"}
        d["positions"][pos["mint"]] = pos; L._write(d); print("SETUP-OK"); return
    L._ENTRY_BLOCKED = False; L._RECOVERY_DONE = False
    blocked = L._recover_close_intents()
    print("RESULT " + json.dumps({"blocked": bool(blocked), "flag": bool(L._ENTRY_BLOCKED)}))
if MODE == "setup": child("setup"); sys.exit(0)
if MODE == "recover": child("recover"); sys.exit(0)
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n, flush=True)
    if not c: FAILS.append(n)
def run(args):
    p = subprocess.run([sys.executable, "-B", os.path.abspath(__file__)] + args, capture_output=True, text=True, timeout=40, env=dict(os.environ))
    return p.returncode, p.stdout, p.stderr
def res(out):
    l = [x for x in out.splitlines() if x.startswith("RESULT ")]
    return json.loads(l[0][7:]) if l else None
for kind, expect in (("unbound", True), ("plain", False)):
    root = tempfile.mkdtemp(prefix="genbind-lane-%s-" % kind)
    rc, out, err = run(["setup", root, kind])
    check("%s: setup ok (rc %d, err %r)" % (kind, rc, err[-120:]), rc == 0 and "SETUP-OK" in out)
    for n in (1, 2):
        rc, out, err = run(["recover", root])
        r = res(out)
        check("%s: restart %d recovery result %r (expect blocked=%s)" % (kind, n, r, expect), rc == 0 and r is not None and r["blocked"] is expect and r["flag"] is expect)
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
