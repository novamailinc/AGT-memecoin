#!/usr/bin/env python3
"""Realistic per-hour trading costs from the data itself (stdlib only).

Flat 0.2% round-trip is a simplification: spreads are wider in quiet,
illiquid hours — which can kill exactly the time windows being hunted.
This module estimates the effective spread per hour-of-day with the
Corwin-Schultz (2012) high-low spread estimator, then adds retail taker
fees to get an honest per-hour round-trip cost.

Corwin-Schultz, for consecutive periods t, t+1 with highs H and lows L:
    beta  = mean over windows of (ln(H_t/L_t))^2 + (ln(H_{t+1}/L_{t+1}))^2
    gamma = mean over windows of (ln(max(H_t,H_{t+1}) / min(L_t,L_{t+1})))^2
    alpha = (sqrt(2*beta) - sqrt(beta)) / (3 - 2*sqrt(2)) - sqrt(gamma / (3 - 2*sqrt(2)))
    S     = 2*(e^alpha - 1) / (1 + e^alpha)   (0 if alpha < 0)

For hour h we use the adjacent-hour pair (h, h+1) on each day — the spread
relevant to trading during hour h. Round-trip cost_h = S_h + 2 * taker_fee.
"""
import datetime
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_data

TAKER_FEE = 0.001  # 0.1% per side, conservative retail spot (no BNB discount)


def _cs_spread(pairs):
    """pairs: list of ((H1, L1), (H2, L2)). Returns spread as fraction of price."""
    n = len(pairs)
    if n < 30:
        return None  # too few windows for a stable estimate
    beta_sum = 0.0
    gamma_sum = 0.0
    for (h1, l1), (h2, l2) in pairs:
        if min(h1, l1, h2, l2) <= 0:
            continue
        beta_sum += math.log(h1 / l1) ** 2 + math.log(h2 / l2) ** 2
        gamma_sum += math.log(max(h1, h2) / min(l1, l2)) ** 2
    beta = beta_sum / n
    gamma = gamma_sum / n
    if beta <= 0:
        return 0.0
    k = 3.0 - 2.0 * math.sqrt(2.0)
    alpha = (math.sqrt(2.0 * beta) - math.sqrt(beta)) / k \
        - math.sqrt(gamma / k)
    if alpha <= 0:
        return 0.0
    return 2.0 * (math.exp(alpha) - 1.0) / (1.0 + math.exp(alpha))


def hourly_costs(symbol, interval="1h", fee=TAKER_FEE):
    """Return (costs[24], spreads[24], n_windows[24]) for a symbol.

    costs[h] = estimated round-trip cost of trading during hour h (UTC).
    Falls back to 2*fee for any hour with too few windows.
    """
    rows = fetch_data.load_series(symbol, interval)
    # (date, hour) -> (high, low)
    bars = {}
    for open_ms, _o, h, l, _c, _v in rows:
        dt = datetime.datetime.fromtimestamp(open_ms / 1000.0,
                                             tz=datetime.timezone.utc)
        bars[(dt.date(), dt.hour)] = (h, l)
    spreads = []
    counts = []
    for hh in range(24):
        pairs = []
        for (d, h), (h1, l1) in bars.items():
            if h != hh:
                continue
            if hh < 23:
                key2 = (d, hh + 1)
            else:
                # hour 23 pairs with hour 0 of the NEXT day
                key2 = (d + datetime.timedelta(days=1), 0)
            nxt = bars.get(key2)
            if nxt is None:
                continue
            pairs.append(((h1, l1), nxt))
        s = _cs_spread(pairs)
        spreads.append(s if s is not None else 0.0)
        counts.append(len(pairs))
    costs = [s + 2.0 * fee for s in spreads]
    return costs, spreads, counts


def cost_summary(symbol, interval="1h"):
    costs, spreads, counts = hourly_costs(symbol, interval)
    avg = sum(costs) / len(costs)
    return {
        "symbol": symbol,
        "interval": interval,
        "hourly_cost": costs,
        "hourly_spread": spreads,
        "avg_round_trip": avg,
        "min_cost": min(costs),
        "max_cost": max(costs),
        "min_hour": min(range(24), key=lambda h: costs[h]),
        "max_hour": max(range(24), key=lambda h: costs[h]),
        "flat_assumption": 2 * TAKER_FEE,
    }
