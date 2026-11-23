#!/usr/bin/env python3
"""Central position marker for the shared paper account (2026-11-22).

Stage 2 of the shared-account layer introduced entry gates that
evaluate marked equity, so every lane needs marks built the SAME
way from the SAME source. This module is that one way: it reads the
account's open positions and prices them once, centrally, instead of
letting each lane invent its own marking (and its own failure mode).

Source chain, per mint:
  1. jup_price.get_prices() (Jupiter Price API v3), which never
     raises and simply omits mints it cannot price.
  2. reserves.curve_price_usd() for the mints Jupiter omits: the
     pump.fun bonding curve's virtual reserves, read from chain
     state. This is what covers PRE-GRADUATION holdings, which
     Jupiter v3 does not list.
A position neither source can price is absent from the returned
map, so paper_account's view stays fail-closed ("no mark") instead
of falling back to book value - book value is not liquidation value
and must never feed equity decisions. A graduated token Jupiter
omits ALSO stays absent: the curve reader refuses completed curves
and the account's position rows carry no pool address to read
PumpSwap vaults from, so there is nothing trustworthy to mark it
with here.

Curve-mark caveat: the curve price is the SPOT price implied by
virtual reserves; it is not an execution quote and ignores the
price impact of selling the position's remaining units back into
the curve. For gate purposes it is a fresh, provenanced mark, not
a liquidation guarantee. Each curve read spends one non-exempt
pacer token per unpriced mint per collect.

Mark contract (validated by paper_account._mark_value):
    {"value": <remaining qty x price, finite, >= 0>,
     "ts": <unix seconds at pricing>,
     "source": "jup-v3" | "reserves:curve"}

Paper only. Nothing here touches real money, keys, or wallets.
"""

import logging
import math
import time

from . import jup_price
from . import paper_account as pa

try:                                    # a broken reserves module
    from . import reserves as _reserves  # must never break marking
except Exception:
    try:
        import reserves as _reserves    # script import context
    except Exception:
        _reserves = None

logger = logging.getLogger("forex_agent.daemon.launch_scanner.account_marks")

SOURCE = jup_price.SOURCE
CURVE_SOURCE = "reserves:curve"


def _ok_price(px) -> bool:
    return (isinstance(px, (int, float)) and not isinstance(px, bool)
            and math.isfinite(px) and px > 0)


def _default_curve_price(mint):
    """(price, detail) from the bonding curve, (0.0, why) when
    unavailable. Isolated so a reserves failure can never propagate
    into the marker."""
    if _reserves is None:
        return 0.0, "reserves module unavailable"
    return _reserves.curve_price_usd(mint)


def collect_marks(price_fn=None, curve_price_fn=None, now=None):
    """{lane: {mint: mark}} for every open account position.

    price_fn(mints) -> {mint: usd_price} and
    curve_price_fn(mint) -> (usd_price, detail) are injectable for
    tests; the defaults are jup_price.get_prices (cached, batched,
    never raises) and reserves.curve_price_usd. Jupiter prices win;
    the curve is only consulted for mints Jupiter omitted.
    Positions no source can price are absent, so the downstream
    gate fails closed on them. Never raises: a broken marker must
    degrade the account to fail-closed, not crash lanes.
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
    # Jupiter's omissions are the pre-graduation holdings: price
    # them from the bonding curve. One call per unpriced mint; a
    # curve failure or a completed (graduated) curve leaves the
    # mint absent, never coerced.
    curve_px = {}
    cfn = curve_price_fn or _default_curve_price
    for mint in mints:
        if _ok_price(prices.get(mint)):
            continue
        try:
            px, _detail = cfn(mint)
            if _ok_price(px):
                curve_px[mint] = float(px)
        except Exception as e:
            logger.warning("account marks: curve %s failed: %s",
                           mint[:8], type(e).__name__)
    marks = {}
    for lane, mint, qty in wanted:
        try:
            if _ok_price(prices.get(mint)):
                px, source = float(prices[mint]), SOURCE
            elif mint in curve_px:
                px, source = curve_px[mint], CURVE_SOURCE
            else:
                continue   # no trustworthy price: absent -> fail closed
            marks.setdefault(lane, {})[mint] = {
                "value": round(qty * px, 6),
                "ts": now,
                "source": source,
            }
        except Exception as e:
            logger.warning("account marks: %s/%s skipped: %s",
                           lane, mint[:8], type(e).__name__)
    return marks
