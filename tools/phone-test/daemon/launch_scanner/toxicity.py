#!/usr/bin/env python3
"""Toxicity veto for smart-money cards (2026-10-17 rebuild).

Copier-farming fingerprints (field research 2025-2026): ~25% of coins
show bot farming; unfiltered copy systems had negative expectancy, and
filtering was the difference between positive and negative follower
returns. These checks run at card-fire time on free DexScreener data and
VETO the card when the mint looks farmed. Every check fail-opens —
missing data never vetoes.

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


def check_toxicity(info: Dict[str, Any],
                   now: float = None) -> Tuple[bool, str]:
    """Veto a card when the mint shows copier-farming fingerprints.

    ``info``: enrich() dict with ``txns`` ({m5/h1: {buys, sells}}),
    ``volume`` ({m5/h1: usd}), ``pairCreatedAt`` (ms). Returns
    (veto: bool, reason: str). Fail-open on missing data. Never raises.
    """
    try:
        now = now if now is not None else time.time()
        info = info or {}
        txns = info.get("txns") or {}
        vol = info.get("volume") or {}
        m5 = txns.get("m5") or {}
        buys5, sells5 = _i(m5.get("buys")), _i(m5.get("sells"))
        vol5 = _f(vol.get("m5"))
        total5 = buys5 + sells5

        # 1. Bump farm: many tiny buys, dust-size average trade.
        if buys5 >= BUMP_MIN_BUYS_5M and total5 > 0:
            avg = vol5 / total5 if vol5 > 0 else 0.0
            if avg < BUMP_MAX_AVG_USD:
                return True, (
                    "TOXIC BLOCK: bump-farmed (%d buys/5m, "
                    "avg $%.2f/trade)" % (buys5, avg))

        # 2. Wash skew: heavy buys, literally zero sells.
        if buys5 >= WASH_MIN_BUYS_5M and sells5 == 0 and vol5 > 0:
            return True, (
                "TOXIC BLOCK: one-sided flow (%d buys, 0 sells/5m)"
                % buys5)

        # 3. Bot swarm: newborn pair already swarmed.
        created_ms = _f(info.get("pairCreatedAt"))
        if created_ms > 0:
            age_s = now - created_ms / 1000.0
            if 0 <= age_s < SWARM_MAX_AGE_S:
                h1 = txns.get("h1") or {}
                buys1 = _i(h1.get("buys"))
                if buys1 >= SWARM_MIN_BUYS_H1:
                    return True, (
                        "TOXIC BLOCK: bot swarm (pair %.0fs old, "
                        "%d buys/h1)" % (age_s, buys1))

        return False, "ok"
    except Exception as exc:
        logger.warning("toxicity check failed (fail-open): %s", exc)
        return False, "ok"
