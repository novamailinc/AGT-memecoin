#!/usr/bin/env python3
"""MIRROR-ONLY restart/persistence contract under the bb55 PRE-APPLY GATE (bb59; a NEW file, tests/test_close_intents_frozen_restart.py is unchanged and keeps its
coverage of the other four lanes). ENGINEERING TEST: REAL mirror._recover_close_intents, REAL shared close_intents journal and REAL paper account in REAL separate
processes sharing one scratch home (a restart is a new process). Setup journals an intent with close_intents.begin directly and writes the scratch ledger file
directly (both LABELLED injections, scratch root only); no trading, no flag/config change. Expectations are hand predictions (UNVERIFIED) until run. NOT run.
Kinds of intent: SYN = the old synthetic ids ("pos-frz-1": not a mirror instance id); SHP = real-writer-shaped (pos_id "<lane>/<mint>#<seq>", op id
"<lane>:leg:a:<mint>:<pos_id>", exactly the 8 keys _pa_settle records, close_info with reason). Ledger variants for SHP: NONEMPTY (a real ledger that does not
contain the instance), MISSING (no ledger file) and PRESENT (FIELD-VALID: the ledger holds the matching OPEN position of this lane with an explicit lane field,
the same pa_pos_id and mint, tokens/legs such that the recorded leg-a qty is exactly tokens*frac; the gate says ok). For PRESENT the ACCOUNT refuses the
setup-op credit through a LABELLED INJECTION in the counting wrapper (scratch only): the real gated path account_apply -> credit is reached (counted), the
refusal makes the real shared recover_lane freeze with "account re-apply refused" BEFORE any lane check or lane_apply (control flow: recover_lane applies
account ops first; lane_has_close/lane_apply are never reached, so open_same versus closed_same does not change the prediction; the ledger bytes stay unchanged).
Mapping of the ORIGINAL mirror rows of tests/test_close_intents_frozen_restart.py (the old file keeps the other four lanes only; its mirror iteration was removed):
C control -> C; A setup -> each kind's A setup; A blocked/not frozen -> each kind's A; A "account path hit >= 1" + ledger unchanged -> PRESENT A (hits == 1, ledger same;
the SYN/SHP kinds now assert hits == 0 because the gate rejects them first); A fresh -> each kind's A fresh; B first freezes -> B first; B "account hit >= 1" -> PRESENT B;
B FRESH persisted frozen -> B FRESH.
Contract: gate BEFORE account: the account credit for the setup op id is hit 0 times (hit counter proven by an ORACLE CONTROL that makes exactly one
direct counted call), the ledger file bytes are unchanged, entries stay blocked, and the blocked state persists across restarts.
  SYN and SHP+NONEMPTY (identified-invalid): A freeze write fails: blocked, intent stays open (not frozen), fresh process (freeze still failing) still blocks.
                                           B freeze works: first process freezes durably and blocks; a FRESH process still blocks from the persisted frozen intent, no re-freeze.
  SHP+MISSING (hold, non-mutating): blocked, NO freeze attempted, intent stays open, journal unchanged, fresh process still blocks (no ledger file created).
  C: an empty journal does not block entries.
Run: python3 -B tests/test_close_intents_frozen_restart_mirror_gate.py"""
if __name__ != "__main__":
    import pytest
    pytest.skip("script-style test", allow_module_level=True)
import sys, os
MODE = sys.argv[1] if len(sys.argv) > 1 else "parent"
import json, subprocess, tempfile, hashlib
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import tests._guard_fixture as G
G.block_network()
sys.path.insert(0, os.path.join(ROOT, "daemon", "launch_scanner"))
MINT = "FrzMint1"

def _ids(lane, kind):
    if kind == "syn":
        return "pos-frz-1", "%s:leg:a:%s:pos-frz-1" % (lane, MINT)
    pid = "%s/%s#1" % (lane, MINT)
    return pid, "%s:leg:a:%s:%s" % (lane, MINT, pid)

def child(mode, root, kind, flag):
    os.environ["FOREX_AGENT_HOME"] = root; os.environ["PAPER_ACCOUNT_ENABLED"] = "1"; os.environ["PAPER_ACCOUNT_START"] = "10"
    os.environ.pop("AGT_EXECUTION_MODE", None)
    import importlib
    import daemon.launch_scanner.close_intents as CI
    M = importlib.import_module("daemon.launch_scanner.mirror")
    lane = sorted(set(M.LANE_BY_CHAIN.values()))[0]
    pid, opid = _ids(lane, kind.split(":")[0])
    ledvar = kind.split(":")[1] if ":" in kind else None
    def _led():   # on-disk ledger BYTES ('absent' when the file does not exist); never M._read() (blank fallback is not stable)
        try:
            return "absent" if not os.path.exists(M.LEDGER_PATH) else hashlib.sha256(open(M.LEDGER_PATH, "rb").read()).hexdigest()
        except Exception as e: return "unreadable:%s" % type(e).__name__
    if mode == "setup" and ledvar == "present":   # LABELLED scratch ledger: the matching OPEN position of this lane (explicit lane field), qty-consistent with leg a
        op = {"kind": "credit", "op_id": opid, "mint": MINT, "amount": 1.0, "qty": 100.0, "fee": 0.01, "price_kind": "test", "pos_id": pid}
        iid = CI.begin(lane, MINT, pid, [op], {"realized_usd": 1.0, "closed_reason": "engineering_setup", "close_ts": 1.0, "leg_suffix": "leg:a"})
        os.makedirs(os.path.dirname(M.LEDGER_PATH) or ".", exist_ok=True)
        with open(M.LEDGER_PATH, "w") as f:
            json.dump({"positions": {MINT: {"mint": MINT, "lane": lane, "pa_pos_id": pid, "tokens": 200.0, "legs": {"a": {"done": False, "frac": 0.5}}}}, "closes": []}, f)
        print("RESULT " + json.dumps({"iid": iid, "status": CI.intent_status(lane, iid), "led": _led()[:12], "lane": lane})); return
    if mode == "setup":
        op = {"kind": "credit", "op_id": opid, "mint": MINT, "amount": 1.0, "qty": 100.0, "fee": 0.01, "price_kind": "test", "pos_id": pid}
        iid = CI.begin(lane, MINT, pid, [op], {"realized_usd": 1.0, "closed_reason": "engineering_setup", "close_ts": 1.0, "leg_suffix": "leg:a"})
        if ledvar == "nonempty":   # LABELLED scratch ledger: a real, non-empty ledger that does NOT contain the instance
            os.makedirs(os.path.dirname(M.LEDGER_PATH) or ".", exist_ok=True)
            with open(M.LEDGER_PATH, "w") as f:
                json.dump({"positions": {}, "closes": [{"mint": "UnrelatedMint", "lane": lane, "pa_pos_id": "%s/UnrelatedMint#1" % lane}]}, f)
        print("RESULT " + json.dumps({"iid": iid, "status": CI.intent_status(lane, iid), "led": _led()[:12], "lane": lane})); return
    hits = {"n": 0}
    _real_pa = M._paper_account
    class _PX:
        def __init__(self, pa): self._pa = pa
        def __getattr__(self, n): return getattr(self._pa, n)
        def credit(self, *a, **k):
            o = k.get("op_id") if "op_id" in k else (a[3] if len(a) > 3 else None)
            if o == opid: hits["n"] += 1   # scoped to THIS setup op id only
            if o == opid and ledvar == "present" and mode == "recover":   # LABELLED INJECTION: the account refuses the credit of the setup op (after it is counted)
                return False, "INJECTED account refusal (labelled engineering injection)"
            return self._pa.credit(*a, **k)
    M._paper_account = lambda *a, **k: (lambda p: _PX(p) if p is not None else None)(_real_pa(*a, **k))
    if mode == "hitctl":   # POSITIVE ORACLE CONTROL for the hit counter: exactly one direct counted call for the setup op id, one for another id
        px = M._paper_account()
        try: px.credit(lane, MINT, 1.0, opid, pos_id=pid, qty=1.0, fee=0.0, price_kind="test")
        except Exception: pass
        try: px.credit(lane, MINT, 1.0, opid + "-other", pos_id=pid, qty=1.0, fee=0.0, price_kind="test")
        except Exception: pass
        print("RESULT " + json.dumps({"hits": hits["n"]})); return
    if mode == "hashctl":   # POSITIVE ORACLE CONTROL for the ledger-bytes check
        a1 = _led(); a2 = _led()
        with open(M.LEDGER_PATH, "w") as f: json.dump({"positions": {}, "closes": [], "x": 1}, f)
        h1 = _led(); h1b = _led()
        with open(M.LEDGER_PATH, "w") as f: json.dump({"positions": {}, "closes": [], "x": 2}, f)
        h2 = _led()
        print("RESULT " + json.dumps({"absent_stable": a1 == a2 == "absent", "stable": h1 == h1b, "mutation_changes_hash": h1 != h2 and h1 != "absent"})); return
    led0 = _led(); calls = {"freeze": 0, "why": []}
    jp = CI.journal_path(lane); j0 = open(jp, "rb").read() if os.path.exists(jp) else b""
    _real = CI.freeze
    def _fz(l, i, why):
        calls["freeze"] += 1; calls["why"].append(str(why)[:200])
        return False if flag == "freeze_false" else _real(l, i, why)
    CI.freeze = _fz
    M._RECOVERY_DONE = False; M._ENTRY_BLOCKED = False
    ret = M._recover_close_intents()
    ents, st, _t = CI._read(jp)
    iids = [i for i, x in CI._state(ents).items() if x["intent"] is not None]
    j1 = open(jp, "rb").read() if os.path.exists(jp) else b""
    print("RESULT " + json.dumps({"ret": ret, "flag": bool(M._ENTRY_BLOCKED), "freeze_calls": calls["freeze"], "journal_status": st,
                                  "status": CI.intent_status(lane, iids[0]) if iids else None, "n": len(iids), "hits": hits["n"],
                                  "led_same": led0 == _led() and not led0.startswith("unreadable"), "led0": led0[:12], "journal_same": j0 == j1,
                                  "pending_after": len(CI.pending(lane)),
                                  "gate_stats": dict(M._GATE_STATS), "fz_why": calls["why"]}))

if MODE != "parent":
    child(MODE, sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else ""); sys.exit(0)

FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n, flush=True)
    if not c: FAILS.append(n)
def run(mode, root, kind, flag=""):
    p = subprocess.run([sys.executable, "-B", os.path.abspath(__file__), mode, root, kind, flag], capture_output=True, text=True, timeout=60)
    rl = [l for l in p.stdout.splitlines() if l.startswith("RESULT ")]
    return p.returncode, (json.loads(rl[0][7:]) if rl else None), p.stderr

rh = tempfile.mkdtemp(prefix="frzg-H-")
rc, hc, err = run("hashctl", rh, "syn")
check("ORACLE CONTROL ledger bytes: 'absent' and stable with no file, stable across reads, CHANGES when the file bytes change (rc %d, %r)" % (rc, hc),
      rc == 0 and hc is not None and hc["absent_stable"] is True and hc["stable"] is True and hc["mutation_changes_hash"] is True and "Traceback" not in err)
for kind in ("syn", "shp:nonempty"):
    rk = tempfile.mkdtemp(prefix="frzg-K-")
    rc, k, err = run("hitctl", rk, kind)
    check("ORACLE CONTROL account hit counter (%s): exactly 1 for the setup op id after 1 direct counted call (the other id is not counted) (rc %d, %r)" % (kind, rc, k), rc == 0 and k is not None and k["hits"] == 1 and "Traceback" not in err)
r0 = tempfile.mkdtemp(prefix="frzg-C-")
rc, c, err = run("recover", r0, "syn")
check("C control: empty journal does NOT block entries (rc %d, %r)" % (rc, c), rc == 0 and c is not None and c["ret"] is False and c["flag"] is False and "Traceback" not in err)
for kind in ("syn", "shp:nonempty", "shp:missing"):
    hold = kind == "shp:missing"
    tag = kind
    ra = tempfile.mkdtemp(prefix="frzg-A-")
    rc, s, err = run("setup", ra, kind)
    check("%s A setup: intent journaled (rc %d, %r)" % (tag, rc, s), rc == 0 and s is not None and s["status"] is not None and "Traceback" not in err)
    rc, a1, err = run("recover", ra, kind, "freeze_false")
    common_a1 = rc == 0 and a1 is not None and a1["ret"] is True and a1["flag"] is True and a1["status"] != "frozen" and a1["n"] == 1 and a1["pending_after"] == 1 and "Traceback" not in err
    check("%s A (freeze write FAILS): blocked, intent stays open, %s (%r)" % (tag, "NO freeze attempted (hold)" if hold else "freeze attempted", a1),
          common_a1 and (a1["freeze_calls"] == 0 if hold else a1["freeze_calls"] >= 1))
    check("%s A GATE BEFORE ACCOUNT: setup op id credited 0 times, ledger bytes unchanged (same %r, hash %s)" % (tag, (a1 or {}).get("led_same"), (a1 or {}).get("led0")),
          a1 is not None and a1["hits"] == 0 and a1["led_same"] is True)
    if hold: check("%s A hold is NON-MUTATING: journal bytes unchanged (%r)" % (tag, (a1 or {}).get("journal_same")), a1 is not None and a1["journal_same"] is True)
    rc, a2, err = run("recover", ra, kind, "freeze_false")
    check("%s A fresh process, freeze still failing: still blocks, still 0 credits, intent not frozen (%r)" % (tag, a2),
          rc == 0 and a2 is not None and a2["ret"] is True and a2["flag"] is True and a2["status"] != "frozen" and a2["hits"] == 0 and a2["led_same"] is True)
    rb = tempfile.mkdtemp(prefix="frzg-B-")
    rc, s, err = run("setup", rb, kind)
    rc, b1, err = run("recover", rb, kind)
    if hold:
        check("%s B (freeze would work, but this is a HOLD): blocked, NOT frozen, no freeze attempted, intent open, journal unchanged (%r)" % (tag, b1),
              rc == 0 and b1 is not None and b1["ret"] is True and b1["flag"] is True and b1["freeze_calls"] == 0 and b1["status"] != "frozen" and b1["pending_after"] == 1 and b1["journal_same"] is True)
    else:
        check("%s B first process freezes durably and blocks (%r)" % (tag, b1), rc == 0 and b1 is not None and b1["ret"] is True and b1["flag"] is True and b1["freeze_calls"] >= 1 and b1["status"] == "frozen" and b1["n"] == 1)
    check("%s B GATE BEFORE ACCOUNT: setup op id credited 0 times, ledger bytes unchanged (hits %r, same %r)" % (tag, (b1 or {}).get("hits"), (b1 or {}).get("led_same")), b1 is not None and b1["hits"] == 0 and b1["led_same"] is True)
    rc, b2, err = run("recover", rb, kind)
    if hold:
        check("%s B FRESH process: still blocked from the durable record, still not frozen, 0 credits (%r)" % (tag, b2), rc == 0 and b2 is not None and b2["ret"] is True and b2["flag"] is True and b2["freeze_calls"] == 0 and b2["status"] != "frozen" and b2["hits"] == 0 and b2["journal_same"] is True)
    else:
        check("%s B FRESH process: the PERSISTED frozen intent still blocks, no re-freeze, 0 credits (%r)" % (tag, b2), rc == 0 and b2 is not None and b2["ret"] is True and b2["flag"] is True and b2["freeze_calls"] == 0 and b2["status"] == "frozen" and b2["n"] == 1 and b2["hits"] == 0)
# PRESENT: field-valid instance + injected ACCOUNT refusal (gate ok, real gated path reached, refusal persists)
kind = "shp:present"
ra = tempfile.mkdtemp(prefix="frzg-PA-")
rc, s, err = run("setup", ra, kind)
check("shp:present A setup: intent and the matching open-position ledger journaled (rc %d, %r)" % (rc, s), rc == 0 and s is not None and s["status"] is not None and "Traceback" not in err)
rc, a1, err = run("recover", ra, kind, "freeze_false")
gs = (a1 or {}).get("gate_stats") or {}
check("shp:present A (freeze write FAILS): blocked, freeze attempted, intent stays open, not frozen (%r)" % (a1,),
      rc == 0 and a1 is not None and a1["ret"] is True and a1["flag"] is True and a1["freeze_calls"] >= 1 and a1["status"] != "frozen" and a1["n"] == 1 and a1["pending_after"] == 1 and "Traceback" not in err)
check("shp:present A GATE OK (stats %r: ok>=1, no gate freeze, no hold) and the REAL gated account path WAS reached (credit hits %r == 1), ledger bytes unchanged (%r)" % (gs, (a1 or {}).get("hits"), (a1 or {}).get("led_same")),
      a1 is not None and gs.get("ok", 0) >= 1 and gs.get("freeze", 1) == 0 and gs.get("hold", 1) == 0 and a1["hits"] == 1 and a1["led_same"] is True)
check("shp:present A the freeze came from the ACCOUNT refusal in shared recovery, not from the gate (reasons %r)" % ((a1 or {}).get("fz_why"),),
      a1 is not None and len(a1["fz_why"]) >= 1 and all("account re-apply refused" in w and "INJECTED" in w and not w.startswith("gate:") for w in a1["fz_why"]))
rc, a2, err = run("recover", ra, kind, "freeze_false")
check("shp:present A fresh process (freeze still failing, refusal still injected): still blocks, intent not frozen, the account path is hit again (hits %r == 1) (%r)" % ((a2 or {}).get("hits"), a2),
      rc == 0 and a2 is not None and a2["ret"] is True and a2["flag"] is True and a2["status"] != "frozen" and a2["hits"] == 1 and a2["led_same"] is True)
rb = tempfile.mkdtemp(prefix="frzg-PB-")
rc, s, err = run("setup", rb, kind)
rc, b1, err = run("recover", rb, kind)
gs = (b1 or {}).get("gate_stats") or {}
check("shp:present B first process: account refusal freezes DURABLY and blocks (%r)" % (b1,), rc == 0 and b1 is not None and b1["ret"] is True and b1["flag"] is True and b1["freeze_calls"] >= 1 and b1["status"] == "frozen" and b1["n"] == 1)
check("shp:present B gate ok (stats %r), account path hit once (%r), ledger bytes unchanged (%r), freeze reason is the account refusal (%r)" % (gs, (b1 or {}).get("hits"), (b1 or {}).get("led_same"), (b1 or {}).get("fz_why")),
      b1 is not None and gs.get("ok", 0) >= 1 and gs.get("freeze", 1) == 0 and gs.get("hold", 1) == 0 and b1["hits"] == 1 and b1["led_same"] is True and len(b1["fz_why"]) == 1 and "account re-apply refused" in b1["fz_why"][0])
rc, b2, err = run("recover", rb, kind)
check("shp:present B FRESH process: the PERSISTED frozen intent still blocks, no re-freeze, the account path is NOT re-hit (hits %r == 0) (%r)" % ((b2 or {}).get("hits"), b2),
      rc == 0 and b2 is not None and b2["ret"] is True and b2["flag"] is True and b2["freeze_calls"] == 0 and b2["status"] == "frozen" and b2["n"] == 1 and b2["hits"] == 0 and b2["led_same"] is True)
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
