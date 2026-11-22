"""Mimic-edge2 lane — faster TWAP (4/poll + 8/poll below 0.90x),
engine policy, killswitch registration, roster/watchdog wiring.
Plain script: run with the repo venv python (never pytest). Uses
a temp FOREX_AGENT_HOME so no live ledger, killswitch state, or
verdict chain is ever touched."""

import importlib.util
import os
import sys
import tempfile
import time

os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="mimic-edge2-test-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
_LS = os.path.join(_ROOT, "daemon", "launch_scanner")
sys.path.insert(0, _LS)
sys.path.insert(0, _ROOT)

_PATH = os.path.join(_LS, "mimic_edge2.py")
_spec = importlib.util.spec_from_loader("mimic_edge2_mod", loader=None)
M = importlib.util.module_from_spec(_spec)
M.__dict__["__file__"] = _PATH
with open(_PATH) as f:
    exec(compile(f.read(), _PATH, "exec"), M.__dict__)

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# ---- engine overrides -------------------------------------------------
eng = M.make_engine()
check("edge2: lane is mimic_edge2", eng.LANE == "mimic_edge2")
check("edge2: own ledger file",
      str(eng.LEDGER_PATH).endswith("mimic_edge2_ledger.json"))
check("edge2: stake $25", eng.NOTIONAL_USD == 25.00)
check("edge2: max open 20", eng.MAX_OPEN == 20)
check("edge2: bankroll $500", eng.BANKROLL_START == 500.00)
check("edge2: launch window 120s", eng.MAX_GRAD_AGE_S == 120.0)
check("edge2: chase guard 120s", eng.MAX_SLOT_WAIT_S == 120.0)
check("edge2: verdict sample 100", eng.VERDICT_SAMPLE_CLOSES == 100)
check("edge2: tightened stop -25%", eng.HARD_STOP_MULT == 0.75)
check("edge2: TWAP 64 slices", eng.TWAP_SLICES == 64)
check("edge2: TWAP max hold 3600s", eng.TWAP_MAX_HOLD_S == 3600.0)
check("edge2: TWAP 4 slices per poll", eng.TWAP_SLICES_PER_POLL == 4)
check("edge2: accel below 0.90x", eng.TWAP_ACCEL_BELOW_MULT == 0.90)
check("edge2: accel 8 slices per poll",
      eng.TWAP_ACCEL_SLICES_PER_POLL == 8)
check("edge2: no safety gate (passes)",
      eng._safety_ok("AnyMintpump")[0] is True)
check("edge2: no veto (passes)",
      eng._veto_ok("AnyMintpump", "X", None)[0] is True)
check("edge2: engine is not mimic_edge",
      eng.LANE != "mimic_edge"
      and str(eng.LEDGER_PATH) != os.path.join(
          os.environ["FOREX_AGENT_HOME"], "mimic_edge_ledger.json"))

# ---- preregistration terms ---------------------------------------------
pre = eng._preregistration()
check("edge2: prereg lane mimic_edge2", pre.get("lane") == "mimic_edge2")
check("edge2: prereg sample 100", pre.get("sample_closes") == 100)
check("edge2: prereg rule names killswitch mimic_edge2",
      "mimic_edge2" in str(pre.get("rule") or ""))
check("edge2: prereg terms name 4/poll exit",
      "4/poll" in str((pre.get("terms") or {}).get("exit", "")))
check("edge2: prereg terms name superseded mimic_edge",
      "mimic_edge" in str((pre.get("terms") or {}).get("supersedes", "")))

# ---- filter (same code as edge; one decision check) ---------------------
now = time.time()
j2 = {"tickers": {"AAA": [now - 10, now - 20, now - 30]},
      "deployers": {}}
check("edge2: filter rejects 3rd same-ticker launch",
      M._filter_decision(j2, "AAA", "D9", now) ==
      (False, "copycat ticker"))

# ---- TWAP rate + acceleration: engine behavior ---------------------------
eng.TWAP_SLICES = 8
eng.TWAP_SLICES_PER_POLL = 4
eng.TWAP_ACCEL_BELOW_MULT = 0.90
eng.TWAP_ACCEL_SLICES_PER_POLL = 8
eng.HARD_STOP_MULT = 0.75
eng.ensure_ledger()


def seed_pos(mint, entry_ts, entry_price=1.0, tokens=25.0):
    pos = {"mint": mint, "symbol": "TST", "chain": "solana",
           "source": "launch-mimic-edge2", "lane": "mimic_edge2",
           "entry_ts": entry_ts, "quoted_price": entry_price,
           "entry_price": entry_price, "notional_usd": 25.0,
           "peak_price": entry_price, "peak_ts": entry_ts,
           "tokens": tokens, "gas_in_usd": 0.0,
           "legs": {"a": {"frac": 0.50, "target": 2.0, "done": False,
                          "exit_price": 0, "realized_usd": 0},
                    "b": {"frac": 0.25, "target": 3.0, "done": False,
                          "exit_price": 0, "realized_usd": 0},
                    "c": {"frac": 0.25, "trail": 0.30,
                          "peak": entry_price, "done": False,
                          "exit_price": 0, "realized_usd": 0}},
           "realized_usd": 0.0, "status": "open",
           "spot_index": None, "pair": "CurveKey123",
           "entry_snapshot": {"liquidity": None},
           "entry_features": {}}
    with eng._ledger_lock():
        d = eng._read()
        d["positions"][mint] = pos
        eng._write(d)


def close_for(mint):
    with eng._ledger_lock():
        d = eng._read()
    for c in d.get("closes") or []:
        if c.get("mint") == mint:
            return c
    return None


def open_pos(mint):
    with eng._ledger_lock():
        d = eng._read()
    return (d.get("positions") or {}).get(mint)


# 1. Rate: 8 slices at 4/poll complete in exactly 2 polls.
seed_pos("Rate1pump", now)
eng.poll_exits(lambda c, m, s=None: 1.0)
p1 = open_pos("Rate1pump")
check("edge2: 4 slices sold after 1 poll (not 1, not 8)",
      p1 is not None and (p1.get("twap") or {}).get("done") == 4)
eng.poll_exits(lambda c, m, s=None: 1.0)
c1 = close_for("Rate1pump")
check("edge2: rate completes in 2 polls (twap_complete)",
      c1 is not None and c1.get("closed_reason") == "twap_complete"
      and (c1.get("twap") or {}).get("done") == 8)

# 2. Acceleration: below 0.90x, 8 slices in a single poll.
seed_pos("Accel1pump", now)
eng.poll_exits(lambda c, m, s=None: 0.85)
c2 = close_for("Accel1pump")
check("edge2: accel dumps all 8 slices in 1 poll below 0.90x",
      c2 is not None and c2.get("closed_reason") == "twap_complete"
      and (c2.get("twap") or {}).get("done") == 8)

# 3. No acceleration at/above 0.90x: 0.95x sells 4, not 8.
seed_pos("Noacc1pump", now)
eng.poll_exits(lambda c, m, s=None: 0.95)
p3 = open_pos("Noacc1pump")
check("edge2: no accel at 0.95x (4 slices, still open)",
      p3 is not None and (p3.get("twap") or {}).get("done") == 4)

# 4. Ladder unchanged when TWAP flags are off (default-off proof).
with open(os.path.join(_LS, "grad.py")) as f:
    grad_text = f.read()
check("edge2: grad.py TWAP rate defaults are 1/off",
      "TWAP_SLICES_PER_POLL = 1" in grad_text
      and "TWAP_ACCEL_BELOW_MULT = None" in grad_text
      and "TWAP_ACCEL_SLICES_PER_POLL = None" in grad_text)
eng2 = M.make_engine()
eng2.TWAP_SLICES = None
eng2.TWAP_MAX_HOLD_S = None
eng2.TWAP_SLICES_PER_POLL = 1
eng2.TWAP_ACCEL_BELOW_MULT = None
eng2.HARD_STOP_MULT = 0.50
eng2.ensure_ledger()
pos = {"mint": "Lad1pump", "symbol": "TST", "chain": "solana",
       "source": "launch-mimic-edge2", "lane": "mimic_edge2",
       "entry_ts": now, "quoted_price": 1.0,
       "entry_price": 1.0, "notional_usd": 25.0,
       "peak_price": 1.0, "peak_ts": now,
       "tokens": 25.0, "gas_in_usd": 0.0,
       "legs": {"a": {"frac": 0.50, "target": 2.0, "done": False,
                      "exit_price": 0, "realized_usd": 0},
                "b": {"frac": 0.25, "target": 3.0, "done": False,
                      "exit_price": 0, "realized_usd": 0},
                "c": {"frac": 0.25, "trail": 0.30,
                      "peak": 1.0, "done": False,
                      "exit_price": 0, "realized_usd": 0}},
       "realized_usd": 0.0, "status": "open",
       "spot_index": None, "pair": "CurveKey123",
       "entry_snapshot": {"liquidity": None},
       "entry_features": {}}
with eng2._ledger_lock():
    d = eng2._read()
    d["positions"]["Lad1pump"] = pos
    eng2._write(d)
eng2.poll_exits(lambda c, m, s=None: 2.0)
with eng2._ledger_lock():
    d = eng2._read()
lp = (d.get("positions") or {}).get("Lad1pump")
check("edge2: ladder flags-off banks leg A at 2x, stays open",
      lp is not None and (lp.get("legs") or {}).get("a", {}).get(
          "done") is True and "twap" not in lp)

# ---- wiring (file text, no side effects) ----------------------------------
with open(os.path.join(_LS, "killswitch.py")) as f:
    ks_text = f.read()
check("edge2: killswitch CFG has mimic_edge2 {100,100}",
      '"mimic_edge2":' in ks_text and "min_trades" in ks_text)
with open(os.path.join(_ROOT, "scripts", "supervisor")) as f:
    sup_text = f.read()
check("edge2: supervisor roster has mimic-edge2-trader",
      '"mimic-edge2-trader"' in sup_text)
with open(os.path.join(_ROOT, "scripts", "forex-watchdog")) as f:
    wd_text = f.read()
check("edge2: watchdog guards mimic-edge2-trader",
      "scripts/mimic-edge2-trader" in wd_text)
check("edge2: trader script exists",
      os.path.exists(os.path.join(_ROOT, "scripts",
                                  "mimic-edge2-trader")))

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL MIMIC_EDGE2 TESTS PASSED")
