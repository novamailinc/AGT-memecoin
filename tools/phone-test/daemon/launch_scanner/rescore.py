"""Sniper re-score: give quality-passed launches a fair trial at +5/+15 min.

Root cause (2026-10-09, QUBIT post-mortem): the sniper rank runs ONCE, one
second after birth, when every signal it reads (fingerprint composite,
smart-wallet buys, rising polls, buy pressure) is structurally zero — so
score 0.0 < 50.0 is guaranteed and every fresh launch fails the tier-1 bar
by construction. The re-score re-runs ``_sniper_rank`` on LIVE signals at
+5 and +15 minutes, when there is actually something to score.

2026-10-21 extensions: offsets now run to +30/+60 min (late runners like
DACITY peaked ~+40 min, outside the old window), and two routings added:
  * tier-1 promotions also publish to the signal bus (scanner cards
    never reached it, so promotions fed no other lane).
  * Mid-zone feed: score >= sniper_feed_min_score (default 40) with the
    standing runner rule verified on the fresh tape ($25k liq / $50k
    mcap / positive 5m momentum) + the same safety re-gate → card +
    bus publish (ledger outcome "promoted-feed").

Routing at each re-score (same bar, fairer trial — never a lower bar):
  * score >= sniper_tier_threshold → fresh ``_instant_verdict`` re-gate
    (safety can only have gotten worse, never assumed same). Pass →
    ``_sniper_stage``: tier-1 card through the normal 4/hour cap.
    Fail → digest queue (it was quality-passed at birth; still WATCH-worthy).
  * score >= sniper_feed_min_score + runner rule + re-gate → feed card.
  * score >= sniper_digest_min_score (default 25.0) → digest queue.
  * below → honest drop (log + measurement record). No fake queue.

Registry (``state["sniper_rescore"]``) holds a COMPACT payload per mint —
never the full candidate dicts (they are not JSON-stable and would bloat
the state blob). Restart handling: entries older than the last offset are
pruned; entries inside the window resume with their fired flags intact.

Every re-score outcome lands in ``state["sniper_rescore_ledger"]``
(bounded 500, 7-day TTL) — the Oct 30 verdict judges whether re-scores
earn their keep. Both keys ride the daemon-state write-through (see
run_once's finally block), so the registry survives restarts.

Never raises; never trades; never touches the safety floor (the 1/100 bar
is re-gated at every promotion, the 4/hour cap is enforced downstream by
``_flush_sniper_pending``).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("forex_agent.daemon.launch_scanner.rescore")

REG_KEY = "sniper_rescore"
LEDGER_KEY = "sniper_rescore_ledger"

_DEFAULT_OFFSETS = (300.0, 900.0, 1800.0, 3600.0)  # +5/+15/+30/+60 min
_DEFAULT_DIGEST_MIN = 25.0
_DEFAULT_FEED_MIN = 40.0            # mid-zone promotion bar (2026-10-21)
_FEED_MIN_LIQ_USD = 25_000.0        # standing runner rule, feed-enforced
_FEED_MIN_MCAP_USD = 50_000.0       # standing runner rule, feed-enforced
_MAX_REGISTRY = 200
_MAX_AGE_S = 3900.0                 # entries die past max offset + slack
_LEDGER_MAX = 500
_LEDGER_TTL_S = 7 * 24 * 3600


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default


def enabled(degen: Any) -> bool:
    return bool(getattr(degen, "sniper_rescore_enabled", True))


def offsets(degen: Any) -> List[float]:
    raw = getattr(degen, "sniper_rescore_at_s", None)
    out: List[float] = []
    if isinstance(raw, (list, tuple)):
        for v in raw:
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv > 0:
                out.append(fv)
    if not out:
        out = list(_DEFAULT_OFFSETS)
    return sorted(set(out))


def digest_min_score(degen: Any) -> float:
    return _f(getattr(degen, "sniper_digest_min_score",
                      _DEFAULT_DIGEST_MIN), _DEFAULT_DIGEST_MIN)


def feed_enabled(degen: Any) -> bool:
    return bool(getattr(degen, "sniper_feed_enabled", True))


def feed_min_score(degen: Any) -> float:
    return _f(getattr(degen, "sniper_feed_min_score",
                      _DEFAULT_FEED_MIN), _DEFAULT_FEED_MIN)


def _publish_feed_signal(payload: Dict[str, Any], mint: str, score: float,
                         tier: str, mcap: float, tape_stats: Any,
                         at_s: float, first_ts: float, now: float,
                         kind: str) -> bool:
    """Publish a rescore promotion to the signal bus (2026-10-21).

    Scanner cards never reached the bus — only the feed scripts publish
    there — so even a tier-1 promotion fed nobody downstream (CEO tails
    the bus; the mirror hooks card fires). Promotions now publish with
    source ``rescore-tier1`` / ``rescore-feed`` so the lanes see them.
    Never raises; returns whether the signal landed.
    """
    try:
        try:
            from . import signal_bus  # noqa: PLC0415 (lazy: init order)
        except Exception:
            import signal_bus  # type: ignore  # noqa: PLC0415
        st = tape_stats or {}
        sig = signal_bus.make_signal(
            chain=str(payload.get("chain") or "solana"),
            source="rescore-" + kind,
            mint=mint,
            symbol=str(payload.get("symbol") or ""),
            score=float(score or 0),
            tier=tier,
            age_min=((now - first_ts) / 60.0) if first_ts else 0.0,
            mcap=float(mcap or 0),
            liquidity=_f(st.get("usd_liquidity")),
            buys=int(_f(st.get("buys_m5"))),
            sells=int(_f(st.get("sells_m5"))),
            evidence={"rescore_at_s": float(at_s), "promoted": kind},
            extra={"promoted_from": "early-digest"},
        )
        return bool(signal_bus.publish(sig))
    except Exception:
        logger.warning("rescore feed publish failed for %s", mint,
                       exc_info=True)
        return False


def _registry(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    r = state.get(REG_KEY)
    if not isinstance(r, dict):
        r = {}
        state[REG_KEY] = r
    return r


def register(state: Dict[str, Any], degen: Any, now: float, mint: str,
             payload: Dict[str, Any]) -> bool:
    """Register a quality-passed sub-threshold candidate for re-score.

    ``payload`` is the compact rebuild kit (see _compact_payload).
    Idempotent per mint; bounded (oldest-first eviction). Never raises.
    """
    try:
        if not enabled(degen) or not mint or not isinstance(payload, dict):
            return False
        reg = _registry(state)
        if mint in reg:
            return True
        if len(reg) >= _MAX_REGISTRY:
            oldest = sorted(reg.items(),
                            key=lambda kv: _f(kv[1].get("first_ts")))
            for old_mint, _ in oldest[:len(reg) - _MAX_REGISTRY + 1]:
                reg.pop(old_mint, None)
        reg[mint] = {"first_ts": now, "fired": [],
                     "payload": dict(payload)}
        logger.warning("RESCORE registered %s (+%s)",
                       payload.get("symbol") or mint[:8], mint)
        return True
    except Exception:
        logger.exception("rescore register failed for %s", mint)
        return False


def _compact_payload(c: Dict[str, Any], new_cand: Dict[str, Any],
                     entry: Optional[Dict[str, Any]], mcap: float,
                     tier: str, reason: str,
                     report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Minimal rebuild kit for _sniper_rank / _sniper_stage /
    _enqueue_early_digest / _instant_verdict at re-score time."""
    hist = []
    try:
        for pt in ((entry or {}).get("mcap") or [])[-12:]:
            hist.append([float(pt[0]), float(pt[1])])
    except (TypeError, ValueError, IndexError):
        pass
    return {
        "mint": c.get("mint"),
        "symbol": c.get("symbol") or "",
        "name": c.get("name") or "",
        "chain": c.get("chain") or "",
        "url": c.get("url") or "",
        "source": c.get("source") or "",
        "tier": tier,
        "reason": reason,
        "safety_score": (report or {}).get("score"),
        "mcap_birth": _f(mcap),
        "mcap_hist": hist,
        "first_ts": _f((entry or {}).get("first_ts")),
        "narratives": list(new_cand.get("narratives") or ["other"]),
        "narrative_heat": _f(new_cand.get("narrative_heat"), 1.0),
        "sentiment": (new_cand.get("sentiment") or {}).get("verdict"),
    }


def _rebuild(payload: Dict[str, Any]
             ) -> tuple:
    """Rebuild (c, new_cand, entry) from the compact payload."""
    c = {
        "mint": payload.get("mint"),
        "symbol": payload.get("symbol") or "",
        "name": payload.get("name") or "",
        "chain": payload.get("chain") or "",
        "url": payload.get("url") or "",
        "source": payload.get("source") or "",
    }
    new_cand = {
        "narratives": payload.get("narratives") or ["other"],
        "narrative_heat": payload.get("narrative_heat") or 1.0,
        "sentiment": ({"verdict": payload.get("sentiment")}
                      if payload.get("sentiment") else {}),
    }
    entry = {
        "first_ts": payload.get("first_ts") or 0.0,
        "mcap": [list(pt) for pt in (payload.get("mcap_hist") or [])],
    }
    return c, new_cand, entry


def _fresh_stats(scanner: Any, state: Dict[str, Any], degen: Any,
                 mint: str, chain: str, now: float):
    """Freshest (mcap, tape_stats) for a re-score. Zero new HTTP when
    possible.

    1. Fingerprint tracker's in-memory observation series (free) —
       mcap from the last observation; tape stats synthesized from its
       m5 fields (no h1 windows in the tracker tuple).
    2. Single tape fetch (bounded: one call per re-score — DexScreener
       with the GeckoTerminal fallback, sources.tape_batch_stats) —
       full stats dict for the tape fallback.
    (0.0, None) when neither has data — the re-score still runs (rank
    needs no mcap), only card/anchor emission waits for a real number.
    """
    try:
        tr = getattr(scanner, "_fp_tracker", None)
        obs = tr.observations(mint) if tr else []
        if obs and now - float(obs[-1][0]) < 600:
            last = obs[-1]
            tape = {"buys_m5": last[4], "sells_m5": last[5],
                    "vol_m5": last[3], "usd_market_cap": last[1],
                    "price_usd": last[2], "usd_liquidity": last[6]}
            return _f(last[1]), tape
    except Exception:
        pass
    try:
        from . import sources  # noqa: PLC0415 (lazy: init order)
        stats = sources.tape_batch_stats([mint], chain=chain or
                                         "solana")
        st = stats.get(mint) or {}
        return _f(st.get("usd_market_cap")), (st or None)
    except Exception:
        logger.warning("rescore fresh-stats fetch failed for %s", mint,
                       exc_info=True)
        return 0.0, None


def _fresh_mcap(scanner: Any, state: Dict[str, Any], degen: Any,
                mint: str, chain: str, now: float) -> float:
    """Back-compat wrapper: freshest mcap only (see _fresh_stats)."""
    mcap, _tape = _fresh_stats(scanner, state, degen, mint, chain, now)
    return mcap


def _record_ledger(state: Dict[str, Any], mint: str, at_s: float,
                   score: float, outcome: str, now: float) -> None:
    try:
        led = state.get(LEDGER_KEY)
        if not isinstance(led, dict):
            led = {}
            state[LEDGER_KEY] = led
        led[mint] = {"at_s": at_s, "score": round(float(score), 1),
                     "outcome": outcome, "ts": now}
        if len(led) > _LEDGER_MAX:
            for old in sorted(led, key=lambda k: _f(led[k].get("ts"))
                              )[:len(led) - _LEDGER_MAX]:
                led.pop(old, None)
        cutoff = now - _LEDGER_TTL_S
        for m in [m for m, r in led.items()
                  if _f(r.get("ts")) < cutoff]:
            led.pop(m, None)
    except Exception:
        logger.exception("rescore ledger record failed for %s", mint)


def poll(scanner: Any, config: Any, state: Dict[str, Any],
         now: float) -> None:
    """Fire due re-scores. Called once per scan cycle. Never raises."""
    try:
        degen = config.degen
        if not enabled(degen):
            return
        reg = _registry(state)
        if not reg:
            return
        offs = offsets(degen)
        for mint in list(reg.keys()):
            rec = reg.get(mint)
            if not isinstance(rec, dict):
                reg.pop(mint, None)
                continue
            first = _f(rec.get("first_ts"))
            age = now - first
            if age > _MAX_AGE_S:
                reg.pop(mint, None)
                continue
            fired = rec.get("fired")
            if not isinstance(fired, list):
                fired = []
                rec["fired"] = fired
            for at_s in offs:
                if at_s in fired:
                    continue
                if age >= at_s:
                    _rescore_once(scanner, config, state, degen, mint,
                                  rec, now, at_s)
                    fired.append(at_s)
            if all(o in fired for o in offs):
                reg.pop(mint, None)
    except Exception:
        logger.exception("rescore poll failed")


def _rescore_once(scanner: Any, config: Any, state: Dict[str, Any],
                  degen: Any, mint: str, rec: Dict[str, Any],
                  now: float, at_s: float) -> None:
    """One re-score: rank on live signals, route, record. Never raises."""
    try:
        payload = rec.get("payload") or {}
        c, new_cand, entry = _rebuild(payload)
        # Live-history merge (fix 2026-10-10): _rebuild produces a
        # birth-frozen entry, but the re-score must rank on LIVE signals.
        # Merge the freshest mcap series from the scanner's seen map so
        # velocity/pressure reflect the +5/+15min reality, not birth.
        try:
            live_hist = ((state.get("seen") or {}).get(mint)
                         or {}).get("mcap") or []
            if live_hist:
                entry["mcap"] = [list(pt) for pt in live_hist[-40:]]
        except Exception:
            logger.warning("rescore live-history merge failed for %s",
                           mint, exc_info=True)
        mcap, tape_stats = _fresh_stats(
            scanner, state, degen, mint,
            str(payload.get("chain") or "solana"), now)
        # Feed the fresh snapshot INTO the fingerprint tracker
        # (2026-10-21): rescore mints are out of `candidates`, so the
        # judge never observes them and the tracker starved — every
        # rescore ranked fp=None. Observing here accumulates a real
        # series across offsets (+300s, +900s) so the tracker itself
        # can score later offsets; the tape fallback covers this one.
        if tape_stats:
            try:
                _tr = getattr(scanner, "_fp_tracker", None)
                if _tr is not None:
                    _tr.observe(mint, tape_stats, now)
            except Exception:
                pass
        # Feed the fresh point INTO the ranked history (fix 2026-10-20):
        # _fresh_mcap used to be fetched only AFTER ranking, for the
        # log line — so whenever the seen map was sparse the ranker
        # scored a birth-frozen series and rising_polls was structurally
        # 0 (DACITY re-scored 0.0 at +300s/+900s while ripping through
        # $387k mcap). The appended point also persists into the payload
        # so the NEXT offset ranks on the accumulated series.
        if mcap > 0:
            try:
                hist = entry.get("mcap") or []
                if not hist or _f(hist[-1][0]) < now:
                    hist = hist + [[now, mcap]]
                entry["mcap"] = [list(pt) for pt in hist[-40:]]
                payload["mcap_hist"] = entry["mcap"]
            except Exception:
                logger.warning("rescore mcap append failed for %s",
                               mint, exc_info=True)
        try:
            res = scanner._sniper_rank(config, state, c, entry, new_cand,
                                       tape_stats=tape_stats)
        except Exception:
            logger.exception("rescore rank failed for %s", mint)
            _record_ledger(state, mint, at_s, 0.0, "rank-error", now)
            return
        score = _f(res.get("score"))
        threshold = _f(res.get("threshold"), 50.0)
        tier = str(payload.get("tier") or "early")
        reason = str(payload.get("reason") or "rescore")
        logger.warning("RESCORE %s %s +%.0fs: sniper %.1f (bar %.1f) "
                       "mcap $%.0f",
                       payload.get("symbol") or mint[:8], mint, at_s,
                       score, threshold, mcap)
        if score >= threshold and res.get("tier") == "sniper":
            # Promotion: the safety floor is RE-GATED at fire time —
            # a coin can only get more dangerous with age, never safer.
            try:
                allow, rg_reason, report = scanner._instant_verdict(
                    degen, c)
            except Exception:
                logger.exception("rescore re-gate failed for %s", mint)
                allow, rg_reason, report = False, "re-gate-error", None
            if allow and mcap > 0:
                scanner._sniper_stage(
                    config, state, degen, c, new_cand, entry, mcap,
                    tier, "rescore +%.0fs: %s" % (at_s, rg_reason),
                    report, res, now)
                _publish_feed_signal(
                    payload, mint, score, "sniper", mcap, tape_stats,
                    at_s, _f(rec.get("first_ts")), now, "tier1")
                _record_ledger(state, mint, at_s, score,
                               "promoted-tier1", now)
                return
            # Re-gate failed (or no mcap yet): still WATCH-worthy — it
            # was quality-passed at birth. Digest, never a tier-1 card.
            if mcap > 0:
                scanner._enqueue_early_digest(
                    config, state, degen, c, new_cand, mcap, tier,
                    "rescore +%.0fs sniper %.1f but re-gate: %s"
                    % (at_s, score, rg_reason), report, res, now)
                _record_ledger(state, mint, at_s, score,
                               "promoted-digest(regate-fail)", now)
                return
            _record_ledger(state, mint, at_s, score,
                           "held-no-mcap", now)
            return
        # Mid-zone feed (2026-10-21, user's build order): a digest coin
        # that is GENUINELY RUNNING below the tier-1 bar is fed to the
        # other lanes instead of rotting in the digest. Bar is the TOP
        # of the zone (feed_min, default 40) AND the standing runner
        # rule verified on the fresh tape ($25k liq / $50k mcap /
        # positive 5m momentum) AND the same fire-time safety re-gate
        # as tier-1. Card goes through _sniper_stage (4/hour cap at
        # flush; cap-tripped cards fall back to the digest, not the
        # floor) and the promotion publishes to the signal bus.
        if (feed_enabled(degen) and score >= feed_min_score(degen)
                and mcap >= _FEED_MIN_MCAP_USD and tape_stats
                and _f(tape_stats.get("usd_liquidity")) >= _FEED_MIN_LIQ_USD
                and _f(tape_stats.get("chg_m5")) > 0):
            try:
                allow, rg_reason, report = scanner._instant_verdict(
                    degen, c)
            except Exception:
                logger.exception("rescore feed re-gate failed for %s",
                                 mint)
                allow, rg_reason, report = False, "re-gate-error", None
            if allow:
                scanner._sniper_stage(
                    config, state, degen, c, new_cand, entry, mcap,
                    tier, "rescore-feed +%.0fs sniper %.1f: %s"
                    % (at_s, score, rg_reason), report, res, now)
                _publish_feed_signal(
                    payload, mint, score, "feed", mcap, tape_stats,
                    at_s, _f(rec.get("first_ts")), now, "feed")
                _record_ledger(state, mint, at_s, score,
                               "promoted-feed", now)
                logger.warning(
                    "RESCORE FEED %s %s +%.0fs: sniper %.1f liq $%.0f "
                    "mcap $%.0f -> card + bus",
                    payload.get("symbol") or mint[:8], mint, at_s,
                    score, _f(tape_stats.get("usd_liquidity")), mcap)
                return
            logger.warning(
                "RESCORE feed re-gate blocked %s %s +%.0fs: %s",
                payload.get("symbol") or mint[:8], mint, at_s, rg_reason)
        if score >= digest_min_score(degen):
            if mcap > 0:
                scanner._enqueue_early_digest(
                    config, state, degen, c, new_cand, mcap, tier,
                    "rescore +%.0fs sniper %.1f" % (at_s, score),
                    None, res, now)
                _record_ledger(state, mint, at_s, score,
                               "digest", now)
            else:
                _record_ledger(state, mint, at_s, score,
                               "digest-no-mcap", now)
            return
        # Below every bar: honest drop. The coin had its fair trial.
        _record_ledger(state, mint, at_s, score, "dropped", now)
        logger.warning("RESCORE dropped %s %s +%.0fs: sniper %.1f < "
                       "digest bar %.1f",
                       payload.get("symbol") or mint[:8], mint, at_s,
                       score, digest_min_score(degen))
    except Exception:
        logger.exception("rescore once failed for %s", mint)
