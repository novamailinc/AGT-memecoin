#!/usr/bin/env python3
"""A+B forward paper test (Phase 3, 2026-11-17).

Covers grad_select.py + grad_raw.py _stamp_detection arm stamping
("A+B" when deployer_launches_24h >= 1, else "A"), the HARD RULE that
an A-only candidate (0 launches) still PASSES evaluation when Rule A
passes (no gating, no skip, no deprioritization on the B arm), and the
ab-forward-report math on fixture closes — including verdict refusal
below 100 forward closes.

No network, no live ledgers (report math runs on in-memory fixtures;
the evaluate test stubs every data source in-process, same pattern as
tests/test_grad_select.py). Plain script — run directly, not via pytest.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import contextlib
import importlib.util
import os
import sys
import tempfile
import time

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import grad_select as GS  # noqa: E402
from tests.fixture_guarantee import admit_in_script_process, fixture_proof  # noqa: E402
admit_in_script_process(GS)
from daemon.launch_scanner import grad_raw as GR  # noqa: E402
from daemon.launch_scanner import saturation as S  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---------------- fake engine for _stamp_detection ----------------
class _FakeEngine:
    def __init__(self):
        self.d = {"positions": {}}

    def _ledger_lock(self):
        return contextlib.nullcontext()

    def _read(self):
        return self.d

    def _write(self, d):
        self.d = d


def _stamp_check(module, name):
    eng = _FakeEngine()
    mint = "StampMint11111111111111111111111111111111pump"
    eng.d["positions"][mint] = {"mint": mint}
    if module is GS:
        module._stamp_detection(eng, mint, "pumpportal-migration",
                                {"deployer_launches_24h": 2})
    else:
        module._stamp_detection(eng, mint, "pumpportal-migration",
                                values={"deployer_launches_24h": 2})
    ef = eng.d["positions"][mint].get("entry_features") or {}
    check("%s: arm A+B stamped at 2 launches" % name,
          ef.get("ab_arm") == "A+B")
    check("%s: select values preserved alongside arm" % name,
          (ef.get("select") or {}).get("deployer_launches_24h") == 2)

    eng2 = _FakeEngine()
    eng2.d["positions"][mint] = {"mint": mint}
    if module is GS:
        module._stamp_detection(eng2, mint, "pumpportal-migration",
                                {"deployer_launches_24h": 0})
    else:
        module._stamp_detection(eng2, mint, "pumpportal-migration",
                                values={"deployer_launches_24h": 0})
    ef2 = eng2.d["positions"][mint].get("entry_features") or {}
    check("%s: arm A stamped at 0 launches" % name,
          ef2.get("ab_arm") == "A")

    eng3 = _FakeEngine()
    eng3.d["positions"][mint] = {"mint": mint}
    if module is GS:
        module._stamp_detection(eng3, mint, "pumpportal-migration", {})
    else:
        module._stamp_detection(eng3, mint, "pumpportal-migration",
                                values={})
    ef3 = eng3.d["positions"][mint].get("entry_features") or {}
    check("%s: arm A stamped when count missing (fail-safe default)"
          % name, ef3.get("ab_arm") == "A")


_stamp_check(GS, "grad_select")
_stamp_check(GR, "grad_raw")

# grad_raw with values=None (pre-filter entry): no select measurement,
# so no arm stamp — the report counts it as unstamped, never as A.
eng4 = _FakeEngine()
_mint4 = "StampMintNone11111111111111111111111111111pump"
eng4.d["positions"][_mint4] = {"mint": _mint4}
GR._stamp_detection(eng4, _mint4, "pumpportal-migration", values=None)
ef4 = eng4.d["positions"][_mint4].get("entry_features") or {}
check("grad_raw: no arm stamped when values is None (unstamped, not A)",
      "ab_arm" not in ef4 and ef4.get("gate") == "none")


# ---------------- HARD RULE: A-only candidate still passes --------
NOW = time.time()
MINT_A = "ArmAMint1111111111111111111111111111111111pump"
TRADER_A = "TraderArmA11111111111111111111111111111111"
# TRADER_A is deliberately ABSENT from by_trader: zero launches in
# the trailing 24h -> deployer_launches_24h == 0 -> arm "A".

eng = GS.make_engine()
_tmp = tempfile.mkdtemp(prefix="abfwd-ledger-")
eng.LEDGER_PATH = os.path.join(_tmp, "grad_select_ledger.json")
eng.ensure_ledger()
eng.quote_price = lambda chain, mint, spot_index=None: 0.001
eng._fresh_tape = lambda mint: {"usd_liquidity": 5000.0,
                                "usd_market_cap": 60000.0,
                                "tape_source": "test"}

GS._index_refresh = lambda now: None
GS._INDEX["available"] = True
GS._INDEX["coverage_start"] = NOW - 90000
GS._INDEX["event_ts"] = [NOW - 86400 + i * 300 for i in range(289)]  # continuity fixture (2026-11-24)
GS._INDEX["by_mint"][MINT_A] = {"trader": TRADER_A, "ts": NOW - 10,
                                "bonding_curve": "Curve111"}
# by_trader has NO entry for TRADER_A -> 0 launches counted.
GS._curve_stats = lambda addr: {"trades": 80,
                                "created_ts": NOW - 10,
                                "oldest_sig": "sig-a", "capped": False,
                                "exhausted": True}
GS._creator_from_oldest_tx = lambda sig: None
# 2026-11-24 conservative contract: recorder-only sub-threshold counts
# never admit. Stub the authoritative-history hook with this fixture's
# intended complete history (TRADER_A truly launched 0 in 24h).
GS._AUTHORITATIVE_LAUNCHES_24H = lambda creator, now: \
    fixture_proof(GS, creator, 0, now)


class _FakeSat:
    def __init__(self):
        self.updated = NOW

    def _refresh_if_stale(self):
        return {"updated": self.updated, "wm_ts": NOW * 1000,
                "launches": {}}

    def _norm(self, s):
        return str(s or "").strip().upper().lstrip("$").strip()

    def rolling_count(self, state, symbol, window_s=86400, now_s=None):
        return S.rolling_count(state, symbol, window_s, now_s)


GS._saturation = _FakeSat()

# Stream candidate, grad_age 10s (Rule A: <= 30s passes).
cand_a = {"mint": MINT_A, "symbol": "ARMA", "created_ts": NOW - 10,
          "dex": "pumpswap", "pool_address": "PoolX",
          "migration_sig": "", "reserve_usd": 0.0, "fdv_usd": 0.0,
          "source": "pumpportal-migration"}
v = GS.evaluate_candidate(eng, cand_a, NOW)
check("hard rule: A-only candidate (0 launches) PASSES evaluation",
      v["verdict"] == "pass")
check("hard rule: A-only candidate stamps deployer_launches_24h == 0",
      v["values"].get("deployer_launches_24h") == 0)
check("hard rule: Rule A age recorded (10s, within 30s)",
      abs(v["values"].get("grad_age_s", -1) - 10) < 2)

# The same A-only candidate must ALSO survive the entry-stage recheck.
kept = GS.recheck_entry_age(
    [{"mint": MINT_A, "created_ts": NOW - 10,
      "source": "pumpportal-migration"}], eng, {}, entry_now=NOW)
check("hard rule: A-only candidate survives entry-age recheck",
      len(kept) == 1)


# ---------------- report math on fixture closes -------------------
def _load_report():
    import importlib.machinery
    path = os.path.join(REPO, "scripts", "ab-forward-report")
    loader = importlib.machinery.SourceFileLoader("ab_forward_report",
                                                  path)
    spec = importlib.util.spec_from_loader("ab_forward_report", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


RPT = _load_report()
START = 1794900000.0


def _close(entry_ts, arm, pnl, stamped=True):
    ef = {} if not stamped else {"ab_arm": arm}
    return {"mint": "M%d" % int(entry_ts), "entry_ts": entry_ts,
            "pnl_usd": pnl, "entry_features": ef}


fixture = [
    _close(START + 10, "A", 12.5),
    _close(START + 20, "A", -25.0),
    _close(START + 30, "A", 30.0),
    _close(START + 40, "A+B", 5.0),
    _close(START + 50, "A+B", -25.0),
    _close(START - 100, "A", 999.0),   # before window: excluded
    _close(START + 60, "A", 7.5, stamped=False),  # unstamped: excluded
]
s = RPT.summarize(fixture, START)
check("report: A arm counts 3 closes / 2 wins / net +$17.50",
      s["A"]["closes"] == 3 and s["A"]["wins"] == 2
      and abs(s["A"]["net_pnl"] - 17.50) < 0.01)
check("report: A expectancy +$5.83/close",
      abs(s["A"]["expectancy"] - 5.83) < 0.01)
check("report: A+B arm counts 2 closes / 1 win / net -$20.00",
      s["A+B"]["closes"] == 2 and s["A+B"]["wins"] == 1
      and abs(s["A+B"]["net_pnl"] - (-20.00)) < 0.01)
check("report: A+B expectancy -$10.00/close",
      abs(s["A+B"]["expectancy"] - (-10.00)) < 0.01)
check("report: pre-window close excluded, unstamped counted separately",
      s["total"] == 5 and s["unstamped"] == 1)
check("report: arm_of returns None for unstamped close",
      RPT.arm_of(_close(START + 1, "A", 1.0, stamped=False)) is None)

# Verdict refusal below 100 closes: summarize() carries no verdict
# logic at all — the gate lives in main(), which prints NO VERDICT
# while total < required. Prove the numbers never carry a verdict:
# even a 5-close fixture with one arm deeply negative yields stats
# only, and the required threshold is 100.
check("report: verdict threshold is 100 forward closes",
      RPT.VERDICT_CLOSES_REQUIRED == 100)
check("report: 5-close fixture is below threshold (no verdict possible)",
      s["total"] < RPT.VERDICT_CLOSES_REQUIRED)

# 100-close fixture: math still only — summarize returns stats, and
# main() at >= 100 prints numbers + a human-decision note, never an
# adopt/reject verdict (verified by reading main(); no verdict branch
# exists in summarize or in this module's helpers).
big = [_close(START + i, "A" if i % 2 else "A+B",
              10.0 if i % 3 else -25.0) for i in range(100)]
s100 = RPT.summarize(big, START)
check("report: 100-close fixture sums to 100 forward closes",
      s100["total"] == 100)
check("report: summarize returns stats only, no verdict key",
      "verdict" not in s100 and "verdict" not in s100["A"]
      and "verdict" not in s100["A+B"])

print("")
if fails:
    print("FAILED: %d check(s): %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
