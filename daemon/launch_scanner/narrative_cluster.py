"""Narrative clustering — MEASUREMENT/DISPLAY ONLY (safe batch 2).

Deepens the existing narrative work: instead of one coin's tag, track
WHICH narrative clusters are heating across recent coins. A cluster
is heating when >= cluster_min_coins (default 5) coins sharing a tag
appeared within window_hours (default 24).

Coins are registered via ``note_coin(mint, symbol, name)`` — tagging
reuses ``narratives.classify_narrative`` (substring taxonomy, stdlib).
``heating()`` returns per-tag {count, median_move} where median_move
comes from caller-provided peak multiples (milestone tracker) when
available — count-only when not. Never invented.

DISPLAY ONLY: a "🏷️ narrative: <tag> 🔥" line on cards when the
coin's own tag is heating. Never scores entries, never gates.
Pure local computation — no network, no secrets.

Runtime state: ``<home>/launch_scanner/narrative_cluster.json``
(0600, atomic, bounded 2000 coins, 7-day TTL). Never raises.
"""

import json
import logging
import os
import statistics
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("narrative_cluster")

_SUBDIR = "launch_scanner"
_FILENAME = "narrative_cluster.json"
_TTL_S = 7 * 24 * 3600
_MAX_COINS = 2000


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN -> default


def _testing() -> bool:
    return os.environ.get("FOREX_AGENT_TESTING") == "1"


def _knob(degen: Any, name: str, default: Any) -> Any:
    try:
        v = getattr(degen, name, default)
        return default if v is None else v
    except Exception:
        return default


def _resolve_path(path: Optional[str]) -> Optional[str]:
    if path is not None:
        return str(path)
    if _testing():
        return None
    try:
        from daemon.common import get_home  # noqa: PLC0415
        return os.path.join(get_home(), _SUBDIR, _FILENAME)
    except Exception:
        logger.exception("narrative_cluster: could not resolve home")
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
        logger.exception("narrative_cluster: load failed")
        return {}
    if not isinstance(data, dict):
        return {}
    cutoff = time.time() - _TTL_S
    coins = {str(m): r for m, r in data.items()
             if isinstance(r, dict) and _f(r.get("ts")) >= cutoff}
    if len(coins) > _MAX_COINS:
        for m in sorted(coins, key=lambda k: _f(coins[k].get("ts"))
                        )[:len(coins) - _MAX_COINS]:
            coins.pop(m, None)
    return coins


def _save(path: Optional[str], coins: Dict[str, Dict[str, Any]]) -> bool:
    p = _resolve_path(path)
    if not p:
        return False
    try:
        blob = json.dumps(coins, separators=(",", ":")).encode("utf-8")
    except Exception:
        logger.exception("narrative_cluster: serialize failed")
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
        logger.exception("narrative_cluster: write failed")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def note_coin(mint: str, symbol: Any, name: Any,
              ts: Optional[float] = None,
              path: Optional[str] = None) -> Optional[List[str]]:
    """Register a coin's narrative tags. Returns the tags. Never raises."""
    try:
        mint = str(mint or "").strip()
        if not mint:
            return None
        from . import narratives  # noqa: PLC0415
        tags = narratives.classify_narrative(symbol, name)
        if not isinstance(tags, list) or not tags:
            tags = ["other"]
        ts = time.time() if ts is None else float(ts)
        coins = _load(path)
        coins[mint] = {"tags": [str(t) for t in tags], "ts": ts}
        _save(path, coins)
        return [str(t) for t in tags]
    except Exception:
        logger.exception("narrative_cluster note_coin failed")
        return None


def heating(degen: Any, now: Optional[float] = None,
            peaks: Optional[Dict[str, float]] = None,
            path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Heating narrative clusters, hottest first. Never raises.

    peaks: optional {mint: peak_multiple} (milestone tracker). Without
    it, clusters report count only and median_move is None — never
    invented.
    """
    try:
        if not getattr(degen, "narrative_cluster_enabled", True):
            return []
        now = time.time() if now is None else float(now)
        window_h = _f(_knob(degen, "narrative_cluster_window_h", 24.0),
                      24.0)
        min_coins = int(_knob(degen, "narrative_cluster_min_coins", 5)
                        or 5)
        cutoff = now - window_h * 3600.0
        coins = _load(path)
        by_tag: Dict[str, List[str]] = {}
        for mint, rec in coins.items():
            if _f(rec.get("ts")) < cutoff:
                continue
            for t in (rec.get("tags") or ["other"]):
                by_tag.setdefault(str(t), []).append(str(mint))
        out: List[Dict[str, Any]] = []
        for tag, mints in by_tag.items():
            if len(mints) < min_coins:
                continue
            med: Optional[float] = None
            if peaks:
                vals = [float(peaks[m]) for m in mints
                        if m in peaks]
                vals = [v for v in vals if v == v and v > 0]
                if vals:
                    try:
                        med = statistics.median(vals)
                    except Exception:
                        med = None
            out.append({"tag": tag, "count": len(mints),
                        "median_move": med})
        out.sort(key=lambda r: (-int(r["count"]),
                                -(r["median_move"] or 0.0)))
        return out
    except Exception:
        logger.exception("narrative_cluster heating failed")
        return []


def card_line(degen: Any, mint: str, symbol: Any, name: Any,
              path: Optional[str] = None) -> Optional[str]:
    """'🏷️ narrative: <tag> 🔥' when the coin's own tag is heating.

    Registers the coin as a side effect (pure observation). Returns
    None when disabled, on error, or when no tag is heating — never
    gates.
    """
    try:
        if not getattr(degen, "narrative_cluster_enabled", True):
            return None
        if not mint:
            return None
        tags = note_coin(mint, symbol, name, path=path) or []
        hot = {r["tag"] for r in heating(degen, path=path)}
        for t in tags:
            if t in hot:
                return "🏷️ narrative: %s 🔥" % t
        return None
    except Exception:
        return None


def weekly_section(degen: Any,
                   path: Optional[str] = None) -> List[str]:
    """Weekly review lines: heating clusters. Honest zeros. Never raises."""
    out = ["== NARRATIVE CLUSTERS (heating now) =="]
    try:
        hot = heating(degen, path=path)
        if not hot:
            out.append("no heating clusters right now.")
        for r in hot[:8]:
            med = r.get("median_move")
            med_s = ("median %.1fx" % med) if med else "move n/a"
            out.append("  %s: %d coins, %s" % (
                r.get("tag"), int(r.get("count") or 0), med_s))
    except Exception:
        out.append("narrative data unavailable (read failed).")
    return out
