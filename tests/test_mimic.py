"""Mimic lane — launch filter, candidate conversion, engine policy,
killswitch registration, roster/watchdog wiring. Plain script: run
with the repo venv python (never pytest). Uses a temp
FOREX_AGENT_HOME so no live ledger, killswitch state, or verdict
chain is ever touched."""

import importlib.util
import os
import sys
import tempfile
import time

os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="mimic-test-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
_LS = os.path.join(_ROOT, "daemon", "launch_scanner")
sys.path.insert(0, _LS)
sys.path.insert(0, _ROOT)

_PATH = os.path.join(_LS, "mimic.py")
_spec = importlib.util.spec_from_loader("mimic_mod", loader=None)
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
check("mimic: lane is mimic", eng.LANE == "mimic")
check("mimic: own ledger file",
      str(eng.LEDGER_PATH).endswith("mimic_ledger.json"))
check("mimic: stake $25", eng.NOTIONAL_USD == 25.00)
check("mimic: max open 20", eng.MAX_OPEN == 20)
check("mimic: bankroll $500", eng.BANKROLL_START == 500.00)
check("mimic: launch window 120s", eng.MAX_GRAD_AGE_S == 120.0)
check("mimic: chase guard 120s", eng.MAX_SLOT_WAIT_S == 120.0)
check("mimic: verdict sample 100", eng.VERDICT_SAMPLE_CLOSES == 100)
check("mimic: standard ladder (50%@2x / 25%@3x / 25% trail 30%)",
      (eng.LEG_A_FRAC, eng.LEG_A_TARGET) == (0.50, 2.0)
      and (eng.LEG_B_FRAC, eng.LEG_B_TARGET) == (0.25, 3.0)
      and (eng.LEG_C_FRAC, eng.LEG_C_TRAIL) == (0.25, 0.30))
check("mimic: standard hard stop -50%",
      eng.HARD_STOP_MULT == 0.50 and not eng.PROTECT_AFTER_A)
check("mimic: no safety gate (passes)",
      eng._safety_ok("AnyMintpump")[0] is True)
check("mimic: no veto (passes)",
      eng._veto_ok("AnyMintpump", "X", None)[0] is True)
check("mimic: engine is not grad_raw",
      eng.LANE != "grad_raw"
      and str(eng.LEDGER_PATH) != os.path.join(
          os.environ["FOREX_AGENT_HOME"], "grad_raw_ledger.json"))

# ---- preregistration terms ---------------------------------------------
pre = eng._preregistration()
check("mimic: prereg lane mimic", pre.get("lane") == "mimic")
check("mimic: prereg sample 100", pre.get("sample_closes") == 100)
check("mimic: prereg rule names killswitch mimic",
      "mimic" in str(pre.get("rule") or ""))
check("mimic: prereg terms name live-quote entry (not creation price)",
      "live quote" in str((pre.get("terms") or {}).get("entry_price", "")))
check("mimic: prereg terms say no cards in v1",
      "NONE" in str((pre.get("terms") or {}).get("cards", "")))

# ---- launch filter ------------------------------------------------------
now = time.time()
fresh = {"mint": "AbC123pump", "created_ts": now - 10}
stale = {"mint": "AbC123pump", "created_ts": now - 300}
nonpump = {"mint": "AbC123", "created_ts": now - 10}
nocreated = {"mint": "AbC123pump"}
check("mimic: launch filter accepts fresh pump mint",
      eng.is_graduation_candidate(fresh, now) is not None)
check("mimic: launch filter rejects stale launch",
      eng.is_graduation_candidate(stale, now) is None)
check("mimic: launch filter rejects non-pump mint",
      eng.is_graduation_candidate(nonpump, now) is None)
check("mimic: launch filter rejects missing timestamp",
      eng.is_graduation_candidate(nocreated, now) is None)

# ---- candidate conversion ------------------------------------------------
item = {"source": "pumpportal-new", "mint": "AbC123pump",
        "symbol": "TEST", "created_ts": now,
        "extra": {"bonding_curve": "CurveKey123"}}
row = M.new_to_candidate(item, now)
check("mimic: new-token converts to candidate",
      row is not None and row["mint"] == "AbC123pump")
check("mimic: candidate dex is pump curve",
      row is not None and row["dex"] == "pump")
check("mimic: candidate pool is the bonding curve key",
      row is not None and row["pool_address"] == "CurveKey123")
check("mimic: migration item rejected",
      M.new_to_candidate({"source": "pumpportal-migration",
                          "mint": "AbC123pump",
                          "created_ts": now,
                          "extra": {}}, now) is None)
check("mimic: non-pump creation rejected",
      M.new_to_candidate({"source": "pumpportal-new",
                          "mint": "AbC123",
                          "created_ts": now,
                          "extra": {}}, now) is None)
check("mimic: garbage item rejected",
      M.new_to_candidate(None, now) is None
      and M.new_to_candidate({}, now) is None)

# ---- wiring (file text, no side effects) ----------------------------------
with open(os.path.join(_LS, "killswitch.py")) as f:
    ks_text = f.read()
check("mimic: killswitch CFG has mimic {100,100}",
      '"mimic":' in ks_text and "min_trades" in ks_text)
with open(os.path.join(_ROOT, "scripts", "supervisor")) as f:
    sup_text = f.read()
check("mimic: supervisor roster has mimic-trader",
      '"mimic-trader"' in sup_text)
with open(os.path.join(_ROOT, "scripts", "forex-watchdog")) as f:
    wd_text = f.read()
check("mimic: watchdog guards mimic-trader",
      "scripts/mimic-trader" in wd_text)
check("mimic: trader script exists",
      os.path.exists(os.path.join(_ROOT, "scripts", "mimic-trader")))

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL MIMIC TESTS PASSED")
