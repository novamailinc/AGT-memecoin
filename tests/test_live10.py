"""Live10 demo mirror ($10 split across live lanes): scaling math,
funding rule, dedup, verdict, wiring. Plain script: run with the
repo venv python (never pytest). Uses a temp FOREX_AGENT_HOME so
no live ledger or killswitch state is ever touched."""

import importlib.util
import os
import sys
import tempfile

os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="live10-test-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
_LS = os.path.join(_ROOT, "daemon", "launch_scanner")
sys.path.insert(0, _LS)
sys.path.insert(0, _ROOT)

_PATH = os.path.join(_ROOT, "scripts", "live10-demo-mirror")
_spec = importlib.util.spec_from_loader("live10_mod", loader=None)
M = importlib.util.module_from_spec(_spec)
M.__dict__["__file__"] = _PATH
with open(_PATH) as f:
    exec(compile(f.read(), _PATH, "exec"), M.__dict__)

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# ---- constants ----------------------------------------------------------
check("live10: total $10", M.TOTAL_START == 10.00)
check("live10: share $2.50 x 4 lanes",
      M.SHARE_START == 2.50 and len(M.LANES) == 4
      and set(M.LANES) == {"grad_select", "tranche",
                           "tranche_protect", "grad_raw"})
check("live10: stake $10 scaled from $25 paper",
      M.STAKE == 10.00 and M.PAPER_STAKE == 25.00
      and abs(M.SCALE - 0.4) < 1e-9)
check("live10: min balance $10", M.MIN_BALANCE == 10.00)
check("live10: verdict sample 100", M.VERDICT_SAMPLE_CLOSES == 100)

# ---- scaling math ---------------------------------------------------------
check("live10: +$25 paper -> +$10.00", M.scaled_pnl(25.0) == 10.00)
check("live10: -$25 paper -> -$10.00", M.scaled_pnl(-25.0) == -10.00)
check("live10: +$12.50 paper -> +$5.00", M.scaled_pnl(12.5) == 5.00)


def fresh_ledger():
    led = M.new_ledger()
    led["sol_start"] = {"usd": 10.0, "sol_usd": 100.0, "sol": 0.10}
    return led


def mk_close(mint, pnl, ts):
    return {"mint": mint, "symbol": "TST", "pnl_usd": pnl,
            "close_ts": ts, "closed_reason": "test"}


# ---- funding rule + balances ----------------------------------------------
led = fresh_ledger()
led["lanes"]["grad_select"]["balance"] = 25.00
e1 = M.apply_close(led, "grad_select", mk_close("A1pump", -25.0, 1000))
e2 = M.apply_close(led, "grad_select", mk_close("A2pump", -25.0, 1001))
e3 = M.apply_close(led, "grad_select", mk_close("A3pump", -25.0, 1002))
check("live10: first loss funded ($25.00 -> $15.00)",
      e1 is not None and e1["funded"] and e1["balance_after"] == 15.00)
check("live10: second loss funded ($15.00 -> $5.00)",
      e2 is not None and e2["funded"] and e2["balance_after"] == 5.00)
check("live10: third loss UNFUNDED (balance $5.00 < $10, contributes 0)",
      e3 is not None and not e3["funded"]
      and e3["contributed"] == 0.0 and e3["balance_after"] == 5.00)
check("live10: unfunded close does not count toward combined",
      led["combined"]["closes"] == 2 and led["combined"]["net"] == -20.00
      and led["combined"]["observed"] == 3)

# ---- dedup ------------------------------------------------------------------
led2 = fresh_ledger()
led2["lanes"]["tranche"]["balance"] = 125.00
c = mk_close("B1pump", 25.0, 2000)
first = M.apply_close(led2, "tranche", c)
second = M.apply_close(led2, "tranche", c)
check("live10: same close mirrored once",
      first is not None and second is None
      and led2["combined"]["closes"] == 1)

# ---- wins counting ------------------------------------------------------------
led3 = fresh_ledger()
led3["lanes"]["grad_raw"]["balance"] = 125.00
M.apply_close(led3, "grad_raw", mk_close("C1pump", 50.0, 3000))
M.apply_close(led3, "grad_raw", mk_close("C2pump", -25.0, 3001))
check("live10: wins count only positive contributions",
      led3["combined"]["wins"] == 1 and led3["combined"]["net"] == 10.00)


# ---- verdict ------------------------------------------------------------------
class FakeKS:
    def __init__(self):
        self.halted = []

    def halt_lane(self, lane, reason):
        self.halted.append((lane, reason))


# FUNDED-sample terms (fixed 2026-11-02: the first live verdict
# fired on only 3 funded trades because 97 unfunded raw closes
# flooded the old observed count). Unfunded closes never advance
# the sample, so a verdict rests on 100 trades with money behind
# them, never on noise.
# All losses: lane goes unfunded after 1 close; 100 observed but
# only 1 funded -> NO verdict (sample incomplete).
led4 = fresh_ledger()
led4["lanes"]["grad_select"]["balance"] = 15.00
fake4 = FakeKS()
for i in range(100):
    M.apply_close(led4, "grad_select",
                  mk_close("V%dpump" % i, -25.0, 4000 + i))
v4 = M.maybe_verdict(led4, ks=fake4)
check("live10: no verdict at 100 observed with only 1 funded",
      v4 is None and not fake4.halted
      and led4["combined"]["observed"] == 100
      and led4["combined"]["closes"] == 1)
# Alternating win/loss with negative pair net: lane unfunded after
# 2 closes; 100 observed, 2 funded -> still NO verdict.
led5 = fresh_ledger()
led5["lanes"]["tranche_protect"]["balance"] = 12.00
fake5 = FakeKS()
for i in range(100):
    pnl = 25.0 if i % 2 == 0 else -37.50
    M.apply_close(led5, "tranche_protect",
                  mk_close("W%dpump" % i, pnl, 5000 + i))
v5 = M.maybe_verdict(led5, ks=fake5)
check("live10: no verdict at 100 observed with only 2 funded",
      v5 is None and not fake5.halted
      and led5["combined"]["closes"] == 2)
# All wins: funded throughout, 100 funded -> verdict survives.
led6 = fresh_ledger()
led6["lanes"]["tranche"]["balance"] = 1000.00
for i in range(100):
    M.apply_close(led6, "tranche",
                  mk_close("X%dpump" % i, 25.0, 6000 + i))
v6 = M.maybe_verdict(led6, ks=FakeKS())
check("live10: positive verdict survives at 100 funded",
      v6 is not None and v6["action"].startswith("SURVIVED")
      and v6["funded_closes"] == 100)
# No verdict before 100 funded closes.
led7 = fresh_ledger()
led7["lanes"]["grad_raw"]["balance"] = 1000.00
for i in range(99):
    M.apply_close(led7, "grad_raw", mk_close("Y%dpump" % i, 25.0,
                                             7000 + i))
check("live10: no verdict before 100 funded closes",
      M.maybe_verdict(led7, ks=FakeKS()) is None)
# Baseline (restart): pre-sample losses must not pollute the new
# sample. 2 funded losses, then a fresh sample start, then 100
# funded wins -> SURVIVED on the sample alone (+$10.00, not the
# lifetime mean), and the verdict fires only once.
led8 = fresh_ledger()
led8["lanes"]["grad_select"]["balance"] = 250.00
M.apply_close(led8, "grad_select", mk_close("Z1pump", -25.0, 8000))
M.apply_close(led8, "grad_select", mk_close("Z2pump", -25.0, 8001))
led8["verdict_sample"] = {"started_ts": 8002, "funded_start": 2,
                          "net_start": -20.00}
led8["lanes"]["tranche"]["balance"] = 1000.00
for i in range(100):
    M.apply_close(led8, "tranche",
                  mk_close("Z%dpump" % i, 25.0, 8100 + i))
v8 = M.maybe_verdict(led8, ks=FakeKS())
check("live10: baseline sample ignores pre-sample losses",
      v8 is not None and v8["action"].startswith("SURVIVED")
      and v8["funded_closes"] == 100 and v8["expectancy"] == 10.00
      and M.maybe_verdict(led8, ks=FakeKS()) is None)
# Baseline + 100 funded losses -> HALT on the sample mean.
# (Test setup: deep tranche balance so all 100 losses stay funded.)
led9 = fresh_ledger()
led9["lanes"]["grad_select"]["balance"] = 250.00
M.apply_close(led9, "grad_select", mk_close("Q1pump", 25.0, 9000))
led9["verdict_sample"] = {"started_ts": 9001, "funded_start": 1,
                          "net_start": 10.00}
led9["lanes"]["tranche"]["balance"] = 10000.00
fake9 = FakeKS()
for i in range(100):
    M.apply_close(led9, "tranche",
                  mk_close("Q%dpump" % i, -25.0, 9100 + i))
v9 = M.maybe_verdict(led9, ks=fake9)
check("live10: baseline sample halts on funded losses",
      v9 is not None and v9["action"].startswith("HALT")
      and v9["funded_closes"] == 100 and v9["expectancy"] == -10.00
      and fake9.halted and fake9.halted[0][0] == "live10")

# ---- no backfill: pre-account closes are never mirrored --------------
# Plant a fake grad_select ledger in the temp HOME: one old close
# (predating the account) and one new close. poll_once must mirror
# only the new one.
import json as _json
import time as _time
_home = os.environ["FOREX_AGENT_HOME"]
with open(os.path.join(_home, "grad_select_ledger.json"), "w") as f:
    _json.dump({"closes": [
        {"mint": "Old1pump", "symbol": "OLD", "pnl_usd": 25.0,
         "close_ts": 1000, "closed_reason": "test"},
        {"mint": "New1pump", "symbol": "NEW", "pnl_usd": 25.0,
         "close_ts": _time.time() + 60, "closed_reason": "test"},
    ]}, f)
# Pre-seed the live10 ledger with a funded grad_select share ($125),
# so the new close mirrors funded at the $10 stake.
_led0 = M.new_ledger()
_led0["lanes"]["grad_select"]["balance"] = 125.00
with open(os.path.join(_home, "live10_demo_ledger.json"), "w") as f:
    _json.dump(_led0, f)
_n = M.poll_once()
_led = _json.load(open(os.path.join(_home, "live10_demo_ledger.json")))
check("live10: poll mirrors only post-creation closes (no backfill)",
      _n == 1 and _led["combined"]["observed"] == 1
      and _led["combined"]["closes"] == 1
      and _led["combined"]["net"] == 10.00)

# ---- wiring (file text, no side effects) ----------------------------------
with open(os.path.join(_LS, "killswitch.py")) as f:
    ks_text = f.read()
check("live10: killswitch CFG has live10 {100,100}",
      '"live10":' in ks_text)
with open(os.path.join(_ROOT, "scripts", "supervisor")) as f:
    sup_text = f.read()
check("live10: supervisor roster has live10-demo-mirror",
      '"live10-demo-mirror"' in sup_text)
with open(os.path.join(_ROOT, "scripts", "forex-watchdog")) as f:
    wd_text = f.read()
check("live10: watchdog guards live10-demo-mirror",
      "scripts/live10-demo-mirror" in wd_text)
check("live10: mirror script exists",
      os.path.exists(os.path.join(_ROOT, "scripts",
                                  "live10-demo-mirror")))

# ---- load fail-closed (2026-11-04 reset) ----------------------------------
# A corrupt or lane-less ledger file must NEVER be silently replaced
# by a fresh seed; load_ledger raises and the file is left untouched.
import json as _json2
_lp = M.LEDGER_PATH
# No shadow copy for these two: with a valid shadow present, recovery
# (not raising) is the designed outcome, covered by the tests below.
if os.path.exists(_lp + ".bak"):
    os.remove(_lp + ".bak")
with open(_lp, "w") as f:
    f.write("{not json")
_raised = False
try:
    M.load_ledger()
except Exception:
    _raised = True
check("live10: corrupt ledger raises instead of re-seeding", _raised)
check("live10: corrupt ledger file left untouched",
      open(_lp).read() == "{not json")
with open(_lp, "w") as f:
    _json2.dump({"combined": {}}, f)
_raised2 = False
try:
    M.load_ledger()
except Exception:
    _raised2 = True
check("live10: lane-less ledger raises instead of re-seeding", _raised2)
os.remove(_lp)
_seeded = M.load_ledger()
check("live10: missing ledger still seeds on true first run",
      _seeded["combined"]["closes"] == 0
      and _seeded["lanes"]["grad_select"]["balance"] == 2.50)

# ---- shadow-copy fallback (2026-11-04 truncation) -------------------------
# Empty/corrupt live file + valid shadow copy -> load the shadow copy,
# never a fresh seed. Empty live + broken/missing shadow -> raise.
_bak = M.LEDGER_PATH + ".bak"
_good = {"lanes": {"grad_select": {"balance": 5.0}}, "combined": {}}
with open(_bak, "w") as f:
    _json2.dump(_good, f)
with open(M.LEDGER_PATH, "w") as f:
    f.write("")
_rec = M.load_ledger()
check("live10: empty live file recovers from shadow copy",
      _rec["lanes"]["grad_select"]["balance"] == 5.0)
with open(_bak, "w") as f:
    f.write("{broken")
_raised3 = False
try:
    M.load_ledger()
except Exception:
    _raised3 = True
check("live10: empty live + broken shadow raises", _raised3)
os.remove(_bak)
_raised4 = False
try:
    M.load_ledger()
except Exception:
    _raised4 = True
check("live10: empty live + no shadow raises", _raised4)

# ---- single-writer lock (2026-11-14: five 0-byte incidents) -------------
# _atomic_write must use a per-process tmp name (never a shared
# ".tmp"), fsync, and os.replace — and _lock() must serialize
# read-modify-write cycles so concurrent mirror processes cannot
# drop each other's closes or interleave a zeroed file.
import glob as _glob
import multiprocessing as _mp

_lock_home = tempfile.mkdtemp(prefix="live10-lock-test-")
_orig_ledger = M.LEDGER_PATH
_orig_lock = M.LOCK_FILE
M.LEDGER_PATH = os.path.join(_lock_home, "live10_demo_ledger.json")
M.LOCK_FILE = M.LEDGER_PATH + ".lock"

# 1. per-process tmp: no shared ".tmp" file is ever created.
M._atomic_write(M.LEDGER_PATH, {"counter": 0})
check("live10: _atomic_write leaves no shared .tmp file",
      not os.path.exists(M.LEDGER_PATH + ".tmp")
      and not _glob.glob(M.LEDGER_PATH + ".tmp.*"))
check("live10: _atomic_write result parses and is non-empty",
      _json.load(open(M.LEDGER_PATH))["counter"] == 0
      and os.path.getsize(M.LEDGER_PATH) > 0)


# 2. concurrent _atomic_write stress: 4 writers x 25 rounds on the
# same path — the file must always parse and never be 0 bytes.
def _stress_writer(args):
    path, rounds, wid = args
    for i in range(rounds):
        M._atomic_write(path, {"writer": wid, "i": i, "pad": "x" * 200})


with _mp.get_context("fork").Pool(4) as _pool:
    _pool.map(_stress_writer,
              [(M.LEDGER_PATH, 25, w) for w in range(4)])
_stress_ok = False
try:
    _d = _json.load(open(M.LEDGER_PATH))
    _stress_ok = (isinstance(_d, dict) and "writer" in _d
                  and os.path.getsize(M.LEDGER_PATH) > 0)
except Exception:
    _stress_ok = False
check("live10: 4-way concurrent _atomic_write never corrupts",
      _stress_ok
      and not os.path.exists(M.LEDGER_PATH + ".tmp"))


# 3. lock serializes read-modify-write: 4 workers x 25 increments
# must land exactly 100 — any lost update fails this.
def _counter_worker(rounds):
    for _ in range(rounds):
        with M._lock():
            try:
                with open(M.LEDGER_PATH) as f:
                    d = _json.load(f)
            except Exception:
                d = {"counter": 0}
            d["counter"] = int(d.get("counter") or 0) + 1
            M._atomic_write(M.LEDGER_PATH, d)


M._atomic_write(M.LEDGER_PATH, {"counter": 0})
with _mp.get_context("fork").Pool(4) as _pool:
    _pool.map(_counter_worker, [25, 25, 25, 25])
_final = _json.load(open(M.LEDGER_PATH))["counter"]
check("live10: lock serializes increments (4x25 == 100, no lost update)",
      _final == 100)


# 4. poll_once double-writer: two processes mirroring the same 40
# planted closes must land exactly 40, with live and .bak
# byte-identical and the lock file present.
M.LEDGER_PATH = _orig_ledger
M.LOCK_FILE = _orig_lock
_seed = M.new_ledger()
_seed["lanes"]["grad_select"]["balance"] = 125.00
M._atomic_write(M.LEDGER_PATH, _seed)
_now = _time.time()
with open(os.path.join(_home, "grad_select_ledger.json"), "w") as f:
    _json.dump({"closes": [
        {"mint": "R%dpump" % i, "symbol": "R%d" % i, "pnl_usd": 25.0,
         "close_ts": _now + 61 + i, "closed_reason": "test"}
        for i in range(40)
    ]}, f)


def _poll_worker(rounds):
    for _ in range(rounds):
        M.poll_once()


with _mp.get_context("fork").Pool(2) as _pool:
    _pool.map(_poll_worker, [3, 3])
_led4 = _json.load(open(M.LEDGER_PATH))
_live_bytes = open(M.LEDGER_PATH, "rb").read()
_bak_bytes = open(M.LEDGER_PATH + ".bak", "rb").read()
check("live10: double-writer poll lands all 40 closes once",
      _led4["combined"]["closes"] == 40
      and _led4["combined"]["observed"] == 40
      and _led4["combined"]["net"] == 400.00)
check("live10: double-writer leaves live == .bak, non-empty",
      len(_live_bytes) > 0 and _live_bytes == _bak_bytes)
check("live10: lock file exists after poll",
      os.path.exists(M.LOCK_FILE))

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL LIVE10 TESTS PASSED")
