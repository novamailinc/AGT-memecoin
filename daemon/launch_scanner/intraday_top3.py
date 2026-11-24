"""Intraday proof-gated Top 3 (Phase 4, user decision 2026-10-09 — judgment mode).

Event-driven single-coin pick cards: the moment a confirmed runner passes
ALL gates, a 🎯 TOP 3 card drops intraday. Max ~5 picks/day, per-coin 6h
cooldown, ZERO forced picks — on days nothing qualifies, nothing drops
(never "best of the worst").

Judgment-mode gates (the Friday verdict earned no gates):
  (a) safety: known score >= 1/100 with no hard-gate failures; UNVETTED
      confirmed coins are allowed but labeled UNVETTED plainly (the
      confirmed lane itself admits them — user decision 2026-09-28);
  (b) holder-concentration proof check must not FAIL — a fail blocks the
      pick, "unknown" is allowed with an honest "?" on the card. The LP
      check is dead (0% coverage, ignored); distribution is WATCH-only
      (never fires, not gated on).

Ranking reuses ``toppicks.composite`` (sniper/winner/holder/smart/
fingerprint) for the score + "why" line — no new ranking invented.

Alert-only: picks NEVER open positions (scouts handle entries; the
existing daily Top Picks are never auto-traded and neither is this).
Composes with (never bypasses) the 4/hour instant cap — a cap-tripped
pick is skipped for this cycle WITHOUT burning the per-coin cooldown,
so the next qualifying confirmed card for the coin can still pick it
up. Personal filters / quiet-hours apply via SIGNAL_EVENTS.

Never raises into the scanner loop.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("forex_agent.daemon.launch_scanner.intraday_top3")

WAT = timezone(timedelta(hours=1))

DAY_KEY = "intraday_top3_day"        # WAT date string of the current count
COUNT_KEY = "intraday_top3_count"    # picks published today
COOLDOWN_KEY = "intraday_top3_cooldown"  # mint -> last pick ts
LOG_KEY = "intraday_top3_log"        # bounded list of past picks (grading)
_MAX_LOG = 200


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN -> default


def _cfg(degen: Any, name: str, default: Any) -> Any:
    try:
        v = getattr(degen, name, default)
        return default if v is None else v
    except Exception:
        return default


def enabled(degen: Any) -> bool:
    return bool(_cfg(degen, "intraday_top3_enabled", True))


def _safety_ok(report: Optional[Dict[str, Any]], unvetted: bool,
               degen: Any) -> Tuple[bool, str, bool]:
    """Gate (a): safety bar for a Top 3 pick.

    Returns (ok, label, is_unvetted). Unvetted confirmed coins are
    allowed — labeled UNVETTED plainly — because the confirmed lane
    itself admits them; hard fails always block.
    """
    try:
        rep = report or {}
        fails = [str(f) for f in (rep.get("fails") or [])]
        if fails:
            return False, "hard-fail: %s" % fails[0][:48], False
        if unvetted or rep.get("unknown"):
            return True, "UNVETTED", True
        score = _f(rep.get("score"), 0.0)
        floor = _f(_cfg(degen, "early_min_score", 1.0), 1.0)
        if score < floor:
            return False, "score %.0f < %.0f" % (score, floor), False
        return True, "%.0f/100" % score, False
    except Exception:
        return False, "safety-check-error", False


def _proof_ok(proof_rec: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """Gate (b): holder-concentration must not FAIL.

    LP ignored (dead check), distribution WATCH-only. Unknown is
    allowed — the card shows "?" honestly.
    """
    try:
        rec = proof_rec or {}
        holders = (rec.get("holders") or {}).get("verdict")
        if holders == "fail":
            detail = (rec.get("holders") or {}).get("detail") or ""
            return False, "holders FAIL: %s" % detail[:64]
        return True, "proof ok"
    except Exception:
        return False, "proof-check-error"


def proof_line(proof_rec: Optional[Dict[str, Any]]) -> str:
    """Compact proof line for the card, e.g. 'LP:? HOLDERS:PASS DIST:?'."""
    try:
        if not isinstance(proof_rec, dict):
            return "LP:? HOLDERS:? DIST:?"
        short = {"pass": "PASS", "fail": "FAIL", "unknown": "?"}
        get = lambda k: short.get(
            str(((proof_rec.get(k) or {}).get("verdict")) or "?").lower(),
            "?")
        return "LP:%s HOLDERS:%s DIST:%s" % (
            get("lp"), get("holders"), get("distribution"))
    except Exception:
        return "LP:? HOLDERS:? DIST:?"


def _wat_day(now: float) -> str:
    return datetime.fromtimestamp(now, WAT).strftime("%Y-%m-%d")


def _cooldown_left(state: Dict[str, Any], mint: str, degen: Any,
                   now: float) -> float:
    """Seconds remaining on the per-coin cooldown, 0 when clear."""
    try:
        cd = state.get(COOLDOWN_KEY) or {}
        last = float(cd.get(str(mint)) or 0)
    except (TypeError, ValueError):
        return 0.0
    cd_h = _f(_cfg(degen, "intraday_top3_cooldown_hours", 6.0), 6.0)
    if cd_h <= 0 or last <= 0:
        return 0.0
    return max(0.0, cd_h * 3600.0 - (now - last))


def _daily_count(state: Dict[str, Any], now: float) -> int:
    """Picks published today (WAT); rolls over at midnight."""
    try:
        if state.get(DAY_KEY) != _wat_day(now):
            return 0
        return int(state.get(COUNT_KEY) or 0)
    except Exception:
        return 0


def qualify(state: Dict[str, Any], degen: Any, mint: str,
            report: Optional[Dict[str, Any]], unvetted: bool,
            proof_rec: Optional[Dict[str, Any]] = None,
            now: Optional[float] = None) -> Tuple[bool, str, str]:
    """Decide whether a confirmed coin earns a Top 3 pick right now.

    Returns (ok, reason, safety_label). Checks gates + daily cap +
    per-coin cooldown, but NOT the 4/hour instant cap (the caller owns
    that via cap_check, so a trip doesn't burn the cooldown).
    Never raises.
    """
    try:
        now = now if now is not None else time.time()
        if not enabled(degen):
            return False, "intraday top3 disabled", ""
        if not mint:
            return False, "no mint", ""

        ok, label, _is_unv = _safety_ok(report, unvetted, degen)
        if not ok:
            return False, label, ""

        ok, reason = _proof_ok(proof_rec)
        if not ok:
            return False, reason, ""

        max_day = int(_f(_cfg(degen, "intraday_top3_max_per_day", 5), 5))
        if _daily_count(state, now) >= max_day:
            return False, "daily cap (%d)" % max_day, ""

        left = _cooldown_left(state, str(mint), degen, now)
        if left > 0:
            return False, "cooldown %.1fh left" % (left / 3600.0), ""

        return True, "pick ok", label
    except Exception:
        logger.exception("intraday_top3 qualify failed for %s", mint)
        return False, "qualify-error", ""


def _stamp(state: Dict[str, Any], mint: str, now: float) -> None:
    """Stamp per-coin cooldown, bump the daily count, append the log."""
    try:
        day = _wat_day(now)
        if state.get(DAY_KEY) != day:
            state[DAY_KEY] = day
            state[COUNT_KEY] = 0
        state[COUNT_KEY] = int(state.get(COUNT_KEY) or 0) + 1
        cd = state.get(COOLDOWN_KEY)
        if not isinstance(cd, dict):
            cd = {}
            state[COOLDOWN_KEY] = cd
        cd[str(mint)] = now
    except Exception:
        logger.exception("intraday_top3 stamp failed for %s", mint)


def maybe_pick(cap_check: Optional[Callable[[], bool]],
               config: Any, state: Dict[str, Any],
               snap: Dict[str, Any],
               now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Event-driven Top 3 pick for one confirmed runner.

    ``snap`` carries the card-time score snapshot + safety context:
    mint, symbol, chain, sniper, winner, holder, smart_buys, fp, mcap,
    report (safety dict or None), unvetted (bool), proof_rec (optional —
    read from state["proof_checks"] when omitted).

    ``cap_check`` is a zero-arg callable for the 4/hour instant cap;
    a trip skips the pick WITHOUT burning the cooldown. Publishes
    ``launch.intraday_top3`` on success. Returns the pick dict, or
    None. Never raises. Never opens positions.
    """
    try:
        now = now if now is not None else time.time()
        degen = config.degen
        mint = str(snap.get("mint") or "")
        if not mint:
            return None

        proof_rec = snap.get("proof_rec")
        if proof_rec is None:
            proof_rec = (state.get("proof_checks") or {}).get(mint)

        ok, reason, safety_label = qualify(
            state, degen, mint, snap.get("report"),
            bool(snap.get("unvetted")), proof_rec, now=now)
        if not ok:
            logger.info("TOP3 SKIP %s %s: %s",
                        snap.get("symbol"), mint, reason)
            return None

        # 4/hour instant cap composes here — a trip skips this cycle
        # without stamping, so the next qualifying card can still pick.
        try:
            allowed = cap_check() if cap_check is not None else True
        except Exception:
            logger.exception("intraday_top3 cap check failed")
            allowed = True
        if not allowed:
            logger.warning("TOP3 cap-tripped %s %s -> skipped (no burn)",
                           snap.get("symbol"), mint)
            return None

        from daemon.launch_scanner import toppicks  # noqa: PLC0415
        score, why = toppicks.composite({
            "sniper": _f(snap.get("sniper")),
            "winner": _f(snap.get("winner")),
            "holder": _f(snap.get("holder")),
            "smart_buys": int(snap.get("smart_buys") or 0),
            "fp": str(snap.get("fp") or ""),
        })

        _stamp(state, mint, now)
        log = state.get(LOG_KEY)
        if not isinstance(log, list):
            log = []
            state[LOG_KEY] = log
        log.append({"mint": mint, "ts": now, "score": score,
                    "symbol": str(snap.get("symbol") or "")})
        del log[:-_MAX_LOG]

        from agent import events as event_bus  # noqa: PLC0415
        pick = {
            "event": "launch.intraday_top3",
            "mint": mint,
            "symbol": str(snap.get("symbol") or mint[:8]),
            "chain": str(snap.get("chain") or "solana"),
            "score": score,
            "why": "; ".join(why),
            "safety": safety_label,
            "proof": proof_line(proof_rec),
            "usd_market_cap": _f(snap.get("mcap")),
            "note": ("Intraday proof-gated pick — alert only, never "
                     "auto-traded. Paper-tracked, not financial advice."),
        }
        event_bus.publish(pick)
        logger.warning("TOP3 PICK %s %s score=%.1f safety=%s",
                       pick["symbol"], mint, score, safety_label)
        return pick
    except Exception:
        logger.exception("intraday_top3 maybe_pick failed")
        return None
