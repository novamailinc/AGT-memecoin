#!/usr/bin/env python3
"""ALL-LANE contract test for the SHARED recovery (close_intents.recover_lane) through each lane's REAL _recover_close_intents, in REAL separate processes
sharing one scratch home (a "restart" is a new process). ENGINEERING TEST: the intent is journaled with close_intents.begin directly (labelled setup: there is no
lane position, so the lane re-book is refused and recovery freezes it); no trading, no account op beyond the real lane recovery, no config or flag change.
Lanes: ceo, watcher, momentum, grad (the mirror lane is covered by tests/test_close_intents_frozen_restart_mirror_gate.py under the bb55 pre-apply gate).
Contract (existing safety contract: a frozen intent blocks the lane's entries):
  C  control: an empty journal does NOT block entries.
  A  freeze write FAILS (named injected fault, CI.freeze returns False in that process): the divergence is still a BLOCKER (entries blocked), the intent stays
     pending (no frozen event), and a FRESH process (freeze still failing) still blocks.
  B  freeze write works: first process freezes and blocks; a FRESH process (no injection) still BLOCKS from the PERSISTED frozen intent without re-freezing.
Run: python3 -B tests/test_close_intents_frozen_restart.py"""
if __name__ != "__main__":
    import pytest
    pytest.skip("script-style test", allow_module_level=True)
import sys, os
MODE = sys.argv[1] if len(sys.argv) > 1 else "parent"
import json, subprocess, tempfile
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import tests._guard_fixture as G
G.block_network()
sys.path.insert(0, os.path.join(ROOT, "daemon", "launch_scanner"))
LANES = ("ceo", "watcher_lane", "momentum_lane", "grad")   # mirror moved to tests/test_close_intents_frozen_restart_mirror_gate.py (bb55 gate)

def child(mode, root, modname, flag):
    os.environ["FOREX_AGENT_HOME"] = root; os.environ["PAPER_ACCOUNT_ENABLED"] = "1"; os.environ["PAPER_ACCOUNT_START"] = "10"
    for k in ("AGT_EXECUTION_MODE",): os.environ.pop(k, None)
    import importlib
    import daemon.launch_scanner.close_intents as CI
    M = importlib.import_module("daemon.launch_scanner." + modname)
    lane = sorted(set(M.LANE_BY_CHAIN.values()))[0] if modname == "mirror" else M.LANE
    calls = {"freeze": 0}
    if mode == "setup":
        iid = CI.begin(lane, "FrzMint1", "pos-frz-1", [{"kind": "credit", "op_id": "%s:leg:a:FrzMint1:pos-frz-1" % lane, "mint": "FrzMint1", "amount": 1.0, "qty": 100.0,
                       "fee": 0.01, "price_kind": "test", "pos_id": "pos-frz-1"}] if modname != "grad" else [{"kind": "credit", "op_id": "%s:leg:a:FrzMint1:pos-frz-1" % lane, "mint": "FrzMint1", "amount": 1.0, "qty": -1.0,
                       "fee": 0.0, "price_kind": "test", "pos_id": "pos-frz-1"}],
                       {"realized_usd": 1.0, "closed_reason": "engineering_setup", "close_ts": 1.0, "leg_suffix": "leg:a"})
        print("RESULT " + json.dumps({"iid": iid, "status": CI.intent_status(lane, iid)})); return
    import hashlib
    opid = "%s:leg:a:FrzMint1:pos-frz-1" % lane
    def _led():
        try:
            if modname == "mirror": return hashlib.sha256(json.dumps(M._read(), sort_keys=True, default=str).encode()).hexdigest()
            # on-disk ledger BYTES (a missing file is 'absent'): _read() returns a blank ledger with a time.time() created_ts when the file is absent, so it is not stable
            return "absent" if not os.path.exists(M.LEDGER_PATH) else hashlib.sha256(open(M.LEDGER_PATH, "rb").read()).hexdigest()
        except Exception as e: return "unreadable:%s" % type(e).__name__
    if mode == "hashctl":   # POSITIVE ORACLE CONTROL for the ledger-hash check: the hash is stable across reads, and a real byte change to the ledger file changes it
        h_absent = _led(); h_absent2 = _led()
        with open(M.LEDGER_PATH, "w") as _f: json.dump({"positions": {}, "closes": [], "x": 1}, _f)
        h1 = _led(); h1b = _led()
        with open(M.LEDGER_PATH, "w") as _f: json.dump({"positions": {}, "closes": [], "x": 2}, _f)
        h2 = _led()
        print("RESULT " + json.dumps({"absent_stable": h_absent == h_absent2 == "absent", "stable": h1 == h1b, "mutation_changes_hash": h1 != h2 and h1 != "absent"})); return
    hits = {"n": 0}
    _real_pa = M._paper_account
    class _PX:
        def __init__(self, pa): self._pa = pa
        def __getattr__(self, n): return getattr(self._pa, n)
        def credit(self, *a, **k):
            oid = k.get("op_id") if "op_id" in k else (a[3] if len(a) > 3 else None)
            if oid == opid: hits["n"] += 1   # scoped to THIS setup op id only
            return self._pa.credit(*a, **k)
    M._paper_account = lambda *a, **k: (lambda p: _PX(p) if p is not None else None)(_real_pa(*a, **k))
    led0 = _led()
    _real = CI.freeze
    def _fz(l, i, why):
        calls["freeze"] += 1
        return False if flag == "freeze_false" else _real(l, i, why)
    CI.freeze = _fz
    M._RECOVERY_DONE = False; M._ENTRY_BLOCKED = False
    ret = M._recover_close_intents()
    ents, st, _t = CI._read(CI.journal_path(lane))
    iids = [i for i, x in CI._state(ents).items() if x["intent"] is not None]
    print("RESULT " + json.dumps({"ret": ret, "flag": bool(M._ENTRY_BLOCKED), "freeze_calls": calls["freeze"], "journal_status": st,
                                  "status": CI.intent_status(lane, iids[0]) if iids else None, "n": len(iids),
                                  "hits": hits["n"], "led_same": led0 == _led() and not led0.startswith("unreadable"), "led0": led0[:12]}))

if MODE != "parent":
    child(MODE, sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else ""); sys.exit(0)

FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n, flush=True)
    if not c: FAILS.append(n)
def run(mode, root, mod, flag=""):
    p = subprocess.run([sys.executable, "-B", os.path.abspath(__file__), mode, root, mod, flag], capture_output=True, text=True, timeout=60)
    rl = [l for l in p.stdout.splitlines() if l.startswith("RESULT ")]
    return p.returncode, (json.loads(rl[0][7:]) if rl else None), p.stderr
for mod in LANES:
    if mod != "mirror":
        rh = tempfile.mkdtemp(prefix="frz-H-%s-" % mod)
        rc, hc, err = run("hashctl", rh, mod)
        check("%s ORACLE CONTROL: the ledger hash is 'absent' and stable with no file, stable across reads, and CHANGES when the ledger file bytes change (rc %d, %r)" % (mod, rc, hc),
              rc == 0 and hc is not None and hc["absent_stable"] is True and hc["stable"] is True and hc["mutation_changes_hash"] is True and "Traceback" not in err)
    r0 = tempfile.mkdtemp(prefix="frz-C-%s-" % mod)
    rc, c, err = run("recover", r0, mod)
    check("%s C control: empty journal does NOT block entries (rc %d, %r)" % (mod, rc, c), rc == 0 and c is not None and c["ret"] is False and c["flag"] is False and "Traceback" not in err)
    ra = tempfile.mkdtemp(prefix="frz-A-%s-" % mod)
    rc, s, err = run("setup", ra, mod)
    check("%s A setup: intent journaled (rc %d, %r)" % (mod, rc, s), rc == 0 and s is not None and s["status"] is not None and "Traceback" not in err)
    rc, a1, err = run("recover", ra, mod, "freeze_false")
    check("%s A freeze write FAILS: still a BLOCKER (%r), freeze attempted, intent NOT frozen (status %r)" % (mod, a1, (a1 or {}).get("status")),
          rc == 0 and a1 is not None and a1["ret"] is True and a1["flag"] is True and a1["freeze_calls"] >= 1 and a1["status"] != "frozen" and a1["n"] == 1 and "Traceback" not in err)
    check("%s A the REAL account refusal path was hit (credit calls for the setup op id %r >= 1) and the lane ledger was NOT mutated (same %r, hash %s)" % (mod, (a1 or {}).get("hits"), (a1 or {}).get("led_same"), (a1 or {}).get("led0")),
          a1 is not None and a1["hits"] >= 1 and a1["led_same"] is True)
    rc, a2, err = run("recover", ra, mod, "freeze_false")
    check("%s A fresh process, freeze still failing: still blocks (%r)" % (mod, a2), rc == 0 and a2 is not None and a2["ret"] is True and a2["flag"] is True and a2["freeze_calls"] >= 1 and a2["status"] != "frozen")
    rb = tempfile.mkdtemp(prefix="frz-B-%s-" % mod)
    rc, s, err = run("setup", rb, mod)
    rc, b1, err = run("recover", rb, mod)
    check("%s B first process freezes durably and blocks (%r)" % (mod, b1), rc == 0 and b1 is not None and b1["ret"] is True and b1["flag"] is True and b1["freeze_calls"] >= 1 and b1["status"] == "frozen" and b1["n"] == 1)
    check("%s B the account path was hit (%r >= 1) and the lane ledger was NOT mutated (same %r)" % (mod, (b1 or {}).get("hits"), (b1 or {}).get("led_same")), b1 is not None and b1["hits"] >= 1 and b1["led_same"] is True)
    rc, b2, err = run("recover", rb, mod)
    check("%s B FRESH process: the PERSISTED frozen intent still blocks, no re-freeze (%r)" % (mod, b2), rc == 0 and b2 is not None and b2["ret"] is True and b2["flag"] is True and b2["freeze_calls"] == 0 and b2["status"] == "frozen" and b2["n"] == 1)
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
