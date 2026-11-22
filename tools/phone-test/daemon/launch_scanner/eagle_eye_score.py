#!/usr/bin/env python3
"""Eagle Eye momentum scoring: composite 0-100 with traceable factors.

SILENT — no signals, no Telegram. Pure computation for the data layer.

Score components (weights from research):
- Volume acceleration (0-30): RVOL m5 vs baseline + heat accelerator
- Buy pressure (0-25): buy/sell ratio, buyer wallet counts
- Price momentum (0-20): weighted multi-window, capped at extremes
- Freshness/decay (0-15): peaks at maturity 20-90 min, decays after
- Liquidity sanity (0-10): turnover, floors

Rules:
- Maturity: pools <20 min old get score 0 (no history = noise)
- Acceleration not level: +15% with 3x volume beats +400% flat
- Peak caution: heat=100% or extreme m5 move caps score, not boosts
"""
import math
import time
from typing import Dict, Tuple

# Heat bands (memecoin.watch)
HEAT_COLD = 33
HEAT_BUILDING = 48
HEAT_HOT = 99

# Thresholds
MIN_BUY_SELL_RATIO = 1.2
MIN_VOLUME_SOL = 0.5  # minimum consistent volume
MATURITY_MIN_S = 20 * 60  # don't score pools <20 min old
EXTREME_M5_PCT = 50  # cap: moves beyond this score lower (peak caution)

def heat_band(heat: float) -> str:
    if heat < HEAT_COLD:
        return "cold"
    if heat < HEAT_BUILDING:
        return "building"
    if heat <= HEAT_HOT:
        return "hot"
    return "peak"

def compute_heat(vol_1m: float, vol_5m: float) -> float:
    """Heat = (1m_vol / 5m_vol) x 100."""
    if vol_5m <= 0:
        return 0.0
    return min(100.0, (vol_1m / vol_5m) * 100)

def score_volume_accel(vol_m5: float, baseline_m5: float,
                       heat: float) -> Tuple[float, str]:
    """0-30: RVOL vs baseline + heat accelerator."""
    if vol_m5 <= 0 or baseline_m5 <= 0:
        return 0.0, "no volume data"
    rvol = vol_m5 / baseline_m5
    # Base from RVOL: 1x=0, 2x=15, 4x+=25
    base = min(25.0, max(0.0, (rvol - 1.0) * 12.5))
    # Heat accelerator: building/hot bands add up to 5
    band = heat_band(heat)
    accel = {"cold": 0, "building": 3, "hot": 5, "peak": 0}[band]
    total = min(30.0, base + accel)
    return total, f"rvol={rvol:.1f}x heat={band}"

def score_buy_pressure(buys: int, sells: int, buyers: int,
                       sellers: int) -> Tuple[float, str]:
    """0-25: buy/sell ratio + wallet counts."""
    total = buys + sells
    if total < 5:
        return 0.0, "insufficient txns"
    ratio = buys / max(sells, 1)
    if ratio < MIN_BUY_SELL_RATIO:
        return 0.0, f"b/s={ratio:.2f} below 1.2"
    # Ratio component: 1.2x=5, 2x=15, 3x+=20
    r_score = min(20.0, max(0.0, (ratio - 1.2) * 11.1 + 5))
    # Wallet bonus: more distinct buyers than sellers
    w_bonus = 0.0
    if buyers > 0 and sellers > 0:
        w_ratio = buyers / max(sellers, 1)
        w_bonus = min(5.0, max(0.0, (w_ratio - 1.0) * 5))
    total_score = min(25.0, r_score + w_bonus)
    return total_score, f"b/s={ratio:.2f}"

def score_price_momentum(chg_m5: float, chg_m15: float,
                         chg_h1: float) -> Tuple[float, str]:
    """0-20: weighted multi-window, capped at extremes."""
    # Extreme move = peak caution, score lower
    if abs(chg_m5) > EXTREME_M5_PCT:
        return 5.0, f"extreme m5={chg_m5:.0f}% (caution)"
    # Weighted: m5=0.4, m15=0.35, h1=0.25
    weighted = chg_m5 * 0.4 + chg_m15 * 0.35 + chg_h1 * 0.25
    if weighted <= 0:
        return 0.0, "no upward momentum"
    # 5% weighted = 10, 15%+ = 20
    score = min(20.0, weighted * 1.33)
    # Bonus if all windows agree (positive)
    if chg_m5 > 0 and chg_m15 > 0 and chg_h1 > 0:
        score = min(20.0, score + 2)
    return score, f"wtd={weighted:.1f}%"

def score_freshness(age_s: float) -> Tuple[float, str]:
    """0-15: peaks at maturity 20-90 min, decays after."""
    age_min = age_s / 60
    if age_min < 20:
        return 0.0, "immature (<20m)"
    if age_min <= 90:
        # Peak zone: 15
        return 15.0, "peak zone"
    if age_min <= 360:  # 6h
        # Decay: 15 -> 5
        decay = 15 - ((age_min - 90) / 270) * 10
        return max(5.0, decay), "decaying"
    return 3.0, "old"

def score_liquidity(liq_usd: float, vol_h24: float) -> Tuple[float, str]:
    """0-10: turnover sanity + floors."""
    if liq_usd < 5000:
        return 0.0, "liq below $5k floor"
    if liq_usd < 10000:
        base = 3.0
    else:
        base = 7.0
    # Turnover bonus: vol/liq ratio
    if vol_h24 > 0 and liq_usd > 0:
        turnover = vol_h24 / liq_usd
        if turnover > 2.0:
            base = min(10.0, base + 3)
    return base, f"liq=${liq_usd:,.0f}"

def score_token(data: Dict) -> Dict:
    """Composite 0-100 momentum score with traceable factors.

    data: {
        price_usd, volume_m5, volume_m15, volume_h1, volume_h24,
        baseline_m5 (trailing avg), vol_1m (for heat),
        buys_m5, sells_m5, buyers_m5, sellers_m5,
        price_chg_m5, price_chg_m15, price_chg_h1,
        age_s, liq_usd
    }
    Returns: {score, factors: {component: (score, reason)}, band, ...}
    """
    age_s = data.get("age_s", 0)

    # Maturity gate
    if age_s < MATURITY_MIN_S:
        return {
            "score": 0.0, "band": "immature",
            "factors": {"maturity": (0.0, "pool <20 min old")},
            "heat": 0.0,
        }

    heat = compute_heat(data.get("vol_1m", 0), data.get("volume_m5", 0))

    v_score, v_reason = score_volume_accel(
        data.get("volume_m5", 0), data.get("baseline_m5", 0), heat)
    b_score, b_reason = score_buy_pressure(
        data.get("buys_m5", 0), data.get("sells_m5", 0),
        data.get("buyers_m5", 0), data.get("sellers_m5", 0))
    p_score, p_reason = score_price_momentum(
        data.get("price_chg_m5", 0), data.get("price_chg_m15", 0),
        data.get("price_chg_h1", 0))
    f_score, f_reason = score_freshness(age_s)
    l_score, l_reason = score_liquidity(
        data.get("liq_usd", 0), data.get("volume_h24", 0))

    total = v_score + b_score + p_score + f_score + l_score
    total = max(0.0, min(100.0, total))

    return {
        "score": round(total, 1),
        "band": heat_band(heat),
        "heat": round(heat, 1),
        "factors": {
            "volume_accel": (round(v_score, 1), v_reason),
            "buy_pressure": (round(b_score, 1), b_reason),
            "price_momentum": (round(p_score, 1), p_reason),
            "freshness": (round(f_score, 1), f_reason),
            "liquidity": (round(l_score, 1), l_reason),
        },
        "buy_sell_ratio": round(
            data.get("buys_m5", 0) / max(data.get("sells_m5", 1), 1), 2),
    }
