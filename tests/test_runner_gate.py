#!/usr/bin/env python3
"""RUNNER-ONLY rule tests (2026-10-18 standing rule): every lane hunts ONLY
real running signals — liq >= $25k, mcap >= $50k, positive momentum.
Fail-closed on unknown liq/mcap/momentum."""
import os
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys

REPO = _REPO_ROOT


def load_script(name):
    path = "%s/scripts/%s" % (REPO, name)
    src = open(path).read().split('if __name__ == "__main__":')[0]
    g = {"__name__": "test", "__file__": path}
    exec(compile(src, path, "exec"), g)
    return g


df = load_script("degen-feed")
sw = load_script("smart-watcher")

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def df_pool(**kw):
    p = {"mint": "A" * 38 + "pump", "name": "T", "age_min": 5,
         "late_catch": False, "fdv": 100000.0, "reserve": 30000.0,
         "txns_m5": {"buys": 20, "sells": 5, "buyers": 12},
         "txns_m15": {"buys": 40, "sells": 10},
         "chg_m5": 5, "chg_m15": 12, "chg_h1": 25, "has_mom": True}
    p.update(kw)
    return p


def sw_info(**kw):
    i = {"mcap": 100000.0, "price": 0.001, "vol": 50000.0, "liq": 30000.0,
         "symbol": "T", "chg_m5": 3, "chg_h1": 8, "has_mom": True}
    i.update(kw)
    return i


# --- degen-feed runner gate ---
ok, why = df["passes_runner_gate"](df_pool(reserve=4000.0))
check("degen: $4k liq rejected", not ok and "25k" in why)
ok, why = df["passes_runner_gate"](df_pool(reserve=24900.0))
check("degen: $24.9k liq rejected", not ok)
ok, why = df["passes_runner_gate"](df_pool(reserve=0))
check("degen: unknown liq rejected (fail-closed)", not ok)
ok, why = df["passes_runner_gate"](df_pool(fdv=49000.0))
check("degen: $49k mcap rejected", not ok)
ok, why = df["passes_runner_gate"](df_pool(fdv=0))
check("degen: unknown mcap rejected (fail-closed)", not ok)
ok, why = df["passes_runner_gate"](
    df_pool(chg_m15=0, chg_h1=0, has_mom=True))
check("degen: flat momentum rejected", not ok and "rising" in why)
ok, why = df["passes_runner_gate"](
    df_pool(chg_m15=-5, chg_h1=-2, has_mom=True))
check("degen: negative momentum rejected", not ok)
ok, why = df["passes_runner_gate"](df_pool(has_mom=False))
check("degen: no momentum data rejected (fail-closed)", not ok)
ok, why = df["passes_runner_gate"](df_pool())
check("degen: real runner passes", ok)
ok, why = df["passes_runner_gate"](
    df_pool(reserve=25000.0, fdv=50000.0, chg_m15=0, chg_h1=1))
check("degen: boundary $25k/$50k + h1 rising passes", ok)

# --- degen-feed hard-filter integration (gate runs first) ---
knobs = {"min_fdv": 8000.0, "max_fdv": 600000.0, "min_reserve": 4000.0,
         "min_buyers_m5": 3, "min_buy_sell_ratio": 0.8}
ok, why = df["passes_hard_filters"](df_pool(reserve=5000.0), knobs)
check("degen: hard filters reject $5k-liq dust via runner gate",
      not ok and "runner" in why)
ok, why = df["passes_hard_filters"](df_pool(), knobs)
check("degen: hard filters pass real runner", ok)

# --- smart-watcher runner gate ---
ok, why = sw["passes_runner_gate"](sw_info(liq=10000.0))
check("watcher: $10k liq rejected", not ok and "25k" in why)
ok, why = sw["passes_runner_gate"](sw_info(liq=0))
check("watcher: unknown liq rejected (fail-closed)", not ok)
ok, why = sw["passes_runner_gate"](sw_info(mcap=40000.0))
check("watcher: $40k mcap rejected", not ok)
ok, why = sw["passes_runner_gate"](
    sw_info(chg_m5=0, chg_h1=0, has_mom=True))
check("watcher: flat momentum rejected", not ok and "rising" in why)
ok, why = sw["passes_runner_gate"](sw_info(has_mom=False))
check("watcher: no momentum data rejected (fail-closed)", not ok)
ok, why = sw["passes_runner_gate"](sw_info())
check("watcher: real runner passes", ok)
ok, why = sw["passes_runner_gate"](
    sw_info(mcap=600000.0, chg_m5=-3, chg_h1=-5, has_mom=True))
check("watcher: $600k established passes despite red candles", ok)
ok, why = sw["passes_runner_gate"](
    sw_info(liq=25000.0, mcap=50000.0, chg_m5=1, chg_h1=0))
check("watcher: boundary $25k/$50k + m5 rising passes", ok)
ok, why = sw["passes_runner_gate"](
    sw_info(mcap=500000.0, has_mom=False))
check("watcher: exactly $500k skips momentum need", ok)

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
