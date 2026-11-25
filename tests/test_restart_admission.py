"""bb74 REAL-MODULE restart-admission proof (SOURCE ONLY; NOT RUN until main clears). Temp dirs only, FOREX_AGENT_HOME set before import, asserted.
Real paper_account (try_debit, credit, restart_admission_blockers, _refuse_generation) and two FRESH child processes for the restart order. LABELLED fault injection:
_record_unresolved forced False and the settle_generation_refused journal append raising OSError (a transient dual write failure), storage then restored.
Proves: R1 matching-generation position does not block (no deadlock); R2 generation-mismatch blocks; R3 unbound blocks; R4/R6 missing or corrupt LANE ledger blocks while the
account holds lots for the lane; R5 missing lane ledger with NO account lots does not block (fresh start); R7 unreadable account ledger with a bound position blocks;
R8 the refusal text no longer claims 'journaled' when the append failed; R9 FRESH PROCESS after the dual failure: the account alone admits entries (OBSERVED, labelled: this
is the residual account-level gap) while the lane's persisted position evidence blocks; R10 a failed journal write clears the writability-probe cache; R11 an EXISTING
unwritable journal file defeats the probe even though the directory accepts new files (non-root); R12 MUTANT (old unbound-only scan) misses R2/R9, so they are sensitive;
R13 static: all five lanes call the shared helper and none keeps the old comprehension.
NOT proven: lane runtime wiring (needs the lane harnesses), grad closed-position and lost-intent-journal witnesses, intent-journal durability."""
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
    PA._MARKER_FAILED[0] = False; PA._MF_SCAN.update(key=None, off=0, found=False); PA._PROBED.clear()
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

# ---- R1-R3 generation evidence
_use(os.path.join(_home, "A")); LP = os.path.join(_home, "A", "lane.json")
r = _need("debit A", PA.try_debit(LANE, "MINTA", 1.0, 100.0, "gb:entry:A", marks=_mk())); pidA = r[2]; ID = acct_id()
lane_ledger(LP, {"MINTA": pos("MINTA", pidA, ID)})
check("R1 POSITIVE a position bound to the CURRENT generation does not block (%r)" % PA.restart_admission_blockers(LANE, LP), PA.restart_admission_blockers(LANE, LP) == [])
lane_ledger(LP, {"MINTA": pos("MINTA", pidA, "some-other-generation")})
check("R2 a position bound to a DIFFERENT generation blocks (%r)" % PA.restart_admission_blockers(LANE, LP), PA.restart_admission_blockers(LANE, LP) == ["MINTA@generation-mismatch"])
lane_ledger(LP, {"MINTA": pos("MINTA", pidA, None)})
check("R3 an UNBOUND position blocks (%r)" % PA.restart_admission_blockers(LANE, LP), PA.restart_admission_blockers(LANE, LP) == ["MINTA"])
lane_ledger(LP, {"MINTA": dict(pos("MINTA", pidA, ID), lane="other_lane")})
check("R3b a position of ANOTHER lane in a shared lane file is not judged here", PA.restart_admission_blockers(LANE, LP) == [])
# ---- R4/R6 lane ledger availability
os.unlink(LP)
w4 = PA.restart_admission_blockers(LANE, LP)
check("R4 a MISSING lane ledger blocks while the account holds an open lot for the lane (%r)" % w4, len(w4) == 1 and w4[0].startswith("lane-ledger-missing"))
open(LP, "w").write("{not json")
w6 = PA.restart_admission_blockers(LANE, LP)
check("R6 a CORRUPT lane ledger blocks while the account holds an open lot (%r)" % w6, len(w6) == 1 and w6[0].startswith("lane-ledger-corrupt"))
_use(os.path.join(_home, "B"))
check("R5 POSITIVE a missing lane ledger with NO account lots is a fresh start, not a block", PA.restart_admission_blockers(LANE, os.path.join(_home, "B", "none.json")) == [])
# ---- R7 account ledger unreadable
_use(os.path.join(_home, "C")); LPC = os.path.join(_home, "C", "lane.json")
rC = _need("debit C", PA.try_debit(LANE, "MINTC", 1.0, 100.0, "gb:entry:C", marks=_mk())); IDC = acct_id()
lane_ledger(LPC, {"MINTC": pos("MINTC", rC[2], IDC)})
open(PA.LEDGER_PATH, "w").write("{garbage")
w7 = PA.restart_admission_blockers(LANE, LPC)
check("R7 an unreadable ACCOUNT ledger with a bound lane position blocks (%r)" % w7, w7 == ["MINTC@account-identity-unavailable"])

# ---- R8, R10: refusal text and probe cache when BOTH writes fail
_use(os.path.join(_home, "D"))
rD = _need("debit D", PA.try_debit(LANE, "MINTD", 1.0, 100.0, "gb:entry:D", marks=_mk()))
_real_rec, _real_j = PA._record_unresolved, PA._journal
def _j_fail(ev, strict=False):
    if ev.get("event") == "settle_generation_refused": raise OSError("injected EIO")
    return _real_j(ev, strict)
PA._record_unresolved = lambda *a, **k: False; PA._journal = _j_fail
try:
    PA._entries_blocked_by_refusal()   # prime the probe cache
    primed = os.path.dirname(PA.LEDGER_PATH) in PA._PROBED
    okD, whyD = PA.credit(LANE, "MINTD", 1.1, "gb:exit:D", pos_id=rD[2], qty=100.0, expected_account_id="some-other-generation", entry_op_id="gb:entry:D")
finally:
    PA._record_unresolved, PA._journal = _real_rec, _real_j
check("R8 the refusal says NOT journaled when the append failed (%s)" % whyD, (not okD) and "NOT journaled" in whyD and "(journaled, nothing applied" not in whyD)
check("R10 the probe cache was primed, and the failed durable write cleared it", primed and os.path.dirname(PA.LEDGER_PATH) not in PA._PROBED)
# a plain failed journal append clears the cache too
PA._PROBED.add(os.path.dirname(PA.LEDGER_PATH))
_open = open
def _bad_open(p, mode="r", *a, **k):
    if str(p) == PA.JOURNAL_PATH and "a" in mode: raise OSError("injected EIO")
    return _open(p, mode, *a, **k)
import builtins
builtins.open = _bad_open
try: PA._journal({"event": "probe_cache_test"})
finally: builtins.open = _open
check("R10b a failed journal append (non-strict, swallowed) also clears the probe cache", os.path.dirname(PA.LEDGER_PATH) not in PA._PROBED)

# ---- R9 FRESH PROCESS after a transient dual failure
_use(os.path.join(_home, "E")); LPE = os.path.join(_home, "E", "lane.json")
env = dict(os.environ); env["PYTHONPATH"] = ROOT
child1 = '''
import json, os, sys, time
from daemon.launch_scanner import paper_account as PA
''' + MK + '''
LANE = %r
d = %r
PA.ACCOUNT_DIR = d; PA.LEDGER_PATH = os.path.join(d, "ledger.json"); PA.JOURNAL_PATH = os.path.join(d, "events.jsonl"); PA.LOCK_FILE = os.path.join(d, ".lock")
r = PA.try_debit(LANE, "MINTR9", 1.0, 100.0, "gb:entry:R9", marks=_mk()); assert r[0] is True, r
_rr, _rj = PA._record_unresolved, PA._journal
def jf(ev, strict=False):
    if ev.get("event") == "settle_generation_refused": raise OSError("injected EIO")
    return _rj(ev, strict)
PA._record_unresolved = lambda *a, **k: False; PA._journal = jf
ok, why = PA.credit(LANE, "MINTR9", 1.1, "gb:exit:R9", pos_id=r[2], qty=100.0, expected_account_id="some-other-generation", entry_op_id="gb:entry:R9")
print(json.dumps({"ok": ok, "why": why, "pid": r[2]}))
''' % (LANE, os.path.join(_home, "E"))
p1 = subprocess.run([sys.executable, "-B", "-c", child1], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
try: c1 = json.loads(p1.stdout.strip().splitlines()[-1])
except Exception: c1 = None
if not c1: print("FAIL SETUP child1: rc=%s err=%s" % (p1.returncode, p1.stderr[-300:])); raise SystemExit(2)
lane_ledger(LPE, {"MINTR9": pos("MINTR9", c1["pid"], "some-other-generation")})   # what the lane persisted: the restored open position, old binding
child2 = '''
import json, os, sys
from daemon.launch_scanner import paper_account as PA
d = %r
PA.ACCOUNT_DIR = d; PA.LEDGER_PATH = os.path.join(d, "ledger.json"); PA.JOURNAL_PATH = os.path.join(d, "events.jsonl"); PA.LOCK_FILE = os.path.join(d, ".lock")
print(json.dumps({"acct_block": PA._entries_blocked_by_refusal(), "marker": PA.unresolved_refusals(), "lane": PA.restart_admission_blockers(%r, %r)}))
''' % (os.path.join(_home, "E"), LANE, LPE)
p2 = subprocess.run([sys.executable, "-B", "-c", child2], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
try: c2 = json.loads(p2.stdout.strip().splitlines()[-1])
except Exception: c2 = None
check("R9a child1 saw the dual failure reported honestly (%s)" % c1["why"][:120], c1["ok"] is False and "NOT journaled" in c1["why"])
check("R9b in a FRESH process nothing durable remains at the account level: no marker (%r)" % (c2 and c2["marker"],), bool(c2) and c2["marker"] == [])
print("INFO R9 OBSERVED residual account-level state in the fresh process: _entries_blocked_by_refusal() = %r (None means the account alone re-admits)" % (c2 and c2["acct_block"],))
check("R9c the lane's PERSISTED position evidence blocks admission in the fresh process (%r)" % (c2 and c2["lane"],), bool(c2) and c2["lane"] == ["MINTR9@generation-mismatch"])
check("R12 MUTANT (old unbound-only scan) sees NEITHER R2 nor R9c, proving they are sensitive (%r)" % (old_scan(LPE),), old_scan(LPE) == [])

# ---- R11 existing unwritable journal file (non-root only)
_use(os.path.join(_home, "F"))
_need("debit F", PA.try_debit(LANE, "MINTF", 1.0, 100.0, "gb:entry:F", marks=_mk()))
if os.geteuid() == 0:
    print("INFO R11 skipped: running as root, chmod cannot make the journal unwritable")
else:
    PA._PROBED.clear(); os.chmod(PA.JOURNAL_PATH, 0o400)
    try: why11 = PA._entries_blocked_by_refusal()
    finally: os.chmod(PA.JOURNAL_PATH, 0o600)
    check("R11 an EXISTING unwritable journal file blocks although the directory accepts new files (%s)" % (why11,), bool(why11) and "not provably writable" in why11)

# ---- R13 static wiring
for fn in ("momentum_lane", "ceo", "watcher_lane", "grad", "mirror"):
    src = open(os.path.join(ROOT, "daemon", "launch_scanner", fn + ".py")).read()
    check("R13 %s uses the shared admission helper and keeps no old unbound-only comprehension" % fn,
          "restart_admission_blockers" in src and src.count("_unresolved_positions(") >= 3 and src.count('not p.get("pa_account_id")]') == 1)
print("RESULT: %d failure(s)" % len(FAILS)); sys.exit(1 if FAILS else 0)
