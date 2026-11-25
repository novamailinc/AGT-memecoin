"""bb75 REAL-MODULE lifecycle-admission proof (SOURCE ONLY; NOT RUN until main clears). Temp dirs only, FOREX_AGENT_HOME set before import, asserted.
Real paper_account (try_debit, credit, registry, restart_admission_blockers, _entries_blocked_by_refusal), the real close_intents.begin writer, and FRESH child processes.
Proves: L1 a first-ever start (no account ledger, WAL, registry, lane ledger) is admitted at account level AND a real debit then succeeds; L2 POSITIVE a matching, account-proven
position with a registered lane blocks nothing and the registry primary+mirror exist; L3 account ledger deleted while the WAL remains = account-state-lost, never fresh;
L4 account ledger+WAL+registry deleted but a bound lane position remains = blocked; L5 registry primary lost with the mirror kept blocks ACCOUNT-level entries;
L6 corrupt registry blocks; L7 lane ledger lost while the account holds lots blocks at account level; L8 position bound to the wrong entry op blocks; L9 an orphan account lot
(no lane position) blocks the lane restart; L10 the real close_intents.begin registers its journal and its later loss blocks at account level; L11 generation mismatch blocks
at ACCOUNT level (a lane that never called its own check); L12 FRESH PROCESS: account admission refuses on persisted evidence alone, and a fresh process on matching state
admits; L13 the marker latch survives marker deletion and a journal read failure in-process; L14 MUTANT (registry check disabled) admits L11 so it is sensitive; L15 static wiring.
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
    d = os.path.join(_home, name); _use(d); return d, os.path.join(d, "lane.json")
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
d, LP, pid, ID, op = good("L6"); open(PA._registry_paths()[0], "w").write("{bad")
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
check("L11 generation mismatch refuses at ACCOUNT level (%r)" % blocked(), blocked() is not None and "generation-mismatch" in blocked())
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
# L15 static wiring
here = os.path.join(ROOT, "daemon", "launch_scanner")
st = {n: open(os.path.join(here, n + ".py")).read() for n in ("momentum_lane", "ceo", "watcher_lane", "grad", "mirror", "close_intents")}
check("L15a all five lanes call register_lane", all("_pa.register_lane(" in st[n] for n in ("momentum_lane", "ceo", "watcher_lane", "grad", "mirror")))
check("L15b close_intents._append registers after a durable intent append", "_register_journal(path, event.get(\"lane\"))" in st["close_intents"])
print("RESULT: %d failure(s)" % len(FAILS))
sys.exit(1 if FAILS else 0)
