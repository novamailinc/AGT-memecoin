"""Buy-wall spoofing detection — MEASUREMENT/DISPLAY ONLY (safe batch 2).

The trick: a large buy wall appears (support looks strong, buyers
pile in) and disappears before it ever fills — the "support" was
theater to manufacture confidence.

Honesty note: Solana AMM pools have NO order book, so true
spoof-walls are an order-book phenomenon (Serum/OpenBook-style
markets). What IS observable on AMMs is the cousin: a large
LIQUIDITY add (>= wall_mult x trailing median) that vanishes within
wall_minutes while buys keep coming — depth advertised, then pulled.

Detection reads the in-memory fingerprint series (ts, liquidity,
buys) — no extra HTTP. Verdicts:

  watch   — a wall appeared and vanished quickly
  none    — snapshots present, no wall pattern
  unknown — too few snapshots; never guessed

DISPLAY ONLY: a "🧱 spoof wall?" warning line, rendered ONLY on
"watch". Never gates, never touches exits. Never raises.
"""

import logging
import statistics
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("spoof_walls")


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
    """(ts, liquidity_usd, buys_m5) from fingerprint observation tuples.

    Tuple layout: (ts, mcap, price, vol_m5, buys_m5, sells_m5,
    usd_liquidity, ...). Garbage in -> empty out, never raises.
    """
    out: List[Tuple[float, float, float]] = []
    try:
        for o in (observations or []):
            if not isinstance(o, (list, tuple)) or len(o) < 7:
                continue
            ts, buys, liq = _f(o[0]), _f(o[4]), _f(o[6])
            if ts > 0 and liq > 0:
                out.append((ts, liq, buys))
    except Exception:
        pass
    return sorted(out)


def assess(degen: Any, observations: Any) -> Dict[str, Any]:
    """Spoof-wall verdict from liquidity snapshots. Never raises.

    Returns {"verdict": watch|none|unknown, "detail": str}.
    """
    try:
        if not getattr(degen, "spoof_walls_enabled", True):
            return {"verdict": "unknown", "detail": "disabled"}
        snaps = _snaps(observations)
        min_snaps = int(_knob(degen, "spoof_walls_min_snaps", 6) or 6)
        if len(snaps) < min_snaps:
            return {"verdict": "unknown",
                    "detail": "need >=%d snapshots" % min_snaps}
        wall_mult = _f(_knob(degen, "spoof_walls_wall_mult", 4.0), 4.0)
        wall_min = _f(_knob(degen, "spoof_walls_wall_min", 15.0), 15.0)
        liqs = [s[1] for s in snaps]
        try:
            med = statistics.median(liqs)
        except Exception:
            return {"verdict": "unknown", "detail": "no baseline"}
        if med <= 0:
            return {"verdict": "unknown", "detail": "no baseline"}
        for i, (ts, liq, _b) in enumerate(snaps):
            if liq < wall_mult * med:
                continue
            # A wall that fills is real support; a wall that vanishes
            # while buys kept coming was theater.
            for j in range(i + 1, len(snaps)):
                ts2, liq2, buys2 = snaps[j]
                if (ts2 - ts) / 60.0 > wall_min:
                    break
                if liq2 < 1.5 * med and buys2 > 0:
                    return {
                        "verdict": "watch",
                        "detail": "wall %.0f→%.0f in %.0f min while "
                                  "buys continued" % (
                                      liq, liq2, (ts2 - ts) / 60.0)}
        return {"verdict": "none", "detail": "no vanishing walls seen"}
    except Exception:
        logger.exception("spoof_walls assess failed")
        return {"verdict": "unknown", "detail": "check error"}


def card_line(degen: Any, observations: Any) -> Optional[str]:
    """Warning line, rendered ONLY on "watch". Quiet otherwise.

    Never gates, never touches exits.
    """
    try:
        if not getattr(degen, "spoof_walls_enabled", True):
            return None
        rec = assess(degen, observations)
        if rec.get("verdict") == "watch":
            return "🧱 spoof wall?"
        return None
    except Exception:
        return None
