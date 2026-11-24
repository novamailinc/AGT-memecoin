"""Paper-Live lane ($10 paper account simulating live trading):
engine policy, tranche-package behavior, killswitch registration,
roster/watchdog wiring. Plain script: run with the repo venv
python (never pytest). Uses a temp FOREX_AGENT_HOME so no live
ledger, killswitch state, or verdict chain is ever touched."""

import importlib.util
import os
import sys
import tempfile
import time

os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="paper-live-test-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
_LS = os.path.join(_ROOT, "daemon", "launch_scanner")
sys.path.insert(0, _LS)
sys.path.insert(0, _ROOT)

_PATH = os.path.join(_LS, "paper_live.py")
_spec = importlib.util.spec_from_loader("paper_live_mod", loader=None)
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
check("paper_live: lane is paper_live", eng.LANE == "paper_live")
check("paper_live: own ledger file",
      str(eng.LEDGER_PATH).endswith("paper_live_ledger.json"))
check("paper_live: bankroll $10", eng.BANKROLL_START == 10.00)
check("paper_live: stake $1", eng.NOTIONAL_USD == 1.00)
check("paper_live: max open 10", eng.MAX_OPEN == 10)
check("paper_live: grad window 30min", eng.MAX_GRAD_AGE_S == 1800.0)
check("paper_live: verdict sample 100", eng.VERDICT_SAMPLE_CLOSES == 100)
check("paper_live: leg A 25%@1.5x",
      eng.LEG_A_FRAC == 0.25 and eng.LEG_A_TARGET == 1.5)
check("paper_live: leg B 25%@3x",
      eng.LEG_B_FRAC == 0.25 and eng.LEG_B_TARGET == 3.0)
check("paper_live: leg C 50% trail 30%",
      eng.LEG_C_FRAC == 0.50 and eng.LEG_C_TRAIL == 0.30)
check("paper_live: protect after A (breakeven)",
      eng.PROTECT_AFTER_A is True)
check("paper_live: no safety gate (passes)",
      eng._safety_ok("AnyMintpump")[0] is True)
check("paper_live: no veto (passes)",
      eng._veto_ok("AnyMintpump", "X", None)[0] is True)
check("paper_live: reuses select evaluator",
      "evaluate_candidate" in open(_PATH).read())

# ---- preregistration terms ---------------------------------------------
pre = eng._preregistration()
check("paper_live: prereg lane paper_live", pre.get("lane") == "paper_live")
check("paper_live: prereg sample 100", pre.get("sample_closes") == 100)
check("paper_live: prereg rule names killswitch paper_live",
      "paper_live" in str(pre.get("rule") or ""))
terms = pre.get("terms") or {}
check("paper_live: prereg terms $1 stake / $10 bankroll",
      terms.get("notional_usd") == 1.00
      and terms.get("bankroll_start") == 10.00)
check("paper_live: prereg terms say PAPER ONLY",
      "PAPER ONLY" in str(terms.get("paper_note") or ""))

# ---- tranche-package behavior: $1 stake ----------------------------------
eng.ensure_ledger()
now = time.time()


def seed_pos(mint, entry_ts, entry_price=1.0, tokens=1.0):
    pos = {"mint": mint, "symbol": "TST", "chain": "solana",
           "source": "graduation-paper_live", "lane": "paper_live",
           "entry_ts": entry_ts, "quoted_price": entry_price,
           "entry_price": entry_price, "notional_usd": 1.0,
           "peak_price": entry_price, "peak_ts": entry_ts,
           "tokens": tokens, "gas_in_usd": 0.0,
           "legs": {"a": {"frac": 0.25, "target": 1.5, "done": False,
                          "exit_price": 0, "realized_usd": 0},
                    "b": {"frac": 0.25, "target": 3.0, "done": False,
                          "exit_price": 0, "realized_usd": 0},
                    "c": {"frac": 0.50, "trail": 0.30,
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


def open_pos(mint):
    with eng._ledger_lock():
        d = eng._read()
    return (d.get("positions") or {}).get(mint)


def close_for(mint):
    with eng._ledger_lock():
        d = eng._read()
    for c in d.get("closes") or []:
        if c.get("mint") == mint:
            return c
    return None


seed_pos("Live1pump", now)
eng.poll_exits(lambda c, m, s=None: 1.5)
p1 = open_pos("Live1pump")
check("paper_live: leg A banks at 1.5x, position stays open",
      p1 is not None and (p1.get("legs") or {}).get("a", {}).get(
          "done") is True)
eng.poll_exits(lambda c, m, s=None: 0.99)
c1 = close_for("Live1pump")
check("paper_live: drop below entry after A closes it in profit",
      c1 is not None and float(c1.get("pnl_usd") or 0) > 0)

# ---- wiring (file text, no side effects) ----------------------------------
with open(os.path.join(_LS, "killswitch.py")) as f:
    ks_text = f.read()
check("paper_live: killswitch CFG has paper_live {100,100}",
      '"paper_live":' in ks_text)
with open(os.path.join(_ROOT, "scripts", "supervisor")) as f:
    sup_text = f.read()
# Superseded by live10-demo-mirror 2026-11-02 (see
# scripts/supervisor and tests/test_supervisor.py's pinned roster).
check("paper_live: supervisor roster omits the superseded "
      "paper-live-trader",
      '"paper-live-trader"' not in sup_text)
with open(os.path.join(_ROOT, "scripts", "forex-watchdog")) as f:
    wd_text = f.read()
# The watchdog no longer restarts it: the lane is retired, so
# its guard block must be absent or commented out.
check("paper_live: watchdog does not run the superseded "
      "paper-live-trader",
      "scripts/paper-live-trader" not in wd_text
      or all(l.strip().startswith("#")
             for l in wd_text.splitlines()
             if "scripts/paper-live-trader" in l))
check("paper_live: trader script exists",
      os.path.exists(os.path.join(_ROOT, "scripts",
                                  "paper-live-trader")))

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL PAPER_LIVE TESTS PASSED")
