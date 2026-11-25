"""bb75 REAL-MODULE lifecycle-admission proof (SOURCE ONLY; NOT RUN until main clears). Temp dirs only, FOREX_AGENT_HOME set before import, asserted.
Real paper_account (try_debit, credit, registry, restart_admission_blockers, _entries_blocked_by_refusal), the real close_intents.begin writer, and FRESH child processes.
Proves: L1 a first-ever start (no account ledger, WAL, registry, lane ledger) is admitted at account level AND a real debit then succeeds; L2 POSITIVE a matching, account-proven
position with a registered lane blocks nothing and the registry primary+mirror exist; L3 account ledger deleted while the WAL remains = account-state-lost, never fresh;
L4 account ledger+WAL+registry deleted but a bound lane position remains = blocked; L5 registry primary lost with the mirror kept blocks ACCOUNT-level entries;
L6 corrupt registry blocks; L7 lane ledger lost while the account holds lots blocks at account level; L8 position bound to the wrong entry op blocks; L9 an orphan account lot
(no lane position) blocks the lane restart; L10 the real close_intents.begin registers its journal and its later loss blocks at account level; L11 generation mismatch blocks
at ACCOUNT level (a lane that never called its own check); L12 FRESH PROCESS: account admission refuses on persisted evidence alone, and a fresh process on matching state
admits; L13 the marker latch survives marker deletion and a journal read failure in-process; L14 MUTANT (registry check disabled) admits L11 so it is sensitive; L15 static wiring; L16 an account generation created after lane registration is recorded as a witness (ledger+WAL loss then = state-lost); L17 real begin+freeze: pending alone admits, frozen-unapplied and unreadable intent journals block at account level; L18 real dual-write failure in a child, storage restored, fresh child refuses; L19 register-after-primary-loss refuses and the mirror keeps both lanes; L20 torn registry tail tolerated and repaired; L21 a torn intent-journal tail is never read as clean; L22 registry lock serialises appends (blocked while held, mutant sensitivity); L23 six concurrent registrants on a torn tail keep every record; L24 a vanished mirror is rebuilt with ALL primary records, not only the newest; L25 a lagging mirror is completed; L26 a torn mirror tail is trimmed; L27 mutant sensitivity.
NOT proven: lane runtime wiring (needs lane harnesses), whole-filesystem loss (no inside witness exists), journal rotation, grad closed-position witness, sim idempotency."""
import ast, json, os, subprocess, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n, flush=True)
    if not c: FAILS.append(n)
_home = tempfile.mkdtemp(prefix="genbind-home-")
os.environ["FOREX_AGENT_HOME"] = _home
for _k, _v in (("PAPER_ACCOUNT_ENABLED", "1"), ("PAPER_ACCOUNT_GATES_ENABLED", "1"), ("PAPER_ACCOUNT_MAX_DAILY_LOSS_USD", "5.0"),
               ("PAPER_ACCOUNT_MAX_DRAWDOWN_PCT", "50.0"), ("PAPER_ACCOUNT_MAX_EXPOSURE_PCT", "100.0"), ("PAPER_ACCOUNT_MAX_SIMULTANEOUS", "1000"),
               ("PAPER_ACCOUNT_MAX_POSITIONS", "1000"), ("PAPER_ACCOUNT_MAX_DATA_AGE_S", "100000"),
               ("PAPER_ACCOUNT_MAX_TOTAL_EXPOSURE_USD", "1000000"), ("PAPER_ACCOUNT_MAX_TOKEN_EXPOSURE_USD", "1000000")):
    os.environ[_k] = _v
from daemon.launch_scanner import paper_account as PA
if not PA.ACCOUNT_DIR.startswith(_home):
    print("ABORT: not isolated"); sys.exit(2)
LANE = "genbind_fixture_lane"
MK = '''
def _mk():
    try: led = json.load(open(PA.LEDGER_PATH))
    except Exception: return None
    out = {}
    for lane, st in (led.get("lanes") or {}).items():
        for k, pos in (st.get("open") or {}).items():
            key = pos["pos_id"] if pos.get("instance") is True else k
            out.setdefault(lane, {})[key] = {"value": float(pos["basis"]), "ts": time.time(), "source": "jup-v3", "qty": float(pos["qty"]), "pos_id": pos["pos_id"]}
    return out or None
'''
exec(MK)
def _use(d):
    os.makedirs(d, exist_ok=True)
    PA.ACCOUNT_DIR = d; PA.LEDGER_PATH = os.path.join(d, "ledger.json"); PA.JOURNAL_PATH = os.path.join(d, "events.jsonl"); PA.LOCK_FILE = os.path.join(d, ".lock")
    PA._MARKER_FAILED[0] = False; PA._MARKER_SEEN[0] = False; PA._MF_SCAN.update(key=None, off=0, found=False); PA._PROBED.clear()
def _need(label, r):
    if not (isinstance(r, tuple) and r[0] is True):
        print("FAIL SETUP %s: %r" % (label, r)); raise SystemExit(2)
    return r
def acct_id(): return json.load(open(PA.LEDGER_PATH))["account_id"]
def lane_ledger(path, positions):
    json.dump({"positions": positions, "closes": []}, open(path, "w"))
def pos(mint, pid, bound):
    p = {"lane": LANE, "mint": mint, "pa_pos_id": pid}
    if bound is not None: p["pa_account_id"] = bound
    return p
def old_scan(path):   # the V9c/BB73 unbound-only comprehension, as the lanes used it before this revision
    d = json.load(open(path))
    return [str(m) for m, p in (d.get("positions") or {}).items() if isinstance(p, dict) and p.get("pa_pos_id") and not p.get("pa_account_id")]


_ci_home = _home
from daemon.launch_scanner import close_intents as CI
env = dict(os.environ); env["PYTHONPATH"] = ROOT
def fresh(name):
    d = os.path.join(_home, name); _use(d)
    os.environ["FOREX_AGENT_HOME"] = os.path.join(d, "agenthome"); os.makedirs(os.environ["FOREX_AGENT_HOME"], exist_ok=True)   # PER-CASE intent-journal home (BB75c L17e leaked into later cases)
    return d, os.path.join(d, "lane.json")
def lane_ledger(path, positions):
    json.dump({"positions": positions, "closes": []}, open(path, "w"))
def mkpos(mint, pid, bound, op):
    return {"lane": LANE, "mint": mint, "pa_pos_id": pid, "pa_account_id": bound, "pa_entry_op": op}
def good(name, mint="M"):
    d, LP = fresh(name)
    op = "lc:entry:%s" % mint
    r = _need("debit " + name, PA.try_debit(LANE, mint, 1.0, 100.0, op, marks=_mk()))
    ID = acct_id()
    lane_ledger(LP, {mint: mkpos(mint, r[2], ID, op)})
    assert PA.register_lane(LANE, LP, CI.journal_path(LANE))
    return d, LP, r[2], ID, op
def blocked(): return PA._entries_blocked_by_refusal()

# L1 first-ever start
d, LP = fresh("L1")
check("L1a first-ever start: lane check empty (%r)" % PA.restart_admission_blockers(LANE, LP), PA.restart_admission_blockers(LANE, LP) == [])
check("L1b first-ever start: account-level admission open (%r)" % blocked(), blocked() is None)
r1 = PA.try_debit(LANE, "MFRESH", 1.0, 100.0, "lc:entry:fresh", marks=_mk())
check("L1c first-ever start: a real debit succeeds and no registry is invented (%r)" % (r1,), r1[0] is True and not os.path.exists(PA._registry_paths()[0]))
# L2 positive
d, LP, pid, ID, op = good("L2")
check("L2a matching state: lane check empty (%r)" % PA.restart_admission_blockers(LANE, LP), PA.restart_admission_blockers(LANE, LP) == [])
check("L2b matching state: account admission open (%r)" % blocked(), blocked() is None)
check("L2c registry primary and mirror both written", all(os.path.exists(p) for p in PA._registry_paths()))
# L3 account ledger deleted, WAL stays
d, LP, pid, ID, op = good("L3"); os.unlink(PA.LEDGER_PATH)
w = PA.restart_admission_blockers(LANE, LP)
check("L3 ledger deleted, WAL remains: account-state-lost (%r)" % w, len(w) == 1 and w[0].startswith("account-state-lost"))
check("L3b and account-level admission refuses (%r)" % blocked(), blocked() is not None)
# L4 everything of the account gone, lane position remains
d, LP, pid, ID, op = good("L4")
for p in (PA.LEDGER_PATH, PA.JOURNAL_PATH) + PA._registry_paths(): os.unlink(p)
w = PA.restart_admission_blockers(LANE, LP)
check("L4 account+WAL+registry gone, bound lane position remains (%r)" % w, w == ["M@account-identity-unavailable"])
# L5 registry primary lost
d, LP, pid, ID, op = good("L5"); os.unlink(PA._registry_paths()[0])
check("L5 registry primary lost, mirror kept: account-level refuses (%r)" % blocked(), blocked() is not None and "registry lost" in blocked())
# L6 corrupt registry
d, LP, pid, ID, op = good("L6"); open(PA._registry_paths()[0], "w").write("{bad\n")
check("L6 corrupt registry refuses (%r)" % blocked(), blocked() is not None and "unreadable" in blocked())
# L7 lane ledger lost
d, LP, pid, ID, op = good("L7"); os.unlink(LP)
check("L7 lane ledger lost with lots: account-level refuses (%r)" % blocked(), blocked() is not None and "lane-ledger-missing" in blocked())
# L8 wrong entry op
d, LP, pid, ID, op = good("L8"); lane_ledger(LP, {"M": mkpos("M", pid, ID, "lc:entry:OTHER")})
check("L8 wrong entry op blocks (%r)" % PA.restart_admission_blockers(LANE, LP), PA.restart_admission_blockers(LANE, LP) == ["M@entry-op-mismatch"])
check("L8b ...at account level", blocked() is not None)
# L9 orphan
d, LP, pid, ID, op = good("L9"); lane_ledger(LP, {})
w = PA.restart_admission_blockers(LANE, LP)
check("L9 orphan lot reported at lane restart (%r)" % w, w == ["%s@orphan-lot" % pid])
check("L9b orphan_lots diagnostic agrees", PA.orphan_lots(LANE, LP) == [pid])
# L10 real intent writer
d, LP, pid, ID, op = good("L10")
CI_HOME_BEFORE = CI.journal_path(LANE)
if os.path.exists(CI_HOME_BEFORE): os.unlink(CI_HOME_BEFORE)
try:
    iid = CI.begin(LANE, "M", pid, [{"kind": "credit", "op_id": "lc:exit:M", "amount": 1.0, "qty": 100.0, "price_kind": "mark"}],
                   {"realized_usd": 0.0, "entry_notional_usd": 1.0, "closed_reason": "t", "close_ts": int(time.time())}, account_id=ID)
except Exception as e:
    print("FAIL SETUP begin: %r" % (e,)); raise SystemExit(2)
recs = PA._registry_fold(PA._registry_read(PA._registry_paths()[0])[0])[0]
check("L10a begin registered the intent journal (%r)" % recs.get(LANE), recs.get(LANE, {}).get("intent_seen") is True)
check("L10b journal present: admission open (%r)" % blocked(), blocked() is None)
os.unlink(CI.journal_path(LANE))
check("L10c intent journal lost: account-level refuses (%r)" % blocked(), blocked() is not None and "intent-journal-lost" in blocked())
# L11 generation mismatch at account level, L14 mutant
d, LP, pid, ID, op = good("L11"); lane_ledger(LP, {"M": mkpos("M", pid, "other-generation", op)})
check("L11 generation mismatch refuses at ACCOUNT level with exactly that reason (%r)" % blocked(), blocked() == "persisted lane evidence blocks admission (fail-closed): lane %s: M@generation-mismatch" % LANE)
_rb = PA._registry_blockers
PA._registry_blockers = lambda: []
check("L14 MUTANT: with the registry check off the same state is admitted (sensitivity)", blocked() is None)
PA._registry_blockers = _rb
# L12 fresh processes
d, LP, pid, ID, op = good("L12m")
d2, LP2, pid2, ID2, op2 = good("L12x"); lane_ledger(LP2, {"M": mkpos("M", pid2, "other-generation", op2)})
child = '''
import json, os, sys
from daemon.launch_scanner import paper_account as PA
d = %r
PA.ACCOUNT_DIR = d; PA.LEDGER_PATH = os.path.join(d, "ledger.json"); PA.JOURNAL_PATH = os.path.join(d, "events.jsonl"); PA.LOCK_FILE = os.path.join(d, ".lock")
print(json.dumps({"blocked": PA._entries_blocked_by_refusal()}))
'''
def run_child(dd):
    p = subprocess.run([sys.executable, "-B", "-c", child % dd], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    try: return json.loads(p.stdout.strip().splitlines()[-1])["blocked"]
    except Exception: print("FAIL SETUP child: %s" % p.stderr[-300:]); raise SystemExit(2)
bm = run_child(os.path.join(_home, "L12m")); bx = run_child(os.path.join(_home, "L12x"))
check("L12a FRESH PROCESS on matching state admits (%r)" % bm, bm is None)
check("L12b FRESH PROCESS on a mismatched lane position refuses at account level (%r)" % bx, bx is not None and "generation-mismatch" in bx)
# L13 latch survives
d, LP, pid, ID, op = good("L13")
PA._record_unresolved("lc:x", LANE, "M", pid, "test")
check("L13a marker with a record blocks", blocked() is not None)
os.unlink(PA._unresolved_path())
check("L13b marker deleted afterwards: still blocked in this process (%r)" % blocked(), blocked() is not None)
PA._MARKER_SEEN[0] = False
d, LP, pid, ID, op = good("L13c"); _jm = PA._journal_marker_failed
PA._journal_marker_failed = lambda: True
blocked(); PA._journal_marker_failed = _jm
check("L13c a discovered marker_failed latch survives a later journal read result of clean", PA._MARKER_FAILED[0] is True and blocked() is not None)
PA._MARKER_FAILED[0] = False

# L16 account generation created AFTER the lane registered is recorded as a witness; its later loss (ledger AND WAL gone) is state-lost, not fresh
d, LP = fresh("L16"); assert PA.register_lane(LANE, LP, CI.journal_path(LANE))
_need("debit L16", PA.try_debit(LANE, "M", 1.0, 100.0, "lc:entry:L16", marks=_mk())); blocked()
ids16 = PA._registry_fold(PA._registry_read(PA._registry_paths()[0])[0])[1]
check("L16a the registry recorded the account generation on first admission after it was created", acct_id() in ids16)
os.unlink(PA.LEDGER_PATH); os.unlink(PA.JOURNAL_PATH)
w = PA.restart_admission_blockers(LANE, LP)
check("L16b ledger and WAL both gone: state-lost via the registry witness (%r)" % w, len(w) == 1 and w[0].startswith("account-state-lost"))
# L17 frozen unapplied intent content (real writer: begin + freeze), at ACCOUNT level; a PENDING intent alone does not block
d, LP, pid, ID, op = good("L17")
if os.path.exists(CI.journal_path(LANE)): os.unlink(CI.journal_path(LANE))
iid = CI.begin(LANE, "M", pid, [{"kind": "credit", "op_id": "lc:exit:L17", "amount": 1.0, "qty": 100.0, "price_kind": "mark"}],
               {"realized_usd": 0.0, "entry_notional_usd": 1.0, "closed_reason": "t", "close_ts": int(time.time())}, account_id=ID)
check("L17a a PENDING intent alone does not block account admission (%r)" % blocked(), blocked() is None)
check("L17b freeze written", CI.freeze(LANE, iid, "test freeze") is True)
check("L17c a FROZEN unapplied intent blocks at account level, only that reason (%r)" % blocked(), blocked() is not None and blocked().count("lane %s:" % LANE) == 1 and "frozen unapplied" in blocked())
jl = [json.loads(x) for x in open(CI.journal_path(LANE))]
check("L17d journal content: the intent carries its settle op and the freeze names it", jl[0]["settle_ops"][0]["op_id"] == "lc:exit:L17" and jl[1]["intent_id"] == iid)
open(CI.journal_path(LANE), "a").write("{garbage-mid\n{\"ev\": \"x\"}\n")
check("L17e an unreadable intent journal blocks (%r)" % blocked(), blocked() is not None and "intent journal unreadable" in blocked())
# L18 REAL transient dual write failure in child 1, storage restored, FRESH child 2 refuses at account level
d, LP = fresh("L18")
child1 = '''
import json, os, sys, time
from daemon.launch_scanner import paper_account as PA
''' + MK + '''
LANE = %r
d = %r; LP = os.path.join(d, "lane.json")
PA.ACCOUNT_DIR = d; PA.LEDGER_PATH = os.path.join(d, "ledger.json"); PA.JOURNAL_PATH = os.path.join(d, "events.jsonl"); PA.LOCK_FILE = os.path.join(d, ".lock")
r = PA.try_debit(LANE, "M", 1.0, 100.0, "lc:entry:L18", marks=_mk()); assert r[0] is True, r
ID = json.load(open(PA.LEDGER_PATH))["account_id"]
json.dump({"positions": {"M": {"lane": LANE, "mint": "M", "pa_pos_id": r[2], "pa_account_id": ID, "pa_entry_op": "lc:entry:L18"}}, "closes": []}, open(LP, "w"))
assert PA.register_lane(LANE, LP, None)
# lane persisted a position bound to the OLD generation; then the account is replaced (new generation) in the same dir
_rr, _rj = PA._record_unresolved, PA._journal
def jf(ev, strict=False):
    if ev.get("event") == "settle_generation_refused": raise OSError("injected EIO")
    return _rj(ev, strict)
PA._record_unresolved = lambda *a, **k: False; PA._journal = jf
ok, why = PA.credit(LANE, "M", 1.1, "lc:exit:L18", pos_id=r[2], qty=100.0, expected_account_id="some-other-generation", entry_op_id="lc:entry:L18")
print(json.dumps({"ok": ok, "why": why, "pid": r[2], "id": ID}))
''' % (LANE, d)
p1 = subprocess.run([sys.executable, "-B", "-c", child1], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
try: c18 = json.loads(p1.stdout.strip().splitlines()[-1])
except Exception: print("FAIL SETUP child L18: %s" % p1.stderr[-300:]); raise SystemExit(2)
check("L18a dual-failure refusal reports NOT journaled (%s)" % c18["why"], (not c18["ok"]) and "NOT journaled" in c18["why"])
lane_ledger(LP, {"M": mkpos("M", c18["pid"], "some-other-generation", "lc:entry:L18")})   # the lane's persisted position keeps the old binding
b18 = run_child(d)
check("L18b storage restored, FRESH process: account-level admission refuses with EXACTLY the generation-mismatch reason and no unrelated blocker (%r)" % b18, b18 == "persisted lane evidence blocks admission (fail-closed): lane %s: M@generation-mismatch" % LANE)

# L19 register-after-loss negative control: two lanes registered, primary lost, a lane restart (register_lane) must NOT recreate it from one lane
d, LP, pid, ID, op = good("L19")
LANE2 = LANE + "_two"; LP2b = os.path.join(d, "lane2.json"); lane_ledger(LP2b, {})
assert PA.register_lane(LANE2, LP2b, None)
os.unlink(PA._registry_paths()[0])
rA = PA.register_lane(LANE, LP, CI.journal_path(LANE)); rB = PA.register_lane(LANE2, LP2b, None); ri = PA.register_intent_journal(LANE, CI.journal_path(LANE))
check("L19a after primary loss every lane restart registration refuses (%r %r %r)" % (rA, rB, ri), (rA, rB, ri) == (False, False, False))
check("L19b the primary was NOT recreated by a single lane", not os.path.exists(PA._registry_paths()[0]))
check("L19c the loss block still holds after the lane restarts, as the ONLY blocker (%r)" % blocked(), blocked() == "persisted lane evidence blocks admission (fail-closed): lane registry lost (its mirror remains)")
lanes19 = PA._registry_fold(PA._registry_view()[0])[0]
check("L19d the mirror still holds BOTH lanes (union view)", LANE in lanes19 and LANE2 in lanes19)
# L20 torn registry tail (crash mid-append): complete records kept, tail dropped, next append repaired; mid-file garbage still blocks (L6)
d, LP, pid, ID, op = good("L20")
open(PA._registry_paths()[0], "a").write('{"lane": "tor')
check("L20a torn final line: admission stays open on the complete records (%r)" % blocked(), blocked() is None)
LANE3 = LANE + "_three"
check("L20b a new registration after a torn tail succeeds", PA.register_lane(LANE3, LP, None) is True)
check("L20c the registry parses cleanly afterwards (torn line terminated, no glue)", PA._registry_read(PA._registry_paths()[0])[1] == "ok" and PA._registry_read(PA._registry_paths()[1])[1] in ("ok", "torn_tail"))

# L21 a torn (unterminated) last intent-journal record is never read as clean; a complete journal stays clean
d, LP, pid, ID, op = good("L21")
if os.path.exists(CI.journal_path(LANE)): os.unlink(CI.journal_path(LANE))
iid = CI.begin(LANE, "M", pid, [{"kind": "credit", "op_id": "lc:exit:L21", "amount": 1.0, "qty": 100.0, "price_kind": "mark"}],
               {"realized_usd": 0.0, "entry_notional_usd": 1.0, "closed_reason": "t", "close_ts": int(time.time())}, account_id=ID)
open(CI.journal_path(LANE), "a").write('{"ev": "frozen", "intent_id": "' + iid[:12])
check("L21 a partial (torn) freeze record blocks account admission, only that reason (%r)" % blocked(), blocked() is not None and blocked().count("lane %s:" % LANE) == 1 and "unterminated last record" in blocked())

# L22 registry append is serialized by the registry lock (deterministic): parent HOLDS the primary lock, a child registration must not finish; after release it must
cL = '''
import json, os, sys
from daemon.launch_scanner import paper_account as PA
d = %r
PA.ACCOUNT_DIR = d; PA.LEDGER_PATH = os.path.join(d, "ledger.json"); PA.JOURNAL_PATH = os.path.join(d, "events.jsonl"); PA.LOCK_FILE = os.path.join(d, ".lock")
%s
print(json.dumps({"ok": PA.register_lane(%r, os.path.join(d, "x.json"), None)}), flush=True)
'''
def lock_probe(d, null_lock, lane):
    pre = "PA._reg_lock = lambda path: __import__('contextlib').nullcontext()" if null_lock else ""
    import contextlib
    pr = subprocess.Popen([sys.executable, "-B", "-c", cL % (d, pre, lane)], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try: pr.wait(timeout=3); done = True
    except subprocess.TimeoutExpired: done = False
    return pr, done
d, LP = fresh("L22"); assert PA.register_lane(LANE, LP, None)
with PA._reg_lock(PA._registry_paths()[0]):
    pr, done = lock_probe(d, False, LANE + "_c1")
    check("L22a while the registry lock is held a child registration is BLOCKED (not finished after 3s)", done is False)
pr.wait(timeout=30)
check("L22b after release the child registration completes and its record is present", pr.returncode == 0 and LANE + "_c1" in PA._registry_fold(PA._registry_view()[0])[0])
d, LP = fresh("L22m"); assert PA.register_lane(LANE, LP, None)
with PA._reg_lock(PA._registry_paths()[0]):
    pr, done = lock_probe(d, True, LANE + "_c2")
    check("L22c MUTANT (null lock) the same child finishes while the lock is held, so L22a is sensitive", done is True)
pr.wait(timeout=30)
# L23 concurrent registrants on a torn tail: complete records survive, all registrants recorded
d, LP = fresh("L23"); assert PA.register_lane(LANE, LP, None)
open(PA._registry_paths()[0], "a").write('{"lane": "tor')
procs = [subprocess.Popen([sys.executable, "-B", "-c", cL % (d, "", "%s_p%d" % (LANE, i))], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(6)]
rcs = [q.wait(timeout=60) for q in procs]
vw, vs = PA._registry_view(); ln23 = PA._registry_fold(vw)[0]
check("L23 six concurrent registrants on one torn tail: all rc 0, registry parses ok, original lane and all six recorded (%r)" % (rcs,),
      rcs == [0] * 6 and vs == "ok" and PA._registry_read(PA._registry_paths()[0])[1] == "ok" and LANE in ln23 and all(("%s_p%d" % (LANE, i)) in ln23 for i in range(6)))

def lane_names(path):
    return set(PA._registry_fold(PA._registry_read(path)[0])[0])
# L24 a MISSING mirror followed by a new registration must not leave the mirror with only the newest record (the stale/incomplete mirror defect)
d, LP, pid, ID, op = good("L24")
LANE_B = LANE + "_b"; LPB = os.path.join(d, "laneb.json"); lane_ledger(LPB, {})
os.unlink(PA._registry_paths()[1])
check("L24a registering another lane after the mirror vanished succeeds", PA.register_lane(LANE_B, LPB, None) is True)
check("L24b the rebuilt mirror holds BOTH lanes, not only the newest (%r)" % sorted(lane_names(PA._registry_paths()[1])), {LANE, LANE_B} <= lane_names(PA._registry_paths()[1]))
os.unlink(PA._registry_paths()[0])
check("L24c primary then lost: the loss block holds and the mirror still carries both lanes (%r)" % blocked(), blocked() == "persisted lane evidence blocks admission (fail-closed): lane registry lost (its mirror remains)" and {LANE, LANE_B} <= lane_names(PA._registry_paths()[1]))
# L25 a LAGGING mirror (crash between the primary and mirror writes) is completed by the next admission check
d, LP, pid, ID, op = good("L25")
_m = open(PA._registry_paths()[1]).read().splitlines(True)
open(PA._registry_paths()[1], "w").write("".join(_m[:-1]))
before = len(PA._registry_read(PA._registry_paths()[1])[0]); total = len(PA._registry_read(PA._registry_paths()[0])[0])
check("L25a fixture: mirror lags the primary (%d < %d)" % (before, total), before < total)
check("L25b admission check stays open on matching state and completes the mirror", blocked() is None and len(PA._registry_read(PA._registry_paths()[1])[0]) == total)
# L26 torn MIRROR tail is trimmed and completed, never corrupt
d, LP, pid, ID, op = good("L26")
open(PA._registry_paths()[1], "a").write('{"lane": "tor')
check("L26a mirror with torn tail reads as torn_tail, not corrupt", PA._registry_read(PA._registry_paths()[1])[1] == "torn_tail")
PA._mirror_sync(); LANE_C = LANE + "_c"
check("L26b a registration afterwards leaves a clean mirror holding every primary record", PA.register_lane(LANE_C, LP, None) is True and PA._registry_read(PA._registry_paths()[1])[1] == "ok" and {LANE, LANE_C} <= lane_names(PA._registry_paths()[1]))
# L27 MUTANT: sync off => the L24 defect reappears (so L24b is sensitive)
d, LP, pid, ID, op = good("L27"); _ms = PA._mirror_sync
os.unlink(PA._registry_paths()[1]); PA._mirror_sync = lambda: True
PA.register_lane(LANE + "_d", LP, None); PA._mirror_sync = _ms
check("L27 MUTANT (mirror sync off): the mirror lacks the first lane after rebuild, so L24b is sensitive (%r)" % sorted(lane_names(PA._registry_paths()[1])), LANE not in lane_names(PA._registry_paths()[1]))
# L15 static wiring
here = os.path.join(ROOT, "daemon", "launch_scanner")
st = {n: open(os.path.join(here, n + ".py")).read() for n in ("momentum_lane", "ceo", "watcher_lane", "grad", "mirror", "close_intents")}
check("L15a all five lanes call register_lane", all("_pa.register_lane(" in st[n] for n in ("momentum_lane", "ceo", "watcher_lane", "grad", "mirror")))
check("L15b close_intents._append registers after a durable intent append", "_register_journal(path, event.get(\"lane\"))" in st["close_intents"])
print("RESULT: %d failure(s)" % len(FAILS))
sys.exit(1 if FAILS else 0)
