"""Dust-holder inflation detection — MEASUREMENT/DISPLAY ONLY (safe batch 2).

A classic distribution theater: a coin shows hundreds of holders, but
most hold dust — tiny balances that cost almost nothing to create.
The holder COUNT looks like broad distribution; the holder ECONOMICS
say one operator (or a few) still own everything.

Detection: from a holder list with per-holder supply percentages
(RugCheck topHolders, already on the safety report), count holders
below the dust threshold (knob ``dust_holder_pct``, default 0.01% of
supply). Verdicts:

  inflated — >= dust_holder_min_count (default 50) dust holders AND
             dust holders are >= dust_holder_share (default 50%) of
             the holders we can see
  clean    — holder data present, not inflated
  unknown  — no holder data; never guessed

DISPLAY ONLY: a "🧂 dust: <tier>" line on cards. Never gates, never
scores, never trades. Pure function of provided data — no network,
no secrets. Never raises.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("dust_holders")


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


def _holders_from(report: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    try:
        holders = (report or {}).get("topHolders") or []
        return [h for h in holders if isinstance(h, dict)]
    except Exception:
        return []


def assess(degen: Any, report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Dust-holder verdict from a safety report. Never raises.

    Returns {"verdict": inflated|clean|unknown, "dust_count": int,
             "dust_supply_pct": float, "holders_seen": int, "detail": str}.
    """
    try:
        if not getattr(degen, "dust_holders_enabled", True):
            return {"verdict": "unknown", "dust_count": 0,
                    "dust_supply_pct": 0.0, "holders_seen": 0,
                    "detail": "disabled"}
        holders = _holders_from(report)
        if not holders:
            return {"verdict": "unknown", "dust_count": 0,
                    "dust_supply_pct": 0.0, "holders_seen": 0,
                    "detail": "no holder data"}
        dust_pct = _f(_knob(degen, "dust_holder_pct", 0.01), 0.01)
        min_count = int(_knob(degen, "dust_holder_min_count", 50) or 50)
        min_share = _f(_knob(degen, "dust_holder_share", 0.5), 0.5)
        dust = [h for h in holders if _f(h.get("pct")) < dust_pct]
        dust_count = len(dust)
        dust_supply = sum(_f(h.get("pct")) for h in dust)
        share = dust_count / max(1, len(holders))
        if dust_count >= min_count and share >= min_share:
            return {"verdict": "inflated", "dust_count": dust_count,
                    "dust_supply_pct": dust_supply,
                    "holders_seen": len(holders),
                    "detail": "%d dust holders (%.1f%% of supply)" % (
                        dust_count, dust_supply)}
        return {"verdict": "clean", "dust_count": dust_count,
                "dust_supply_pct": dust_supply,
                "holders_seen": len(holders),
                "detail": "%d dust of %d holders" % (
                    dust_count, len(holders))}
    except Exception:
        logger.exception("dust_holders assess failed")
        return {"verdict": "unknown", "dust_count": 0,
                "dust_supply_pct": 0.0, "holders_seen": 0,
                "detail": "check error"}


def card_line(degen: Any,
              report: Optional[Dict[str, Any]]) -> Optional[str]:
    """One-line card fragment, e.g. '🧂 dust: inflated'.

    Returns None when disabled or on error. 'unknown' IS rendered —
    the card says what it doesn't know. Never gates.
    """
    try:
        if not getattr(degen, "dust_holders_enabled", True):
            return None
        rec = assess(degen, report)
        return "🧂 dust: %s" % rec.get("verdict", "unknown")
    except Exception:
        return None
