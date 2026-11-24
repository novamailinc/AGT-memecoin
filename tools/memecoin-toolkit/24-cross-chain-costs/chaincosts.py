"""Per-chain transaction cost model + pre-registered cost gate (lab).

Pure stdlib, no network.  All figures are the documented model
constants, not live quotes:

  * solana     -- $0.001 per transaction, flat
  * base       -- $0.03  per transaction, flat
  * ethereum   -- $1.00  per transaction, flat
  * hyperliquid-- 0.035% of notional per transaction (taker fee)

A position's worst-case round trip is modelled as 4 transactions:
1 entry + up to 3 exit legs (the exit ladder can fire in pieces).
The pre-registered gate rule: a position is BLOCKED when its
worst-case round-trip cost exceeds 25% of the position notional --
at that point the cost model alone destroys the trade before any
market risk is taken.  Exactly 25% is allowed (blocked iff > 25%).

Fail-closed: unknown chains, non-positive notionals, and invalid
transaction counts raise ``ValueError`` instead of returning a
plausible-looking zero cost.
"""
from __future__ import annotations

from dataclasses import dataclass

FLAT_COST_USD = {
    "solana": 0.001,
    "base": 0.03,
    "ethereum": 1.00,
}
PCT_COST_RATE = {
    "hyperliquid": 0.00035,  # 0.035% of notional per tx
}
CHAINS = ("solana", "base", "ethereum", "hyperliquid")
ROUND_TRIP_TXS = 4
GATE_LIMIT_PCT = 25.0


def _require_chain(chain) -> str:
    if chain not in CHAINS:
        raise ValueError(f"unknown chain {chain!r}; expected one of {CHAINS}")
    return chain


def _require_notional(notional) -> float:
    if (notional is None or isinstance(notional, bool)
            or not isinstance(notional, (int, float)) or not (notional > 0)):
        raise ValueError(f"notional must be positive, got {notional!r}")
    return float(notional)


def _require_txs(txs) -> int:
    if txs is None or isinstance(txs, bool) or not isinstance(txs, int) or txs < 1:
        raise ValueError(f"txs must be a positive int, got {txs!r}")
    return txs


def tx_cost_usd(chain, notional) -> float:
    """Cost of ONE transaction on ``chain`` for a ``notional`` position."""
    _require_chain(chain)
    notional = _require_notional(notional)
    if chain in FLAT_COST_USD:
        return FLAT_COST_USD[chain]
    return notional * PCT_COST_RATE[chain]


def round_trip_cost_usd(chain, notional, txs=ROUND_TRIP_TXS) -> float:
    """Worst-case round-trip cost: ``txs`` transactions (default 4)."""
    txs = _require_txs(txs)
    return tx_cost_usd(chain, notional) * txs


def round_trip_cost_pct(chain, notional, txs=ROUND_TRIP_TXS) -> float:
    """Worst-case round-trip cost as a % of the position notional."""
    notional = _require_notional(notional)
    return round_trip_cost_usd(chain, notional, txs) / notional * 100.0


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    cost_usd: float
    cost_pct: float
    reason: str
    chain: str
    notional: float

    def __iter__(self):
        # Allow tuple-style unpacking: allowed, cost, pct, reason.
        yield self.allowed
        yield self.cost_usd
        yield self.cost_pct
        yield self.reason


def cost_gate(notional, chain, txs=ROUND_TRIP_TXS,
              limit_pct=GATE_LIMIT_PCT) -> GateResult:
    """Apply the pre-registered 25% cost gate.

    Returns a :class:`GateResult`; ``allowed`` is False iff the
    worst-case round-trip cost exceeds ``limit_pct`` of notional.
    """
    _require_chain(chain)
    notional = _require_notional(notional)
    if limit_pct is None or not (limit_pct > 0):
        raise ValueError(f"limit_pct must be positive, got {limit_pct!r}")
    cost = round_trip_cost_usd(chain, notional, txs)
    pct = cost / notional * 100.0
    if pct > limit_pct:
        return GateResult(
            False, cost, pct,
            f"blocked: worst-case round-trip cost ${cost:.4f} is "
            f"{pct:.2f}% of ${notional:.2f} notional on {chain}, "
            f"above the {limit_pct:.0f}% limit",
            chain, notional)
    return GateResult(
        True, cost, pct,
        f"allowed: worst-case round-trip cost ${cost:.4f} is "
        f"{pct:.2f}% of ${notional:.2f} notional on {chain}, "
        f"within the {limit_pct:.0f}% limit",
        chain, notional)


def min_viable_notional(chain, limit_pct=GATE_LIMIT_PCT,
                        txs=ROUND_TRIP_TXS) -> float:
    """Smallest notional that passes the gate on ``chain``.

    For flat-fee chains this is ``txs * fee / limit``; for the
    percentage chain the cost scales with notional, so any positive
    notional has the same pct -- the minimum is 0.0 when that constant
    pct passes, and infinity when it never can.
    """
    _require_chain(chain)
    if chain in PCT_COST_RATE:
        pct = round_trip_cost_pct(chain, 1.0, txs)
        return 0.0 if pct <= limit_pct else float("inf")
    return round_trip_cost_usd(chain, 1.0, txs) / (limit_pct / 100.0)


def cost_table(notionals=(1.0, 5.0, 25.0), chains=CHAINS) -> list[dict]:
    """Gate results for every (notional, chain) pair, row per pair."""
    rows = []
    for notional in notionals:
        for chain in chains:
            result = cost_gate(notional, chain)
            rows.append({
                "notional": float(notional),
                "chain": chain,
                "cost_usd": result.cost_usd,
                "cost_pct": result.cost_pct,
                "allowed": result.allowed,
            })
    return rows


def render_cost_table(notionals=(1.0, 5.0, 25.0), chains=CHAINS) -> str:
    """Render the cost table as fixed-width text (used by the demo)."""
    lines = [f"{'notional':>9} {'chain':<12} {'round-trip':>11} "
             f"{'cost %':>9}  verdict"]
    for row in cost_table(notionals, chains):
        verdict = "ALLOWED" if row["allowed"] else "BLOCKED"
        lines.append(
            f"${row['notional']:>8.2f} {row['chain']:<12} "
            f"${row['cost_usd']:>10.4f} {row['cost_pct']:>8.2f}%  {verdict}")
    return "\n".join(lines)
