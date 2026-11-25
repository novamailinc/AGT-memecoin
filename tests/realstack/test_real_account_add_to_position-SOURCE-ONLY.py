"""REAL paper_account add_to_position CHECKS (SOURCE ONLY, WRITTEN NOT RUN). No data, no network. Real account in a scratch FOREX_AGENT_HOME.
Fixture items (labelled): TEST-ENGINEERING limits (tests/_guard_fixture.LIMITS), at-cost marks, stakes of a few dollars, ONE injected fault
(the real _save_locked is made to raise once, after the real journal append, to model a crash between journal and state save).
Exit 0 all pass, 1 any fail."""
import os, sys, tempfile
sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import _guard_fixture as GF
GF.block_network()
RES = {"PASS": 0, "FAIL": 0, "INFO": 0}
def say(k, m): RES[k] += 1; print("%s %s" % (k, m), flush=True)
def check(n, c, e=""): say("PASS" if c else "FAIL", n + ((" | " + e) if e else ""))
def env(extra=None):
    home = tempfile.mkdtemp(prefix="realstack-acct-"); os.environ["FOREX_AGENT_HOME"] = home
    for k in [k for k in os.environ if k.startswith(("PAPER_ACCOUNT_", "AGT_"))]: del os.environ[k]
    os.environ["PAPER_ACCOUNT_ENABLED"] = "1"; GF.set_env(); os.environ.update(extra or {})
    for m in [m for m in sys.modules if m.startswith("daemon")]: del sys.modules[m]
    from daemon.launch_scanner import paper_account as pa
    return home, pa
def lot(pa, lane, mint):
    for p in (pa.snapshot(GF.at_cost_marks(pa)) or {"lanes": {}})["lanes"].get(lane, {"positions": []})["positions"]:
        if p["mint"] == mint: return p
def main():
    L, M = "tranche", "RSADDMINTpump"
    home, pa = env()
    ok, why, pid = pa.try_debit(L, M, 2.0, 100.0, "t:open", marks=GF.at_cost_marks(pa))
    check("seed: real try_debit opens the lot", ok is True, repr((ok, why)))
    s0 = pa.snapshot(GF.at_cost_marks(pa)); c0 = float(s0["cash_spendable"]); p0 = lot(pa, L, M)
    ok, why = pa.add_to_position(L, M, pid, 1.0, 40.0, "t:add1", ts=1.0, marks=GF.at_cost_marks(pa))
    p1 = lot(pa, L, M); s1 = pa.snapshot(GF.at_cost_marks(pa))
    check("add: accepted", ok is True, repr((ok, why)))
    check("add: lot qty +40 and basis +1.0 on the SAME lot", abs(p1["qty"] - p0["qty"] - 40.0) < 1e-9 and abs(p1["basis"] - p0["basis"] - 1.0) < 1e-9 and p1["pos_id"] == pid, repr((p0["qty"], p1["qty"], p0["basis"], p1["basis"])))
    check("add: spendable cash fell by exactly the cost (no double use)", abs((c0 - float(s1["cash_spendable"])) - 1.0) < 1e-6, "%.6f -> %.6f" % (c0, float(s1["cash_spendable"])))
    ok2, why2 = pa.add_to_position(L, M, pid, 1.0, 40.0, "t:add1", ts=2.0, marks=GF.at_cost_marks(pa))
    p2 = lot(pa, L, M)
    check("idempotent retry (same op_id, same params): ok and NOT re-applied", ok2 is True and "retry" in why2 and abs(p2["qty"] - p1["qty"]) < 1e-12, repr((ok2, why2, p2["qty"])))
    ok3, why3 = pa.add_to_position(L, M, pid, 1.5, 40.0, "t:add1", marks=GF.at_cost_marks(pa))
    check("same op_id, different stake -> conflict refused, nothing changed", ok3 is False and "conflict" in why3 and abs(lot(pa, L, M)["qty"] - p1["qty"]) < 1e-12, repr((ok3, why3)))
    ad = pa.applied_add("t:add1")
    check("applied_add returns the exact applied record", bool(ad) and abs(float(ad["stake"]) - 1.0) < 1e-12 and abs(float(ad["qty"]) - 40.0) < 1e-12 and ad["pos_id"] == pid, repr(ad))
    check("applied_add of an unknown op is None", pa.applied_add("t:nope") is None)
    ok4, why4 = pa.add_to_position(L, "NOLOTpump", "x#1", 1.0, 1.0, "t:nolot", marks=GF.at_cost_marks(pa))
    check("no open lot -> refused", ok4 is False and "no open lot" in why4, repr(why4))
    ok5, why5 = pa.add_to_position(L, M, pid, 9999.0, 1.0, "t:big", marks=GF.at_cost_marks(pa))
    check("stake above spendable cash -> refused, lot unchanged", ok5 is False and "spendable cash" in why5 and abs(lot(pa, L, M)["qty"] - p1["qty"]) < 1e-12, repr(why5))
    ok6, why6 = pa.add_to_position(L, M, pid, 0.1, 1.0, "t:wrongpid-op", marks=GF.at_cost_marks(pa)) if False else (None, "")
    ok7, why7 = pa.add_to_position(L, M, "wrong#pid", 0.1, 1.0, "t:wrongpid", marks=GF.at_cost_marks(pa))
    check("wrong pos_id -> refused (never adds to another instance)", ok7 is False and "no open lot" in why7, repr(why7))
    # gates: tight exposure cap refuses an add that an entry would also refuse
    h2, pa2 = env({"PAPER_ACCOUNT_MAX_TOTAL_EXPOSURE_USD": "2.5"})
    ok, why, pid2 = pa2.try_debit(L, M, 2.0, 100.0, "g:open", marks=GF.at_cost_marks(pa2))
    q0 = lot(pa2, L, M)["qty"]
    okg, whyg = pa2.add_to_position(L, M, pid2, 1.0, 40.0, "g:add", marks=GF.at_cost_marks(pa2))
    check("gates apply to an add (total-exposure cap 2.5 refuses 2.0+1.0)", okg is False and abs(lot(pa2, L, M)["qty"] - q0) < 1e-12, repr((okg, whyg)))
    check("refused add left no applied record", pa2.applied_add("g:add") is None)
    h3, pa3 = env()
    ok, why, pid3 = pa3.try_debit(L, M, 2.0, 100.0, "c:open", marks=GF.at_cost_marks(pa3))
    real_save = pa3._save_locked; state = {"n": 0}
    def boom(led):
        state["n"] += 1
        raise RuntimeError("INJECTED FAULT: crash between journal append and state save")
    pa3._save_locked = boom
    crashed = False
    try:
        pa3.add_to_position(L, M, pid3, 1.0, 40.0, "c:add", ts=3.0, marks=GF.at_cost_marks(pa3))
    except RuntimeError:
        crashed = True
    pa3._save_locked = real_save
    check("fault injected after the journal append (crash model)", crashed and state["n"] == 1)
    for m in [m for m in sys.modules if m.startswith("daemon")]: del sys.modules[m]
    from daemon.launch_scanner import paper_account as pa4
    p = lot(pa4, L, M); s = pa4.snapshot(GF.at_cost_marks(pa4))
    check("restart recovery: the journaled add is applied exactly once (qty +40)", p is not None and abs(p["qty"] - 140.0) < 1e-9 and abs(p["basis"] - 3.0) < 1e-9, repr(p and (p["qty"], p["basis"])))
    check("restart recovery: spendable cash reduced by the cost once", abs(float(s["cash_spendable"]) - (10.0 - 3.0)) < 1e-6, repr(s["cash_spendable"]))
    okr, whyr = pa4.add_to_position(L, M, pid3, 1.0, 40.0, "c:add", marks=GF.at_cost_marks(pa4))
    check("retry after crash: original outcome, not re-applied", okr is True and "retry" in whyr and abs(lot(pa4, L, M)["qty"] - 140.0) < 1e-9, repr((okr, whyr)))
    ad = pa4.applied_add("c:add")
    check("adoption evidence after crash: applied_add readable", bool(ad) and abs(float(ad["qty"]) - 40.0) < 1e-12, repr(ad))
    print("SUMMARY PASS=%d FAIL=%d INFO=%d" % (RES["PASS"], RES["FAIL"], RES["INFO"]))
    return 1 if RES["FAIL"] else 0
if __name__ == "__main__":
    sys.exit(main())
