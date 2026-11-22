"""Liquidity mirage detection — MEASUREMENT/DISPLAY ONLY (safe batch 2).

The trick: liquidity appears (a big add, a "locked" claim, depth that
looks safe) and vanishes around buys — the pool looks deep when you
enter and shallow when you try to leave.

Detection: from pool liquidity snapshots (ts, liquidity_usd) — read
from the in-memory fingerprint series, no extra HTTP — flag a
MIRAGE when liquidity spikes >= spike_mult (default 3.0x) over the
trailing median and collapses back below 1.5x median within
collapse_minutes (default 30), while volume stays flat (so it wasn't
a real inflow driven by trading).

DISPLAY ONLY: a "🌊 liquidity mirage?" warning line, rendered ONLY
when a mirage is actually detected — quiet otherwise. Never gates,
never scores, never touches exits. Never raises; a dead module never
affects the scan cycle.
"""

import logging
import statistics
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("liquidity_mirage")


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN -> default


def _knob(degen: Any, name: str, default: Any) -> Any:
    try:
        v = getattr(degen, name, default)
        return default if v is None else v
    except Exception:
        return default


def _snaps(observations: Any) -> List[Tuple[float, float, float]]:
    """(ts, liquidity_usd, vol_m5) from fingerprint observation tuples.

    Tuple layout: (ts, mcap, price, vol_m5, buys_m5, sells_m5,
    usd_liquidity, ...). Garbage in -> empty out, never raises.
    """
    out: List[Tuple[float, float, float]] = []
    try:
        for o in (observations or []):
            if not isinstance(o, (list, tuple)) or len(o) < 7:
                continue
            ts, vol, liq = _f(o[0]), _f(o[3]), _f(o[6])
            if ts > 0 and liq > 0:
                out.append((ts, liq, vol))
    except Exception:
        pass
    return sorted(out)


def assess(degen: Any, observations: Any) -> Dict[str, Any]:
    """Mirage verdict from liquidity snapshots. Never raises.

    Returns {"verdict": mirage|stable|unknown, "detail": str}.
    """
    try:
        if not getattr(degen, "liquidity_mirage_enabled", True):
            return {"verdict": "unknown", "detail": "disabled"}
        snaps = _snaps(observations)
        min_snaps = int(_knob(degen, "liquidity_mirage_min_snaps", 6) or 6)
        if len(snaps) < min_snaps:
            return {"verdict": "unknown", "detail": "need >=%d snapshots"
                    % min_snaps}
        spike_mult = _f(_knob(degen, "liquidity_mirage_spike_mult", 3.0),
                        3.0)
        collapse_min = _f(_knob(degen, "liquidity_mirage_collapse_min",
                                30.0), 30.0)
        liqs = [s[1] for s in snaps]
        vols = [s[2] for s in snaps]
        try:
            med = statistics.median(liqs[:-1]) if len(liqs) > 1 else liqs[0]
        except Exception:
            return {"verdict": "unknown", "detail": "no baseline"}
        if med <= 0:
            return {"verdict": "unknown", "detail": "no baseline"}
        # Find a spike: any snapshot >= spike_mult * median, then check
        # whether liquidity fell back within collapse_min while volume
        # stayed flat (median volume ratio < 1.5x).
        try:
            vol_med = statistics.median(vols) if vols else 0.0
        except Exception:
            vol_med = 0.0
        for i, (ts, liq, _v) in enumerate(snaps):
            if liq < spike_mult * med:
                continue
            for j in range(i + 1, len(snaps)):
                ts2, liq2, _v2 = snaps[j]
                if (ts2 - ts) / 60.0 > collapse_min:
                    break
                if liq2 < 1.5 * med:
                    flat_vol = (vol_med <= 0) or (
                        max(vols[i:j + 1] or [0.0])
                        < 1.5 * max(vol_med, 1e-9))
                    if flat_vol:
                        return {
                            "verdict": "mirage",
                            "detail": "liq %.0f→%.0f in %.0f min, "
                                      "volume flat" % (
                                          liq, liq2, (ts2 - ts) / 60.0)}
                    break
        return {"verdict": "stable", "detail": "no spike-and-vanish seen"}
    except Exception:
        logger.exception("liquidity_mirage assess failed")
        return {"verdict": "unknown", "detail": "check error"}


def card_line(degen: Any, observations: Any) -> Optional[str]:
    """Warning line, rendered ONLY on a detected mirage.

    Quiet otherwise — a warning that fires on "unknown" would be
    noise. Never gates.
    """
    try:
        if not getattr(degen, "liquidity_mirage_enabled", True):
            return None
        rec = assess(degen, observations)
        if rec.get("verdict") == "mirage":
            return "🌊 liquidity mirage?"
        return None
    except Exception:
        return None
