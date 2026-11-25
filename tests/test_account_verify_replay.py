"""REAL-MODULE proof for paper_account.verify_replay (SOURCE ONLY until cleared; NOT RUN by the producer).

Scope: stdlib + daemon.launch_scanner.paper_account against a TEMP account dir (FOREX_AGENT_HOME is set BEFORE import, and the paths are asserted to
be inside it). No process, thread or network. Each case runs on a fresh COPY of one pristine temp account, built with the real try_debit_instance
and credit, so the WAL shape is the real one.

Proves: (a) a valid WAL gives (True, ...) and verify_replay leaves every byte AND mtime of an EXISTING account dir unchanged (it takes the account flock for its read phase and creates NOTHING: a missing dir or lock file is refused, V7/V8);
(b) a torn tail, a mid-file torn record, a sequence violation and a missing ledger each give the PRECISE (None, reason) and ALSO leave every byte
unchanged (no truncation, no restore, no creation); (c) a ledger whose cash diverges from its own journal gives (False, ...).
NOT proven: lock behaviour under contention (needs a second process or thread, not authorized), holds/pending_sol/qty comparison (not implemented),
independent conservation."""
import json, os, shutil, subprocess, sys, tempfile, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n)
    if not c: FAILS.append(n)
STARTS = []
def _no(*a, **k):
    STARTS.append(1); raise RuntimeError("process/thread start forbidden")
subprocess.Popen = _no; subprocess.run = _no; threading.Thread.start = _no

_home = tempfile.mkdtemp(prefix="verify-replay-home-")
os.environ["FOREX_AGENT_HOME"] = _home
# The account's MANDATORY entry gates must be ON or every try_debit refuses ("mandatory gates off; entry blocked fail-closed"), and with
# gates on every limit must be configured or the gate fails closed. These are TEST-FIXTURE limits in this isolated temp env (set only so the
# real debit path can be exercised to build a WAL); they are NOT owner operating limits and nothing here arms a production account.
for _k, _v in (("PAPER_ACCOUNT_ENABLED", "1"), ("PAPER_ACCOUNT_GATES_ENABLED", "1"), ("PAPER_ACCOUNT_MAX_DAILY_LOSS_USD", "5.0"),
               ("PAPER_ACCOUNT_MAX_DRAWDOWN_PCT", "50.0"), ("PAPER_ACCOUNT_MAX_EXPOSURE_PCT", "100.0"), ("PAPER_ACCOUNT_MAX_SIMULTANEOUS", "1000"),
               ("PAPER_ACCOUNT_MAX_POSITIONS", "1000"), ("PAPER_ACCOUNT_MAX_DATA_AGE_S", "100000"),
               ("PAPER_ACCOUNT_MAX_TOTAL_EXPOSURE_USD", "1000000"), ("PAPER_ACCOUNT_MAX_TOKEN_EXPOSURE_USD", "1000000")):
    os.environ[_k] = _v
from daemon.launch_scanner import paper_account as PA
check("T0 the account module is pointed at the TEMP dir, never a real home (%s)" % PA.ACCOUNT_DIR, PA.ACCOUNT_DIR.startswith(_home))
if not PA.ACCOUNT_DIR.startswith(_home):
    print("ABORT: not isolated"); sys.exit(2)

def _use(d):
    PA.ACCOUNT_DIR = d; PA.LEDGER_PATH = os.path.join(d, "ledger.json"); PA.JOURNAL_PATH = os.path.join(d, "events.jsonl"); PA.LOCK_FILE = os.path.join(d, ".lock")

def _tree(d):
    out = {}
    for r, _ds, fs in os.walk(d):
        for f in sorted(fs):
            p = os.path.join(r, f); st = os.stat(p)
            out[os.path.relpath(p, d)] = (open(p, "rb").read(), st.st_mtime_ns, st.st_ino)
    return out


import time as _time
def _mk(PA_):
    """Fresh, fully bound marks for every open position (fixture-only; value = cost basis): with a position open, the gate needs a trusted
    mark for each one or it refuses. Keyed like the account does (pos_id for instance lots, mint otherwise)."""
    try:
        led = json.load(open(PA_.LEDGER_PATH))
    except Exception:
        return None
    out = {}
    for lane, st in (led.get("lanes") or {}).items():
        for k, pos in (st.get("open") or {}).items():
            key = pos["pos_id"] if pos.get("instance") is True else k
            out.setdefault(lane, {})[key] = {"value": float(pos["basis"]), "ts": _time.time(), "source": "jup-v3", "qty": float(pos["qty"]), "pos_id": pos["pos_id"]}
    return out or None
def _need(label, res):
    """FAIL FAST: a setup call that did not succeed aborts the whole file with the raw result, so no later case can pass on an empty account."""
    ok = isinstance(res, tuple) and len(res) >= 1 and res[0] is True
    if not ok:
        print("FAIL SETUP %s: %r" % (label, res)); raise SystemExit("setup call failed: %s: %r" % (label, res))
    return res
def _deb(lane, mint, stake, qty, op):
    return _need("debit " + op, PA.try_debit_instance(lane, mint, stake, qty, op, marks=_mk(PA)))

_pristine = os.path.join(_home, "pristine"); os.makedirs(_pristine)
_use(_pristine)
_r1 = _deb("grad", "MINTA", 1.0, 100.0, "grad:entry:MINTA:1")
check("T1a try_debit_instance returned (True, 'ok', pos_id) (%s)" % (_r1,), isinstance(_r1, tuple) and len(_r1) == 3 and _r1[0] is True and _r1[1] == "ok" and isinstance(_r1[2], str) and _r1[2])
_led_after_debit = open(PA.LEDGER_PATH, "rb").read()      # the ledger as it stood after the debit, BEFORE the credit (for the crash-window case)
_r2 = _need("credit k1", PA.credit("grad", "MINTA", 1.5, "grad:exit:MINTA:k1", pos_id=_r1[2], qty=100.0))
check("T1b the credit was accepted (%s)" % (_r2,), isinstance(_r2, tuple) and _r2[0] is True)
_j = [json.loads(l) for l in open(PA.JOURNAL_PATH) if l.strip()]
_mut = [e for e in _j if isinstance(e.get("seq"), int) and e.get("event") in PA._MUTATION_EVENTS]
check("T1 the real API produced a WAL with >= 2 sequenced mutation events (debit %s, credit %s, events %s)" % (_r1, _r2, [e.get("event") for e in _j]), len(_mut) >= 2)

_n = [0]
def _fresh_from(src):
    _n[0] += 1; d = os.path.join(_home, "case%d" % _n[0]); shutil.copytree(src, d); _use(d); return d
def _fresh():
    return _fresh_from(_pristine)

d = _fresh(); b0 = _tree(d); res = PA.verify_replay(); b1 = _tree(d)
check("V1 a valid WAL replays to (True, ...) (%s)" % (res,), res[0] is True)
check("V1b verify_replay is read-only on a valid account: same files, bytes and mtimes", b0 == b1)

d = _fresh()
with open(PA.JOURNAL_PATH, "ab") as fh: fh.write(b'{"event": "deb')       # crash mid-append: partial last record, no newline
b0 = _tree(d); res = PA.verify_replay(); b1 = _tree(d)
check("V2 a torn TAIL gives (None, journal_status:torn_tail) (%s)" % (res,), res == (None, "journal_status:torn_tail"))
check("V2b the torn tail is NOT truncated (bytes and mtimes unchanged)", b0 == b1)

d = _fresh()
_lines = open(PA.JOURNAL_PATH, "rb").read().split(b"\n")
_lines.insert(1, b"not json at all")                                      # mid-file corruption
open(PA.JOURNAL_PATH, "wb").write(b"\n".join(_lines))
b0 = _tree(d); res = PA.verify_replay(); b1 = _tree(d)
check("V3 mid-file corruption gives (None, journal_status:torn) (%s)" % (res,), res == (None, "journal_status:torn"))
check("V3b nothing was repaired", b0 == b1)

d = _fresh()
_ev = [json.loads(l) for l in open(PA.JOURNAL_PATH) if l.strip()]
_idx = [i for i, e in enumerate(_ev) if isinstance(e.get("seq"), int) and e.get("event") in PA._MUTATION_EVENTS]
_ev[_idx[-1]]["seq"] += 5                                                  # a lost mutation: a gap in the chain
open(PA.JOURNAL_PATH, "w").write("".join(json.dumps(e) + "\n" for e in _ev))
b0 = _tree(d); res = PA.verify_replay(); b1 = _tree(d)
check("V4 a sequence gap gives (None, journal_seq_violation) (%s)" % (res,), res == (None, "journal_seq_violation"))
check("V4b nothing was repaired", b0 == b1)

d = _fresh(); os.remove(PA.LEDGER_PATH)
b0 = _tree(d); res = PA.verify_replay(); b1 = _tree(d)
check("V5 a MISSING ledger file gives (None, ledger_file_unreadable_or_missing) (%s)" % (res,), res == (None, "ledger_file_unreadable_or_missing"))
check("V5b the ledger is NOT recreated or restored from .bak (no new files, bytes unchanged)", b0 == b1 and not os.path.exists(PA.LEDGER_PATH))

d = _fresh()
_led = json.load(open(PA.LEDGER_PATH)); _led["cash_spendable"] = float(_led.get("cash_spendable") or 0.0) + 1.0
open(PA.LEDGER_PATH, "w").write(json.dumps(_led))
b0 = _tree(d); res = PA.verify_replay(); b1 = _tree(d)
check("V6 a ledger whose cash disagrees with its own journal gives (False, ...) (%s)" % (res,), res[0] is False)
check("V6b the divergence is reported, not reconciled (bytes unchanged)", b0 == b1)

# V7/V8: verify_replay must NOT create anything (no dir, no lock file)
_v7 = os.path.join(_home, "virgin", "paper_account")
_use(_v7)
check("V7a precondition: the virgin account dir does not exist", not os.path.exists(_v7))
res = PA.verify_replay()
check("V7 a virgin path gives exactly (None, account_dir_or_lock_file_missing_not_created) (%s)" % (res,), res == (None, "account_dir_or_lock_file_missing_not_created"))
check("V7b NOTHING was created: the account dir still does not exist", not os.path.exists(_v7) and not os.path.exists(os.path.dirname(_v7)))
d = _fresh(); os.remove(PA.LOCK_FILE)
b0 = _tree(d); res = PA.verify_replay(); b1 = _tree(d)
check("V8 an existing account WITHOUT a .lock file gives the same exact verdict and creates no lock (%s)" % (res,), res == (None, "account_dir_or_lock_file_missing_not_created") and b0 == b1 and not os.path.exists(PA.LOCK_FILE))

# V9-V11: one tamper per compared quantity, each (False, ...) and untouched
for _name, _mut in (("cash (TOTAL)", lambda L: L.__setitem__("cash", float(L.get("cash") or 0.0) + 1.0)),
                    ("cash_estimated", lambda L: L.__setitem__("cash_estimated", float(L.get("cash_estimated") or 0.0) + 1.0)),
                    ("assumed_sold keys", lambda L: L.__setitem__("assumed_sold", dict(L.get("assumed_sold") or {}, **{"grad|FAKE": {"qty": 1}}))),
                    ("cash_spendable", lambda L: L.__setitem__("cash_spendable", float(L.get("cash_spendable") or 0.0) - 1.0))):
    d = _fresh(); _led = json.load(open(PA.LEDGER_PATH)); _mut(_led); open(PA.LEDGER_PATH, "w").write(json.dumps(_led))
    b0 = _tree(d); res = PA.verify_replay(); b1 = _tree(d)
    check("V9 tampered %s gives (False, ...) (%s) and nothing is reconciled" % (_name, res), res[0] is False and b0 == b1)

# V12: the CRASH WINDOW the verifier exists for: the WAL holds the credit but the ledger file is still the pre-credit one.
d = _fresh(); open(PA.LEDGER_PATH, "wb").write(_led_after_debit)
b0 = _tree(d); res = PA.verify_replay(); b1 = _tree(d)
check("V12 ledger BEHIND the WAL gives (False, ...) before any snapshot, files untouched (%s)" % (res,), res[0] is False and b0 == b1)
PA.snapshot()
res2 = PA.verify_replay()
check("V12b snapshot() HEALS the ledger from the WAL, and verify_replay then says (True, ...) (%s): the account heals on access, so clear evidence must read first" % (res2,), res2[0] is True)

# V13: a torn tail is healed (truncated) by snapshot(), proving why verify_replay must run first
d = _fresh()
with open(PA.JOURNAL_PATH, "ab") as fh: fh.write(b'{"event": "deb')
_n0 = os.path.getsize(PA.JOURNAL_PATH); PA.snapshot(); _n1 = os.path.getsize(PA.JOURNAL_PATH)
check("V13 snapshot() truncates a torn tail (%d -> %d bytes): the heal that verify_replay-first protects against" % (_n0, _n1), _n1 < _n0)

# R: a REALISTIC valid WAL: a partial sell with a fee and a VERIFIED price kind (spendable bucket), a fully closed instance, a reopened mint (second
# instance), one open position left, a hold, and a released hold. Every call's result is asserted so a wrong API assumption fails loudly here.
_real = os.path.join(_home, "realistic"); os.makedirs(_real); _use(_real)
_a1 = _deb("grad", "MINTA", 1.0, 100.0, "grad:entry:MINTA:1")
_a2 = _need("credit A", PA.credit("grad", "MINTA", 1.4, "grad:exit:MINTA:k1", pos_id=_a1[2], qty=100.0, fee=0.01, price_kind="current_price_simulated_fill"))
_a3 = _deb("grad", "MINTA", 1.0, 80.0, "grad:entry:MINTA:2")          # the SAME mint reopened: a second instance
_b1 = _deb("grad", "MINTB", 1.0, 50.0, "grad:entry:MINTB:1")
_b2 = _need("partial credit B", PA.credit("grad", "MINTB", 0.6, "grad:exit:MINTB:k1", pos_id=_b1[2], qty=20.0, fee=0.01, price_kind="current_price_simulated_fill"))   # PARTIAL: B stays open
_h1 = _need("hold 1", PA.hold_stake("grad", 0.3, "grad:hold:1", marks=_mk(PA))); _h2 = _need("hold 2", PA.hold_stake("grad", 0.2, "grad:hold:2", marks=_mk(PA))); _h3 = _need("release hold 2", PA.release_hold("grad:hold:2", "test: provider reports the tx never landed"))
_all = (_a1, _a2, _a3, _b1, _b2, _h1, _h2, _h3)
check("R0 every real call in the realistic WAL succeeded (%s)" % [x[0] if isinstance(x, tuple) else x for x in _all], all(isinstance(x, tuple) and x[0] is True for x in _all))
_ev = [json.loads(l) for l in open(PA.JOURNAL_PATH) if l.strip()]
check("R0b the WAL holds debit, credit, hold and hold-release events (%s)" % sorted({e.get("event") for e in _ev}), {"debit", "credit"} <= {e.get("event") for e in _ev} and len([e for e in _ev if "hold" in str(e.get("event"))]) >= 2)
_led_r = json.load(open(PA.LEDGER_PATH))
_open_keys = sorted(((_led_r.get("lanes") or {}).get("grad") or {}).get("open", {}).keys())
check("R0c two positions are open on the account (reopened MINTA instance and partially sold MINTB): %s" % _open_keys, len(_open_keys) == 2)
b0 = _tree(_real); res = PA.verify_replay(); b1 = _tree(_real)
check("R1 the realistic valid WAL replays to (True, ...) (%s)" % (res,), res[0] is True)
check("R1b and verify_replay changed nothing (bytes, mtime, inode)", b0 == b1)
d = _fresh_from(_real); _led = json.load(open(PA.LEDGER_PATH)); _o = _led["lanes"]["grad"]["open"]; _o.pop(sorted(_o)[0]); open(PA.LEDGER_PATH, "w").write(json.dumps(_led))
b0 = _tree(d); res = PA.verify_replay(); b1 = _tree(d)
check("R2 dropping ONE open position key from the ledger gives (False, ...) (%s), untouched" % (res,), res[0] is False and b0 == b1)

d = _fresh_from(_real); _led = json.load(open(PA.LEDGER_PATH)); _led["cash"] = float(_led["cash"]) + 1.0; open(PA.LEDGER_PATH, "w").write(json.dumps(_led))
b0 = _tree(d); res = PA.verify_replay(); b1 = _tree(d)
check("R3 on the REALISTIC WAL, a tampered TOTAL cash gives (False, ...) (%s), untouched" % (res,), res[0] is False and b0 == b1)
check("T9 no process/thread start attempted", STARTS == [])
print("FAILS: %d" % len(FAILS))
sys.exit(1 if FAILS else 0)
