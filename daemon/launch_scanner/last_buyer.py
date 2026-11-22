"""Last-buyer alarm — buying-pressure dry-up, DISPLAY ONLY (safe batch).

Reads the existing fingerprint time series (in-memory, no HTTP calls):
observations are (ts, mcap, price, vol_m5, buys, sells, liq, holders).

Verdict "drying" when recent-window buys/min collapsed versus the
earlier baseline (<= dry_ratio, default 0.25) AND the price is flat or
fading (recent max <= baseline max * (1 + flat_pct) — no new highs
being made while buyers vanish). "ok" when buying is healthy or the
price is still making highs. "unknown" when the series is too short
(< 6 observations) — recorded distinctly, never guessed.

DISPLAY ONLY: a "🥀 buyers drying up" warning line on cards. This
module NEVER changes exit logic — the exits cannot see it. It is a
heads-up for the human, not a trigger for the machine.
"""

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("last_buyer")

# fingerprint obs tuple: (ts, mcap, price, vol_m5, buys, sells, liq, holders)
_TS, _PRICE, _BUYS = 0, 2, 4
_MIN_OBS = 6


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN -> default


def _buys_per_min(obs: Sequence[Tuple[float, ...]]) -> float:
    try:
        if len(obs) < 2:
            return 0.0
        span_min = max(1.0 / 60.0,
                       (_f(obs[-1][_TS]) - _f(obs[0][_TS])) / 60.0)
        return sum(_f(o[_BUYS]) for o in obs) / span_min
    except Exception:
        return 0.0


def verdict(obs: Optional[Sequence[Tuple[float, ...]]],
            degen: Any) -> Dict[str, str]:
    """Buying-pressure verdict from a fingerprint observation series.

    Returns {"verdict": drying|ok|unknown, "detail": ...}.
    Pure function of the series — no I/O, never raises.
    """
    try:
        if not getattr(degen, "last_buyer_enabled", True):
            return {"verdict": "unknown", "detail": "disabled"}
        rows = [o for o in (obs or []) if isinstance(o, (list, tuple))
                and len(o) > _BUYS]
        if len(rows) < _MIN_OBS:
            return {"verdict": "unknown",
                    "detail": "only %d observations" % len(rows)}
        dry_ratio = float(getattr(degen, "last_buyer_dry_ratio", 0.25)
                          or 0.25)
        flat_pct = float(getattr(degen, "last_buyer_flat_pct", 5.0) or 5.0)

        base, recent = rows[:-3], rows[-3:]
        base_bpm = _buys_per_min(base)
        recent_bpm = _buys_per_min(recent)
        base_max = max(_f(o[_PRICE]) for o in base)
        recent_max = max(_f(o[_PRICE]) for o in recent)

        if base_bpm <= 0:
            return {"verdict": "unknown", "detail": "no baseline buys"}
        collapsed = recent_bpm <= dry_ratio * base_bpm
        no_new_highs = recent_max <= base_max * (1.0 + flat_pct / 100.0)
        if collapsed and no_new_highs:
            return {"verdict": "drying",
                    "detail": "buys/min %.1f -> %.1f, no new highs"
                    % (base_bpm, recent_bpm)}
        return {"verdict": "ok",
                "detail": "buys/min %.1f -> %.1f" % (base_bpm, recent_bpm)}
    except Exception:
        logger.exception("last_buyer verdict failed")
        return {"verdict": "unknown", "detail": "check error"}


def card_line(v: Optional[Dict[str, str]]) -> Optional[str]:
    """Warning fragment for the card. Only renders on 'drying' —
    a quiet series is not news. None otherwise (fail-soft)."""
    try:
        if not isinstance(v, dict) or v.get("verdict") != "drying":
            return None
        return "🥀 buyers drying up (%s)" % v.get("detail", "")
    except Exception:
        return None
