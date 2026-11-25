"""REAL-MODULE generation-binding proof for paper_account.credit/write_down (SOURCE ONLY; NOT RUN by the producer until main clears).

Temp account dir only (FOREX_AGENT_HOME set before import, asserted). Real try_debit/credit, real WAL. Fixture limits are test-only, not owner limits.
Proves (red before the patch, green after): bound credit applies once and dedupes; omitted/wrong expected id is REFUSED and leaves ledger bytes unchanged
(journal only gains the settle_generation_refused record); a wiped account that re-creates under a REUSED entry op/pos_id cannot be credited with the OLD
binding (E9); a missing ledger with a non-empty WAL degrades and creates no new ledger and no create event (B2); debit_binding is captured atomically at
the debit and is None for an unrelated op. NOT proven: lane-level behaviour, SIGKILL (needs the BB66-style harness)."""
import json, os, shutil, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n)
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
def _use(d):
    os.makedirs(d, exist_ok=True)
    PA.ACCOUNT_DIR = d; PA.LEDGER_PATH = os.path.join(d, "ledger.json"); PA.JOURNAL_PATH = os.path.join(d, "events.jsonl"); PA.LOCK_FILE = os.path.join(d, ".lock")
def _mk():
    try: led = json.load(open(PA.LEDGER_PATH))
    except Exception: return None
    out = {}
    for lane, st in (led.get("lanes") or {}).items():
        for k, pos in (st.get("open") or {}).items():
            key = pos["pos_id"] if pos.get("instance") is True else k
            out.setdefault(lane, {})[key] = {"value": float(pos["basis"]), "ts": time.time(), "source": "jup-v3", "qty": float(pos["qty"]), "pos_id": pos["pos_id"]}
    return out or None
def _need(label, r):
    if not (isinstance(r, tuple) and r[0] is True):
        print("FAIL SETUP %s: %r" % (label, r)); raise SystemExit(2)
    return r
def _bytes(p):
    return open(p, "rb").read() if os.path.exists(p) else None
def _creates():
    return [json.loads(l) for l in open(PA.JOURNAL_PATH) if l.strip() and '"create"' in l] if os.path.exists(PA.JOURNAL_PATH) else []

# ---- A: bound credit applies once, dedupes
_use(os.path.join(_home, "A"))
r = _need("debit A", PA.try_debit(LANE, "MINTA", 1.0, 100.0, "gb:entry:A", marks=_mk()))
pid = r[2]
bind = PA.entry_binding(LANE, "MINTA", "gb:entry:A", pid)
check("A1 debit-time binding captured atomically and is the ledger account_id", bool(bind) and bind == json.load(open(PA.LEDGER_PATH)).get("account_id"))
check("A2 binding is None for an op this thread did not debit", PA.entry_binding(LANE, "MINTA", "gb:entry:OTHER", pid) is None)
ok, why = PA.credit(LANE, "MINTA", 1.1, "gb:exit:A", pos_id=pid, qty=100.0, expected_account_id=bind, entry_op_id="gb:entry:A")
check("A3 bound credit applies (%s)" % why, ok)
cash1 = json.load(open(PA.LEDGER_PATH))["cash"]
ok2, why2 = PA.credit(LANE, "MINTA", 1.1, "gb:exit:A", pos_id=pid, qty=100.0, expected_account_id=bind, entry_op_id="gb:entry:A")
check("A4 same-op retry dedupes (%s) and cash unchanged" % why2, ok2 and json.load(open(PA.LEDGER_PATH))["cash"] == cash1)

# ---- B: omitted / wrong expected id refused, ledger bytes unchanged
_use(os.path.join(_home, "B"))
r = _need("debit B", PA.try_debit(LANE, "MINTB", 1.0, 100.0, "gb:entry:B", marks=_mk()))
pid = r[2]; bind = PA.entry_binding(LANE, "MINTB", "gb:entry:B", pid)
led0 = _bytes(PA.LEDGER_PATH); j0 = _bytes(PA.JOURNAL_PATH)
ok, why = PA.credit(LANE, "MINTB", 1.1, "gb:exit:B1", pos_id=pid, qty=100.0)
check("B1 omitted expected id REFUSED (%s)" % why, (not ok) and "no account generation" in why)
ok, why = PA.credit(LANE, "MINTB", 1.1, "gb:exit:B2", pos_id=pid, qty=100.0, expected_account_id="acct-WRONG", entry_op_id="gb:entry:B")
check("B2 wrong expected id REFUSED (%s)" % why, not ok)
ok, why = PA.write_down(LANE, "MINTB", "gb:wd:B3", pos_id=pid, qty=100.0, reason="t", expected_account_id="acct-WRONG", entry_op_id="gb:entry:B")
check("B3 write_down with wrong id REFUSED (%s)" % why, not ok)
check("B4 ledger bytes unchanged by refusals", _bytes(PA.LEDGER_PATH) == led0)
j1 = _bytes(PA.JOURNAL_PATH)
check("B5 journal only gained refusal records", j1.startswith(j0) and all('settle_generation_refused' in l for l in j1[len(j0):].decode().splitlines() if l.strip()) and j1 != j0)
ok, why = PA.credit(LANE, "MINTB", 1.1, "gb:exit:B9", pos_id=pid, qty=100.0, expected_account_id=bind, entry_op_id="gb:entry:B")
check("B6 positive control: the right binding still credits (%s)" % why, ok)

# ---- C: wipe_all then re-create under the SAME entry op/pos_id (E9): old binding refused
_use(os.path.join(_home, "C"))
r = _need("debit C1", PA.try_debit(LANE, "MINTC", 1.0, 100.0, "gb:entry:C", marks=_mk()))
pid1 = r[2]; old_bind = PA.entry_binding(LANE, "MINTC", "gb:entry:C", pid1)
for p in (PA.LEDGER_PATH, PA.JOURNAL_PATH):
    os.remove(p)
r = _need("debit C2 after wipe", PA.try_debit(LANE, "MINTC", 1.0, 100.0, "gb:entry:C", marks=_mk()))
check("C1 fixture: pos_id repeats after wipe (%s vs %s)" % (pid1, r[2]), r[2] == pid1)
new_bind = PA.entry_binding(LANE, "MINTC", "gb:entry:C", r[2])
check("C2 new generation has a DIFFERENT account_id", bool(new_bind) and new_bind != old_bind)
cashC = json.load(open(PA.LEDGER_PATH))["cash"]; ledC = _bytes(PA.LEDGER_PATH)
ok, why = PA.credit(LANE, "MINTC", 1.1, "gb:exit:C", pos_id=pid1, qty=100.0, expected_account_id=old_bind, entry_op_id="gb:entry:C")
check("C3 OLD binding cannot credit the reused pos_id/op (%s)" % why, (not ok) and _bytes(PA.LEDGER_PATH) == ledC and json.load(open(PA.LEDGER_PATH))["cash"] == cashC)
ok, why = PA.credit(LANE, "MINTC", 1.1, "gb:exit:C", pos_id=pid1, qty=100.0, expected_account_id=new_bind, entry_op_id="gb:entry:C")
check("C4 positive control: the NEW generation's binding credits (%s)" % why, ok)

# ---- D: missing ledger + non-empty WAL degrades, no regeneration (B2)
_use(os.path.join(_home, "D"))
_need("debit D", PA.try_debit(LANE, "MINTD", 1.0, 100.0, "gb:entry:D", marks=_mk()))
os.remove(PA.LEDGER_PATH)
n_create = len(_creates())
r = PA.try_debit(LANE, "MINTD2", 1.0, 100.0, "gb:entry:D2", marks=_mk())
check("D1 new debit refused with ledger missing and WAL present (%r)" % (r,), r[0] is False)
check("D2 no ledger regenerated", not os.path.exists(PA.LEDGER_PATH))
check("D3 no new create event", len(_creates()) == n_create)

# ---- E: thread-local capture hygiene
import threading
_use(os.path.join(_home, "E"))
r = _need("debit E", PA.try_debit(LANE, "MINTE", 1.0, 100.0, "gb:entry:E", marks=_mk()))
pidE = r[2]
check("E1 capture present right after the debit", bool(PA.entry_binding(LANE, "MINTE", "gb:entry:E", pidE)))
_bad = PA.try_debit(LANE, "MINTE2", -1.0, 100.0, "gb:entry:E2", marks=_mk())
check("E2 a FAILED debit attempt clears the previous capture (%r)" % (_bad[:2],), _bad[0] is False and PA.entry_binding(LANE, "MINTE", "gb:entry:E", pidE) is None)
r = _need("debit E again", PA.try_debit(LANE, "MINTE3", 1.0, 100.0, "gb:entry:E3", marks=_mk()))
check("E3 non-matching op_id or pos_id gives None", PA.entry_binding(LANE, "MINTE3", "gb:entry:E3", "wrong#1") is None and PA.entry_binding(LANE, "MINTE3", "gb:entry:EX", r[2]) is None)
_box = []
_t = threading.Thread(target=lambda: _box.append(PA.entry_binding(LANE, "MINTE3", "gb:entry:E3", r[2])))
_t.start(); _t.join()
check("E4 another thread never sees this thread's capture", _box == [None])
_rt = PA.try_debit(LANE, "MINTE3", 1.0, 100.0, "gb:entry:E3", marks=_mk())
check("E5 retry of the applied op re-captures the ORIGINAL stored account_id (%r)" % (_rt[:2],), _rt[0] is True and PA.entry_binding(LANE, "MINTE3", "gb:entry:E3", r[2]) == json.load(open(PA.LEDGER_PATH))["account_id"])

# ---- F: G1 pos_id None bypass refused
_use(os.path.join(_home, "F"))
r = _need("debit F", PA.try_debit(LANE, "MINTF", 1.0, 100.0, "gb:entry:F", marks=_mk()))
pidF = r[2]; bindF = PA.entry_binding(LANE, "MINTF", "gb:entry:F", pidF); ledF = _bytes(PA.LEDGER_PATH)
ok, why = PA.credit(LANE, "MINTF", 1.1, "gb:exit:F0", pos_id=None, qty=100.0, expected_account_id=bindF, entry_op_id="gb:entry:F")
check("F1 credit with pos_id=None and a valid binding is REFUSED (%s)" % why, (not ok) and "no pos_id" in why and _bytes(PA.LEDGER_PATH) == ledF)
ok, why = PA.write_down(LANE, "MINTF", "gb:wd:F0", pos_id=None, qty=100.0, reason="t", expected_account_id=bindF, entry_op_id="gb:entry:F")
check("F2 write_down with pos_id=None REFUSED (%s)" % why, not ok)
# G5 refusal dedupe
_n0 = len(open(PA.JOURNAL_PATH).read().splitlines())
for _i in range(5):
    PA.credit(LANE, "MINTF", 1.1, "gb:exit:F0", pos_id=pidF, qty=100.0, expected_account_id="acct-WRONG", entry_op_id="gb:entry:F")
_n1 = len(open(PA.JOURNAL_PATH).read().splitlines())
check("F3 five identical refusals add exactly ONE journal record (%d -> %d)" % (_n0, _n1), _n1 - _n0 == 1)
# G6 exact same-op retry after the credit applied returns the recorded outcome
ok, why = PA.credit(LANE, "MINTF", 1.1, "gb:exit:F1", pos_id=pidF, qty=100.0, expected_account_id=bindF, entry_op_id="gb:entry:F")
ok2, why2 = PA.credit(LANE, "MINTF", 1.1, "gb:exit:F1", pos_id=pidF, qty=100.0, expected_account_id=bindF, entry_op_id="gb:entry:F")
check("F4 same-op retry WITH the matching binding and identical economics is answered from the recorded op (%s / %s)" % (why, why2), ok and ok2)
ok4, why4 = PA.credit(LANE, "MINTF", 1.1, "gb:exit:F1", pos_id=pidF, qty=100.0, expected_account_id="acct-WRONG", entry_op_id="gb:entry:F")
check("F4b same-op retry with a WRONG binding is refused even though the op exists (%s)" % why4, not ok4)
ok4c, why4c = PA.credit(LANE, "MINTF", 1.1, "gb:exit:F1", pos_id=pidF, qty=100.0)
check("F4c same-op retry with NO binding is refused, never an ack (%s)" % why4c, not ok4c)
ok4d, why4d = PA.credit(LANE, "MINTF", 9.9, "gb:exit:F1", pos_id=pidF, qty=100.0, expected_account_id=bindF, entry_op_id="gb:entry:F")
check("F4d same-op retry with DIFFERENT proceeds is refused (%s)" % why4d, (not ok4d) and "parameter conflict" in why4d)
ok4e, why4e = PA.credit(LANE, "MINTF", 1.1, "gb:exit:F1", pos_id=pidF, qty=50.0, expected_account_id=bindF, entry_op_id="gb:entry:F")
check("F4e same-op retry with a DIFFERENT qty is refused (%s)" % why4e, not ok4e)
_ledF2 = _bytes(PA.LEDGER_PATH)
with open(PA.JOURNAL_PATH, "a") as _f: _f.write("{torn record\n")
ok4f, why4f = PA.credit(LANE, "MINTF", 1.1, "gb:exit:F1", pos_id=pidF, qty=100.0, expected_account_id=bindF, entry_op_id="gb:entry:F")
check("F4f same-op retry with a torn journal is NOT answered as success (%s)" % why4f, not ok4f)
ok3, why3 = PA.credit(LANE, "MINTF", 1.1, "gb:exit:F1", pos_id="wrong#9", qty=100.0, expected_account_id=None, entry_op_id=None)
check("F5 same op_id with a DIFFERENT pos_id is not a retry: refused (%s)" % why3, not ok3)

# ---- G: mixed-generation replay (G2): journal has a bound credit, a restored OLDER ledger replays it
_use(os.path.join(_home, "G"))
r = _need("debit G", PA.try_debit(LANE, "MINTG", 1.0, 100.0, "gb:entry:G", marks=_mk()))
pidG = r[2]; bindG = PA.entry_binding(LANE, "MINTG", "gb:entry:G", pidG)
shutil.copy(PA.LEDGER_PATH, os.path.join(_home, "G.pre-credit.json"))
_need("credit G", PA.credit(LANE, "MINTG", 1.1, "gb:exit:G", pos_id=pidG, qty=100.0, expected_account_id=bindG, entry_op_id="gb:entry:G"))
ev = [json.loads(l) for l in open(PA.JOURNAL_PATH) if l.strip()]
cr = [e for e in ev if e.get("event") == "credit"]
check("G1 credit and write_down events now carry the account_id", len(cr) == 1 and cr[0].get("account_id") == bindG)
# same-generation older ledger: replay applies the credit once (positive control)
shutil.copy(os.path.join(_home, "G.pre-credit.json"), PA.LEDGER_PATH)
if os.path.exists(PA.LEDGER_PATH + ".bak"): shutil.copy(os.path.join(_home, "G.pre-credit.json"), PA.LEDGER_PATH + ".bak")
snapG = PA.snapshot()
led = json.load(open(PA.LEDGER_PATH))
check("G2 positive control: same-generation older ledger replays the credit exactly once (ops has it: %s)" % ("gb:exit:G" in (led.get("ops") or {}), ), "gb:exit:G" in (led.get("ops") or {}) and sum(1 for l in open(PA.JOURNAL_PATH) if '"event": "credit"' in l) == 1)
# foreign generation: rewrite the journal credit to ANOTHER account_id and restore the older ledger again
shutil.copy(os.path.join(_home, "G.pre-credit.json"), PA.LEDGER_PATH)
if os.path.exists(PA.LEDGER_PATH + ".bak"): shutil.copy(os.path.join(_home, "G.pre-credit.json"), PA.LEDGER_PATH + ".bak")
lines = open(PA.JOURNAL_PATH).read().splitlines(keepends=True)
with open(PA.JOURNAL_PATH, "w") as f:
    for l in lines:
        e = json.loads(l) if l.strip() else None
        if e and e.get("event") == "credit": e["account_id"] = "foreign-generation"; l = json.dumps(e) + "\n"
        f.write(l)
r2 = PA.try_debit(LANE, "MINTG2", 1.0, 100.0, "gb:entry:G2", marks=_mk())
check("G3 a journal credit from another generation is NOT replayed: the account degrades and refuses (%r)" % (r2[:2],), r2[0] is False and "gb:exit:G" not in (json.load(open(PA.LEDGER_PATH)).get("ops") or {}))
# unbound credit AFTER a bound event: mixed-generation divergence
shutil.copy(os.path.join(_home, "G.pre-credit.json"), PA.LEDGER_PATH)
if os.path.exists(PA.LEDGER_PATH + ".bak"): shutil.copy(os.path.join(_home, "G.pre-credit.json"), PA.LEDGER_PATH + ".bak")
lines = open(PA.JOURNAL_PATH).read().splitlines(keepends=True)
with open(PA.JOURNAL_PATH, "w") as f:
    for l in lines:
        e = json.loads(l) if l.strip() else None
        if e and e.get("event") == "credit": e.pop("account_id", None); l = json.dumps(e) + "\n"
        f.write(l)
r3 = PA.try_debit(LANE, "MINTG3", 1.0, 100.0, "gb:entry:G3", marks=_mk())
check("G4 an UNBOUND credit after the first bound event is refused as mixed-generation (%r)" % (r3[:2],), r3[0] is False and "gb:exit:G" not in (json.load(open(PA.LEDGER_PATH)).get("ops") or {}))

# ---- H: same-generation rollback, entry op ABSENT from the restored pair: refused (no positive evidence of the debit)
_use(os.path.join(_home, "H"))
_need("debit H1", PA.try_debit(LANE, "MINTH1", 1.0, 100.0, "gb:entry:H1", marks=_mk()))
shutil.copy(PA.LEDGER_PATH, os.path.join(_home, "H.l")); shutil.copy(PA.JOURNAL_PATH, os.path.join(_home, "H.j"))
r = _need("debit H2", PA.try_debit(LANE, "MINTH2", 1.0, 100.0, "gb:entry:H2", marks=_mk()))
pidH2 = r[2]; bindH = PA.entry_binding(LANE, "MINTH2", "gb:entry:H2", pidH2)
for src, dst in (("H.l", PA.LEDGER_PATH), ("H.l", PA.LEDGER_PATH + ".bak"), ("H.j", PA.JOURNAL_PATH)):
    shutil.copy(os.path.join(_home, src), dst)
_ledH = _bytes(PA.LEDGER_PATH)
ok, why = PA.credit(LANE, "MINTH2", 1.1, "gb:exit:H2", pos_id=pidH2, qty=100.0, expected_account_id=bindH, entry_op_id="gb:entry:H2")
check("H1 same-generation id but the entry op is absent after a consistent rollback: REFUSED (%s)" % why, (not ok) and _bytes(PA.LEDGER_PATH) == _ledH)

# ---- M: MUTATION CONTROL. With the guard neutralised the same wrong-binding credit APPLIES, so B/C/F above are sensitive to the guard.
_use(os.path.join(_home, "M"))
r = _need("debit M", PA.try_debit(LANE, "MINTM", 1.0, 100.0, "gb:entry:M", marks=_mk()))
_real_gr = PA._generation_refusal
PA._generation_refusal = lambda *a, **k: None
try:
    okm, whym = PA.credit(LANE, "MINTM", 1.1, "gb:exit:M", pos_id=r[2], qty=100.0, expected_account_id="acct-WRONG", entry_op_id="gb:entry:M")
finally:
    PA._generation_refusal = _real_gr
check("M1 MUTANT (guard removed) credits with a WRONG binding, proving the refusal tests are sensitive (%s)" % whym, okm is True)

# ---- C2: new generation closed under the IDENTICAL op id; the stale old-generation intent retries it (A1)
ok, why = PA.credit(LANE, "MINTC", 1.1, "gb:exit:C", pos_id=pid1, qty=100.0, expected_account_id=old_bind, entry_op_id="gb:entry:C")
check("C5 stale OLD-generation retry of an op id the NEW generation already applied is REFUSED, not acked (%s)" % why, not ok)
print("RESULT: %d failure(s)" % len(FAILS))
sys.exit(1 if FAILS else 0)
