"""Copy-the-copiers signal — MEASUREMENT/DISPLAY ONLY (safe batch 2).

The insight: when MANY untracked wallets pile into a mint that NO
tracked smart wallet touched, that's the copier herd arriving late —
often the top, or close to it. It's a contrarian read: smart money
absent + crowd present = you may be the exit liquidity.

Inputs (all provided by the caller, never fetched here):
  buyer_count — distinct buyers seen (from the fingerprint buy series)
  smart_buys  — tracked smart wallets that bought (smartmoney module)

Verdicts:
  pilein   — buyer_count >= pilein_min_buyers (default 30) AND
             smart_buys == 0
  smartled — smart_buys > 0 (smart money is in; not a copier pile-in)
  quiet    — data present, neither pattern
  unknown  — no data; never guessed

DISPLAY ONLY: a "🪞 copier pile-in: <buyers> buyers, 0 smart" line,
rendered ONLY on "pilein". Never gates, never scores, never trades.

Pile-in events are recorded to runtime state
(``<home>/launch_scanner/copy_copiers.json``, 0600, bounded 500,
7-day TTL) for the weekly review. Never raises.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("copy_copiers")

_SUBDIR = "launch_scanner"
_FILENAME = "copy_copiers.json"
_TTL_S = 7 * 24 * 3600
_MAX_EVENTS = 500


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
        logger.exception("copy_copiers: could not resolve home")
        return None


def _load(path: Optional[str]) -> List[Dict[str, Any]]:
    p = _resolve_path(path)
    if not p:
        return []
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, ValueError):
        return []
    except Exception:
        logger.exception("copy_copiers: load failed")
        return []
    if not isinstance(data, list):
        return []
    cutoff = time.time() - _TTL_S
    return [r for r in data
            if isinstance(r, dict) and _f(r.get("ts")) >= cutoff
            ][:_MAX_EVENTS]


def _save(path: Optional[str], events: List[Dict[str, Any]]) -> bool:
    p = _resolve_path(path)
    if not p:
        return False
    try:
        blob = json.dumps(events[:_MAX_EVENTS],
                          separators=(",", ":")).encode("utf-8")
    except Exception:
        logger.exception("copy_copiers: serialize failed")
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
        logger.exception("copy_copiers: write failed")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _buyer_count(observations: Any) -> Optional[int]:
    """Distinct-buyer proxy: polls with buys>0, scaled.

    The fingerprint series records buys_m5 per poll (tuple index 4).
    We count polls with buys and scale by the per-poll buy count as a
    rough buyer proxy. Returns None when there is no usable series —
    never guessed.
    """
    try:
        total = 0.0
        polls = 0
        for o in (observations or []):
            if not isinstance(o, (list, tuple)) or len(o) < 6:
                continue
            b = _f(o[4])
            if b > 0:
                polls += 1
                total += b
        if polls == 0:
            return None
        return int(total)
    except Exception:
        return None


def signal(degen: Any, mint: str, observations: Any, smart_buys: Any,
           now: Optional[float] = None,
           path: Optional[str] = None) -> Dict[str, Any]:
    """Copier pile-in signal. Measurement only. Never raises.

    Returns {"verdict": pilein|smartled|quiet|unknown,
             "buyers": int|None, "smart_buys": int, "detail": str}.
    A "pilein" verdict is recorded to the runtime event log.
    """
    try:
        if not getattr(degen, "copy_copiers_enabled", True):
            return {"verdict": "unknown", "buyers": None,
                    "smart_buys": 0, "detail": "disabled"}
        mint = str(mint or "").strip()
        buyers = _buyer_count(observations)
        try:
            smart = int(smart_buys or 0)
        except (TypeError, ValueError):
            smart = 0
        if buyers is None:
            return {"verdict": "unknown", "buyers": None,
                    "smart_buys": smart, "detail": "no buy data"}
        min_buyers = int(_knob(degen, "copy_copiers_min_buyers", 30) or 30)
        now = time.time() if now is None else float(now)
        if smart > 0:
            return {"verdict": "smartled", "buyers": buyers,
                    "smart_buys": smart,
                    "detail": "%d tracked smart wallets in" % smart}
        if buyers >= min_buyers:
            events = _load(path)
            if not any(r.get("mint") == mint for r in events):
                events.insert(0, {"mint": mint, "buyers": buyers,
                                  "ts": now})
                _save(path, events)
            return {"verdict": "pilein", "buyers": buyers,
                    "smart_buys": 0,
                    "detail": "%d buyers, 0 tracked smart wallets"
                              % buyers}
        return {"verdict": "quiet", "buyers": buyers,
                "smart_buys": 0,
                "detail": "%d buyers (< %d)" % (buyers, min_buyers)}
    except Exception:
        logger.exception("copy_copiers signal failed for %s", mint)
        return {"verdict": "unknown", "buyers": None,
                "smart_buys": 0, "detail": "check error"}


def pilein_events(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Recorded pile-in events (weekly review). Never raises."""
    try:
        return _load(path)
    except Exception:
        return []


def weekly_section(degen: Any = None,
                   path: Optional[str] = None) -> List[str]:
    """Weekly review lines: pile-in counts. Honest zeros. Never raises."""
    out = ["== COPY-THE-COPIERS (last 7 days) =="]
    try:
        events = _load(path)
        out.append("pile-in events: %d" % len(events))
        if events:
            buyers = [int(e.get("buyers") or 0) for e in events]
            try:
                import statistics  # noqa: PLC0415
                out.append("median buyers at pile-in: %d" % int(
                    statistics.median(buyers)))
            except Exception:
                pass
            for e in events[:5]:
                out.append("  %s…: %d buyers, 0 smart" % (
                    str(e.get("mint") or "")[:8],
                    int(e.get("buyers") or 0)))
        else:
            out.append("no pile-ins recorded yet.")
    except Exception:
        out.append("copier data unavailable (read failed).")
    return out


def card_line(degen: Any, mint: str, observations: Any,
              smart_buys: Any,
              path: Optional[str] = None) -> Optional[str]:
    """Contrarian line, rendered ONLY on a pile-in verdict.

    e.g. '🪞 copier pile-in: 47 buyers, 0 smart'. Quiet otherwise.
    Never gates.
    """
    try:
        if not getattr(degen, "copy_copiers_enabled", True):
            return None
        if not mint:
            return None
        rec = signal(degen, mint, observations, smart_buys, path=path)
        if rec.get("verdict") == "pilein":
            return "🪞 copier pile-in: %d buyers, 0 smart" % int(
                rec.get("buyers") or 0)
        return None
    except Exception:
        return None
