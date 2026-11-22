"""Mimic-edge lane — TWAP exit, -25% stop, 60min hold, launch
filter, engine policy, killswitch registration, roster/watchdog
wiring. Plain script: run with the repo venv python (never
pytest). Uses a temp FOREX_AGENT_HOME so no live ledger,
killswitch state, or verdict chain is ever touched."""

import importlib.util
import os
import sys
import tempfile
import time

os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="mimic-edge-test-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
_LS = os.path.join(_ROOT, "daemon", "launch_scanner")
sys.path.insert(0, _LS)
sys.path.insert(0, _ROOT)

_PATH = os.path.join(_LS, "mimic_edge.py")
_spec = importlib.util.spec_from_loader("mimic_edge_mod", loader=None)
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
check("edge: lane is mimic_edge", eng.LANE == "mimic_edge")
check("edge: own ledger file",
      str(eng.LEDGER_PATH).endswith("mimic_edge_ledger.json"))
check("edge: stake $25", eng.NOTIONAL_USD == 25.00)
check("edge: max open 20", eng.MAX_OPEN == 20)
check("edge: bankroll $500", eng.BANKROLL_START == 500.00)
check("edge: launch window 120s", eng.MAX_GRAD_AGE_S == 120.0)
check("edge: chase guard 120s", eng.MAX_SLOT_WAIT_S == 120.0)
check("edge: verdict sample 100", eng.VERDICT_SAMPLE_CLOSES == 100)
check("edge: tightened stop -25%", eng.HARD_STOP_MULT == 0.75)
check("edge: TWAP 64 slices", eng.TWAP_SLICES == 64)
check("edge: TWAP max hold 3600s", eng.TWAP_MAX_HOLD_S == 3600.0)
check("edge: no safety gate (passes)",
      eng._safety_ok("AnyMintpump")[0] is True)
check("edge: no veto (passes)",
      eng._veto_ok("AnyMintpump", "X", None)[0] is True)
check("edge: engine is not mimic v1",
      eng.LANE != "mimic"
      and str(eng.LEDGER_PATH) != os.path.join(
          os.environ["FOREX_AGENT_HOME"], "mimic_ledger.json"))

# ---- preregistration terms ---------------------------------------------
pre = eng._preregistration()
check("edge: prereg lane mimic_edge", pre.get("lane") == "mimic_edge")
check("edge: prereg sample 100", pre.get("sample_closes") == 100)
check("edge: prereg rule names killswitch mimic_edge",
      "mimic_edge" in str(pre.get("rule") or ""))
check("edge: prereg terms name TWAP exit",
      "TWAP" in str((pre.get("terms") or {}).get("exit", "")))
check("edge: prereg terms name copycat filter",
      "copycat" in str((pre.get("terms") or {}).get("filters", "")))
check("edge: prereg terms say no cards in v1",
      "NONE" in str((pre.get("terms") or {}).get("cards", "")))

# ---- launch filter ------------------------------------------------------
now = time.time()
fresh = {"mint": "AbC123pump", "created_ts": now - 10}
stale = {"mint": "AbC123pump", "created_ts": now - 300}
nonpump = {"mint": "AbC123", "created_ts": now - 10}
nocreated = {"mint": "AbC123pump"}
check("edge: launch filter accepts fresh pump mint",
      eng.is_graduation_candidate(fresh, now) is not None)
check("edge: launch filter rejects stale launch",
      eng.is_graduation_candidate(stale, now) is None)
check("edge: launch filter rejects non-pump mint",
      eng.is_graduation_candidate(nonpump, now) is None)
check("edge: launch filter rejects missing timestamp",
      eng.is_graduation_candidate(nocreated, now) is None)

# ---- candidate conversion ------------------------------------------------
item = {"source": "pumpportal-new", "mint": "AbC123pump",
        "symbol": "TEST", "created_ts": now,
        "extra": {"bonding_curve": "CurveKey123",
                  "trader": "TraderKey123"}}
row = M.new_to_candidate(item, now)
check("edge: new-token converts to candidate",
      row is not None and row["mint"] == "AbC123pump")
check("edge: candidate carries trader for deployer filter",
      row is not None and row.get("trader") == "TraderKey123")
check("edge: candidate dex is pump curve",
      row is not None and row["dex"] == "pump")
check("edge: migration item rejected",
      M.new_to_candidate({"source": "pumpportal-migration",
                          "mint": "AbC123pump",
                          "created_ts": now,
                          "extra": {}}, now) is None)

# ---- evidence filter: pure decision --------------------------------------
j = {"tickers": {"AAA": [now - 10, now - 20]},
     "deployers": {"D1": [now - 10]}}
check("edge: filter passes 2nd same-ticker launch",
      M._filter_decision(j, "AAA", "D9", now)[0] is True)
j2 = {"tickers": {"AAA": [now - 10, now - 20, now - 30]},
      "deployers": {}}
check("edge: filter rejects 3rd same-ticker launch",
      M._filter_decision(j2, "AAA", "D9", now) ==
      (False, "copycat ticker"))
j3 = {"tickers": {},
      "deployers": {"D1": [now - 10, now - 20, now - 30]}}
check("edge: filter rejects 3rd same-deployer launch",
      M._filter_decision(j3, "ZZZ", "D1", now) ==
      (False, "serial deployer"))
j4 = {"tickers": {},
      "deployers": {"D1": [now - 90000, now - 91000, now - 92000]}}
check("edge: filter ignores timestamps older than 24h",
      M._filter_decision(j4, "ZZZ", "D1", now)[0] is True)
check("edge: filter passes when trader missing (ticker still gates)",
      M._filter_decision({"tickers": {}}, "NEW", "", now)[0] is True)
check("edge: filter rejects copycat even with trader missing",
      M._filter_decision(j2, "AAA", "", now)[0] is False)

# ---- evidence filter: journal recording ----------------------------------
jj = {"tickers": {}, "deployers": {}}
M._record_launch(jj, "AAA", "D1", now)
M._record_launch(jj, "AAA", "D1", now)
check("edge: journal counts 2 same-ticker launches, passes",
      M._filter_decision(jj, "AAA", "D1", now)[0] is True)
M._record_launch(jj, "AAA", "D1", now)
check("edge: journal counts 3rd launch, rejects",
      M._filter_decision(jj, "AAA", "D1", now)[0] is False)

# ---- evidence filter: consider wrapper ------------------------------------
class _FakeLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeEngine:
    def __init__(self):
        self.written = None
        self.base_calls = []

    def _ledger_lock(self):
        return _FakeLock()

    def _read(self):
        return {}

    def _write(self, d):
        self.written = d

    def _base_consider_candidate(self, cand, now=None):
        self.base_calls.append(cand.get("mint"))
        return True, "base-ok", False


M.JOURNAL = {"tickers": {"AAA": [now - 10, now - 20, now - 30]},
             "deployers": {}}
fe = _FakeEngine()
res = M._filtered_consider(
    fe, {"mint": "M1pump", "symbol": "AAA", "trader": "D9"}, now)
check("edge: wrapper rejects copycat, final, base not called",
      res == (False, "filter: copycat ticker", True)
      and fe.base_calls == [])
check("edge: wrapper counts copycat rejection in ledger stats",
      fe.written is not None
      and (fe.written.get("filter_stats") or {}).get(
          "rejected_copycat") == 1)
M.JOURNAL = {"tickers": {}, "deployers": {}}
fe2 = _FakeEngine()
res2 = M._filtered_consider(
    fe2, {"mint": "M2pump", "symbol": "NEW", "trader": "D9"}, now)
check("edge: wrapper passes clean launch to base consider",
      res2 == (True, "base-ok", False)
      and fe2.base_calls == ["M2pump"])

# ---- TWAP exit: engine behavior -------------------------------------------
# Small slice count for speed; the production value (64) is
# asserted above on the fresh engine.
eng.TWAP_SLICES = 4
eng.TWAP_MAX_HOLD_S = 3600.0
eng.HARD_STOP_MULT = 0.75
eng.ensure_ledger()


def seed_pos(mint, entry_ts, entry_price=1.0, tokens=25.0):
    pos = {"mint": mint, "symbol": "TST", "chain": "solana",
           "source": "launch-mimic-edge", "lane": "mimic_edge",
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


def still_open(mint):
    with eng._ledger_lock():
        d = eng._read()
    return mint in (d.get("positions") or {})


# 1. TWAP completes in exactly N polls at a flat price.
seed_pos("Twap1pump", now)
for _ in range(3):
    eng.poll_exits(lambda c, m, s=None: 1.0)
check("edge: TWAP still open after 3 of 4 slices",
      still_open("Twap1pump"))
eng.poll_exits(lambda c, m, s=None: 1.0)
c1 = close_for("Twap1pump")
check("edge: TWAP finalizes on 4th slice (twap_complete)",
      c1 is not None and c1.get("closed_reason") == "twap_complete")
check("edge: TWAP close carries slice journal, no ladder legs",
      c1 is not None and c1.get("legs") == {}
      and (c1.get("twap") or {}).get("done") == 4)
check("edge: TWAP sold all tokens exactly once",
      c1 is not None
      and abs(float((c1.get("twap") or {}).get("sold_tokens") or 0)
              - 25.0) < 1e-6)
check("edge: TWAP flat-price close banks near notional (fees only)",
      c1 is not None and 20.0 < float(c1.get("realized_usd") or 0)
      <= 25.0)

# 2. Hard stop mid-TWAP exits ONLY the remainder (no double-sell).
seed_pos("Twap2pump", now)
eng.poll_exits(lambda c, m, s=None: 1.0)  # slice 1 of 4 (250 tokens)
eng.poll_exits(lambda c, m, s=None: 0.5)  # 0.5x < 0.75 stop
c2 = close_for("Twap2pump")
tw2 = (c2 or {}).get("twap") or {}
check("edge: TWAP stop close reason is hard_stop_-25pct",
      c2 is not None and c2.get("closed_reason") == "hard_stop_-25pct")
check("edge: TWAP stop accounts every token exactly once",
      c2 is not None
      and abs(float(tw2.get("sold_tokens") or 0)
              + float((tw2.get("remainder") or {}).get("tokens") or 0)
              - 25.0) < 1e-6)
check("edge: TWAP stop close is a loss (slice + remainder < stake)",
      c2 is not None and float(c2.get("realized_usd") or 0) < 25.0)

# 3. TWAP max-hold backstop fires at 60min, not 24h.
seed_pos("Twap3pump", now - 3700)
eng.poll_exits(lambda c, m, s=None: 1.0)
c3 = close_for("Twap3pump")
check("edge: TWAP time stop fires at 60min (twap_time_stop_60min)",
      c3 is not None
      and c3.get("closed_reason") == "twap_time_stop_60min")
check("edge: TWAP time stop exits full remainder, no slices",
      c3 is not None
      and (c3.get("twap") or {}).get("done") == 0
      and abs(float(((c3.get("twap") or {}).get("remainder") or {})
                     .get("tokens") or 0) - 25.0) < 1e-6)

# 4. Ladder unchanged when TWAP flags are off (default-off proof).
eng2 = M.make_engine()
eng2.TWAP_SLICES = None
eng2.TWAP_MAX_HOLD_S = None
eng2.HARD_STOP_MULT = 0.50
eng2.ensure_ledger()
pos = {"mint": "Lad1pump", "symbol": "TST", "chain": "solana",
       "source": "launch-mimic-edge", "lane": "mimic_edge",
       "entry_ts": now, "quoted_price": 1.0,
       "entry_price": 1.0, "notional_usd": 25.0,
       "peak_price": 1.0, "peak_ts": now,
       "tokens": 1000.0, "gas_in_usd": 0.0,
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
check("edge: ladder flags-off banks leg A at 2x, stays open",
      lp is not None and (lp.get("legs") or {}).get("a", {}).get(
          "done") is True and "twap" not in lp)
with open(os.path.join(_LS, "grad.py")) as f:
    grad_text = f.read()
check("edge: grad.py TWAP defaults are off (None)",
      "TWAP_SLICES = None" in grad_text
      and "TWAP_MAX_HOLD_S = None" in grad_text)

# ---- wiring (file text, no side effects) ----------------------------------
with open(os.path.join(_LS, "killswitch.py")) as f:
    ks_text = f.read()
check("edge: killswitch CFG has mimic_edge {100,100}",
      '"mimic_edge":' in ks_text and "min_trades" in ks_text)
with open(os.path.join(_ROOT, "scripts", "supervisor")) as f:
    sup_text = f.read()
check("edge: supervisor roster has mimic-edge-trader",
      '"mimic-edge-trader"' in sup_text)
with open(os.path.join(_ROOT, "scripts", "forex-watchdog")) as f:
    wd_text = f.read()
check("edge: watchdog guards mimic-edge-trader",
      "scripts/mimic-edge-trader" in wd_text)
check("edge: trader script exists",
      os.path.exists(os.path.join(_ROOT, "scripts",
                                  "mimic-edge-trader")))

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL MIMIC_EDGE TESTS PASSED")
