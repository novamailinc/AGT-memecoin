#!/usr/bin/env python3
"""Block-bootstrap null distribution for the "best window" t-statistic.

Why not a plain shuffle (as in validator.py's permutation test)? A plain
shuffle destroys the autocorrelation in hourly returns, so the null it
builds is "whiter" than the real market — slightly rigging the test. The
circular block bootstrap (Politis & Romano) resamples *blocks* of 24
consecutive hourly returns (one full daily cycle), which preserves local
autocorrelation while breaking the hour-of-day -> return mapping under test.

Procedure per permutation:
  1. Cut the time-ordered gross returns into blocks of 24; reassemble from
     randomly chosen blocks (wrapping around the end of the series).
  2. Recompute every candidate's t-statistic on the shuffled series
     (hour labels stay fixed; only returns move).
  3. Record max |t| across candidates.

|t| is invariant to long/short direction, so 24 hourly + 7 weekday = 31
unique series are evaluated (equivalent to the 62 directed candidates).

Stdlib only.
"""
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

BLOCK = 24  # one full daily cycle of hourly bars


def _tstat(vals):
    n = len(vals)
    if n < 2:
        return 0.0
    mean = sum(vals) / n
    var = sum((x - mean) ** 2 for x in vals) / (n - 1)
    if var <= 0:
        return 0.0
    return mean / (math.sqrt(var) / math.sqrt(n))


def _prepare(candles, candidates):
    """Precompute per-candidate index structure.

    Returns (hour_idx, days) where hour_idx[h] = list of positions with
    that hour, and days = list of (weekday, [positions]) in date order.
    """
    hour_idx = [[] for _ in range(24)]
    for i, c in enumerate(candles):
        hour_idx[c["hour"]].append(i)
    day_map = {}
    for i, c in enumerate(candles):
        d = c["dt"].date()
        day_map.setdefault(d, []).append(i)
    days = []
    for d in sorted(day_map):
        idxs = day_map[d]
        # weekday from the first candle's date (all same day)
        wd = candles[idxs[0]]["dt"].weekday()
        days.append((wd, idxs))
    # unique (kind, key) pairs — direction irrelevant under |t|
    uniq = []
    seen = set()
    for cand in candidates:
        kk = (cand["kind"], cand["key"])
        if kk not in seen:
            seen.add(kk)
            uniq.append(kk)
    return hour_idx, days, uniq


def _max_abs_t(g, hour_idx, days, uniq):
    best = 0.0
    for kind, key in uniq:
        if kind == "hourly":
            vals = [g[i] for i in hour_idx[key]]
        else:  # daily: aggregate shuffled hourly returns into day returns
            vals = []
            for wd, idxs in days:
                if wd != key:
                    continue
                gross = 1.0
                for i in idxs:
                    gross *= (1.0 + g[i])
                vals.append(gross - 1.0)
        t = abs(_tstat(vals))
        if t > best:
            best = t
    return best


def block_null_max_t(candles, candidates, block=BLOCK, n_perm=500, seed=7):
    """Sorted null distribution of max |t| under the block bootstrap."""
    rng = random.Random(seed)
    g = [c["ret"] for c in candles]  # time-ordered gross returns
    n = len(g)
    hour_idx, days, uniq = _prepare(candles, candidates)
    n_blocks = (n + block - 1) // block
    null = []
    for _ in range(n_perm):
        shuffled = []
        for _b in range(n_blocks):
            s = rng.randrange(n)
            # manual loop is faster than slice+concat with wraparound
            for j in range(block):
                shuffled.append(g[(s + j) % n])
        shuffled = shuffled[:n]
        null.append(_max_abs_t(shuffled, hour_idx, days, uniq))
    null.sort()
    return null


def observed_best_t(candles, candidates):
    """Best |t| on the real (unshuffled) gross returns."""
    g = [c["ret"] for c in candles]
    hour_idx, days, uniq = _prepare(candles, candidates)
    return _max_abs_t(g, hour_idx, days, uniq)


def percentile(null_sorted, x):
    """Fraction of null values strictly below x."""
    return sum(1 for v in null_sorted if v < x) / len(null_sorted)
