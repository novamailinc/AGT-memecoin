#!/usr/bin/env python3
"""Toxicity veto for smart-money cards (2026-10-17 rebuild).

Copier-farming fingerprints (field research 2025-2026): ~25% of coins
show bot farming; unfiltered copy systems had negative expectancy, and
filtering was the difference between positive and negative follower
returns. These checks run at card-fire time on free DexScreener data and
VETO the card when the mint looks farmed. Missing data never
CLEARS a mint either: check_toxicity_detailed() reports per-check
coverage ("clear" / "veto" / "unverifiable: <what was missing>")
so a pass on absent data is journaled as unverifiable, not as safe
(2026-11-23 unknown-vs-safe reconciliation). Two legacy vetoes DO
fire on partially-absent data (bump-farm with volume absent,
wash-skew with sells absent) — preserved exactly, and the coverage
label now says the veto rested on partial data.

Fingerprints implemented:
1. BUMP_FARM — dozens of tiny buys in 5 min with dust-size average trade
   (pump.fun bump bots spamming micro-buys to hold front-page visibility).
2. WASH_SKEW — heavy buy count with zero sells in 5 min (one-sided flow;
   organic interest always has some sellers).
3. BOT_SWARM — newborn pair (<5 min old) already hit by 100+ buys in the
   hour window: sniper bots swarmed the launch, not humans.

NOT implemented (needs slot-level holder snapshots — no free source;
documented, not faked): creation-block and first-5-blocks buyer analysis.

Never raises.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Tuple

logger = logging.getLogger("forex_agent.daemon.launch_scanner.toxicity")

# Knobs (conservative by design — vetoes only clear farming)
BUMP_MIN_BUYS_5M = 40        # buys in 5 min to suspect bump-farming
BUMP_MAX_AVG_USD = 2.0        # avg $/trade below this = dust spam
WASH_MIN_BUYS_5M = 50         # buys with zero sells = one-sided
SWARM_MAX_AGE_S = 5 * 60      # pair younger than this...
SWARM_MIN_BUYS_H1 = 100       # ...with this many h1 buys = bot swarm


def _i(v: Any) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def check_toxicity_detailed(
        info: Dict[str, Any],
        now: float = None) -> Tuple[bool, str, Dict[str, str]]:
    """Veto a card when the mint shows copier-farming fingerprints.

    ``info``: enrich() dict with ``txns`` ({m5/h1: {buys, sells}}),
    ``volume`` ({m5/h1: usd}), ``pairCreatedAt`` (ms). Returns
    (veto: bool, reason: str, coverage: {check: status}).

    Coverage is the unknown-vs-safe reconciliation (2026-11-23):
    every check reports one of "clear" (ran, no fingerprint),
    "veto" (ran, fingerprint fired) or "unverifiable: <missing>",
    so a card that passed because the data was ABSENT is visibly
    different from a card whose data was present and clean.
    Veto posture is unchanged: only positive evidence vetoes, and
    nothing here invents a confidence score. Never raises.
    """
    cov = {"bump_farm": "clear", "wash_skew": "clear",
           "bot_swarm": "clear"}
    try:
        now = now if now is not None else time.time()
        info = info or {}
        txns = info.get("txns") or {}
        vol = info.get("volume") or {}
        m5 = txns.get("m5") or {}
        buys5_raw, sells5_raw = m5.get("buys"), m5.get("sells")
        vol5_raw = vol.get("m5")
        buys5, sells5 = _i(buys5_raw), _i(sells5_raw)
        vol5 = _f(vol5_raw)
        total5 = buys5 + sells5

        # 1. Bump farm: many tiny buys, dust-size average trade.
        # LEGACY VETO OUTCOME PRESERVED (2026-11-23): when volume is
        # absent the legacy code computed avg as 0.0 and vetoed on
        # buy-count alone. That veto still fires - only its coverage
        # label is new, so the journaled record shows the fingerprint
        # rested on count, not on a measured dust average.
        if buys5_raw is None:
            cov["bump_farm"] = "unverifiable: no 5m txns"
        elif buys5 >= BUMP_MIN_BUYS_5M and total5 > 0:
            avg = vol5 / total5 if vol5 > 0 else 0.0
            if avg < BUMP_MAX_AVG_USD:
                cov["bump_farm"] = (
                    "veto" if vol5_raw is not None else
                    "veto (legacy: volume absent, avg not computable)")
                return True, (
                    "TOXIC BLOCK: bump-farmed (%d buys/5m, "
                    "avg $%.2f/trade)" % (buys5, avg)), cov
        elif vol5_raw is None:
            cov["bump_farm"] = (
                "clear (buy count below threshold; volume unverified)")

        # 2. Wash skew: heavy buys, literally zero sells.
        # LEGACY VETO OUTCOME PRESERVED: absent sells counted as zero
        # sells, so a heavy one-sided-looking window vetoed. It still
        # does - the label now says the zero was an absence, not a
        # measured zero.
        if buys5_raw is None or vol5_raw is None:
            cov["wash_skew"] = "unverifiable: no 5m txns/volume"
        elif buys5 >= WASH_MIN_BUYS_5M and sells5 == 0 and vol5 > 0:
            cov["wash_skew"] = (
                "veto" if sells5_raw is not None else
                "veto (legacy: sells absent, read as zero)")
            return True, (
                "TOXIC BLOCK: one-sided flow (%d buys, 0 sells/5m)"
                % buys5), cov
        elif sells5_raw is None:
            cov["wash_skew"] = (
                "clear (buy count below threshold; sells unverified)")

        # 3. Bot swarm: newborn pair already swarmed.
        created_ms = _f(info.get("pairCreatedAt"))
        if created_ms <= 0:
            cov["bot_swarm"] = "unverifiable: no pairCreatedAt"
        else:
            age_s = now - created_ms / 1000.0
            if 0 <= age_s < SWARM_MAX_AGE_S:
                h1 = txns.get("h1") or {}
                if h1.get("buys") is None:
                    cov["bot_swarm"] = "unverifiable: no h1 txns"
                else:
                    buys1 = _i(h1.get("buys"))
                    if buys1 >= SWARM_MIN_BUYS_H1:
                        cov["bot_swarm"] = "veto"
                        return True, (
                            "TOXIC BLOCK: bot swarm (pair %.0fs old, "
                            "%d buys/h1)" % (age_s, buys1)), cov

        return False, "ok", cov
    except Exception as exc:
        logger.warning("toxicity check failed (fail-open): %s", exc)
        return False, "ok", {k: "unverifiable: error" for k in cov}


def check_toxicity(info: Dict[str, Any],
                   now: float = None) -> Tuple[bool, str]:
    """Veto a card when the mint shows copier-farming fingerprints.

    ``info``: enrich() dict with ``txns`` ({m5/h1: {buys, sells}}),
    ``volume`` ({m5/h1: usd}), ``pairCreatedAt`` (ms). Returns
    (veto: bool, reason: str). Fail-open on missing data. Never raises.
    Use check_toxicity_detailed() when the caller can journal which
    checks actually ran (unknown vs safe)."""
    veto, reason, _cov = check_toxicity_detailed(info, now)
    return veto, reason
