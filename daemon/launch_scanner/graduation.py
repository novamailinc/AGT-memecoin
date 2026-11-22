"""Graduation filter (Phase 5, user decision 2026-10-09 — judgment mode).

Rule: FULL-SIZE paper entries open ONLY on graduated coins. Scouts
(0.01 SOL, Phase 3) may still enter pre-graduation at tiny size — the
filter never blocks scouts.

"Graduated" = the bonding curve completed and the coin trades on a real
DEX pool. Modern pump.fun graduations land on PumpSwap (not Raydium).

Three-valued: True = graduated, False = known bonding-curve,
None = unknown. Unknown FAILS CLOSED for full-size entries — the
user-approved rule is "not proven graduated = no full-size entry".
A filter-code exception fails OPEN with a loud log, so a bug in this
module can never silently halt entries (safety gates stay fail-closed;
this is a judgment-mode profitability filter, not a safety gate).

Sources, first hit wins:
  1. Migration annotation — Phase 2 records pumpportal migration events
     on state["seen"][mint]["extra"]["migration"], and milestone
     records carry migration_pool. A direct graduation event → True.
  2. pump.fun "complete" flag — candidate extra["complete"] from the
     pump.fun frontend feed: True = curve complete → True;
     False = still on the curve → False.
  3. DexScreener venue — highest-liquidity pair dexId. DexScreener
     reports the pump.fun bonding curve as dexId "pump" (exact match —
     note "pumpswap" merely contains the substring) → False. Any
     other non-empty venue is a real DEX pool → True. Absent → None.
     Read from card["dex"] when the caller threads it through, else one
     defensive live lookup (fail-soft; skipped under unit tests).
  4. Otherwise → None.

Blocked full-size entries are journaled as ``graduation.blocked``
events (INFO — routine filter hold, never an alert) so the weekly
review can report the counter. Journaling is fail-soft and skipped
under unit tests (no test pollution of the live journal).

Never raises into the scanner loop.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("forex_agent.daemon.launch_scanner.graduation")

# DexScreener dexId for the pump.fun bonding curve (NOT graduated).
# Exact match only — "pumpswap" is a real DEX and merely contains "pump".
_BONDING_CURVE_DEXES = frozenset({"pump"})

_SENTINEL = "sentinel"  # _live_dex default = do the real lookup


def _cfg(degen: Any, name: str, default: Any) -> Any:
    try:
        v = getattr(degen, name, default)
        return default if v is None else v
    except Exception:
        return default


def enabled(degen: Any) -> bool:
    return bool(_cfg(degen, "graduation_filter_enabled", True))


def _migration_recorded(state: Dict[str, Any], mint: str) -> bool:
    """True when a graduation/migration event was recorded for the mint."""
    try:
        entry = (state.get("seen") or {}).get(str(mint))
        if isinstance(entry, dict):
            mig = (entry.get("extra") or {}).get("migration")
            if isinstance(mig, dict) and mig.get("pool"):
                return True
        # Milestone records also carry migration_pool (Phase 2).
        try:
            from . import milestones  # noqa: PLC0415 (lazy: init order)
            rec = (milestones.tracked(state) or {}).get(str(mint))
            if isinstance(rec, dict) and rec.get("migration_pool"):
                return True
        except Exception:
            pass
        return False
    except Exception:
        return False


def _live_dex_lookup(mint: str) -> str:
    """Defensive DexScreener venue lookup. Fail-soft → "" on any error."""
    try:
        from . import sources  # noqa: PLC0415 (lazy: init order)
        stats = sources.dexscreener_token_stats(str(mint))
        return str((stats or {}).get("dex") or "").strip().lower()
    except Exception:
        logger.exception("graduation dex lookup failed for %s", mint)
        return ""


def is_graduated(state: Dict[str, Any], mint: str,
                 card: Optional[Dict[str, Any]] = None,
                 _live_dex: Any = _SENTINEL) -> Optional[bool]:
    """Three-valued graduation check: True / False / None (unknown).

    ``_live_dex`` is a test injection: ``"sentinel"`` (default) performs
    the real DexScreener lookup when nothing else is known; a dict like
    ``{"dex": "pumpswap"}`` is used as the fake lookup result; None
    skips the lookup entirely. Never raises.
    """
    try:
        mint = str(mint or "")
        if not mint:
            return None
        # 1. Migration event — direct graduation evidence.
        if _migration_recorded(state, mint):
            return True
        # 2. pump.fun "complete" flag.
        extra = ((card or {}).get("extra") or {})
        if isinstance(extra, dict) and "complete" in extra:
            return bool(extra.get("complete"))
        # 3. DexScreener venue.
        dex = str((card or {}).get("dex") or "").strip().lower()
        if not dex:
            if _live_dex == _SENTINEL:
                dex = _live_dex_lookup(mint)
            elif isinstance(_live_dex, dict):
                dex = str(_live_dex.get("dex") or "").strip().lower()
        if not dex:
            return None
        if dex in _BONDING_CURVE_DEXES:
            return False
        return True  # any real DEX venue
    except Exception:
        logger.exception("graduation check failed for %s", mint)
        return None


def check(state: Dict[str, Any], degen: Any, mint: str,
          card: Optional[Dict[str, Any]] = None,
          tag: Optional[str] = None,
          _live_dex: Any = _SENTINEL) -> Tuple[bool, str, Optional[bool]]:
    """Entry gate. Returns (allow, reason, graduation_state).

    Scouts always pass (tiny size). Unknown fails CLOSED for full-size
    entries per the user-approved rule. Pure decision — no side
    effects; callers journal blocks via ``record_block``. Never raises.
    """
    try:
        if not enabled(degen):
            return True, "filter disabled", None
        if str(tag or "") == "scout":
            return True, "scout bypass (tiny size)", None
        g = is_graduated(state, mint, card, _live_dex=_live_dex)
        if g is True:
            return True, "graduated", True
        if g is False:
            return False, "bonding curve — not graduated", False
        return False, "graduation unknown — not proven graduated", None
    except Exception:
        logger.exception("graduation gate failed for %s", mint)
        return True, "check-error (fail-open)", None


def record_block(mint: str, symbol: str, tag: Optional[str],
                 grad_state: Optional[bool], reason: str) -> None:
    """Journal a ``graduation.blocked`` event. Fail-soft, never raises.

    Skipped under unit tests (FOREX_AGENT_TESTING=1) so the suite never
    pollutes the live journal.
    """
    try:
        import os
        if os.environ.get("FOREX_AGENT_TESTING") == "1":
            return
        from agent import events as event_bus  # noqa: PLC0415
        event_bus.publish({
            "event": "graduation.blocked",
            "mint": str(mint or ""),
            "symbol": str(symbol or ""),
            "tag": str(tag or ""),
            "graduation_state": ("true" if grad_state is True else
                                 "false" if grad_state is False else
                                 "unknown"),
            "reason": str(reason or "")[:128],
        })
    except Exception:
        logger.exception("graduation block journal failed for %s", mint)


def block_summary(db_path: str, days: float = 7.0,
                  now: Optional[float] = None) -> Dict[str, Any]:
    """Count graduation.blocked journal events. Measurement only."""
    try:
        now = now if now is not None else time.time()
        cutoff = datetime.fromtimestamp(now - days * 86400.0,
                                        tz=timezone.utc).isoformat()
        db = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        try:
            rows = db.execute(
                "select ts, payload from journal "
                "where kind = 'event' "
                "and payload like '%\"event\": \"graduation.blocked\"%' "
                "and ts >= ? "
                "order by rowid desc limit 2000", (cutoff,)).fetchall()
        finally:
            db.close()
    except Exception:
        logger.warning("graduation block summary read failed", exc_info=True)
        return {"days": days, "blocks": 0, "mints": []}
    mints: List[str] = []
    for _ts, blob in rows:
        try:
            e = json.loads(blob)
        except Exception:
            continue
        if "event" not in e and isinstance(e.get("payload"), dict):
            e = e["payload"]
        if str(e.get("event") or "") != "graduation.blocked":
            continue
        m = str(e.get("mint") or "")
        if m and m not in mints:
            mints.append(m)
    return {"days": days, "blocks": len(rows), "mints": mints[:20]}
