"""Thesis-break + plan-level watcher for 🎯 PICK cards (lifecycle cards).

Closes the loop on the card visual language (user decision 2026-10-09):
a PICK card states its invalidation line ("thesis dies if it loses the
X level") and its PLAN — this watcher tracks the picked coin's price and
emits a descriptive alert when the pick's OWN plan fires.

DISPLAY-LAYER ONLY: watches prices, publishes alert events. Never
trades, never gates, never touches entries/exits/sizing. Every alert
composes with the 4/hour instant cap (own rolling window, same knobs),
personal filters, and quiet hours via the normal journal fan-out.

Guards (user-approved):
  1. Confirmation, not hair-trigger: a level must hold broken across
     ``thesis_break_confirm_polls`` consecutive 60s price checks.
     A single wick resets the counter — no cry-wolf.
  2. Rate limit: confirmed breaks consume the 4/hour instant budget
     (same knobs as the scanner's cap). Excess breaks collapse into ONE
     ``launch.thesis_break_digest`` card — never a card storm.
  3. Honesty: the break references the actual invalidation level from
     the pick card (0.5x card mcap, the exact formula the renderer
     uses). A pick with no concrete card mcap is never watched.

Price source: the blink system's keyless DexScreener fetch
(``blink.fetch_stats``), sharing its 1s politeness gap. Fail-soft:
a failed fetch keeps the counters and retries next poll.

Runs inside the telegram daemon (which owns the 60s blink poll loop),
NOT the scanner. State lives in the telegram daemon's persisted state
under ``state["thesis_watch"]``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("forex_agent.daemon.telegram.thesis_watch")

POLL_S = 60.0              # price re-check cadence (matches blink)
MAX_WATCH = 20             # max concurrently watched picks (oldest evicted)
PENDING_MAX = 20           # cap-tripped breaks waiting for the digest
PENDING_TTL_S = 6 * 3600   # pending breaks older than this are dropped
DEFAULT_TTL_H = 24.0       # a pick is watched this long, then released

_DEGEN_CACHE: Dict[str, Any] = {"ts": 0.0, "degen": None}
_DEGEN_TTL_S = 300.0


def _cfg(degen: Any, name: str, default: Any) -> Any:
    try:
        v = getattr(degen, name, default)
        return default if v is None else v
    except Exception:
        return default


def _degen() -> Any:
    """DegenConfig for the watcher knobs (cached 5 min). None when the
    config can't load — every knob then falls back to its default."""
    now = time.time()
    if (_DEGEN_CACHE["degen"] is not None
            and now - _DEGEN_CACHE["ts"] < _DEGEN_TTL_S):
        return _DEGEN_CACHE["degen"]
    try:
        from config.config import load_config  # noqa: PLC0415
        d = load_config().degen
    except Exception:
        d = None
    _DEGEN_CACHE["ts"] = now
    _DEGEN_CACHE["degen"] = d
    return d


def _watch(state: Dict[str, Any]) -> Dict[str, Any]:
    w = state.get("thesis_watch")
    if not isinstance(w, dict):
        w = {}
        state["thesis_watch"] = w
    return w


def register_pick(state: Dict[str, Any], event: Dict[str, Any]) -> bool:
    """Track a freshly sent 🎯 PICK card for thesis-break watching.

    Called AFTER the pick card is successfully sent (same spot as
    blink.register). Only ``launch.intraday_top3`` picks carry the
    WATCH OUT invalidation line, so only they are watched (GUARD 3:
    no concrete level -> no watcher). Returns True when tracked.
    Never raises.
    """
    try:
        if str(event.get("event") or "") != "launch.intraday_top3":
            return False
        mint = str(event.get("mint") or "")
        try:
            mcap = float(event.get("usd_market_cap") or 0)
        except (TypeError, ValueError):
            mcap = 0.0
        if not mint or mcap <= 0:
            return False
        # Plan levels mirror exactly what the PICK card renders: the
        # per-coin plan when the event carries one, else the standing
        # two-tier policy (leg A stop -30% / TP +200%).
        plan = event.get("plan")
        if isinstance(plan, dict):
            try:
                stop_pct = float(plan.get("stop_pct") or 30.0)
            except (TypeError, ValueError):
                stop_pct = 30.0
            try:
                tp_pct = float(plan.get("tp_pct") or 200.0)
            except (TypeError, ValueError):
                tp_pct = 200.0
        else:
            stop_pct, tp_pct = 30.0, 200.0
        watch = _watch(state)
        while len(watch) >= MAX_WATCH:
            oldest = min(watch, key=lambda k: watch[k].get("ts", 0))
            del watch[oldest]
        watch[mint] = {
            "mint": mint,
            "symbol": str(event.get("symbol") or "?")[:16],
            "chain": str(event.get("chain") or "solana"),
            "card_mcap": mcap,
            # The exact invalidation formula the card renderer uses
            # (_invalidation_lines: "thesis dies if it loses the
            # 0.5x-card-mcap level").
            "invalidation_level": mcap * 0.5,
            "stop_level": mcap * (1.0 - stop_pct / 100.0),
            "tp_level": mcap * (1.0 + tp_pct / 100.0),
            "below": 0,        # consecutive polls below invalidation
            "at_tp": 0,        # consecutive polls at/above leg-A TP
            "at_stop": 0,      # consecutive polls at/below stop
            "fired_break": False,
            "fired_tp": False,
            "fired_stop": False,
            "ts": time.time(),
        }
        logger.info("thesis_watch: tracking %s (invalidation %s)",
                    watch[mint]["symbol"], watch[mint]["invalidation_level"])
        return True
    except Exception:
        logger.exception("thesis_watch register_pick crashed")
        return False


def _window_ok(state: Dict[str, Any], degen: Any, now: float) -> bool:
    """The 4/hour instant budget, same knobs as the scanner's cap.

    The telegram daemon keeps its own rolling window (per-daemon state
    can't share the scanner's) with identical semantics: max N sends
    per window. A trip returns False WITHOUT consuming a slot.
    """
    try:
        max_n = int(_cfg(degen, "early_alert_max_per_window", 4) or 4)
        window_s = float(
            _cfg(degen, "early_alert_window_seconds", 3600) or 3600)
    except (TypeError, ValueError):
        max_n, window_s = 4, 3600.0
    w = state.get("thesis_break_window")
    if (not isinstance(w, dict)
            or now - float(w.get("start", 0) or 0) >= window_s):
        state["thesis_break_window"] = {"start": now, "count": 1}
        return True
    if int(w.get("count", 0) or 0) >= max(1, max_n):
        return False
    w["count"] = int(w.get("count", 0) or 0) + 1
    return True


def _pending(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    p = state.get("thesis_break_pending")
    if not isinstance(p, list):
        p = []
        state["thesis_break_pending"] = p
    return p


def _queue_pending(state: Dict[str, Any], item: Dict[str, Any],
                  now: float) -> None:
    """Cap-tripped break: hold for the collapsed digest, never dropped
    silently (entries expire after PENDING_TTL_S)."""
    pend = _pending(state)
    item = dict(item)
    item["ts"] = now
    pend.append(item)
    # Drop stale first, then bound.
    fresh = [p for p in pend if now - float(p.get("ts", now) or now)
             <= PENDING_TTL_S]
    del fresh[:-PENDING_MAX]
    state["thesis_break_pending"] = fresh


def _flush_pending(state: Dict[str, Any],
                   publish: Callable[..., Dict[str, Any]],
                   degen: Any, now: float) -> bool:
    """Publish ONE digest card for cap-tripped breaks when the window
    has room. Returns True when a digest went out. Never raises."""
    try:
        pend = [p for p in _pending(state)
                if now - float(p.get("ts", now) or now) <= PENDING_TTL_S]
        if not pend:
            state["thesis_break_pending"] = []
            return False
        if not _window_ok(state, degen, now):
            state["thesis_break_pending"] = pend[-PENDING_MAX:]
            return False
        items = [{
            "symbol": p.get("symbol", "?"),
            "mint": p.get("mint", ""),
            "label": p.get("label", "?"),
        } for p in pend]
        publish({
            "event": "launch.thesis_break_digest",
            "count": len(items),
            "items": items,
            "note": ("Cap-collapsed: %d thesis breaks in one card — "
                     "the 4/hour instant budget held the rest." % len(items)),
        }, severity="WARNING")
        state["thesis_break_pending"] = []
        logger.warning("thesis_watch: digest flushed (%d breaks)", len(items))
        return True
    except Exception:
        logger.exception("thesis_watch _flush_pending crashed")
        return False


def _fire_break(state: Dict[str, Any],
                publish: Callable[..., Dict[str, Any]],
                degen: Any, w: Dict[str, Any], mcap: float,
                now: float) -> None:
    """Emit (or queue) the thesis-break alert for one watched pick."""
    try:
        card_mcap = float(w.get("card_mcap") or 0) or 1.0
        event = {
            "event": "launch.thesis_break",
            "symbol": w.get("symbol", "?"),
            "mint": w.get("mint", ""),
            "chain": w.get("chain") or "solana",
            "card_mcap": w.get("card_mcap"),
            "break_mcap": mcap,
            "break_pct": (mcap - card_mcap) / card_mcap * 100.0,
            "invalidation_level": w.get("invalidation_level"),
            "plan_text": "exit — the thesis this pick was built on has failed",
            "minutes_since_pick": round((now - float(w.get("ts", now) or now)) / 60.0, 1),
            "polls_below": int(w.get("below", 0) or 0),
            "note": "Descriptive: the pick's own WATCH OUT line firing. Not financial advice.",
        }
        if _window_ok(state, degen, now):
            publish(event, severity="WARNING")
            logger.warning("THESIS BREAK %s @ %s", w.get("symbol"), mcap)
        else:
            _queue_pending(state, {
                "symbol": w.get("symbol", "?"),
                "mint": w.get("mint", ""),
                "label": "thesis broken (invalidation failed)",
            }, now)
            logger.warning("THESIS BREAK %s cap-tripped -> pending digest",
                           w.get("symbol"))
    except Exception:
        logger.exception("thesis_watch _fire_break crashed")


def _fire_plan_level(state: Dict[str, Any],
                     publish: Callable[..., Dict[str, Any]],
                     degen: Any, w: Dict[str, Any], mcap: float,
                     level_kind: str, now: float) -> None:
    """Emit (or queue) a plan-level-hit alert: leg-A TP or stop."""
    try:
        if level_kind == "leg_a_tp":
            level = float(w.get("tp_level") or 0)
            plan_text = "bank leg A (+200%); leg B runner keeps riding per plan"
        else:
            level = float(w.get("stop_level") or 0)
            plan_text = "cut — stop -30% hit, exit per plan"
        card_mcap = float(w.get("card_mcap") or 0) or 1.0
        event = {
            "event": "launch.plan_level_hit",
            "symbol": w.get("symbol", "?"),
            "mint": w.get("mint", ""),
            "chain": w.get("chain") or "solana",
            "level_kind": level_kind,
            "card_mcap": w.get("card_mcap"),
            "level_mcap": level,
            "mcap_now": mcap,
            "plan_text": plan_text,
            "minutes_since_pick": round((now - float(w.get("ts", now) or now)) / 60.0, 1),
            "polls_at_level": int(w.get("at_tp" if level_kind == "leg_a_tp" else "at_stop", 0) or 0),
            "note": "Descriptive: the pick's own PLAN level firing. Not financial advice.",
        }
        _ = card_mcap
        if _window_ok(state, degen, now):
            publish(event, severity="NOTICE")
            logger.warning("PLAN LEVEL %s %s @ %s",
                           level_kind, w.get("symbol"), mcap)
        else:
            _queue_pending(state, {
                "symbol": w.get("symbol", "?"),
                "mint": w.get("mint", ""),
                "label": "plan level hit (%s)" % level_kind,
            }, now)
    except Exception:
        logger.exception("thesis_watch _fire_plan_level crashed")


def _evaluate(w: Dict[str, Any], mcap: float, confirm: int,
              plan_levels: bool) -> List[str]:
    """Pure counter update for one price poll. Returns the list of
    levels that newly confirmed this poll ("break" / "leg_a_tp" /
    "stop"). A wick back across a level resets that level's counter —
    confirmation, never hair-trigger. Never raises."""
    fired: List[str] = []
    try:
        if not w.get("fired_break"):
            if mcap < float(w.get("invalidation_level") or 0):
                w["below"] = int(w.get("below", 0) or 0) + 1
            else:
                w["below"] = 0
            if w["below"] >= confirm:
                w["fired_break"] = True
                fired.append("break")
        if plan_levels:
            if not w.get("fired_tp"):
                if mcap >= float(w.get("tp_level") or float("inf")):
                    w["at_tp"] = int(w.get("at_tp", 0) or 0) + 1
                else:
                    w["at_tp"] = 0
                if w["at_tp"] >= confirm:
                    w["fired_tp"] = True
                    fired.append("leg_a_tp")
            if not w.get("fired_stop"):
                if mcap < float(w.get("stop_level") or 0):
                    w["at_stop"] = int(w.get("at_stop", 0) or 0) + 1
                else:
                    w["at_stop"] = 0
                if w["at_stop"] >= confirm:
                    w["fired_stop"] = True
                    fired.append("stop")
    except Exception:
        logger.exception("thesis_watch _evaluate crashed")
    return fired


def maybe_check(state: Dict[str, Any],
                publish: Callable[..., Dict[str, Any]],
                degen: Any = None,
                now: Optional[float] = None) -> Dict[str, int]:
    """One watch cycle: re-price watched picks, fire on confirmation.

    Throttled to POLL_S. Fail-soft per coin; never raises. Returns
    counters for observability.
    """
    now = now if now is not None else time.time()
    done = {"checked": 0, "fired": 0, "dropped": 0}
    try:
        if degen is None:
            degen = _degen()
        if not _cfg(degen, "thesis_break_enabled", True):
            return done
        watch = _watch(state)
        if not watch:
            _flush_pending(state, publish, degen, now)
            return done
        last = float(state.get("thesis_watch_last") or 0)
        if now - last < POLL_S:
            return done
        state["thesis_watch_last"] = now
        try:
            confirm = int(_cfg(degen, "thesis_break_confirm_polls", 3) or 3)
        except (TypeError, ValueError):
            confirm = 3
        confirm = max(1, confirm)
        plan_levels = bool(_cfg(degen, "thesis_break_plan_levels_enabled",
                                True))
        try:
            ttl_h = float(_cfg(degen, "thesis_break_ttl_hours",
                               DEFAULT_TTL_H) or DEFAULT_TTL_H)
        except (TypeError, ValueError):
            ttl_h = DEFAULT_TTL_H
        from daemon.telegram import blink  # noqa: PLC0415 (lazy: no import cycle)
        for mint in list(watch.keys()):
            w = watch[mint]
            try:
                if now - float(w.get("ts", now) or now) > ttl_h * 3600.0:
                    watch.pop(mint, None)
                    done["dropped"] += 1
                    continue
                mcap, _, _ = blink.fetch_stats(
                    w.get("mint", ""), w.get("chain") or "solana")
                if mcap <= 0:
                    continue  # price failed: keep counters, retry next poll
                done["checked"] += 1
                for kind in _evaluate(w, mcap, confirm, plan_levels):
                    done["fired"] += 1
                    if kind == "break":
                        _fire_break(state, publish, degen, w, mcap, now)
                    else:
                        _fire_plan_level(state, publish, degen, w, mcap,
                                         kind, now)
                # Lifecycle: thesis dead or fully exited -> release the
                # coin. A banked leg-A TP keeps the runner watched.
                if w.get("fired_break") or w.get("fired_stop"):
                    watch.pop(mint, None)
                    done["dropped"] += 1
            except Exception:
                logger.exception("thesis_watch coin check crashed for %s",
                                 w.get("symbol"))
                continue
        _flush_pending(state, publish, degen, now)
    except Exception:
        logger.exception("thesis_watch maybe_check crashed")
    return done
