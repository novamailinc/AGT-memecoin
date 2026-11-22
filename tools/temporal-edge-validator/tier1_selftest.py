#!/usr/bin/env python3
"""Self-tests for Tier 1 modules (stdlib only, no network, no DB writes).

  deflated_sharpe : known inverse-normal values, hand-checked Sharpe,
                    DSR properties (bounds, monotonicity, trial penalty)
  costs           : Corwin-Schultz recovers a known synthetic spread;
                    zero-spread candles -> zero spread
  blockbootstrap  : finds an injected hour-14 edge; rejects pure noise;
                    deterministic under fixed seed
  walkforward     : qualifies a persistent injected edge; rejects noise

Exit non-zero on any failure.
"""
import datetime
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_data
import validator
import deflated_sharpe as ds
import costs
import blockbootstrap as bb
import walkforward as wf

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def synth_candles(n, seed=1, edge_hour=None, edge=0.0, vol=0.005,
                  start_ms=1577836800000):
    """Synthetic hourly_returns-style candle dicts."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        ms = start_ms + i * 3600_000
        dt = datetime.datetime.fromtimestamp(ms / 1000.0,
                                             tz=datetime.timezone.utc)
        r = rng.gauss(0.0, vol)
        if edge_hour is not None and dt.hour == edge_hour:
            r += edge
        out.append({"ms": ms, "dt": dt, "ret": r,
                    "hour": dt.hour, "weekday": dt.weekday()})
    return out


def synth_rows(n, seed=1, edge_hour=None, edge=0.0, vol=0.005,
               spread=0.0, start_ms=1577836800000):
    """Synthetic raw klines rows (open_ms, o, h, l, c, v)."""
    rng = random.Random(seed)
    rows = []
    px = 100.0
    for i in range(n):
        ms = start_ms + i * 3600_000
        o = px
        r = rng.gauss(0.0, vol)
        dt = datetime.datetime.fromtimestamp(ms / 1000.0,
                                             tz=datetime.timezone.utc)
        if edge_hour is not None and dt.hour == edge_hour:
            r += edge
        c = o * (1.0 + r)
        # high/low carry a known half-spread each side when spread > 0
        h = max(o, c) * (1.0 + spread / 2.0)
        l = min(o, c) * (1.0 - spread / 2.0)
        rows.append((ms, o, h, l, c, 1000.0))
        px = c
    return rows


print("== deflated_sharpe ==")
check("phi_inv(0.975)", abs(ds.phi_inv(0.975) - 1.9599639845) < 1e-6)
check("phi_inv(0.5)==0", abs(ds.phi_inv(0.5)) < 1e-9)
check("phi_inv(0.025)", abs(ds.phi_inv(0.025) + 1.9599639845) < 1e-6)
try:
    ds.phi_inv(1.5)
    check("phi_inv rejects p>1", False)
except ValueError:
    check("phi_inv rejects p>1", True)

rets = [0.01, 0.02, -0.01, 0.03]
m = sum(rets) / 4
sd = math.sqrt(sum((x - m) ** 2 for x in rets) / 3)
check("sharpe matches hand calc",
      abs(ds.sharpe_ratio(rets) - m / sd) < 1e-12)

rng = random.Random(11)
noise_trials = [[rng.gauss(0, 0.01) for _ in range(500)] for _ in range(62)]
r = ds.deflated_sharpe(noise_trials)
check("DSR in [0,1]", 0.0 <= r["dsr"] <= 1.0)
check("noise DSR does not reject", r["dsr"] < 0.95,
      f"dsr={r['dsr']:.3f}")

edge_trial = [x + 0.002 for x in noise_trials[0]]  # modest injected edge
r2 = ds.deflated_sharpe(noise_trials[:31] + [edge_trial] + noise_trials[31:],
                        best_idx=31)
check("strong edge DSR rejects null", r2["dsr"] > 0.95,
      f"dsr={r2['dsr']:.3f}")
check("best_idx honored", r2["best_idx"] == 31)

# more trials -> harsher null for the same best Sharpe (ceteris paribus:
# tile the same trials so the Sharpe variance is (almost) identical)
base = [edge_trial] + noise_trials[:9]
tiled = (base * 7)[:62]
r_few = ds.deflated_sharpe(base, best_idx=0)
r_many = ds.deflated_sharpe(tiled, best_idx=0)
check("more trials lowers DSR",
      r_many["dsr"] < r_few["dsr"] < 1.0,
      f"few={r_few['dsr']:.3f} many={r_many['dsr']:.3f}")
# hand-computed null Sharpe: sharpes=[0.5,-0.5] -> V=0.5, K=2,
# SR0 = sqrt(0.5)*(0.5772*PhiInv(1-1/(2e))) = 0.7071*0.5197 ~= 0.3675
check("null Sharpe matches hand calc",
      abs(ds.expected_sharpe_null([0.5, -0.5]) - 0.3675) < 0.003,
      f"got {ds.expected_sharpe_null([0.5, -0.5]):.4f}")

print("== costs (Corwin-Schultz) ==")
# detectable regime: spread well above hourly volatility
rows = synth_rows(3000, seed=3, spread=0.002, vol=0.0005)
pairs = [((rows[i][2], rows[i][3]), (rows[i + 1][2], rows[i + 1][3]))
         for i in range(0, len(rows) - 1, 2)]
est = costs._cs_spread(pairs)
check("recovers known 0.20% spread", est is not None and abs(est - 0.002) < 0.0006,
      f"est={est}")
rows0 = synth_rows(3000, seed=4, spread=0.0, vol=0.0005)
pairs0 = [((rows0[i][2], rows0[i][3]), (rows0[i + 1][2], rows0[i + 1][3]))
          for i in range(0, len(rows0) - 1, 2)]
check("zero spread -> zero estimate", costs._cs_spread(pairs0) == 0.0)
check("too few windows -> None", costs._cs_spread(pairs[:10]) is None)

print("== blockbootstrap ==")
candles_edge = synth_candles(2000, seed=5, edge_hour=14, edge=0.004)
cands = validator.build_candidates_hourly(candles_edge)
null = bb.block_null_max_t(candles_edge, cands, n_perm=200, seed=7)
obs = bb.observed_best_t(candles_edge, cands)
p = bb.percentile(null, obs)
check("injected edge beats block null", p > 0.95, f"pct={p:.3f}")
null2 = bb.block_null_max_t(candles_edge, cands, n_perm=200, seed=7)
check("deterministic under seed", null == null2)

candles_noise = synth_candles(2000, seed=6)
cands_n = validator.build_candidates_hourly(candles_noise)
null_n = bb.block_null_max_t(candles_noise, cands_n, n_perm=200, seed=7)
obs_n = bb.observed_best_t(candles_noise, cands_n)
p_n = bb.percentile(null_n, obs_n)
check("pure noise stays inside null", p_n < 0.95, f"pct={p_n:.3f}")

print("== walkforward ==")
# monkeypatch the DB loader with synthetic rows
real_load = fetch_data.load_series
try:
    # injected edge must clear the Bonferroni t-bar (~3.35): with 125
    # train trades/fold, edge 0.006 - cost 0.002 = 0.004 net, t ~= 8.9
    edge_rows = synth_rows(6000, seed=8, edge_hour=9, edge=0.006)
    fetch_data.load_series = lambda s, i="1h": edge_rows
    res = wf.walkforward("BTCUSDT")
    check("5 folds built", res["n_folds"] == 5, f"got {res['n_folds']}")
    q = [c["candidate"] for c in res["qualified"]]
    check("persistent edge qualifies", "hour_09_long" in q, f"qualified={q}")
    h9 = next(c for c in res["candidates"] if c["candidate"] == "hour_09_long")
    check("edge discovered in most folds", h9["folds_discovered"] >= 3,
          f"{h9['folds_discovered']}/5")

    noise_rows = synth_rows(6000, seed=9)
    fetch_data.load_series = lambda s, i="1h": noise_rows
    res_n = wf.walkforward("BTCUSDT")
    check("noise qualifies nothing", len(res_n["qualified"]) == 0,
          f"qualified={[c['candidate'] for c in res_n['qualified']]}")

    # regression: _apply_beta must return a list, never None
    cs = synth_candles(100, seed=12)
    byms = {c["ms"]: 0.001 for c in cs}
    applied = wf._apply_beta(cs, byms, 0.5)
    check("_apply_beta returns list", isinstance(applied, list)
          and len(applied) == 100)
    check("_apply_beta math", abs(applied[0]["ret"] - (cs[0]["ret"] - 0.0005)) < 1e-12)
finally:
    fetch_data.load_series = real_load

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES: {FAILURES}")
    sys.exit(1)
print("all tier-1 self-tests passed")
