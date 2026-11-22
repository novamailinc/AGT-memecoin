#!/usr/bin/env python3
"""Nadir-v0 Phase 2: pure signal engine.

ALL functions are pure: same input -> same output, no I/O, no network.
Implements the exact Nadir-v0 rules from the blueprint.

Rules:
- Universe: Solana, pool >=30d old, >=720 hourly candles, mcap $100K-$10M,
  liquidity >=$25K, 24h volume >=$10K, 24h trades >=100
- Drawdown: 70%-90% off 30d high (inclusive)
- Falling-knife filter: no fresh 24h low, 15%+ rebound off 7d low,
  close > close 6h ago, buys >= sells, volume >= 14d median
- Safety: fail-closed (passed in, not fetched here)
"""
from typing import List, Dict, Tuple, Optional

# Frozen v0 config
CFG = {
    "min_pool_age_days": 30,
    "lookback_hours": 720,
    "drawdown_min_pct": 70.0,
    "drawdown_max_pct": 90.0,
    "min_mcap": 100_000,
    "max_mcap": 10_000_000,
    "min_liquidity": 25_000,
    "min_vol_24h": 10_000,
    "min_trades_24h": 100,
    "min_rebound_pct": 15.0,
}

Candle = Tuple[int, float, float, float, float, float]  # ts, o, h, l, c, v


def drawdown_pct(current_close: float, high_30d: float) -> Optional[float]:
    """100 * (1 - close/high). None if invalid."""
    if not high_30d or high_30d <= 0 or current_close < 0:
        return None
    return 100.0 * (1.0 - current_close / high_30d)


def in_drawdown_band(dd: Optional[float]) -> bool:
    if dd is None:
        return False
    return CFG["drawdown_min_pct"] <= dd <= CFG["drawdown_max_pct"]


def check_universe(mcap: float, liquidity: float, vol_24h: float,
                   trades_24h: int, pool_age_days: float,
                   candle_count: int) -> Tuple[bool, str]:
    """All universe gates. Returns (pass, reason)."""
    if pool_age_days < CFG["min_pool_age_days"]:
        return False, "pool too young: %.1fd" % pool_age_days
    if candle_count < CFG["lookback_hours"]:
        return False, "insufficient candles: %d" % candle_count
    if not (CFG["min_mcap"] <= mcap <= CFG["max_mcap"]):
        return False, "mcap out of range: $%.0f" % mcap
    if liquidity < CFG["min_liquidity"]:
        return False, "liquidity too low: $%.0f" % liquidity
    if vol_24h < CFG["min_vol_24h"]:
        return False, "volume too low: $%.0f" % vol_24h
    if trades_24h < CFG["min_trades_24h"]:
        return False, "too few trades: %d" % trades_24h
    return True, "ok"


def check_falling_knife(candles: List[Candle]) -> Tuple[bool, str, dict]:
    """Falling-knife filter on completed hourly candles (oldest->newest).
    Returns (pass, reason, details)."""
    details = {}
    if len(candles) < 720:
        return False, "need 720 candles, got %d" % len(candles), details

    # 1. No fresh low: lowest low in last 24h >= lowest low in prior 48h
    last24 = candles[-24:]
    prior48 = candles[-72:-24]
    low24 = min(c[3] for c in last24)
    low48 = min(c[3] for c in prior48)
    details["low_24h"] = low24
    details["low_prior48h"] = low48
    if low24 < low48:
        return False, "fresh 24h low", details

    # 2. Rebound: latest close >= 15% above 7d lowest low
    last7d = candles[-168:]
    low7d = min(c[3] for c in last7d)
    latest_close = candles[-1][4]
    details["low_7d"] = low7d
    details["latest_close"] = latest_close
    if low7d <= 0:
        return False, "invalid 7d low", details
    rebound = 100.0 * (latest_close / low7d - 1.0)
    details["rebound_pct"] = round(rebound, 2)
    if rebound < CFG["min_rebound_pct"]:
        return False, "rebound too weak: %.1f%%" % rebound, details

    # 3. Short recovery: latest close > close 6h ago
    close_6h_ago = candles[-7][4]  # 6 completed hours before latest
    details["close_6h_ago"] = close_6h_ago
    if not latest_close > close_6h_ago:
        return False, "no 6h recovery", details

    # 4. Participation: filled by caller (needs trade counts)
    # This function checks price structure; volume checks are separate
    return True, "ok", details


def check_participation(buys_24h: int, sells_24h: int,
                        vol_24h: float, median_daily_vol_14d: float
                        ) -> Tuple[bool, str]:
    if sells_24h > 0 and buys_24h < sells_24h:
        return False, "sells exceed buys: %d<%d" % (buys_24h, sells_24h)
    if vol_24h < median_daily_vol_14d:
        return False, "volume below 14d median"
    return True, "ok"


def evaluate_signal(mint: str, symbol: str,
                    candles: List[Candle],
                    mcap: float, liquidity: float,
                    vol_24h: float, trades_24h: int,
                    buys_24h: int, sells_24h: int,
                    median_vol_14d: float,
                    pool_age_days: float,
                    safety_ok: bool, safety_reason: str,
                    ) -> Dict:
    """Full Nadir-v0 signal evaluation. Pure function.
    Returns dict with signal bool and all intermediate values."""
    out = {"mint": mint, "symbol": symbol, "signal": False,
           "block_reason": "", "details": {}}

    # Universe
    ok, reason = check_universe(mcap, liquidity, vol_24h, trades_24h,
                                pool_age_days, len(candles))
    if not ok:
        out["block_reason"] = "universe: " + reason
        return out

    # Drawdown
    highs = [c[2] for c in candles[-720:]]
    high_30d = max(highs) if highs else 0
    latest_close = candles[-1][4]
    dd = drawdown_pct(latest_close, high_30d)
    out["details"]["high_30d"] = high_30d
    out["details"]["drawdown_pct"] = round(dd, 2) if dd is not None else None
    if not in_drawdown_band(dd):
        out["block_reason"] = "drawdown: %.2f%% not in [70,90]" % (
            dd if dd is not None else -1)
        return out

    # Falling knife
    ok, reason, fk = check_falling_knife(candles)
    out["details"]["falling_knife"] = fk
    if not ok:
        out["block_reason"] = "knife: " + reason
        return out

    # Participation
    ok, reason = check_participation(buys_24h, sells_24h, vol_24h,
                                     median_vol_14d)
    if not ok:
        out["block_reason"] = "participation: " + reason
        return out

    # Safety (fail-closed, decided by caller)
    if not safety_ok:
        out["block_reason"] = "safety: " + safety_reason
        return out

    out["signal"] = True
    return out
