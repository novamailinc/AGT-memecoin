"""Scout engine (Phase 3, user decision 2026-10-09 — judgment mode).

Tiny paper entries (default 0.01 SOL) on proof-passing EARLY coins
(pre-confirmation), so the bot is already inside when a runner
teleports. A scout that later chart-confirms enters full size through
the existing launch pipeline; the ``scout`` tag keeps the books
separate so scout performance is measurable on its own.

Judgment-mode qualification (the Friday verdict earned no gates):
  (a) safety score >= 1/100 with no hard-gate failures — the same bar
      as the early card that triggers the scout (re-checked here,
      defense in depth);
  (b) the holder-concentration proof check must not FAIL — a fail
      blocks the scout, "unknown" is allowed at scout size (tiny stake
      = tiny risk). The LP check is dead (0% coverage, ignored); the
      distribution check is WATCH-only (never fires, not gated on).

Controls (all ``degen.*`` knobs):
  scout_enabled, scout_size_sol (0.01), scout_max_open (10),
  scout_daily_loss_budget_sol (0.05 — halts NEW scouts for the day),
  scout_cooldown_hours (6.0, per-coin).

Kill criterion (the math is the boss): with 0.01 SOL scouts and a -30%
stop, the ~2% that run must average ~14.5x gross for breakeven.
``weekly_summary()`` reports count / closes / avg winner multiple
from the journal + fills — measurement only, no auto-kill.

Safety: scouts ride the PROVEN exit machinery (two-tier + profit-lock
v2) via ``_paper_enter`` with ``tag="scout"``, respect the broker kill
switch (``submit_order`` raises when engaged), and compose with
``max_positions`` and the 4/hour instant cap (scouts only fire after a
published early card — they can never exceed the alert rate).

Never raises into the scanner loop. Pure logic + cheap local reads.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("forex_agent.daemon.launch_scanner.scouts")

STATE_COOLDOWN = "scout_cooldown"   # mint -> last scout entry ts
STATE_LOG = "scout_log"             # bounded list of scout entry records
_MAX_LOG = 200

_CANT_EXIT_MARKERS = ("honeypot", "cannot sell", "can't sell", "sell tax")


def _cfg(degen: Any, name: str, default: Any) -> Any:
    try:
        v = getattr(degen, name, default)
        return default if v is None else v
    except Exception:
        return default


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN -> default


def enabled(degen: Any) -> bool:
    return bool(_cfg(degen, "scout_enabled", True))


def open_scout_count(positions: Optional[Dict[str, Any]]) -> int:
    """Base positions currently open with the scout tag (leg pairs = 1)."""
    try:
        # Lazy: this module is imported by the package __init__.
        from . import LEG_B_SUFFIX  # noqa: PLC0415
    except Exception:
        LEG_B_SUFFIX = "|B"
    try:
        seen = set()
        for key, pos in (positions or {}).items():
            if not isinstance(pos, dict):
                continue
            if str(pos.get("label") or "") != "scout":
                continue
            base = (key[:-len(LEG_B_SUFFIX)] if key.endswith(LEG_B_SUFFIX)
                    else key)
            seen.add(base)
        return len(seen)
    except Exception:
        return 0


def _safety_ok(report: Optional[Dict[str, Any]], degen: Any) -> Tuple[bool, str]:
    """Defense-in-depth re-check of the early-card safety bar.

    The scout only ever fires after a published ``launch.early`` card,
    which already passed ``_instant_verdict`` — this re-check guards
    the scout path against future refactors that move the hook.
    """
    try:
        rep = report or {}
        if rep.get("unknown"):
            return False, "safety-unknown"
        fails = [str(f) for f in (rep.get("fails") or [])]
        if fails:
            return False, "hard-fail: %s" % fails[0][:48]
        # Can't-exit coins are never scouted, whatever the score.
        low = " ".join(fails).lower()
        if any(m in low for m in _CANT_EXIT_MARKERS):
            return False, "can't-exit"
        score = _f(rep.get("score"), 0.0)
        floor = _f(_cfg(degen, "early_min_score", 1.0), 1.0)
        if score < floor:
            return False, "score %.0f < %.0f" % (score, floor)
        return True, "safety ok %.0f" % score
    except Exception:
        return False, "safety-check-error"


def proof_snapshot(rec: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Compact proof record for the position + scout log."""
    try:
        if not isinstance(rec, dict):
            return {"lp": "?", "holders": "?", "distribution": "?"}
        get = lambda k: str(((rec.get(k) or {}).get("verdict")) or "?")
        return {"lp": get("lp"), "holders": get("holders"),
                "distribution": get("distribution")}
    except Exception:
        return {"lp": "?", "holders": "?", "distribution": "?"}


def qualify(state: Dict[str, Any], degen: Any, mint: str, chain: str,
            report: Optional[Dict[str, Any]] = None,
            now: Optional[float] = None,
            _proof_rec: Optional[Dict[str, Any]] = "sentinel"
            ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Decide whether a coin may take a scout entry right now.

    Returns (ok, reason, proof_record). ``_proof_rec`` is a test-only
    injection (``"sentinel"`` = run the real check); production always
    runs the real check. Never raises.
    """
    try:
        now = now if now is not None else time.time()
        if not enabled(degen):
            return False, "scout disabled", None
        if not mint:
            return False, "no mint", None

        ok, why = _safety_ok(report, degen)
        if not ok:
            return False, why, None

        # Proof gate (judgment mode): holder-concentration FAIL blocks;
        # unknown is allowed at scout size. LP ignored (dead), dist
        # ignored (WATCH-only, never fires).
        if _proof_rec == "sentinel":
            try:
                from . import proof  # noqa: PLC0415 (lazy: init order)
                rec = proof.measure(state, degen, str(mint),
                                    chain or "solana", now=now)
            except Exception:
                logger.exception("scout proof measure failed for %s", mint)
                rec = None
        else:
            rec = _proof_rec
        holders = ((rec or {}).get("holders") or {}).get("verdict")
        if holders == "fail":
            detail = ((rec or {}).get("holders") or {}).get("detail") or ""
            return False, "holders FAIL: %s" % detail[:64], rec

        # Per-coin cooldown.
        cd = state.get(STATE_COOLDOWN) or {}
        try:
            last = float(cd.get(str(mint)) or 0)
        except (TypeError, ValueError):
            last = 0.0
        cd_h = _f(_cfg(degen, "scout_cooldown_hours", 6.0), 6.0)
        if cd_h > 0 and last > 0 and now - last < cd_h * 3600:
            return False, "cooldown %.1fh left" % (
                (cd_h * 3600 - (now - last)) / 3600.0), rec

        # Concurrent-open cap.
        max_open = int(_f(_cfg(degen, "scout_max_open", 10), 10))
        if open_scout_count(state.get("degen_positions")) >= max_open:
            return False, "max open scouts (%d)" % max_open, rec

        # Daily scout loss budget — halts NEW scouts, never closes.
        budget = _f(_cfg(degen, "scout_daily_loss_budget_sol", 0.05), 0.05)
        if budget > 0:
            try:
                from storage.store import DEFAULT_DB_PATH  # noqa: PLC0415
                pnl = daily_scout_pnl_sol(str(DEFAULT_DB_PATH), now)
                if pnl <= -budget:
                    return False, "daily scout budget hit (%+.4f)" % pnl, rec
            except Exception:
                logger.exception("scout budget read failed; allowing")

        return True, "scout ok (%s)" % proof_snapshot(rec), rec
    except Exception:
        logger.exception("scout qualify failed for %s", mint)
        return False, "qualify-error", None


def record_scout(state: Dict[str, Any], mint: str,
                 proof_rec: Optional[Dict[str, Any]],
                 size_sol: float, now: Optional[float] = None) -> None:
    """Stamp the per-coin cooldown and append the bounded scout log."""
    try:
        now = now if now is not None else time.time()
        cd = state.get(STATE_COOLDOWN)
        if not isinstance(cd, dict):
            cd = {}
            state[STATE_COOLDOWN] = cd
        cd[str(mint)] = now
        log = state.get(STATE_LOG)
        if not isinstance(log, list):
            log = []
            state[STATE_LOG] = log
        log.append({"mint": str(mint), "ts": now,
                    "size_sol": float(size_sol),
                    "proof": proof_snapshot(proof_rec)})
        del log[:-_MAX_LOG]
    except Exception:
        logger.exception("scout record failed for %s", mint)


def _journal_closes(db_path: str, since_ts: float
                    ) -> List[Dict[str, Any]]:
    """position.closed journal rows for scout-tagged tickets since ts.

    Reads the persistent delivery journal (``journal``, kind='event'),
    NOT the drainable ``event_queue`` — the health monitor deletes
    queue rows as it syncs them to the Worker, so the queue can go
    blind silently and must never be the record of close events.
    Journal rows wrap the event in
    ``{'kind': 'event', 'payload': {...}}``; legacy rows carry the
    event flat — both shapes are handled.
    """
    out: List[Dict[str, Any]] = []
    try:
        db = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        try:
            rows = db.execute(
                "select ts, payload from journal "
                "where kind = 'event' "
                "and payload like '%\"event\": \"position.closed\"%' "
                "order by rowid desc limit 5000").fetchall()
        finally:
            db.close()
    except Exception:
        logger.warning("scout journal read failed", exc_info=True)
        return []
    for ts, blob in rows:
        try:
            e = json.loads(blob)
        except Exception:
            continue
        if "event" not in e and isinstance(e.get("payload"), dict):
            e = e["payload"]
        if str(e.get("event") or "") != "position.closed":
            continue
        if str(e.get("label") or "") != "scout":
            continue
        try:
            ets = datetime.fromisoformat(str(ts)).timestamp()
        except Exception:
            continue
        if ets < since_ts:
            continue
        e["_ts"] = ets
        out.append(e)
    return out


def daily_scout_pnl_sol(db_path: str, now: Optional[float] = None) -> float:
    """Realized P&L (SOL) of scout closes since UTC midnight. <= 0."""
    try:
        now = now if now is not None else time.time()
        day_start = datetime.fromtimestamp(now, tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        closes = _journal_closes(db_path, day_start)
        return sum(_f(c.get("profit")) for c in closes)
    except Exception:
        logger.exception("daily scout pnl read failed")
        return 0.0


def weekly_summary(db_path: str, fills_path: str,
                   days: float = 7.0,
                   now: Optional[float] = None) -> Dict[str, Any]:
    """Lightweight scout ledger: closes, winners, avg winner multiple.

    Measurement only — no auto-kill. Multiples come from fills.jsonl
    (CLOSE price / BUY price per ticket); tickets without both sides
    count in closes/pnl but not in the multiple average.
    """
    try:
        now = now if now is not None else time.time()
        cutoff = now - days * 86400.0
        closes = _journal_closes(db_path, cutoff)
        # Ticket -> (buy_price, close_price) from the fills journal.
        buys: Dict[Any, float] = {}
        sells: Dict[Any, float] = {}
        try:
            with open(fills_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    t, k = r.get("ticket"), str(r.get("kind") or "")
                    px = _f(r.get("price_sol"))
                    if t is None or px <= 0:
                        continue
                    if k == "BUY":
                        buys.setdefault(t, px)
                    elif k == "CLOSE":
                        sells[t] = px
        except FileNotFoundError:
            pass
        mults: List[float] = []
        tick_mult: Dict[Any, float] = {}
        for c in closes:
            t = c.get("ticket")
            b, s = buys.get(t), sells.get(t)
            if b and s and b > 0:
                m = s / b
                mults.append(m)
                tick_mult[t] = m
        winners = [c for c in closes if _f(c.get("profit")) > 0]
        win_mults = [tick_mult[c.get("ticket")] for c in winners
                     if c.get("ticket") in tick_mult]
        pnl = sum(_f(c.get("profit")) for c in closes)
        return {
            "days": days,
            "closes": len(closes),
            "wins": len(winners),
            "losses": sum(1 for c in closes if _f(c.get("profit")) < 0),
            "pnl_sol": round(pnl, 6),
            "avg_winner_mult": (round(sum(win_mults) / len(win_mults), 3)
                                if win_mults else None),
            "avg_mult": (round(sum(mults) / len(mults), 3)
                         if mults else None),
            # Kill-criterion input: winners must average ~14.5x gross.
            "kill_bar_mult": 14.5,
        }
    except Exception:
        logger.exception("scout weekly summary failed")
        return {"days": days, "closes": 0, "wins": 0, "losses": 0,
                "pnl_sol": 0.0, "avg_winner_mult": None, "avg_mult": None,
                "kill_bar_mult": 14.5}
