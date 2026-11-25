#!/usr/bin/env python3
"""SOURCE ONLY, UNEXECUTED (written, not run). Needs main's explicit clearance for this exact blob before any run. Contains DELIBERATE SIGKILLs of child processes at instrumented, child-local seams (no production change).
Evidence class: process-crash (SIGKILL) at the instrumented site only; NOT power loss. Imports tests/real_lane_two_process_harness-SOURCE-ONLY.py (spawn/reap/token helpers) and
tests/real_lane_crash_window_harness-SOURCE-ONLY.py (EXTRA helpers) as modules (import runs nothing) plus the hook helper. TESTS THE CANDIDATE daemon/launch_scanner/grad_entry_intent.py + grad.py hooks with
AGT_GRAD_ENTRY_INTENT_ENABLED set per child ("1" on / "0" off). Real grad.py, real paper_account, real demo_fill, temp homes, the shared $10 DEMO account only, no network, no executor.
Every case asserts specific expectations derived from the candidate source; a failure is information about the candidate, never relaxed. Kill sites: A after try_debit before the lane write (grad._INFLIGHT_OPEN.add seam,
as the P3 baseline); B after the entry-intent record is durable and before try_debit (PA.try_debit wrapper); C after the lane write before the record unlink (entry-intent module remove_record wrapper).
R1  (flag on, kill A, NO retry): a fresh poll_exits (recovery pass runs before the exit snapshot, price == entry so no exit fires) ADOPTS: lane holds the position with pa_pos_id == the killed pos_id, tokens == account qty,
    record directory empty, exactly one debit, cash 10 - stake, lane bankroll decremented once (== start - stake), entries_day {today, 1}. THEN a different mint enters (HYPOTHESIS, marks provable now): (True, "opened"), two debits.
R1x same kill but the recovery process pretends a later day (GR._today patched in the child): adoption still happens but NO daily counter is charged (entries_day unchanged), the explicit attribution rule.
R2  (flag on, kill B, record only): record exists, NO debit. A fresh entry for a DIFFERENT mint runs the recovery pass at its start: class C drops the record and the demo intent; the new entry opens; exactly one debit (the new mint's), no debit for the killed mint.
R3  (flag on, kill C, lane committed, record left): fresh poll_exits: class A unlinks only; lane position unchanged (same pa_pos_id, still one), bankroll decremented ONCE total, entries_day count 1, one debit.
R4  (flag on, kill A, concurrency): a same-candidate retry process and a poll_exits recovery process start together after a start barrier. INVARIANTS: exactly one debit, one lane position with pa_pos_id == killed pos_id, tokens == account qty, bankroll decremented once,
    entries_day count 1, record directory empty, AT MOST ONE class-B adoption across both children (captured from the recover_pass return value; zero if the retry's own op replay committed first), retry result in {opened, already open (race)} consistent with who adopted (reported, INFO).
R5  (flag on, frozen recovery, known exits continue): a legitimate position is open; a corrupt record file is planted in the entry-intent directory (an UNREADABLE record is the freeze condition); poll_exits at 3x still performs the 2 exit credits (as the S1/TP2/TP3 baselines), while a new-mint entry is refused with a reason naming the record and fail-closed,
    no new debit, and the corrupt file is byte-identical afterward.
R6  (flag-off comparison): the same entry with flag "0" and flag "1" in two homes: lane position, account debit and cash fields are EQUAL; the flag-off home has NO entry-intents directory; the flag-on home's directory exists and is empty. R7  (flag on, populated same-day counter): first entry opens normally (counter {today,1}); a SECOND entry (mint 2) is killed after its debit (seam A); a fresh poll recovery ADOPTS it: lane holds both positions, mint-2 pa_pos_id == killed, debits 2, cash 8.0, counter == {today, 2}. (HYPOTHESIS: the second entry passes the marks gate with one existing lane position, as P2/S1 observed for second entries.)
R8  same setup, recovery process patched to a LATER day while the stored counter day still equals the original entry day: counter == {original day, 2}; the entry is charged to its own day, not the later one.
R6b: flag "0" + kill A + poll_exits:
    the orphan is NOT recovered (lane 0, account open 1), i.e. flag-off behavior is the unchanged baseline.
CLAIM LIMITS: one machine, SIGKILL not power loss; fixture stubs; R1 hypothesis (second mint opened) is from the marks-gate source read and is reported with its result either way; R4 is one interleaving class, not a race proof;
not covered: other openers, status/account visibility of orphan states, non-demo mode, ledger-corruption variants."""
import importlib.util, json, os, sys, time, hashlib
HERE = os.path.dirname(os.path.abspath(__file__))
def _load(n, f):
    sp = importlib.util.spec_from_file_location(n, os.path.join(HERE, f)); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m); return m
B = _load("tp3base", "real_lane_two_process_harness-SOURCE-ONLY.py")
CW = _load("cwbase", "real_lane_crash_window_harness-SOURCE-ONLY.py")
H = B.H; check = B.check; FAILS = B.FAILS
PRE = H.PRE2 + B.CTLP + CW.EXTRA + r'''
os.environ["AGT_GRAD_ENTRY_INTENT_ENABLED"] = sys.argv[3]
EIM = None
if sys.argv[3] == "1":
    EIM = GR._ei()
def rec_dir(): return GR.LEDGER_PATH + ".entry-intents"
def rec_files():
    try: return sorted(os.listdir(rec_dir()))
    except FileNotFoundError: return None
def full():
    L = raw_ledger()
    return {"acct": acct(), "lane": lane_state(), "intents": intents_map(), "j": jstate(), "rec_files": rec_files(), "entries_day": L.get("entries_day"), "bankroll": L.get("bankroll"), "bankroll_start": L.get("bankroll_start")}
ACTS = []
if EIM is not None:
    _rp = EIM.recover_pass
    def _cap(*a, **k):
        r = _rp(*a, **k); ACTS.extend([list(x) for x in r[1]]); return r
    EIM.recover_pass = _cap
T.register(GR)
'''
KA = PRE + r'''
class _Seam(set):
    def add(self, x):
        token("SEAM_REACHED", pa_pos_id=str(x), intents=intents_map(), accts=acct(), rec_files=rec_files())
        os.kill(os.getpid(), signal.SIGKILL)
GR._INFLIGHT_OPEN = _Seam()
n = int(sys.argv[4]) if len(sys.argv) > 4 else 1
r = GR.consider_candidate(mkcand(n), time.time()); token("SURVIVED", r=str(r)); os._exit(99)
'''
KB = PRE + r'''
def _kill_before_debit(*a, **k):
    token("SEAM_REACHED", rec_files=rec_files(), accts=acct(), intents=intents_map())
    os.kill(os.getpid(), signal.SIGKILL)
PA.try_debit = _kill_before_debit
r = GR.consider_candidate(mkcand(1), time.time()); token("SURVIVED", r=str(r)); os._exit(99)
'''
KC = PRE + r'''
def _kill_before_unlink(path):
    token("SEAM_REACHED", rec_files=rec_files(), lane=lane_state(), accts=acct())
    os.kill(os.getpid(), signal.SIGKILL)
EIM.remove_record = _kill_before_unlink
r = GR.consider_candidate(mkcand(1), time.time()); token("SURVIVED", r=str(r)); os._exit(99)
'''
POLL = PRE + r'''
if len(sys.argv) > 4 and sys.argv[4] == "future":
    GR._today = lambda: "2099-01-01"
b0 = full()
mult = float(sys.argv[5]) if len(sys.argv) > 5 else 1.0
FakeJup.mult = mult
try: cl = GR.poll_exits(price_fn=gpf(mult))
finally: FakeJup.mult = 1.0
print(json.dumps({"name": name, "before": b0, "after": full(), "acts": ACTS, "ncl": len(cl or [])}, default=str))
'''
ENTER = PRE + r'''
n = int(sys.argv[4])
if len(sys.argv) > 5 and sys.argv[5] == "barrier": wait_for(os.path.join(CTL, "GO"), 20.0)
r = GR.consider_candidate(mkcand(n), time.time())
print(json.dumps({"name": name, "ok": bool(r[0]), "why": str(r[1]), "final": r[2], "acts": ACTS, "after": full()}, default=str))
'''
POLLB = POLL.replace('b0 = full()', 'if os.path.exists(os.path.join(CTL, "GO")) or True: wait_for(os.path.join(CTL, "GO"), 20.0)\nb0 = full()')
OPEN1 = PRE + r'''
r = GR.consider_candidate(mkcand(1), time.time())
print(json.dumps({"name": name, "ok": bool(r[0]), "why": str(r[1]), "after": full()}, default=str))
'''
def go(h): fh = open(os.path.join(h, "ctl", "GO"), "w"); fh.write("1"); fh.flush(); os.fsync(fh.fileno()); fh.close()
def kill_phase(h, code, flag, label, n=None):
    pk = B.spawn(code, h, "k" + label, flag, *([] if n is None else [n]))
    try: pk.wait(timeout=min(30, max(1.0, B.left())))
    except Exception: raise B.Stop("kill child did not exit")
    t = B.tok(h, "k" + label, "SEAM_REACHED", 1.0)
    check("%s kill child hit the seam (fsynced token) and died by SIGKILL (rc %s), SURVIVED absent" % (label, pk.returncode), t is not None and pk.returncode == -9 and B.tok(h, "k" + label, "SURVIVED", 0.0) is None)
    return t
def run(h, code, name, *args):
    p = B.spawn(code, h, name, *args); r = B.finish(p)
    if isinstance(r, dict) and r.get("acts"):  # DIAGNOSTIC (read-only): every recovery action as (op, class, bounded enum reason); reasons are fixed classify() strings, no secrets
        print("INFO %s recovery actions: %s" % (name, [(str(x[0])[-24:], x[2], str(x[3])[:200]) for x in r["acts"]]))
    return p, r
def debits(j): return B.stakes(j)
def r1(future=False):
    lab = "R1x" if future else "R1"; h = B.newhome(); t = kill_phase(h, KA, "1", lab)
    p, r = run(h, POLL, "p1", "1", "future" if future else "now", 1.0); b, a = r["before"], r["after"]
    pid = t["pa_pos_id"]; pos = list(a["lane"]["positions"].values())
    check("%s pre-recovery: record present (%s), lane 0, account open 1" % (lab, b["rec_files"]), len(b["rec_files"] or []) == 1 and b["lane"]["positions"] == {} and len(b["acct"]) == 1)
    check("%s ADOPTED by the poll recovery pass: one lane position, pa_pos_id %s == killed %s, tokens == account qty, record dir empty, actions %s" % (lab, [x.get("pa_pos_id") for x in pos], pid, [(x[2]) for x in r["acts"]]),
          p.returncode == 0 and len(pos) == 1 and pos[0]["pa_pos_id"] == pid and len(a["acct"]) == 1 and abs(float(a["acct"][0]["qty"]) - float(pos[0]["tokens"])) < 1e-9 and a["rec_files"] == [] and [x[2] for x in r["acts"]] == ["B"])
    st = debits(a["j"])
    check("%s one debit %s, cash %s == 10 - stake, lane bankroll %s == start %s - stake" % (lab, st, a["j"]["cash"], a["bankroll"], a["bankroll_start"]),
          len(st) == 1 and abs(float(a["j"]["cash"]) - (10.0 - st[0])) < 1e-5 and abs(float(a["bankroll"]) - (float(a["bankroll_start"]) - st[0])) < 1e-5)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if future:
        check("R1x explicit attribution: entries_day UNCHANGED %s (no later day charged for the earlier entry)" % (a["entries_day"],), a["entries_day"] == b["entries_day"])
    else:
        check("R1 entries_day %s == {date today, count 1} (the original entry's day)" % (a["entries_day"],), a["entries_day"] == {"date": today, "count": 1})
        p2, r2 = run(h, ENTER, "e2", "1", 2); j2 = r2["after"]["j"]
        check("R1 HYPOTHESIS second mint after adoption: (%s, %s), debits %s" % (r2["ok"], r2["why"][:90], debits(j2)), p2.returncode == 0 and r2["ok"] is True and r2["why"] == "opened" and len(debits(j2)) == 2)
def r2():
    h = B.newhome(); t = kill_phase(h, KB, "1", "R2")
    check("R2 at the kill: record present %s, NO debit (account open %d)" % (t["rec_files"], len(t["accts"])), len(t["rec_files"] or []) == 1 and t["accts"] == [])
    p, r = run(h, ENTER, "e2", "1", 2); a = r["after"]
    check("R2 recovery class C ran %s, record dropped (%s), killed-mint intent dropped (%s)" % ([x[2] for x in r["acts"]], a["rec_files"], a["intents"]), [x[2] for x in r["acts"]] == ["C"] and a["rec_files"] == [] and not any("GradMint1pump" in k for k in (a["intents"] or {})))
    check("R2 new mint opened (%s %s), exactly one debit %s, lane positions %s" % (r["ok"], r["why"][:60], debits(a["j"]), sorted(a["lane"]["positions"])), r["ok"] is True and r["why"] == "opened" and len(debits(a["j"])) == 1 and sorted(a["lane"]["positions"]) == ["GradMint2pump"])
def r3():
    h = B.newhome(); t = kill_phase(h, KC, "1", "R3")
    check("R3 at the kill: lane holds %s, record still present %s" % (sorted(t["lane"]["positions"]), t["rec_files"]), len(t["lane"]["positions"]) == 1 and len(t["rec_files"] or []) == 1)
    p, r = run(h, POLL, "p1", "1", "now", 1.0); b, a = r["before"], r["after"]
    check("R3 class A ran %s: record unlinked, same single lane position, one debit, bankroll decremented ONCE, entries_day unchanged from before recovery (%s)" % ([x[2] for x in r["acts"]], a["entries_day"]),
          [x[2] for x in r["acts"]] == ["A"] and a["rec_files"] == [] and a["lane"] == b["lane"] and len(debits(a["j"])) == 1 and a["bankroll"] == b["bankroll"] and a["entries_day"] == b["entries_day"] and a["entries_day"]["count"] == 1)
def r7(future=False):
    lab = "R8" if future else "R7"; h = B.newhome(); p0, r0 = run(h, OPEN1, "o1", "1")
    check("%s setup: first entry opened normally, counter populated %s" % (lab, r0["after"]["entries_day"]), p0.returncode == 0 and r0["ok"] is True and r0["after"]["entries_day"].get("count") == 1)
    t = kill_phase(h, KA, "1", lab, 2); pid = t["pa_pos_id"]
    p, r = run(h, POLL, "p1", "1", "future" if future else "now", 1.0); b, a = r["before"], r["after"]
    check("%s pre-recovery: record present, lane holds ONLY the first mint %s, account open %d, counter still %s" % (lab, sorted(b["lane"]["positions"]), len(b["acct"]), b["entries_day"]), len(b["rec_files"] or []) == 1 and sorted(b["lane"]["positions"]) == ["GradMint1pump"] and len(b["acct"]) == 2 and b["entries_day"].get("count") == 1)
    lp = a["lane"]["positions"]; st = debits(a["j"])
    check("%s ADOPTED second position: classes %s, lane mints %s, GradMint2pump pa_pos_id == killed %s, tokens == account qty for BOTH, record dir empty" % (lab, [x[2] for x in r["acts"]], sorted(lp), pid),
          p.returncode == 0 and [x[2] for x in r["acts"]] == ["B"] and sorted(lp) == ["GradMint1pump", "GradMint2pump"] and lp["GradMint2pump"]["pa_pos_id"] == pid and len(a["acct"]) == 2 and a["rec_files"] == []
          and all(abs(float(x["qty"]) - float(lp[x["mint"]]["tokens"])) < 1e-9 for x in a["acct"]))
    check("%s two debits %s, cash %s == 10 - sum, bankroll %s == start %s - sum" % (lab, st, a["j"]["cash"], a["bankroll"], a["bankroll_start"]),
          len(st) == 2 and abs(float(a["j"]["cash"]) - (10.0 - sum(st))) < 1e-5 and abs(float(a["bankroll"]) - (float(a["bankroll_start"]) - sum(st))) < 1e-5)
    exp_date = b["entries_day"].get("date")
    check("%s daily counter %s == {date %s, count 2}%s" % (lab, a["entries_day"], exp_date, " (recovery process pretends a LATER day, stored counter day == the original entry day: still charged to that day)" if future else " (same day)"),
          a["entries_day"] == {"date": exp_date, "count": 2} and (future or exp_date == time.strftime("%Y-%m-%d", time.gmtime())))
def r4():
    h = B.newhome(); t = kill_phase(h, KA, "1", "R4"); pid = t["pa_pos_id"]
    pe = B.spawn(ENTER, h, "e1", "1", 1, "barrier"); pp = B.spawn(POLLB, h, "p1", "1", "now", 1.0)
    time.sleep(0.5); go(h); re_ = B.finish(pe); rp = B.finish(pp)
    state = B.spawn(PRE + 'print(json.dumps({"after": full()}, default=str))', h, "v", "1"); sv = B.finish(state)["after"]
    pos = list(sv["lane"]["positions"].values()); acts = [x[2] for x in (re_ or {}).get("acts", [])] + [x[2] for x in (rp or {}).get("acts", [])]
    st = debits(sv["j"])
    check("R4 final: exactly one debit %s, one lane position with pa_pos_id == killed (%s), tokens == account qty, record dir empty %s" % (st, [x["pa_pos_id"] for x in pos], sv["rec_files"]),
          len(st) == 1 and len(pos) == 1 and pos[0]["pa_pos_id"] == pid and len(sv["acct"]) == 1 and abs(float(sv["acct"][0]["qty"]) - float(pos[0]["tokens"])) < 1e-9 and sv["rec_files"] == [])
    check("R4 bankroll decremented once (%s vs start %s - %s), entries_day count %s == 1" % (sv["bankroll"], sv["bankroll_start"], st, sv["entries_day"]),
          len(st) == 1 and abs(float(sv["bankroll"]) - (float(sv["bankroll_start"]) - st[0])) < 1e-5 and sv["entries_day"].get("count") == 1)
    check("R4 at most ONE class-B adoption across both children (classes seen %s); retry result (%s, %s) INFO" % (acts, (re_ or {}).get("ok"), str((re_ or {}).get("why"))[:60]), acts.count("B") <= 1 and pe.returncode == 0 and pp.returncode == 0)
def r5():
    h = B.newhome(); p0, r0 = run(h, OPEN1, "o1", "1")
    check("R5 setup: one legitimate position opened (%s)" % (r0["ok"],), p0.returncode == 0 and r0["ok"] is True)
    d = os.path.join(h, "led", "grad.json.entry-intents"); os.makedirs(d, exist_ok=True); f = os.path.join(d, "planted.json"); open(f, "w").write("{corrupt")
    before = hashlib.sha256(open(f, "rb").read()).hexdigest()
    p1, r1_ = run(h, POLL, "p1", "1", "now", 3.0); j = r1_["after"]["j"]; creds = [e for e in j["events"] if e["event"] == "credit"]
    check("R5 known exits continue while recovery is frozen: poll_exits at 3x credited %d (expected 2), the corrupt record did not gate the exit path" % len(creds), p1.returncode == 0 and len(creds) == 2)
    p2, r2_ = run(h, ENTER, "e2", "1", 2)
    check("R5 new-mint entry refused fail-closed naming the record: (%s) %s" % (r2_["ok"], r2_["why"][:140]), r2_["ok"] is False and "entry-intent" in r2_["why"] and "fail-closed" in r2_["why"] and len(debits(r2_["after"]["j"])) == 1)
    check("R5 corrupt record byte-identical afterward (kept as evidence)", hashlib.sha256(open(f, "rb").read()).hexdigest() == before)
def r6():
    ho, hn = B.newhome(), B.newhome(); po, ro = run(ho, OPEN1, "o1", "0"); pn, rn = run(hn, OPEN1, "o1", "1"); ao, an = ro["after"], rn["after"]
    def norm(a):
        p = list(a["lane"]["positions"].values())[0]; d = a["acct"][0]
        return (round(float(p["tokens"]), 9), p["pa_pos_id"], round(float(d["qty"]), 9), a["j"]["cash"], a["bankroll"], debits(a["j"]), a["entries_day"].get("count"))
    check("R6 flag off vs on, same entry: position/account/cash/bankroll/counter fields equal (off %s, on %s)" % (norm(ao), norm(an)), ro["ok"] and rn["ok"] and norm(ao) == norm(an))
    check("R6 flag off creates NO entry-intents directory (%s); flag on leaves the directory empty (%s)" % (ao["rec_files"], an["rec_files"]), ao["rec_files"] is None and an["rec_files"] == [])
    h = B.newhome(); t = kill_phase(h, KA, "0", "R6b"); p, r = run(h, POLL, "p1", "0", "now", 1.0); a = r["after"]
    check("R6b flag off + kill A + poll_exits: orphan NOT recovered (lane %s, account open %d, no dir %s): baseline behavior unchanged" % (a["lane"]["positions"], len(a["acct"]), a["rec_files"]), a["lane"]["positions"] == {} and len(a["acct"]) == 1 and a["rec_files"] is None)
def main():
    try:
        for n, f in (("R1", r1), ("R1x", lambda: r1(True)), ("R2", r2), ("R3", r3), ("R4", r4), ("R5", r5), ("R6", r6), ("R7", r7), ("R8", lambda: r7(True))): B.scen(n, f)
    finally: B.reap_all()
    alive = [p.pid for p in B.KIDS if p.poll() is None]
    check("all children reaped (still alive: %s), total %d children spawned" % (alive, len(B.KIDS)), not alive)
    print("elapsed %.1fs of 90s budget; FAILS: %d" % (90.0 - B.left(), len(FAILS))); return 1 if FAILS else 0
if __name__ == "__main__":
    sys.exit(main())
