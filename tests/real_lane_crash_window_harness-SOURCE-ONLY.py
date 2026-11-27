#!/usr/bin/env python3
"""SOURCE ONLY, UNEXECUTED (written, not run). Needs main's explicit clearance for this exact blob before any run. Contains a DELIBERATE SIGKILL of a child process; the kill is instrumented and
child-local (no production change). Evidence class: external-style SIGKILL of the child at ONE instrumented site; NOT power loss, NOT the live trader.
Reuses tests/real_lane_two_process_harness-SOURCE-ONLY.py (blob d236b3c2, run green 13/0) as a module for spawn/reap/token/probe helpers and its helper chain (hook helper 742c8cdc). Import runs no scenario.

THE WINDOW (grad.py consider_candidate, b2adde0f 1444-1468): paper-account try_debit succeeds and its WAL/ledger debit is durable; then pos["pa_pos_id"] is set; then _INFLIGHT_OPEN.add(); then d["positions"][mint]=pos; _write(d).
SEAM: the child replaces grad._INFLIGHT_OPEN with a set subclass whose add() (the first statement after the debit succeeded and before the lane write) writes an fsynced SEAM_REACHED token (pa_pos_id, demo intents map)
and then SIGKILLs its own process. So the kill site is exactly "account debit durable, lane write not started". The parent asserts rc == -9 AND the token.

CASE B (same candidate retried, deterministic, expectation from source): a FRESH process re-runs consider_candidate(mkcand(1)) (same mint). Source chain: demo intent "grad|<mint>" survives the kill (consume_intent never ran) -> intent_id returns the
same op id; _intent_state sees the account debit for that op and an open account position -> 'resume'; get_fill returns the journaled fill, op_committed True, buy replays the saved fill; post_quote_refusal passes (committed True, fill == prior);
commit_barrier free; try_debit op-replay branch returns (True, "ok (retry of applied op)", original pos_id) BEFORE spendable/open/gates/marks. Asserted: result (True, "opened", True); account journal still has EXACTLY ONE grad debit (no second);
lane positions 1 with pa_pos_id == the pa_pos_id in the kill token; account open qty == lane tokens; the demo intent key consumed; cash == 10 - stake; the lane position's execution.entry_op == the intent op id recorded at the kill.
HYPOTHESES from source reads (not run): the fresh process passes the lane race checks and quote_age_refusal with the helper stubs. If B fails, that is evidence about the convergence preconditions, not noise.

CASE C (NO RETRY, negative control): after the kill a fresh READ-ONLY child inspects, then a fresh process considers a DIFFERENT mint. Source expectation: account has 1 open grad position (pos_id == token pid); lane ledger has 0 positions and bankroll unchanged;
the other-mint entry is refused at the account marks gate: result (False, why, _) with why containing "marks not trustworthy" (collect_marks omits the unproven account position); NO second debit. FINDING (asserted as a labeled fact, not a pass of recovery):
the orphan has no exit owner: poll_exits (run on the same fresh process, price 3x) performs ZERO credits for it because the lane has no record; account open still 1 after. This case documents the UNRESOLVED functional defect. A change that fixes it must change this expectation deliberately.
CLAIM LIMITS: one site, SIGKILL not power loss; fixture feed/fill/safety stubs; C's marks-gate wording is from entry gate source (paper_account.py ~440), not run; poll_exits-no-credit is inferred from lane-record-driven exits and is asserted by observation."""
import importlib.util, json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
_s = importlib.util.spec_from_file_location("tp3base", os.path.join(HERE, "real_lane_two_process_harness-SOURCE-ONLY.py"))
B = importlib.util.module_from_spec(_s); _s.loader.exec_module(B)
H = B.H; check = B.check; FAILS = B.FAILS

EXTRA = r'''
import signal
def intents_map():
    try: return dict((json.load(open(FILL_PATH)).get("intents") or {}))
    except Exception: return None
def acct():
    with PA._lock():
        led, healthy, degraded = PA._load_locked()
    g = ((led or {}).get("lanes") or {}).get("grad") or {}
    return [{"pos_id": p.get("pos_id"), "mint": p.get("mint"), "qty": p.get("qty")} for p in (g.get("open") or {}).values() if isinstance(p, dict)]
def lane_state():
    L = raw_ledger()
    return {"positions": {m: {"pa_pos_id": p.get("pa_pos_id"), "tokens": p.get("tokens"), "entry_op": (p.get("execution") or {}).get("entry_op")} for m, p in L.get("positions", {}).items()}, "bankroll": L.get("bankroll")}
'''
K_CHILD = H.PRE2 + B.CTLP + EXTRA + r'''
T.register(GR)
class _Seam(set):
    def add(self, x):
        token("SEAM_REACHED", pa_pos_id=str(x), intents=intents_map(), accts=acct())
        os.kill(os.getpid(), signal.SIGKILL)
GR._INFLIGHT_OPEN = _Seam()
r = GR.consider_candidate(mkcand(1), time.time())
token("SURVIVED", r=str(r)); os._exit(99)
'''
V_CHILD = H.PRE2 + B.CTLP + EXTRA + r'''
print(json.dumps({"name": name, "acct": acct(), "lane": lane_state(), "intents": intents_map(), "j": jstate()}, default=str))
'''
R_CHILD = H.PRE2 + B.CTLP + EXTRA + r'''
T.register(GR); n = int(sys.argv[3])
r = GR.consider_candidate(mkcand(n), time.time())
print(json.dumps({"name": name, "ok": bool(r[0]), "why": str(r[1]), "final": r[2], "acct": acct(), "lane": lane_state(), "intents": intents_map(), "j": jstate()}, default=str))
'''
C_CHILD = H.PRE2 + B.CTLP + EXTRA + r'''
T.register(GR)
r = GR.consider_candidate(mkcand(2), time.time()); a1 = acct(); j1 = jstate()
FakeJup.mult = 3.0
try: GR.poll_exits(price_fn=gpf(3.0))
finally: FakeJup.mult = 1.0
print(json.dumps({"name": name, "ok": bool(r[0]), "why": str(r[1]), "acct_after_entry": a1, "j_after_entry": j1, "acct_after_exits": acct(), "lane": lane_state(), "j": jstate()}, default=str))
'''
def kill_phase(h):
    pk = B.spawn(K_CHILD, h, "k1")
    try: pk.wait(timeout=min(30, max(1.0, B.left())))
    except Exception: raise B.Stop("kill child did not exit")
    t = B.tok(h, "k1", "SEAM_REACHED", 1.0)
    check("P3 kill child hit the seam (fsynced token, pa_pos_id %s) and died by SIGKILL (rc %s)" % ((t or {}).get("pa_pos_id"), pk.returncode),
          t is not None and bool(t.get("pa_pos_id")) and pk.returncode == -9 and B.tok(h, "k1", "SURVIVED", 0.0) is None)
    pv = B.spawn(V_CHILD, h, "v1"); v = B.finish(pv)
    a = (v or {}).get("acct") or []; l = (v or {}).get("lane") or {}
    d = B.stakes((v or {}).get("j") or {"events": []})
    check("P3 post-kill state: account open 1 (pos_id == token), exactly one grad debit %s, lane positions 0, intent key kept %s" % (d, (v or {}).get("intents")),
          pv.returncode == 0 and len(a) == 1 and a[0]["pos_id"] == t["pa_pos_id"] and len(d) == 1 and l.get("positions") == {} and any(k.startswith("grad|") for k in (v.get("intents") or {})))
    return t, v
def case_b():
    h = B.newhome(); t, v = kill_phase(h); stake = B.stakes(v["j"])[0]
    pr = B.spawn(R_CHILD, h, "r1", 1); r = B.finish(pr); j = (r or {}).get("j") or {}; l = (r or {}).get("lane") or {}; a = (r or {}).get("acct") or []
    pos = (l.get("positions") or {}).get("GradMint1pump") or {}
    opid = (t.get("intents") or {}).get("grad|GradMint1pump")
    check("P3-B same candidate retried: (%s, %s) rc %s" % ((r or {}).get("ok"), (r or {}).get("why"), pr.returncode), r is not None and pr.returncode == 0 and r["ok"] is True and r["why"] == "opened" and r["final"] is True)
    check("P3-B exactly ONE grad debit in the journal after the retry (%s), cash %s == 10 - stake" % (B.stakes(j), j.get("cash")), len(B.stakes(j)) == 1 and abs(float(j["cash"]) - (10.0 - stake)) < 1e-5)
    check("P3-B lane position adopted: pa_pos_id %s == killed pos_id %s, account open 1, account qty %s == lane tokens %s" % (pos.get("pa_pos_id"), t["pa_pos_id"], (a[0]["qty"] if a else None), pos.get("tokens")),
          len(l.get("positions") or {}) == 1 and pos.get("pa_pos_id") == t["pa_pos_id"] and len(a) == 1 and abs(float(a[0]["qty"]) - float(pos["tokens"])) < 1e-9)
    check("P3-B entry op replayed: lane entry_op %s == intent op at kill %s; intent key consumed (%s)" % (pos.get("entry_op"), opid, (r or {}).get("intents")),
          bool(opid) and pos.get("entry_op") == opid and "grad|GradMint1pump" not in ((r or {}).get("intents") or {}))
def case_c():
    h = B.newhome(); t, v = kill_phase(h); stake = B.stakes(v["j"])[0]
    pc = B.spawn(C_CHILD, h, "c1"); r = B.finish(pc); j = (r or {}).get("j") or {}
    check("P3-C other-mint entry refused at the marks gate: (%s) why=%s" % ((r or {}).get("ok"), str((r or {}).get("why"))[:160]), r is not None and pc.returncode == 0 and r["ok"] is False and "marks not trustworthy" in r["why"])
    check("P3-C no second debit %s, cash %s == 10 - stake, lane positions 0, account open 1" % (B.stakes(j), j.get("cash")),
          len(B.stakes(j)) == 1 and abs(float(j["cash"]) - (10.0 - stake)) < 1e-5 and (r["lane"].get("positions") == {}) and len(r["acct_after_exits"]) == 1)
    creds = [e for e in j.get("events", []) if e["event"] == "credit"]
    check("P3-C FINDING (unresolved defect, NOT a recovery): orphan has NO exit owner: poll_exits at 3x produced %d credits, account open still %d, lane 0. Entries stay blocked account-wide." % (len(creds), len(r["acct_after_exits"])),
          len(creds) == 0 and len(r["acct_after_exits"]) == 1 and r["acct_after_exits"][0]["pos_id"] == t["pa_pos_id"])
def main():
    try:
        for n, f in (("P3-B", case_b), ("P3-C", case_c)): B.scen(n, f)
    finally: B.reap_all()
    alive = [p.pid for p in B.KIDS if p.poll() is None]
    check("all children reaped (still alive: %s), total %d children spawned" % (alive, len(B.KIDS)), not alive)
    print("elapsed %.1fs of 90s budget; FAILS: %d" % (90.0 - B.left(), len(FAILS))); return 1 if FAILS else 0
if __name__ == "__main__":
    sys.exit(main())
