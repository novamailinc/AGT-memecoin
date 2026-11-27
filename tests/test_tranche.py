"""Tranche lane — leg specs, buyback rule, prereg verdict terms,
killswitch registration. Plain script: run with the repo venv
python (never pytest). Uses a temp FOREX_AGENT_HOME so no live
ledger, killswitch state, or verdict chain is ever touched."""

import contextlib
import importlib.util
import os
import sys
import tempfile
import time

os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="tranche-test-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
_LS = os.path.join(_ROOT, "daemon", "launch_scanner")
sys.path.insert(0, _LS)
sys.path.insert(0, _ROOT)

_PATH = os.path.join(_LS, "tranche.py")
_spec = importlib.util.spec_from_loader("tranche_mod", loader=None)
T = importlib.util.module_from_spec(_spec)
T.__dict__["__file__"] = _PATH
with open(_PATH) as f:
    exec(compile(f.read(), _PATH, "exec"), T.__dict__)

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# ---- engine overrides -------------------------------------------------
eng = T.make_engine()
check("tranche: lane is tranche", eng.LANE == "tranche")
check("tranche: own ledger file",
      str(eng.LEDGER_PATH).endswith("tranche_ledger.json"))
check("tranche: stake $25", eng.NOTIONAL_USD == 25.00)
check("tranche: max open 20", eng.MAX_OPEN == 20)
check("tranche: bankroll $500", eng.BANKROLL_START == 500.00)
check("tranche: leg A 25%@1.5x",
      (eng.LEG_A_FRAC, eng.LEG_A_TARGET) == (0.25, 1.5))
check("tranche: leg B 25%@3x",
      (eng.LEG_B_FRAC, eng.LEG_B_TARGET) == (0.25, 3.0))
check("tranche: leg C 50% trail 30%",
      (eng.LEG_C_FRAC, eng.LEG_C_TRAIL) == (0.50, 0.30))
check("tranche: verdict sample 100",
      eng.VERDICT_SAMPLE_CLOSES == 100)
check("tranche: no safety gate override",
      eng._safety_ok is not None)
check("tranche: engine is not grad_select",
      eng.LANE != "grad_select"
      and str(eng.LEDGER_PATH) != os.path.join(
          os.environ["FOREX_AGENT_HOME"], "grad_select_ledger.json"))

# ---- preregistration terms ---------------------------------------------
pre = T._preregistration()
check("tranche: prereg lane", pre["lane"] == "tranche")
check("tranche: prereg sample 100", pre["sample_closes"] == 100)
check("tranche: prereg rule names 100 closes",
      "100 closes" in pre["rule"])
check("tranche: prereg rule names 7 days",
      "7 days" in pre["rule"])
check("tranche: prereg ladder text",
      pre["terms"]["ladder"]
      == "25%@1.5x / 25%@3x / 50% trail 30% / -50% stop / 24h")
check("tranche: prereg buyback text",
      "25%" in pre["terms"]["buyback"]
      and "30%" in pre["terms"]["buyback"])
check("tranche: prereg killswitch cfg",
      pre["killswitch_cfg"]
      == {"window": 100, "min_trades": 100, "halt": True})

# ---- killswitch registration (source-level: the LANES dict) -------------
with open(os.path.join(_LS, "killswitch.py")) as f:
    ks_src = f.read()
check("tranche: killswitch entry present",
      '"tranche"' in ks_src
      and '"tranche":' in ks_src.replace(" ", "").replace(
          '"tranche":      ', '"tranche":'))

# ---- buyback decision (pure) --------------------------------------------
NOW = time.time()


def base_pos(**over):
    pos = {
        "mint": "TESTMINT",
        "legs": {"a": {"done": True}, "b": {"done": False},
                 "c": {"done": False}},
        "entry_price": 0.000025,
        "peak_price": 0.00005,
        "tokens": 1000000.0,
        "realized_usd": 10.0,
        "entry_ts": NOW - 3600,
    }
    pos.update(over)
    return pos


# Base: mult 1.4, retrace exactly 30% from peak, proceeds cover cost.
fire, cost, tokens = T._buyback_due(
    base_pos(), 0.000035, 0.000035, NOW)
check("tranche: buyback fires at 30% retrace", fire is True)
check("tranche: buyback tokens 25% of original", tokens == 250000.0)
check("tranche: buyback cost = tokens x fill",
      abs(cost - 250000.0 * 0.000035) < 1e-9)

fire, _r, _t = T._buyback_due(
    base_pos(legs={"a": {"done": False}, "b": {"done": False},
                   "c": {"done": False}}),
    0.000035, 0.000035, NOW)
check("tranche: buyback blocked, no tranche banked", fire is False)

fire, _r, _t = T._buyback_due(
    base_pos(buyback_used=True), 0.000035, 0.000035, NOW)
check("tranche: buyback blocked, already used", fire is False)

fire, _r, _t = T._buyback_due(
    base_pos(), 0.00002, 0.00002, NOW)
check("tranche: buyback blocked, below entry", fire is False)

fire, _r, _t = T._buyback_due(
    base_pos(), 0.000045, 0.000045, NOW)
check("tranche: buyback blocked, retrace < 30%", fire is False)

fire, _r, _t = T._buyback_due(
    base_pos(entry_ts=NOW - 90000), 0.000035, 0.000035, NOW)
check("tranche: buyback blocked, past 24h", fire is False)

fire, _r, _t = T._buyback_due(
    base_pos(realized_usd=5.0), 0.000035, 0.000035, NOW)
check("tranche: buyback blocked, proceeds short", fire is False)

# ---- buyback booking (fake engine) ---------------------------------------
class FakeFills:
    @staticmethod
    def honest_entry(quoted, size, liq):
        return quoted * 1.005


class FakeKS:
    def __init__(self):
        self.halted = False
        self.halts = []

    def is_halted(self, lane):
        return self.halted

    def gate(self, lane):
        return not self.is_halted(lane)

    def halt_lane(self, lane, reason):
        self.halts.append((lane, reason))
        self.halted = True


class FakeEngine:
    def __init__(self, ledger):
        self.d = ledger
        self._ks = FakeKS()
        self._fills = FakeFills()
        self.events = []

    @contextlib.contextmanager
    def _ledger_lock(self):
        yield

    def _read(self):
        return self.d

    def _write(self, d):
        self.d = d

    def open_positions(self):
        return list(self.d["positions"].values())

    def quote_price(self, chain, mint):
        return 0.000035

    def _verdict_event(self, lane, kind, payload):
        self.events.append((lane, kind, payload))


T._LAST_BUYBACK_CHECK = 0.0
pos = base_pos()
pos["chain"] = "solana"
pos["entry_snapshot"] = {"liquidity": 30000.0}
pos["last_liquidity"] = 30000.0
fake = FakeEngine({"positions": {"TESTMINT": pos}, "closes": [],
                   "created_ts": NOW - 3600})
buys = T.maybe_buyback(fake, NOW)
# RECONCILED BUYBACK (bb3 root-cause fix): a buyback is an entry through
# the shared paper account + the real DemoFiller quote path. The old
# lane-only model fill is GONE by design ("NO fallback ... never a
# lane-only buyback"): an engine without the account interface gets NO
# buyback at all, however eligible the position looks. The positive
# path is covered by tests/test_tranche_buyback_demofill.py (real
# account + real DemoFiller, passing). These checks assert the
# fail-closed side: the stub engine books nothing and mutates nothing.
check("tranche: no lane-only buyback without the account interface",
      buys == [])
live = fake.d["positions"]["TESTMINT"]
check("tranche: refused buyback leaves tokens untouched",
      live["tokens"] == 1000000.0)
check("tranche: refused buyback leaves proceeds untouched",
      live["realized_usd"] == 10.0)
check("tranche: refused buyback not flagged used",
      live.get("buyback_used") is not True)
check("tranche: refused buyback stamps nothing on entry_features",
      "buyback" not in live.get("entry_features", {}))
T._LAST_BUYBACK_CHECK = 0.0
buys2 = T.maybe_buyback(fake, NOW)
check("tranche: buyback never repeats (still refused)", buys2 == [])

# Regression (2026-10-30): the real grad engine's open_positions()
# returns {mint: position}, not a list. Iterating the dict fed mint
# strings into pos.get(), so every live buyback check failed and no
# buyback ever fired. Dict-shaped positions must work identically.
class DictFakeEngine(FakeEngine):
    def open_positions(self):
        return self.d["positions"]

T._LAST_BUYBACK_CHECK = 0.0
pos3 = base_pos()
pos3["chain"] = "solana"
pos3["entry_snapshot"] = {"liquidity": 30000.0}
pos3["last_liquidity"] = 30000.0
fake3 = DictFakeEngine({"positions": {"TESTMINT": pos3}, "closes": [],
                        "created_ts": NOW - 3600})
buys3 = T.maybe_buyback(fake3, NOW)
check("tranche: dict-shaped positions also refuse without the account interface",
      buys3 == [] and fake3.d["positions"]["TESTMINT"].get(
          "buyback_used") is not True)

# ---- deadline verdict (fake engine) ---------------------------------------
# Profitable sample at the deadline -> SURVIVED, no halt.
d = {"positions": {}, "closes": [
    {"pnl_usd": 5.0, "close_ts": NOW - 100},
    {"pnl_usd": -2.0, "close_ts": NOW - 90},
    {"pnl_usd": 1.0, "close_ts": NOW - 80}],
    "created_ts": NOW - 8 * 86400,
    "verdict_deadline_ts": NOW - 3600}
fake2 = FakeEngine(d)
rep = T.maybe_verdict_report(fake2)
check("tranche: deadline verdict fires", rep is not None)
check("tranche: deadline profitable survives",
      fake2.d.get("verdict", {}).get("action", "").startswith(
          "SURVIVED"))
check("tranche: deadline profitable does not halt",
      fake2._ks.halts == [])
check("tranche: deadline verdict trigger recorded",
      fake2.d.get("verdict", {}).get("trigger") == "deadline")

# Losing sample at the deadline -> HALT.
d3 = {"positions": {}, "closes": [
    {"pnl_usd": -5.0, "close_ts": NOW - 100},
    {"pnl_usd": -2.0, "close_ts": NOW - 90}],
    "created_ts": NOW - 8 * 86400,
    "verdict_deadline_ts": NOW - 3600}
fake3 = FakeEngine(d3)
rep3 = T.maybe_verdict_report(fake3)
check("tranche: deadline losing verdict fires", rep3 is not None)
check("tranche: deadline losing halts",
      len(fake3._ks.halts) == 1
      and fake3._ks.halts[0][0] == "tranche")

# Deadline not reached -> no verdict, no halt.
d4 = {"positions": {}, "closes": [
    {"pnl_usd": -5.0, "close_ts": NOW - 100}],
    "created_ts": NOW - 3600,
    "verdict_deadline_ts": NOW + 6 * 86400}
fake4 = FakeEngine(d4)
rep4 = T.maybe_verdict_report(fake4)
check("tranche: no verdict before deadline",
      rep4 is None and "verdict" not in fake4.d)

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL TRANCHE TESTS PASSED")
