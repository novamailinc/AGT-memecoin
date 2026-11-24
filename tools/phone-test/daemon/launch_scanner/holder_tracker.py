#!/usr/bin/env python3
"""Holder acceleration tracker for launch lanes.

Research (2026-10-15): holder growth ~64% stronger predictor than trader
count; accelerating holders = organic demand, flat holders + rising txns
= bump-bot wash. This module polls holder counts and computes velocity
(holders/min) and acceleration (Δ velocity).

Free sources (in order): Helius getTokenLargestAccounts (count), fallback
to DexScreener. Stores 60s-interval history per mint, pruned to 30 min.
"""
import json
import os
import time
import urllib.request

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
HISTORY_PATH = os.path.join(HOME, "holder_history.json")
# Max age of history per mint: 30 min (launch window relevance)
MAX_AGE_SEC = 30 * 60


def _helius_key():
    for p in ("helius/api_key_discovery", "helius/api_key"):
        try:
            with open(os.path.join(HOME, p)) as f:
                k = f.read().strip()
                if k:
                    return k
        except Exception:
            continue
    return ""


def get_holder_count(mint):
    """Current holder count for a mint. 0 on any failure."""
    # Helius: getTokenSupply doesn't give holders; use token accounts.
    # Simplest reliable: DexScreener holders field when present.
    try:
        url = "https://api.dexscreener.com/tokens/v1/solana/%s" % mint
        req = urllib.request.Request(url, headers={
            "User-Agent": "forex-agent/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
        p = d[0] if isinstance(d, list) and d else {}
        # DexScreener nests holders under info or top-level
        h = p.get("holders")
        if isinstance(h, dict):
            return int(h.get("count") or 0)
        if h:
            return int(h)
    except Exception:
        pass
    return 0


def _load():
    try:
        with open(HISTORY_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(hist):
    try:
        now = time.time()
        # prune old entries
        for mint in list(hist.keys()):
            hist[mint] = [(ts, h) for ts, h in hist[mint]
                          if now - ts < MAX_AGE_SEC]
            if not hist[mint]:
                del hist[mint]
        tmp = HISTORY_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(hist, f)
        os.replace(tmp, HISTORY_PATH)
    except Exception:
        pass


def record(mint, count=None, ts=None):
    """Record a holder-count observation. Returns (velocity, acceleration)."""
    ts = ts or time.time()
    if count is None:
        count = get_holder_count(mint)
    hist = _load()
    obs = hist.get(mint, [])
    obs.append([ts, count])
    hist[mint] = obs
    _save(hist)
    return analyze(mint)


def analyze(mint, now=None):
    """(holders_per_min, acceleration) from history.

    Velocity: holders/min over the last 5 min (or available window).
    Acceleration: change in velocity vs the prior 5-min window.
    Returns (0, 0) when insufficient data.
    """
    now = now or time.time()
    hist = _load().get(mint, [])
    if len(hist) < 3:
        return 0.0, 0.0
    # Sort by ts (defensive)
    hist = sorted(hist, key=lambda x: x[0])
    # Recent window: last 5 min
    recent = [(ts, h) for ts, h in hist if now - ts <= 300]
    prior = [(ts, h) for ts, h in hist
             if 300 < now - ts <= 600]
    def _vel(win):
        if len(win) < 2:
            return 0.0
        dt_min = (win[-1][0] - win[0][0]) / 60.0
        if dt_min <= 0:
            return 0.0
        return (win[-1][1] - win[0][1]) / dt_min
    v_recent = _vel(recent)
    v_prior = _vel(prior)
    accel = v_recent - v_prior if prior else 0.0
    return round(v_recent, 2), round(accel, 2)


def wash_detected(mint, tx_count_5m, now=None):
    """Bump-bot wash signature: rising txns but flat holders.

    Returns True if tx_count_5m is high (>50) but holder velocity
    is ~flat (<1/min). This is the MELT wash pattern.
    """
    vel, _ = analyze(mint, now)
    return tx_count_5m > 50 and vel < 1.0
