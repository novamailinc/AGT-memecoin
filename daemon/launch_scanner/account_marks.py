#!/usr/bin/env python3
"""Central position marker for the shared paper account (2026-11-22).

Stage 2 of the shared-account layer introduced entry gates that
evaluate marked equity, so every lane needs marks built the SAME
way from the SAME source. This module is that one way: it reads the
account's open positions and prices them once, centrally, instead of
letting each lane invent its own marking (and its own failure mode).

Source: jup_price.get_prices() (Jupiter Price API v3), which never
raises and simply omits mints it cannot price. A position without a
price is absent from the returned map, so paper_account's view stays
fail-closed ("no mark") instead of falling back to book value - book
value is not liquidation value and must never feed equity decisions.

Mark contract (validated by paper_account._mark_value):
    {"value": <remaining qty x price, finite, >= 0>,
     "ts": <unix seconds at pricing>,
     "source": "jup-v3"}

Paper only. Nothing here touches real money, keys, or wallets.
"""

import logging
import math
import time

from . import jup_price
from . import paper_account as pa

logger = logging.getLogger("forex_agent.daemon.launch_scanner.account_marks")

SOURCE = jup_price.SOURCE


def collect_marks(price_fn=None, now=None):
    """{lane: {mint: mark}} for every open account position.

    price_fn(mints) -> {mint: usd_price} is injectable for tests;
    the default is jup_price.get_prices (cached, batched, never
    raises). Positions the price source cannot price are absent, so
    the downstream gate fails closed on them. Never raises: a broken
    marker must degrade the account to fail-closed, not crash lanes.
    """
    now = time.time() if now is None else now
    try:
        snap = pa.snapshot()
    except Exception as e:
        logger.warning("account marks: snapshot failed: %s",
                       type(e).__name__)
        return {}
    wanted = []   # (lane, mint, qty)
    mints = []
    try:
        for lane, info in (snap.get("lanes") or {}).items():
            for p in info.get("positions") or []:
                mint = p.get("mint")
                qty = p.get("qty")
                if (not isinstance(mint, str) or not mint
                        or not isinstance(qty, (int, float))
                        or isinstance(qty, bool) or qty <= 0):
                    continue
                wanted.append((lane, mint, float(qty)))
                if mint not in mints:
                    mints.append(mint)
    except Exception as e:
        logger.warning("account marks: bad snapshot shape: %s",
                       type(e).__name__)
        return {}
    if not wanted:
        return {}
    try:
        prices = (price_fn or jup_price.get_prices)(mints)
        if not isinstance(prices, dict):
            prices = {}
    except Exception as e:
        logger.warning("account marks: price source failed: %s",
                       type(e).__name__)
        prices = {}
    marks = {}
    for lane, mint, qty in wanted:
        try:
            px = prices.get(mint)
            if (not isinstance(px, (int, float)) or isinstance(px, bool)
                    or not math.isfinite(px) or px <= 0):
                continue   # no trustworthy price: absent -> fail closed
            marks.setdefault(lane, {})[mint] = {
                "value": round(qty * float(px), 6),
                "ts": now,
                "source": SOURCE,
            }
        except Exception as e:
            logger.warning("account marks: %s/%s skipped: %s",
                           lane, mint[:8], type(e).__name__)
    return marks
