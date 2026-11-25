"""bb73 REAL-MODULE durability proof for the generation-refusal block (SOURCE ONLY; NOT RUN until main clears).
Temp account dirs only (FOREX_AGENT_HOME set before import, asserted). Real paper_account, real WAL. LABELLED fault injection: _record_unresolved forced False.
Proves: (D1) a marker_failed refusal followed by MORE than 256 KB of later journal records still blocks entries after a simulated restart (red on the bounded-tail
version, green on the incremental whole-journal scan); (D2) positive control: a journal of the same size WITHOUT marker_failed does not raise that block;
(D3) a marker_failed record appended after a clean check is seen (no stale cache); (D4) MUTATION: the old bounded-tail check lets D1's state through;
(D5) an account directory that cannot be written blocks entries fail-closed with a named reason and the block lifts when it is writable again (skipped when euid 0).
NOT proven: both stores failing mid-run after a good probe (each write still fails closed on its own), kernel-level fsync loss, SIGKILL."""
import json, os, sys, tempfile, time
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
    PA._MARKER_FAILED[0] = False; PA._MF_SCAN.update(key=None, off=0, found=False); PA._PROBED.clear()
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
def _filler(n, tag):
    for i in range(n):
        PA._journal({"event": "settle_generation_refused", "lane": LANE, "mint": "FILL", "op_id": "gb:fill:%s:%d" % (tag, i), "pos_id": "%s/FILL#1" % LANE,
                     "reason": "filler record %s %d padding padding padding padding padding padding padding" % (tag, i)})
BLOCK = "could not be recorded"

# ---- D1: marker_failed followed by > 256 KB of later records
_use(os.path.join(_home, "D1"))
r = _need("debit D1", PA.try_debit(LANE, "MINTD1", 1.0, 100.0, "gb:entry:D1", marks=_mk())); pid = r[2]
check("D0 normal admission works with the writability probe in place (account dir probed once)", os.path.dirname(PA.LEDGER_PATH) in PA._PROBED)
_real = PA._record_unresolved
PA._record_unresolved = lambda *a, **k: False   # LABELLED fault injection: the marker file cannot be written
try:
    okD, whyD = PA.credit(LANE, "MINTD1", 1.1, "gb:exit:D1", pos_id=pid, qty=100.0, expected_account_id="some-other-generation", entry_op_id="gb:entry:D1")
finally:
    PA._record_unresolved = _real
check("D1a refusal reports the marker as unwritable (%s)" % whyD, (not okD) and "unwritable" in whyD)
_filler(1500, "d1")
_sz = os.path.getsize(PA.JOURNAL_PATH)
check("D1b the journal is now larger than the old 262144-byte tail window (%d bytes)" % _sz, _sz > 262144)
PA._MARKER_FAILED[0] = False; PA._MF_SCAN.update(key=None, off=0, found=False)   # emulate a restart: only the journal remains
okE, whyE, _x = PA.try_debit(LANE, "MINTD1B", 1.0, 100.0, "gb:entry:D1B", marks=_mk())
check("D1c after the restart entries are STILL blocked although the marker_failed record is outside the old tail (%s)" % whyE, (not okE) and BLOCK in whyE)
okE2, whyE2 = PA.hold_stake(LANE, 1.0, "gb:hold:D1", marks=_mk(), mint="MINTD1H")
check("D1d a NEW hold_stake is blocked too (%s)" % whyE2, (not okE2) and BLOCK in whyE2)
# ---- D4 mutation: the OLD bounded-tail check
_old = PA._journal_marker_failed
PA._journal_marker_failed = lambda: any(e.get("marker_failed") for e in PA._recent_refusals())
try:
    PA._MARKER_FAILED[0] = False
    _why_old = PA._entries_blocked_by_refusal()
finally:
    PA._journal_marker_failed = _old
check("D4 MUTANT (old bounded-tail check) does NOT report the marker_failed block for the same state, proving D1c is sensitive (%s)" % (_why_old,), not (_why_old and BLOCK in _why_old))

# ---- D2 positive control: same size, NO marker_failed
_use(os.path.join(_home, "D2"))
_need("debit D2", PA.try_debit(LANE, "MINTD2", 1.0, 100.0, "gb:entry:D2", marks=_mk()))
_filler(1500, "d2")
check("D2 a >256 KB journal of plain refusals without marker_failed does not raise the marker_failed block", (PA._entries_blocked_by_refusal() or "").find(BLOCK) < 0 and os.path.getsize(PA.JOURNAL_PATH) > 262144)

# ---- D3 no stale cache
_use(os.path.join(_home, "D3"))
_need("debit D3", PA.try_debit(LANE, "MINTD3", 1.0, 100.0, "gb:entry:D3", marks=_mk()))
check("D3a clean journal: no block", PA._entries_blocked_by_refusal() is None)
PA._journal({"event": "settle_generation_refused", "lane": LANE, "mint": "MINTD3", "op_id": "gb:exit:D3", "pos_id": "%s/MINTD3#1" % LANE, "reason": "late failure", "marker_failed": True})
check("D3b a marker_failed record appended AFTER a clean check is seen at once", BLOCK in (PA._entries_blocked_by_refusal() or ""))

# ---- D5 unwritable account directory
_use(os.path.join(_home, "D5"))
if os.geteuid() == 0:
    print("INFO D5 skipped: running as root, chmod cannot make the directory unwritable")
else:
    os.chmod(PA.ACCOUNT_DIR, 0o500)
    try:
        why5 = PA._entries_blocked_by_refusal()
    finally:
        os.chmod(PA.ACCOUNT_DIR, 0o700)
    check("D5a an unwritable account directory blocks entries with a named reason (%s)" % (why5,), bool(why5) and "not provably writable" in why5)
    check("D5b the failed probe was not cached, and the block lifts once the directory is writable again", PA._entries_blocked_by_refusal() is None)

print("RESULT: %d failure(s)" % len(FAILS))
sys.exit(1 if FAILS else 0)
