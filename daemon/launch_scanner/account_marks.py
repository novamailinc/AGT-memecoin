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
     "ts": <OBSERVATION time of the oldest input: the jup cache's
           fetched_ts for a cache hit, or min(curve read, SOL/USD
           cache ts) for a curve mark - never the time this module
           wrapped a cached number>,
     "source": "jup-v3" | "reserves:curve",
     "qty": <remaining units the value was computed from>,
     "pos_id": <the open instance the value was computed for>}
  The account re-checks qty AND pos_id against the CURRENT
  position under its lock, so a mark collected before a tranche
  close is rejected as qty drift and one collected before a
  close/reopen - even at the same size - is rejected as pos_id
  drift. Source timestamps are passed through, never clamped:
  a future-dated source observation fails the account's TTL
  check instead of being laundered into freshness.

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
    """(price, observed_ts, detail) from the bonding curve,
    (0.0, now, why) when unavailable. Isolated so a reserves
    failure can never propagate into the marker."""
    if _reserves is None:
        return 0.0, time.time(), "reserves module unavailable"
    return _reserves.curve_price_observed(mint)


def collect_marks(price_fn=None, curve_price_fn=None, now=None):
    """{lane: {mint: mark}} for every open account position.

    price_fn(mints) -> {mint: usd_price | (usd_price, obs_ts)} and
    curve_price_fn(mint) -> (usd_price, detail) |
    (usd_price, obs_ts, detail) are injectable for tests; the
    defaults are jup_price.get_prices_observed and
    reserves.curve_price_observed, whose observation timestamps
    become the mark's ts (a bare injected price is treated as
    observed at `now`). Jupiter prices win; the curve is only
    consulted for mints Jupiter omitted. Positions no source can
    price are absent, so the downstream gate fails closed on them.
    Never raises: a broken marker must degrade the account to
    fail-closed, not crash lanes.
    """
    now = time.time() if now is None else now
    try:
        snap = pa.snapshot()
    except Exception as e:
        logger.warning("account marks: snapshot failed: %s",
                       type(e).__name__)
        return {}
    wanted = []   # (lane, mint, qty, pos_id)
    mints = []
    try:
        for lane, info in (snap.get("lanes") or {}).items():
            for p in info.get("positions") or []:
                mint = p.get("mint")
                qty = p.get("qty")
                pos_id = p.get("pos_id")
                if (not isinstance(mint, str) or not mint
                        or not isinstance(qty, (int, float))
                        or isinstance(qty, bool) or qty <= 0
                        or not isinstance(pos_id, str)
                        or not pos_id):
                    continue
                wanted.append((lane, mint, float(qty), pos_id, p.get("mark_key", mint)))
                if mint not in mints:
                    mints.append(mint)
    except Exception as e:
        logger.warning("account marks: bad snapshot shape: %s",
                       type(e).__name__)
        return {}
    if not wanted:
        return {}
    try:
        raw = (price_fn or jup_price.get_prices_observed)(mints)
        if not isinstance(raw, dict):
            raw = {}
    except Exception as e:
        logger.warning("account marks: price source failed: %s",
                       type(e).__name__)
        raw = {}
    # Normalize to {mint: (price, obs_ts)}; a bare price is treated
    # as observed at `now` (test injections, legacy call shapes).
    prices = {}
    for mint, entry in raw.items():
        if isinstance(entry, (tuple, list)) and len(entry) == 2:
            prices[mint] = (entry[0], entry[1])
        else:
            prices[mint] = (entry, now)
    # Jupiter's omissions are the pre-graduation holdings: price
    # them from the bonding curve. One call per unpriced mint; a
    # curve failure or a completed (graduated) curve leaves the
    # mint absent, never coerced.
    curve_px = {}
    cfn = curve_price_fn or _default_curve_price
    for mint in mints:
        if _ok_price(prices.get(mint, (None,))[0]):
            continue
        try:
            res = cfn(mint)
            if isinstance(res, (tuple, list)) and len(res) == 3:
                px, obs, _detail = res
            else:
                px, _detail = res
                obs = now
            if _ok_price(px) and isinstance(obs, (int, float)) \
                    and not isinstance(obs, bool) \
                    and math.isfinite(obs):
                curve_px[mint] = (float(px), float(obs))
        except Exception as e:
            logger.warning("account marks: curve %s failed: %s",
                           mint[:8], type(e).__name__)
    marks = {}
    for lane, mint, qty, pos_id, mark_key in wanted:
        try:
            jpx, jts = prices.get(mint, (None, None))
            if _ok_price(jpx):
                px, source, ots = float(jpx), SOURCE, jts
            elif mint in curve_px:
                px, ots = curve_px[mint]
                source = CURVE_SOURCE
            else:
                continue   # no trustworthy price: absent -> fail closed
            if not isinstance(ots, (int, float)) \
                    or isinstance(ots, bool) or not math.isfinite(ots):
                ots = now
            marks.setdefault(lane, {})[mark_key] = {
                "value": round(qty * px, 6),
                "ts": float(ots),
                "source": source,
                "qty": qty,
                "pos_id": pos_id,
            }
        except Exception as e:
            logger.warning("account marks: %s/%s skipped: %s",
                           lane, mint[:8], type(e).__name__)
    return marks
