"""REAL paper_account add_to_position CHECKS rev2 (SOURCE ONLY, WRITTEN NOT RUN). No data, no network, no fake engine. Real account in scratch
FOREX_AGENT_HOME dirs. Fixture items (labelled): TEST-ENGINEERING limits (tests/_guard_fixture.LIMITS), at-cost marks, small stakes, and NAMED fault
injections into the real account (real _save_locked raising once after the real journal append; real _journal raising once before any append),
plus one hand-appended journal line for the divergence case. A green here proves account-side add/idempotency/replay only, NOT buyback acceptance.
Exit 0 all pass, 1 any fail."""
import json, os, sys, tempfile, math
sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import _guard_fixture as GF
GF.block_network()
RES = {"PASS": 0, "FAIL": 0, "INFO": 0}
HOMES = []
def say(k, m): RES[k] += 1; print("%s %s" % (k, m), flush=True)
def check(n, c, e=""): say("PASS" if c else "FAIL", n + ((" | " + e) if e else ""))
def env(extra=None, drop=()):
    home = tempfile.mkdtemp(prefix="realstack-acct-"); HOMES.append(home); os.environ["FOREX_AGENT_HOME"] = home
    for k in [k for k in os.environ if k.startswith(("PAPER_ACCOUNT_", "AGT_"))]: del os.environ[k]
    os.environ["PAPER_ACCOUNT_ENABLED"] = "1"; GF.set_env()
    for k in drop: os.environ.pop(k, None)
    os.environ.update(extra or {})
    for m in [m for m in sys.modules if m.startswith("daemon")]: del sys.modules[m]
    from daemon.launch_scanner import paper_account as pa
    return home, pa
def mk(pa): return GF.at_cost_marks(pa)
def snap(pa): return pa.snapshot(mk(pa))
def lot(pa, lane, mint):
    for p in (snap(pa) or {"lanes": {}})["lanes"].get(lane, {"positions": []})["positions"]:
        if p["mint"] == mint: return p
def near(a, b, t=1e-6): return abs(float(a) - float(b)) <= t
L, M = "tranche", "RSADDMINTpump"
def main():
    home, pa = env()
    ok, why, pid = pa.try_debit(L, M, 2.0, 100.0, "t:open", marks=mk(pa))
    check("seed: real try_debit opens the lot", ok is True, repr((ok, why)))
    s0 = snap(pa); c0 = float(s0["cash_spendable"]); p0 = lot(pa, L, M); ex0 = float(s0["cost_exposure"]); bv0 = float(s0["book_value"])
    ok, why = pa.add_to_position(L, M, pid, 1.0, 40.0, "t:add1", ts=1.0, marks=mk(pa))
    p1 = lot(pa, L, M); s1 = snap(pa)
    check("add: accepted", ok is True, repr((ok, why)))
    check("add: lot qty +40 and basis +1.0 on the SAME lot", near(p1["qty"] - p0["qty"], 40.0, 1e-9) and near(p1["basis"] - p0["basis"], 1.0, 1e-9) and p1["pos_id"] == pid, repr((p0["qty"], p1["qty"], p0["basis"], p1["basis"])))
    check("add: spendable cash fell by exactly the cost (no double use)", near(c0 - float(s1["cash_spendable"]), 1.0), "%.6f -> %.6f" % (c0, float(s1["cash_spendable"])))
    check("money conservation: cost_exposure +cost exactly, book_value unchanged", near(float(s1["cost_exposure"]) - ex0, 1.0) and near(float(s1["book_value"]), bv0), "dexp=%.6f dbook=%.6f" % (float(s1["cost_exposure"]) - ex0, float(s1["book_value"]) - bv0))
    check("lot entry_qty/entry_cost rose by qty/cost", near(float(p1.get("entry_qty", -1)) - float(p0.get("entry_qty", -2)), 40.0, 1e-9) and near(float(p1.get("entry_cost", -1)) - float(p0.get("entry_cost", -2)), 1.0, 1e-9), repr((p0.get("entry_qty"), p1.get("entry_qty"), p0.get("entry_cost"), p1.get("entry_cost"))))
    ok2, why2 = pa.add_to_position(L, M, pid, 1.0, 40.0, "t:add1", ts=2.0, marks=mk(pa))
    check("idempotent retry (same op_id, same params): ok and NOT re-applied", ok2 is True and "retry" in why2 and near(lot(pa, L, M)["qty"], p1["qty"], 1e-12), repr((ok2, why2)))
    ok3, why3 = pa.add_to_position(L, M, pid, 1.5, 40.0, "t:add1", marks=mk(pa))
    check("same op_id, different stake -> conflict refused, nothing changed", ok3 is False and "conflict" in why3 and near(lot(pa, L, M)["qty"], p1["qty"], 1e-12), repr((ok3, why3)))
    ad = pa.applied_add("t:add1")
    check("applied_add returns the exact applied record", bool(ad) and near(ad["stake"], 1.0, 1e-12) and near(ad["qty"], 40.0, 1e-12) and ad["pos_id"] == pid, repr(ad))
    check("applied_add of an unknown op is None", pa.applied_add("t:nope") is None)
    ok4, why4 = pa.add_to_position(L, "NOLOTpump", "x#1", 1.0, 1.0, "t:nolot", marks=mk(pa))
    check("no open lot -> refused", ok4 is False and "no open lot" in why4, repr(why4))
    ok5, why5 = pa.add_to_position(L, M, pid, 9999.0, 1.0, "t:big", marks=mk(pa))
    check("stake above spendable cash -> refused, lot unchanged", ok5 is False and "spendable cash" in why5 and near(lot(pa, L, M)["qty"], p1["qty"], 1e-12), repr(why5))
    ok7, why7 = pa.add_to_position(L, M, "wrong#pid", 0.1, 1.0, "t:wrongpid", marks=mk(pa))
    check("wrong pos_id -> refused (never adds to another instance)", ok7 is False and "no open lot" in why7, repr(why7))
    bad = [("stake 0", dict(stake=0.0, qty=1.0, op="t:b1")), ("stake NaN", dict(stake=float("nan"), qty=1.0, op="t:b2")), ("stake inf", dict(stake=float("inf"), qty=1.0, op="t:b3")),
           ("stake negative", dict(stake=-1.0, qty=1.0, op="t:b4")), ("qty 0", dict(stake=0.1, qty=0.0, op="t:b5")), ("qty NaN", dict(stake=0.1, qty=float("nan"), op="t:b6")),
           ("qty inf", dict(stake=0.1, qty=float("inf"), op="t:b7")), ("empty op_id", dict(stake=0.1, qty=1.0, op=""))]
    for nm, a in bad:
        try:
            r = pa.add_to_position(L, M, pid, a["stake"], a["qty"], a["op"], marks=mk(pa))
            check("invalid input refused without raising: " + nm, r[0] is False, repr(r))
        except Exception as e:
            check("invalid input refused without raising: " + nm, False, "RAISED %r" % (e,))
    check("invalid inputs changed nothing", near(lot(pa, L, M)["qty"], p1["qty"], 1e-12))

    # ---- the failing scenario's account chain: partial sale -> add -> full credit
    h2, pa2 = env()
    ok, why, pid2 = pa2.try_debit(L, M, 2.0, 100.0, "p:open", marks=mk(pa2))
    okc, whyc = pa2.credit(L, M, 0.5, "p:legA", pos_id=pid2, qty=25.0, fee=0.0, price_kind="current_price_simulated_fill")
    q = lot(pa2, L, M)
    check("chain: leg A real credit accepted; lot qty 75, basis 1.5", ok and okc and near(q["qty"], 75.0, 1e-9) and near(q["basis"], 1.5, 1e-9), repr((okc, whyc, q and (q["qty"], q["basis"]))))
    oka, whya = pa2.add_to_position(L, M, pid2, 1.0, 40.0, "p:add", marks=mk(pa2))
    q = lot(pa2, L, M)
    check("chain: add on the PARTIALLY SOLD lot: qty 75+40=115, basis 1.5+1.0=2.5", oka is True and near(q["qty"], 115.0, 1e-9) and near(q["basis"], 2.5, 1e-9), repr((oka, whya, q and (q["qty"], q["basis"]))))
    okf, whyf = pa2.credit(L, M, 2.6, "p:close", pos_id=pid2, qty=None, fee=0.0, price_kind="current_price_simulated_fill")
    sn = snap(pa2)
    check("chain: full credit of the remaining 115 units accepted (no 'qty exceeds remaining units')", okf is True and "exceeds" not in whyf, repr((okf, whyf)))
    check("chain: lot gone; cash = 10 - 2 + 0.5 - 1 + 2.6 = 10.1; spendable == cash", lot(pa2, L, M) is None and near(sn["cash"], 10.1) and near(sn["cash_spendable"], sn["cash"]), repr((sn["cash"], sn["cash_spendable"])))

    # ---- refusals the code has
    h3, pa3 = env({"PAPER_ACCOUNT_LANE_CAP": "2.5"})
    ok, why, pid3 = pa3.try_debit(L, M, 2.0, 100.0, "c:open", marks=mk(pa3)); q0 = lot(pa3, L, M)["qty"]
    okg, whyg = pa3.add_to_position(L, M, pid3, 1.0, 40.0, "c:add", marks=mk(pa3))
    check("lane cap 2.5 refuses add on the enlarged basis (2.0 + 1.0)", okg is False and "lane cap" in whyg and near(lot(pa3, L, M)["qty"], q0, 1e-12) and pa3.applied_add("c:add") is None, repr((okg, whyg)))
    h4, pa4 = env({"PAPER_ACCOUNT_MAX_TOTAL_EXPOSURE_USD": "2.5"})
    ok, why, pid4 = pa4.try_debit(L, M, 2.0, 100.0, "g:open", marks=mk(pa4)); q0 = lot(pa4, L, M)["qty"]
    okg, whyg = pa4.add_to_position(L, M, pid4, 1.0, 40.0, "g:add", marks=mk(pa4))
    check("gates apply to an add (total-exposure cap 2.5)", okg is False and near(lot(pa4, L, M)["qty"], q0, 1e-12) and pa4.applied_add("g:add") is None, repr((okg, whyg)))
    h5, pa5 = env()
    ok, why, pid5 = pa5.try_debit(L, M, 2.0, 100.0, "o:open", marks=mk(pa5)); os.environ["PAPER_ACCOUNT_GATES_ENABLED"] = "0"
    q0 = lot(pa5, L, M)["qty"] if False else 100.0
    okg, whyg = pa5.add_to_position(L, M, pid5, 1.0, 40.0, "o:add", marks=None)
    os.environ["PAPER_ACCOUNT_GATES_ENABLED"] = "1"
    check("gates off -> refused 'mandatory gates off', nothing applied", okg is False and "gates off" in whyg and pa5.applied_add("o:add") is None and near(lot(pa5, L, M)["qty"], q0, 1e-12), repr((okg, whyg)))
    # quarantined cash: spendable < stake while total cash covers it
    h6, pa6 = env()
    ok, why, pidA = pa6.try_debit(L, M, 2.0, 100.0, "q:A", marks=mk(pa6))
    ok, why, pidB = pa6.try_debit(L, "RSQMINTpump", 7.5, 100.0, "q:B", marks=mk(pa6))
    okq, whyq = pa6.credit(L, "RSQMINTpump", 7.0, "q:Bclose", pos_id=pidB, qty=None, fee=0.0, price_kind=None)   # estimated-price proceeds: cash, NOT spendable
    sq = snap(pa6)
    say("INFO", "quarantine precondition: cash=%.6f spendable=%.6f estimated=%.6f (credit ok=%s)" % (sq["cash"], sq["cash_spendable"], float(sq.get("cash_estimated", 0.0)), okq))
    if float(sq["cash"]) >= 1.0 > float(sq["cash_spendable"]):
        oqa, wqa = pa6.add_to_position(L, M, pidA, 1.0, 40.0, "q:add", marks=mk(pa6))
        check("quarantined cash: add refused on SPENDABLE (not total) cash", oqa is False and "spendable" in wqa and pa6.applied_add("q:add") is None, repr((oqa, wqa)))
    else:
        say("INFO", "NOT COVERED: could not reach spendable < stake <= cash on this credit path")
    # degraded / unreadable ledger
    h7, pa7 = env()
    ok, why, pid7 = pa7.try_debit(L, M, 2.0, 100.0, "d:open", marks=mk(pa7))
    open(pa7.LEDGER_PATH, "w").write("{not json")
    try:
        r = pa7.add_to_position(L, M, pid7, 1.0, 40.0, "d:add", marks=mk(pa7))
        check("unreadable/corrupt ledger: add refused, no exception", r[0] is False, repr(r))
    except Exception as e:
        check("unreadable/corrupt ledger: add refused, no exception", False, "RAISED %r (finding)" % (e,))
    try:
        check("unreadable/corrupt ledger: applied_add is None (unknown is never proof)", pa7.applied_add("d:open") is None)
    except Exception as e:
        check("unreadable/corrupt ledger: applied_add is None (unknown is never proof)", False, "RAISED %r (finding)" % (e,))
    # replay divergence: journaled debit_add for a lot that does not exist
    h8, pa8 = env()
    ok, why, pid8 = pa8.try_debit(L, M, 2.0, 100.0, "v:open", marks=mk(pa8))
    seq = int(json.load(open(pa8.LEDGER_PATH)).get("seq") or 0)
    with open(pa8.JOURNAL_PATH, "a") as f:
        f.write(json.dumps({"event": "debit_add", "event_id": "ev#%d" % (seq + 1), "op_id": "v:ghost", "lane": L, "mint": "GHOSTpump", "pos_id": "ghost#1", "stake": 1.0, "qty": 1.0, "seq": seq + 1, "ts": 1.0}) + "\n")
    try:
        r = pa8.add_to_position(L, M, pid8, 1.0, 40.0, "v:add", marks=mk(pa8))
        check("replay divergence (journaled add for a missing lot): account refuses, nothing applied", r[0] is False and pa8.applied_add("v:ghost") is None, repr(r))
    except Exception as e:
        check("replay divergence (journaled add for a missing lot): account refuses, nothing applied", False, "RAISED %r" % (e,))
    # fault BEFORE the journal append
    h9, pa9 = env()
    ok, why, pid9 = pa9.try_debit(L, M, 2.0, 100.0, "b:open", marks=mk(pa9))
    real_j = pa9._journal
    def jboom(ev, strict=False): raise OSError("INJECTED FAULT: journal append failed")
    pa9._journal = jboom
    r = pa9.add_to_position(L, M, pid9, 1.0, 40.0, "b:add", marks=mk(pa9))
    pa9._journal = real_j
    check("fault BEFORE journal append: add refused, nothing applied", r[0] is False and "journal" in r[1] and near(lot(pa9, L, M)["qty"], 100.0, 1e-9) and pa9.applied_add("b:add") is None, repr(r))
    r2 = pa9.add_to_position(L, M, pid9, 1.0, 40.0, "b:add", marks=mk(pa9))
    check("after that fault, the retry applies exactly once", r2[0] is True and near(lot(pa9, L, M)["qty"], 140.0, 1e-9), repr(r2))
    # crash AFTER journal append, BEFORE state save (named fault)
    h10, pa10 = env()
    ok, why, pid10 = pa10.try_debit(L, M, 2.0, 100.0, "k:open", marks=mk(pa10))
    real_save = pa10._save_locked; st = {"n": 0}
    def boom(led): st["n"] += 1; raise RuntimeError("INJECTED FAULT: crash between journal append and state save")
    pa10._save_locked = boom; crashed = False
    try: pa10.add_to_position(L, M, pid10, 1.0, 40.0, "k:add", ts=3.0, marks=mk(pa10))
    except RuntimeError: crashed = True
    pa10._save_locked = real_save
    check("fault injected after the journal append (crash model)", crashed and st["n"] == 1)
    for m in [m for m in sys.modules if m.startswith("daemon")]: del sys.modules[m]
    from daemon.launch_scanner import paper_account as pa11
    p = lot(pa11, L, M); s = snap(pa11)
    check("restart recovery: the journaled add is applied exactly once (qty 140, basis 3.0)", p is not None and near(p["qty"], 140.0, 1e-9) and near(p["basis"], 3.0, 1e-9), repr(p and (p["qty"], p["basis"])))
    check("restart recovery: spendable cash reduced by the cost once (7.0)", near(s["cash_spendable"], 7.0), repr(s["cash_spendable"]))
    okr, whyr = pa11.add_to_position(L, M, pid10, 1.0, 40.0, "k:add", marks=mk(pa11))
    check("retry after crash: original outcome, not re-applied", okr is True and "retry" in whyr and near(lot(pa11, L, M)["qty"], 140.0, 1e-9), repr((okr, whyr)))
    ad = pa11.applied_add("k:add")
    check("adoption evidence after crash: applied_add readable", bool(ad) and near(ad["qty"], 40.0, 1e-12), repr(ad))
    say("INFO", "NOT COVERED (labelled): cash-split migration (_derive_buckets_locked) debit_add branch; torn-tail/torn mid-file journal with a debit_add; concurrent callers; the tranche side (adoption, legacy flag, gates) which is a separate test.")
    say("INFO", "leftover scratch homes (not deleted): %s" % ", ".join(HOMES))
    print("SUMMARY PASS=%d FAIL=%d INFO=%d" % (RES["PASS"], RES["FAIL"], RES["INFO"]))
    return 1 if RES["FAIL"] else 0
if __name__ == "__main__":
    sys.exit(main())
