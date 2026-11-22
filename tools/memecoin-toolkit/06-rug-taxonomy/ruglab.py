"""Rug-pattern detectors over synthetic token lifecycle fixtures.

A lifecycle is a list of snapshot dicts, oldest first:

    {"ts": int, "price": float, "liquidity_usd": float,
     "holders": int, "supply": float, "mint_authority_active": bool,
     "sell_sim_ok": bool, "buy_vol": float, "sell_vol": float,
     "unique_buyers": int, "trades": int, "top_holder_pct": float}

Every detector returns ``(triggered: bool, evidence: str)`` -- the
evidence string always names the numbers that fired (or why nothing
could be judged). Pure stdlib, no network, fixtures only. Missing
fields never trigger a detector: absence of data is stated in the
evidence, not silently treated as guilt or innocence.
"""

from __future__ import annotations

import math

LP_DROP_PCT = 80.0            # liquidity collapse for lp_pull
LP_PRICE_DROP_PCT = 50.0      # accompanying price collapse
LP_WINDOW_SEC = 3600          # "short window": one hour
LP_WINDOW_STEPS = 3           # fallback window when ts is absent
MINT_INFLATION_PCT = 20.0     # supply growth for mint_and_dump
MINT_PRICE_DROP_PCT = 50.0
HOLDER_CRASH_PCT = 40.0
WASH_VOLUME_THRESHOLD = 10_000.0   # buy_vol + sell_vol, USD
WASH_UNIQUE_RATIO = 0.1            # unique_buyers / trades, strictly below
WASH_HOLDER_CHANGE_PCT = 2.0       # holders move strictly below this


def _num(snapshot: dict, key: str):
    try:
        value = snapshot.get(key)
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError, AttributeError):
        return None
    # NaN / infinity in a snapshot field is corrupt data, not a
    # measurement: treat it exactly like a missing field.
    return number if math.isfinite(number) else None


def _within_window(a: dict, b: dict, step_gap: int) -> bool:
    ta, tb = _num(a, "ts"), _num(b, "ts")
    if ta is not None and tb is not None:
        return 0 <= tb - ta <= LP_WINDOW_SEC
    return step_gap <= LP_WINDOW_STEPS


def lp_pull(lifecycle) -> tuple[bool, str]:
    """Liquidity drops >=80% in a short window while price collapses.

    Signature: lp_pull(lifecycle: list[dict]) -> (bool, str)
    A pair (i, j) fires when j is within LP_WINDOW_SEC of i (or <=3
    steps when timestamps are absent), liquidity falls by >=80%
    from i to j, and price falls by >=50% over the same pair.
    Fail-closed: fewer than two usable snapshots -> (False, reason).
    """
    snaps = lifecycle or []
    for i, start in enumerate(snaps):
        liq_i, price_i = _num(start, "liquidity_usd"), _num(start, "price")
        if not liq_i or not price_i:
            continue
        for j in range(i + 1, len(snaps)):
            if not _within_window(start, snaps[j], j - i):
                continue
            liq_j, price_j = _num(snaps[j], "liquidity_usd"), _num(snaps[j], "price")
            if liq_j is None or price_j is None:
                continue
            liq_drop = (liq_i - liq_j) / liq_i * 100.0
            price_drop = (price_i - price_j) / price_i * 100.0
            if liq_drop >= LP_DROP_PCT and price_drop >= LP_PRICE_DROP_PCT:
                return True, (f"liquidity -{liq_drop:.1f}% "
                              f"(${liq_i:,.0f} -> ${liq_j:,.0f}) and price "
                              f"-{price_drop:.1f}% within short window "
                              f"(snapshots {i}->{j})")
    return False, "no >=80% liquidity drop with >=50% price collapse in window"


def mint_and_dump(lifecycle) -> tuple[bool, str]:
    """Mint authority active + supply inflation >20%, then price -50%+.

    Signature: mint_and_dump(lifecycle) -> (bool, str)
    Fires when some snapshot has mint_authority_active, a later
    snapshot's supply is >20% above the earlier one, and price at or
    after the inflation snapshot has fallen >50% from the earlier
    snapshot's price. Missing supply data -> never fires.
    """
    snaps = lifecycle or []
    for i, start in enumerate(snaps):
        supply_i, price_i = _num(start, "supply"), _num(start, "price")
        if not supply_i or not price_i:
            continue
        for j in range(i + 1, len(snaps)):
            supply_j = _num(snaps[j], "supply")
            if supply_j is None:
                continue
            inflation = (supply_j - supply_i) / supply_i * 100.0
            mint_active = bool(start.get("mint_authority_active")
                               or snaps[j].get("mint_authority_active"))
            if inflation <= MINT_INFLATION_PCT or not mint_active:
                continue
            for k in range(j, len(snaps)):
                price_k = _num(snaps[k], "price")
                if price_k is None:
                    continue
                drop = (price_i - price_k) / price_i * 100.0
                if drop > MINT_PRICE_DROP_PCT:
                    return True, (f"mint authority active, supply "
                                  f"+{inflation:.1f}% (snapshots {i}->{j}), "
                                  f"then price -{drop:.1f}% by snapshot {k}")
    return False, "no active-mint supply inflation >20% followed by price drop >50%"


def cant_sell(lifecycle) -> tuple[bool, str]:
    """A sell simulation failed at any snapshot.

    Signature: cant_sell(lifecycle) -> (bool, str)
    Only an explicit ``sell_sim_ok is False`` fires. A missing field
    is unknown, not a failure, and never fires this detector.
    """
    for idx, snap in enumerate(lifecycle or []):
        if snap.get("sell_sim_ok") is False:
            return True, f"sell_sim_ok=False at snapshot {idx} (ts={snap.get('ts')})"
    return False, "sell simulation ok (or unreported) at every snapshot"


def wash_skew(lifecycle) -> tuple[bool, str]:
    """High volume, tiny unique-buyer share, flat holder count.

    Signature: wash_skew(lifecycle) -> (bool, str)
    Concrete rule -- any snapshot fires when ALL of these hold:
    buy_vol + sell_vol > $10,000, unique_buyers / trades < 0.1, and
    the holder count has moved <2% versus the first snapshot.
    Snapshots missing any field are skipped, never assumed dirty.
    """
    snaps = lifecycle or []
    if not snaps:
        return False, "no snapshots"
    holders_0 = _num(snaps[0], "holders")
    for idx, snap in enumerate(snaps):
        buy, sell = _num(snap, "buy_vol"), _num(snap, "sell_vol")
        unique, trades = _num(snap, "unique_buyers"), _num(snap, "trades")
        holders = _num(snap, "holders")
        if None in (buy, sell, unique, trades, holders) or not holders_0:
            continue
        volume = buy + sell
        if trades <= 0 or volume <= WASH_VOLUME_THRESHOLD:
            continue
        ratio = unique / trades
        holder_change = abs(holders - holders_0) / holders_0 * 100.0
        if ratio < WASH_UNIQUE_RATIO and holder_change < WASH_HOLDER_CHANGE_PCT:
            return True, (f"snapshot {idx}: volume ${volume:,.0f}, "
                          f"unique/trades {unique:.0f}/{trades:.0f}={ratio:.3f} "
                          f"(<0.1), holders moved {holder_change:.2f}% (<2%)")
    return False, "no snapshot with high volume, <0.1 unique ratio and flat holders"


def holder_crash(lifecycle) -> tuple[bool, str]:
    """Holder count drops >=40% between consecutive snapshots.

    Signature: holder_crash(lifecycle) -> (bool, str)
    Consecutive pairs only -- a slow bleed over many snapshots does
    not fire this; that distinction is the point of the detector.
    """
    snaps = lifecycle or []
    for i in range(len(snaps) - 1):
        before = _num(snaps[i], "holders")
        after = _num(snaps[i + 1], "holders")
        if not before or after is None:
            continue
        drop = (before - after) / before * 100.0
        if drop >= HOLDER_CRASH_PCT:
            return True, (f"holders -{drop:.1f}% between consecutive "
                          f"snapshots {i}->{i + 1} ({before:.0f} -> {after:.0f})")
    return False, "no consecutive holder drop >=40%"


DETECTORS = {
    "lp_pull": lp_pull,
    "mint_and_dump": mint_and_dump,
    "cant_sell": cant_sell,
    "wash_skew": wash_skew,
    "holder_crash": holder_crash,
}


def run_detectors(lifecycle) -> dict:
    """Run every detector. Signature: run_detectors(lifecycle) -> dict

    Returns {name: (triggered, evidence), ...} for all five
    detectors, always -- an empty lifecycle yields five
    (False, reason) entries rather than an exception.
    """
    return {name: fn(lifecycle) for name, fn in DETECTORS.items()}


def triggered_names(lifecycle) -> list:
    """Convenience: sorted names of detectors that fired."""
    return sorted(name for name, (hit, _) in run_detectors(lifecycle).items() if hit)


# Combined scoring: the detectors stay independent and binary; this
# layer only *weighs* how many fired at once. Weights reflect how
# directly each pattern destroys holder value on its own: an LP pull
# or a mint-and-dump is close to dispositive, wash skew alone is
# circumstantial.
DETECTOR_WEIGHTS = {
    "lp_pull": 35,
    "mint_and_dump": 30,
    "cant_sell": 25,
    "holder_crash": 20,
    "wash_skew": 15,
}


def rug_score(lifecycle) -> int:
    """Weighted 0-100 score from whichever detectors fired.

    Signature: rug_score(lifecycle) -> int
    Sum of DETECTOR_WEIGHTS over triggered detectors, capped at
    100 (all five firing sums to 125 raw). A clean lifecycle scores
    exactly 0 -- the score never invents risk from missing data,
    because no detector fires on missing data.
    """
    fired = triggered_names(lifecycle)
    return min(100, sum(DETECTOR_WEIGHTS[name] for name in fired))


def risk_grade(score) -> str:
    """Band a rug score: 0 clean, 1-24 watch, 25-49 elevated, 50+ severe.

    Signature: risk_grade(score) -> str
    Raises ValueError unless ``score`` is a number in [0, 100] --
    a grade computed from an out-of-range score would be a lie in
    either direction.
    """
    try:
        value = float(score)
    except (TypeError, ValueError):
        raise ValueError(f"score must be a number in [0, 100], got {score!r}")
    if not math.isfinite(value) or not (0 <= value <= 100):
        raise ValueError(f"score must be in [0, 100], got {score!r}")
    if value == 0:
        return "clean"
    if value < 25:
        return "watch"
    if value < 50:
        return "elevated"
    return "severe"


def assess(lifecycle) -> dict:
    """One-call summary: triggered names, combined score, grade.

    Signature: assess(lifecycle) -> dict
    Returns {"triggered": [...], "score": int, "grade": str,
    "detectors_fired": int}. Consistent by construction: the grade
    is always ``risk_grade(score)`` and the count is always
    ``len(triggered)``.
    """
    fired = triggered_names(lifecycle)
    score = min(100, sum(DETECTOR_WEIGHTS[name] for name in fired))
    return {"triggered": fired, "score": score,
            "grade": risk_grade(score), "detectors_fired": len(fired)}
