"""Quiet-coin breakout trigger: attention signal, data only.

Watches coins that have been QUIET (low volume/buys for several polls)
for a sudden volume acceleration AND holder-count acceleration, then
raises an attention signal through the EXISTING warn lane
(``degen.warning``, door="breakout") — the lane's own per-coin cooldown
and 4/hour global cap apply unchanged. It NEVER opens a position and
never changes any entry logic.

Built on the fingerprint tracker's per-mint time series (read-only via
``FingerprintTracker.observations(mint)``) — no parallel time series is
invented. Obs tuple: (ts, mcap, price, vol_m5, buys, sells, liq,
holders|None).

Definitions (all knobs on the degen config):

* QUIET — the ``breakout_quiet_polls`` polls before the latest one
  averaged ``vol_m5 <= breakout_quiet_vol_max_usd`` AND ``buys_m5 <=
  breakout_quiet_buys_max``.
* BREAKOUT — the latest poll's ``vol_m5`` is ``>=
  breakout_vol_mult`` times the quiet-baseline mean AND clears the
  absolute ``breakout_vol_floor_usd`` (a real-money reality check), and
  holder growth from the quiet baseline is ``>= breakout_holder_pct``.
  Holder growth is enforced whenever holder data actually covers the
  window (strict volume-AND-holders); when holders are unknown the
  trigger may still fire on volume alone (``holder_evidence:
  "unknown"``) unless ``breakout_require_holders`` is true — the mode is
  always logged so the ledger can later compare vol-only vs
  vol+holder breakouts.

Every trigger is logged to ``state["breakout_log"]`` (bounded,
measurement-friendly: baseline vs breakout inputs, evidence mode,
whether a card fired and why not). Cards additionally respect a
breakout-specific per-coin cooldown (``breakout_cooldown_hours``) ON TOP
of the warn lane's own cooldown/cap — the trigger can never become a
spam vector.

Pure logic + bounded state writes; no I/O, no broker, no entries.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("forex_agent.daemon.launch_scanner.breakouts")

_BREAKOUT_LOG_KEY = "breakout_log"
_BREAKOUT_COOLDOWN_KEY = "breakout_cooldown"


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


class BreakoutDetector:
    """Quiet -> breakout detection over the fingerprint time series."""

    def __init__(self, cfg):
        self.cfg = cfg  # retained so owners can detect config swaps
        self.enabled = bool(getattr(cfg, "breakout_enabled", True))
        self.quiet_polls = max(2, int(getattr(cfg, "breakout_quiet_polls", 4)))
        self.quiet_vol_max = float(
            getattr(cfg, "breakout_quiet_vol_max_usd", 500.0))
        self.quiet_buys_max = float(
            getattr(cfg, "breakout_quiet_buys_max", 5.0))
        self.vol_mult = max(1.0, float(getattr(cfg, "breakout_vol_mult", 5.0)))
        self.vol_floor = float(getattr(cfg, "breakout_vol_floor_usd", 2000.0))
        self.holder_pct = float(getattr(cfg, "breakout_holder_pct", 20.0))
        self.require_holders = bool(
            getattr(cfg, "breakout_require_holders", False))
        self.cooldown_h = float(getattr(cfg, "breakout_cooldown_hours", 6.0))

    # -- pure detection -------------------------------------------------
    def update(self, mint: str, series: List[Tuple[float, ...]], *,
               symbol: str = "", chain: str = "solana",
               now: Optional[float] = None,
               fp_verdict: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Evaluate one mint. Returns the trigger dict, or None.

        Never raises.
        """
        try:
            if not self.enabled or not mint:
                return None
            obs = [o for o in (series or [])
                   if o is not None and len(o) >= 8]
            if len(obs) < self.quiet_polls + 1:
                return None  # not enough history for quiet + breakout
            baseline = obs[-(self.quiet_polls + 1):-1]
            latest = obs[-1]
            base_vol = _mean([_f(o[3]) for o in baseline])
            base_buys = _mean([_f(o[4]) for o in baseline])
            last_vol = _f(latest[3])
            last_buys = _f(latest[4])
            last_mcap = _f(latest[1])

            # QUIET: baseline polls were genuinely sleepy.
            if base_vol > self.quiet_vol_max or base_buys > self.quiet_buys_max:
                return None

            # BREAKOUT: sudden volume acceleration + absolute reality check.
            if last_vol < self.vol_floor:
                return None
            vol_mult = last_vol / max(base_vol, 1e-9)
            if vol_mult < self.vol_mult:
                return None

            # Holders: strict AND whenever the data covers the window.
            h_base = [_f(o[7]) for o in baseline if o[7]]
            h_last = _f(latest[7]) if latest[7] else 0.0
            holder_evidence = "unknown"
            holder_growth_pct: Optional[float] = None
            base_h = _mean(h_base) if h_base else 0.0
            if base_h > 0 and h_last > 0:
                holder_growth_pct = ((h_last - base_h) / base_h) * 100.0
                holder_growth_pct = max(-999.0, min(999.0,
                                                   holder_growth_pct))
                if holder_growth_pct >= self.holder_pct:
                    holder_evidence = "confirmed"
                else:
                    # Holders measurable but NOT accelerating: the AND
                    # fails — volume alone isn't the signal we defined.
                    return None
            elif self.require_holders:
                return None

            return {
                "mint": mint,
                "symbol": symbol or "",
                "chain": chain or "solana",
                "ts": float(now if now is not None else time.time()),
                "trigger": ("volume_holder_breakout"
                            if holder_evidence == "confirmed"
                            else "volume_breakout"),
                "vol_mult": round(vol_mult, 2),
                "baseline_vol_m5": round(base_vol, 2),
                "breakout_vol_m5": round(last_vol, 2),
                "baseline_buys_m5": round(base_buys, 2),
                "breakout_buys_m5": round(last_buys, 2),
                "holder_evidence": holder_evidence,
                "holder_growth_pct": (round(holder_growth_pct, 1)
                                      if holder_growth_pct is not None
                                      else None),
                "baseline_holders": (round(base_h, 1)
                                     if base_h > 0 else None),
                "latest_holders": (round(h_last, 1)
                                   if h_last > 0 else None),
                "mcap": round(last_mcap, 2),
                "quiet_polls": self.quiet_polls,
                "fp_verdict": fp_verdict,
            }
        except Exception:
            logger.warning("breakout update failed for %s", mint,
                           exc_info=True)
            return None

    # -- per-coin cooldown ----------------------------------------------
    def cooldown_ok(self, state: Dict[str, Any], mint: str,
                    now: float) -> bool:
        try:
            cd = state.get(_BREAKOUT_COOLDOWN_KEY) or {}
            last = _f(cd.get(mint))
            return (float(now) - last) >= self.cooldown_h * 3600.0
        except Exception:
            return True

    def stamp_cooldown(self, state: Dict[str, Any], mint: str,
                       now: float) -> None:
        try:
            cd = state.setdefault(_BREAKOUT_COOLDOWN_KEY, {})
            if isinstance(cd, dict):
                cd[mint] = float(now)
        except Exception:
            pass


def log_trigger(state: Dict[str, Any], degen: Any,
                trig: Dict[str, Any], *,
                card_fired: bool,
                suppressed: Optional[str] = None) -> Dict[str, Any]:
    """Append one measurement row for a breakout trigger. Never raises.

    Logged for EVERY trigger — card fired or not — so the ledger can
    later evaluate whether breakout signals (vol-only vs vol+holder)
    had any predictive value. Bounded by ``breakout_log_max``.
    """
    row = {
        "mint": trig.get("mint"),
        "symbol": trig.get("symbol"),
        "chain": trig.get("chain"),
        "ts": trig.get("ts") or time.time(),
        "trigger": trig.get("trigger"),
        "vol_mult": trig.get("vol_mult"),
        "baseline_vol_m5": trig.get("baseline_vol_m5"),
        "breakout_vol_m5": trig.get("breakout_vol_m5"),
        "baseline_buys_m5": trig.get("baseline_buys_m5"),
        "breakout_buys_m5": trig.get("breakout_buys_m5"),
        "holder_evidence": trig.get("holder_evidence"),
        "holder_growth_pct": trig.get("holder_growth_pct"),
        "baseline_holders": trig.get("baseline_holders"),
        "latest_holders": trig.get("latest_holders"),
        "mcap": trig.get("mcap"),
        "quiet_polls": trig.get("quiet_polls"),
        "fp_verdict": trig.get("fp_verdict"),
        "safety_score": trig.get("safety_score"),
        "card_fired": bool(card_fired),
        "suppressed": suppressed,
    }
    try:
        log = state.setdefault(_BREAKOUT_LOG_KEY, [])
        if not isinstance(log, list):
            log = state[_BREAKOUT_LOG_KEY] = []
        log.append(row)
        try:
            cap = max(20, int(getattr(degen, "breakout_log_max", 200)))
        except (TypeError, ValueError):
            cap = 200
        del log[:-cap]
    except Exception:
        logger.warning("breakout log append failed", exc_info=True)
    return row
