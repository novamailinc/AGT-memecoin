"""Social attention radar — ATTENTION PROXIES ONLY (safe batch 2026-10-09).

Uses free keyless DexScreener endpoints:
  - GET https://api.dexscreener.com/token-boosts/top/v1     (top paid boosts)
  - GET https://api.dexscreener.com/token-boosts/latest/v1  (latest boosts)

A boost is PAID PROMOTION — an honest, observable proxy for "someone is
spending money to get attention on this coin". It is NOT a mention
count, NOT KOL data, NOT sentiment. Those are never invented: if a
proxy has no data, the tier is "unknown", recorded distinctly.

Tiers:
  high    — mint is in the top-boosts list right now
  med     — mint is in the latest-boosts list only
  low     — both lists fetched fine and the mint is in neither
            (no paid promotion detected)
  unknown — the fetch failed; never guessed

DISPLAY ONLY: a "📣 social heat: <tier>" line on cards. No gating, no
scoring changes, no entry/exit influence. One list fetch per ~10 min
(cached in memory), shared 1s DexScreener politeness gap, fail-soft —
a dead radar never affects the scan cycle.
"""

import json
import logging
import time
import urllib.request
from typing import Any, Dict, Optional, Set, Tuple

logger = logging.getLogger("social_radar")

_TOP_URL = "https://api.dexscreener.com/token-boosts/top/v1"
_LATEST_URL = "https://api.dexscreener.com/token-boosts/latest/v1"

_CACHE_TTL_S = 600.0
_MIN_CALL_GAP_S = 1.0  # DexScreener politeness gap (module-wide)

_last_call_ts = 0.0
_CACHE: Dict[str, Any] = {"ts": 0.0, "top": set(), "latest": set(),
                          "ok": False}


def _knob(degen: Any, name: str, default: Any) -> Any:
    try:
        v = getattr(degen, name, default)
        return default if v is None else v
    except Exception:
        return default


def _polite_get(url: str, timeout: float = 8.0) -> Optional[Any]:
    """GET with the shared 1s DexScreener gap. None on any failure."""
    global _last_call_ts
    try:
        gap = _MIN_CALL_GAP_S - (time.time() - _last_call_ts)
        if gap > 0:
            time.sleep(gap)
        req = urllib.request.Request(
            url, headers={"User-Agent": "forex-agent/1.0 (+paper-trading-bot)",
                          "Accept": "application/json",
                          "Connection": "close"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        _last_call_ts = time.time()
        return data
    except Exception as exc:
        logger.warning("social_radar fetch failed %s: %s", url, exc)
        return None


def _mint_set(payload: Any) -> Set[str]:
    out: Set[str] = set()
    try:
        items = payload if isinstance(payload, list) else []
        for it in items:
            if not isinstance(it, dict):
                continue
            addr = str(it.get("tokenAddress") or "").strip()
            if addr:
                out.add(addr)
    except Exception:
        pass
    return out


def _refresh(now: float, ttl_s: float = _CACHE_TTL_S
             ) -> Tuple[Set[str], Set[str], bool]:
    """Return (top, latest, ok); uses the cache when fresh."""
    try:
        if now - float(_CACHE.get("ts") or 0.0) < ttl_s:
            return _CACHE["top"], _CACHE["latest"], bool(_CACHE.get("ok"))
        top = _mint_set(_polite_get(_TOP_URL))
        latest = _mint_set(_polite_get(_LATEST_URL))
        # ok = at least one list came back non-empty (a fully empty
        # response from both endpoints means the fetch failed, not
        # that nobody is boosting anything).
        ok = bool(top or latest)
        _CACHE.update({"ts": now, "top": top, "latest": latest, "ok": ok})
        return top, latest, ok
    except Exception:
        logger.exception("social_radar refresh failed")
        return set(), set(), False


def heat(degen: Any, mint: str,
         now: Optional[float] = None) -> Dict[str, str]:
    """Attention tier for one mint. Display only. Never raises.

    Returns {"tier": high|med|low|unknown, "detail": ...}.
    """
    try:
        if not getattr(degen, "social_radar_enabled", True):
            return {"tier": "unknown", "detail": "disabled"}
        if not mint:
            return {"tier": "unknown", "detail": "no mint"}
        now = time.time() if now is None else float(now)
        ttl_s = float(_knob(degen, "social_radar_cache_min", 10.0) or 10.0
                      ) * 60.0
        top, latest, ok = _refresh(now, ttl_s)
        if not ok:
            return {"tier": "unknown", "detail": "boost lists unavailable"}
        m = str(mint).strip()
        variants = {m, m.lower()}
        if variants & top:
            return {"tier": "high", "detail": "in DexScreener top boosts"}
        if variants & latest:
            return {"tier": "med", "detail": "in DexScreener latest boosts"}
        return {"tier": "low", "detail": "no paid boost detected"}
    except Exception:
        logger.exception("social_radar heat failed for %s", mint)
        return {"tier": "unknown", "detail": "check error"}


def card_line(degen: Any, mint: str) -> Optional[str]:
    """One-line card fragment, e.g. '📣 social heat: high'.

    Returns None when disabled or on error (fail-soft: no line rather
    than a wrong line). 'unknown' IS rendered — the card says what it
    doesn't know.
    """
    try:
        if not getattr(degen, "social_radar_enabled", True):
            return None
        if not mint:
            return None
        rec = heat(degen, mint)
        return "📣 social heat: %s" % rec.get("tier", "unknown")
    except Exception:
        return None
