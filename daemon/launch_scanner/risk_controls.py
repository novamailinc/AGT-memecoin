#!/usr/bin/env python3
"""Slippage enforcement at the fill (2026-11-23).

fills.py declares the model's own slippage cap: slippage_pct() prices a
one-leg fill from pool liquidity and MAX_SLIPPAGE_PCT is the point at
which too_thin_to_trade() refuses the trade outright. No lane called
either, so a fill the model prices AT the cap was silently accepted as
if honestly priced. check_slippage wires that declared refusal at the
fill, where the lane's liquidity read exists.

Scope rule (owner-owned risk values): the declared fills cap is always
enforced when liquidity is known; unknown/invalid liquidity or size REFUSES
the entry fail-closed (2026-11-24; unknown slippage is never zero). AGT_RISK_MAX_SLIPPAGE_PCT may only
TIGHTEN it (min), never loosen it; when that env var is set but
unparsable/non-positive, or set while liquidity is unknown, the gate
fails CLOSED. Unknown liquidity always fails closed.
Entry/account gating (daily loss, drawdown, exposure) is owned by
paper_account's owner-config gates; this module deliberately does not
duplicate them.
"""
import os

try:
    from daemon.launch_scanner import fills as _fills
except Exception:  # direct script execution fallback
    try:
        import fills as _fills
    except Exception:
        _fills = None

_ENV_SLIPPAGE = "AGT_RISK_MAX_SLIPPAGE_PCT"
_DECLARED_SLIPPAGE_CAP = 25.0  # fallback if fills is unimportable


def _declared_cap():
    try:
        if _fills is not None:
            return float(_fills.MAX_SLIPPAGE_PCT)
    except Exception:
        pass
    return _DECLARED_SLIPPAGE_CAP


def check_slippage(size_usd, liquidity_usd, env=None):
    """(allowed, reason, effective_cap_pct) for one entry's modeled
    one-leg slippage. Never raises."""
    try:
        import math
        try:
            liq = float(liquidity_usd) if liquidity_usd else None
        except (TypeError, ValueError):
            liq = None
        if liq is not None and (not math.isfinite(liq) or liq <= 0):
            liq = None
        try:
            size = float(size_usd or 0)
        except (TypeError, ValueError):
            size = float("nan")
        if not math.isfinite(size) or size <= 0:
            return False, "entry size unknown, non-finite or not positive - failing closed", _declared_cap()

        cap = _declared_cap()
        explicit = False
        raw = (env or os.environ).get(_ENV_SLIPPAGE)
        if raw is not None and str(raw).strip() != "":
            try:
                v = float(str(raw).strip())
                if v != v or v <= 0:  # NaN or non-positive
                    return False, ("%s set but invalid - failing "
                                   "closed" % _ENV_SLIPPAGE), cap
                cap = min(cap, v)  # tighten-only
                explicit = True
            except Exception:
                return False, ("%s set but invalid - failing "
                               "closed" % _ENV_SLIPPAGE), cap

        if liq is None:
            # 2026-11-24: unknown liquidity means unknown slippage, which is
            # never treated as zero. The ENTRY is refused (exits do not call
            # this gate).
            return False, ("liquidity unknown: slippage cannot be "
                           "established - failing closed"), cap
        if _fills is not None:
            slip = _fills.slippage_pct(size, liq)
        else:
            slip = min(max(200.0 * size / liq, 0.5), cap)
        if slip >= cap:
            return False, ("modeled slippage %.1f%% >= cap %.1f%% on "
                           "$%.2f liquidity $%.2f"
                           % (slip, cap, liq, size)), cap
        return True, "", cap
    except Exception as e:
        return False, "slippage gate error: %s" % e, 0.0
