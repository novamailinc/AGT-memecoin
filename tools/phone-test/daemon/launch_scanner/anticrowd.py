"""Anti-crowd filter — DISPLAY ONLY (safe batch 2026-10-09).

Crowd score from two honest inputs:
  - boost status (from social_radar's cache: boosted / not / unknown)
  - how often the coin has been carded (this module's own counter,
    fed by note_card() at the card-assembly hook)

Tiers:
  high    — score >= 3  (boosted AND repeatedly carded)
  med     — score >= 1
  low     — score 0 with real data (not boosted, rarely carded)
  unknown — no data at all

The name is aspirational: today this NEVER gates or filters anything.
It only renders the "👥 crowd: <tier>" line on cards so a human can
see when a coin is crowded — crowded coins are where *you* are the
exit liquidity.

Card counts live in runtime state (``<home>/launch_scanner/
anticrowd.json``, 0600, atomic writes), bounded (2000 mints) with a
7-day TTL. Never raises; a dead module never affects the scan cycle.
"""

import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("anticrowd")

_SUBDIR = "launch_scanner"
_FILENAME = "anticrowd.json"
_TTL_S = 7 * 24 * 3600
_MAX_MINTS = 2000


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN -> default


def _testing() -> bool:
    return os.environ.get("FOREX_AGENT_TESTING") == "1"


def _resolve_path(path: Optional[str]) -> Optional[str]:
    if path is not None:
        return str(path)
    if _testing():
        return None
    try:
        from daemon.common import get_home  # noqa: PLC0415
        return os.path.join(get_home(), _SUBDIR, _FILENAME)
    except Exception:
        logger.exception("anticrowd: could not resolve home")
        return None


def _load(path: Optional[str]) -> Dict[str, Dict[str, Any]]:
    p = _resolve_path(path)
    if not p:
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, ValueError):
        return {}
    except Exception:
        logger.exception("anticrowd: load failed")
        return {}
    if not isinstance(data, dict):
        return {}
    cutoff = time.time() - _TTL_S
    return {str(m): r for m, r in data.items()
            if isinstance(r, dict) and _f(r.get("last_ts")) >= cutoff}


def _save(path: Optional[str], counts: Dict[str, Dict[str, Any]]) -> bool:
    p = _resolve_path(path)
    if not p:
        return False
    try:
        blob = json.dumps(counts, separators=(",", ":")).encode("utf-8")
    except Exception:
        logger.exception("anticrowd: serialize failed")
        return False
    tmp = "%s.tmp.%d" % (p, os.getpid())
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        return True
    except Exception:
        logger.exception("anticrowd: write failed")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def note_card(mint: str, ts: Optional[float] = None,
              path: Optional[str] = None) -> None:
    """Count one carded appearance of a mint (card-assembly hook).

    Pure observation. Never raises.
    """
    try:
        if not mint:
            return
        mint = str(mint)
        ts = time.time() if ts is None else float(ts)
        counts = _load(path)
        rec = counts.get(mint) or {"count": 0, "first_ts": ts}
        rec["count"] = int(rec.get("count") or 0) + 1
        rec["last_ts"] = ts
        if "first_ts" not in rec:
            rec["first_ts"] = ts
        counts[mint] = rec
        if len(counts) > _MAX_MINTS:
            for m in sorted(counts,
                            key=lambda k: _f(counts[k].get("last_ts"))
                            )[:len(counts) - _MAX_MINTS]:
                counts.pop(m, None)
        _save(path, counts)
    except Exception:
        pass  # observation must never break card assembly


def crowd(degen: Any, mint: str, boosted: Optional[bool] = None,
          now: Optional[float] = None,
          path: Optional[str] = None) -> Dict[str, Any]:
    """Crowd tier for one mint. Display only. Never raises, never gates.

    boosted: True/False from social_radar (None = unknown).
    Returns {"tier": high|med|low|unknown, "score": int, "detail": str}.
    """
    try:
        if not getattr(degen, "anticrowd_enabled", True):
            return {"tier": "unknown", "score": 0, "detail": "disabled"}
        if not mint:
            return {"tier": "unknown", "score": 0, "detail": "no mint"}
        counts = _load(path)
        rec = counts.get(str(mint)) or {}
        cards = int(rec.get("count") or 0)

        score = 0
        parts = []
        if boosted is True:
            score += 2
            parts.append("boosted")
        elif boosted is None:
            parts.append("boost unknown")
        if cards >= 3:
            score += 2
            parts.append("carded %dx" % cards)
        elif cards >= 1:
            score += 1
            parts.append("carded %dx" % cards)

        has_data = boosted is not None or cards > 0
        if not has_data:
            return {"tier": "unknown", "score": 0,
                    "detail": "no crowd data"}
        tier = "high" if score >= 3 else "med" if score >= 1 else "low"
        return {"tier": tier, "score": score,
                "detail": ", ".join(parts) or "quiet"}
    except Exception:
        logger.exception("anticrowd crowd failed for %s", mint)
        return {"tier": "unknown", "score": 0, "detail": "check error"}


def card_line(degen: Any, mint: str,
              boosted: Optional[bool] = None) -> Optional[str]:
    """One-line card fragment, e.g. '👥 crowd: high'.

    Returns None when disabled or on error. 'unknown' is rendered —
    the card says what it doesn't know.
    """
    try:
        if not getattr(degen, "anticrowd_enabled", True):
            return None
        if not mint:
            return None
        rec = crowd(degen, mint, boosted=boosted)
        return "👥 crowd: %s" % rec.get("tier", "unknown")
    except Exception:
        return None
