# Cross-Chain Costs — study notes

## Core ideas (teach-back)

Before asking "can this trade win?", ask a cheaper question: "can this trade survive its own transaction
costs?" On some chains, at some sizes, the answer is no — before any market risk is taken at all.

This lab models transaction costs per chain and builds a single pre-registered gate on top of the model.

There are two structurally different fee shapes:

- **Flat fees** — a fixed dollar cost per transaction, independent of trade size:
- solana: $0.001 per
  tx
- base: $0.03 per tx
- ethereum: $1.00 per tx
- **Percentage fees** — a fixed fraction of notional:
 
- hyperliquid: 0.035% of notional per tx (`0.00035`)

A position's cost is not one transaction. The exit ladder can fire in pieces — partial exits at several
rungs — so the worst-case round trip is modelled as `ROUND_TRIP_TXS = 4` transactions: 1 entry + up to 3
exit legs. Modelling only the happy-path 2 transactions (1 in, 1 out) would understate the cost of
exactly the trades the ladder manages hardest.

The gate is a single rule, fixed in advance:

> **Blocked iff the worst-case round-trip cost > exceeds 25% of the notional.** Exactly 25% is allowed.

`GATE_LIMIT_PCT = 25.0`. "Pre-registered" is the point: the limit is a model constant chosen before any
particular trade is judged, so it cannot be bent after the fact to let a favoured trade through. When
the gate blocks, it blocks *before entry*, and `GateResult.reason` states the chain, the dollar cost,
the percentage, and the limit — for both verdicts.

Everything is fail-closed: an unknown chain (including case-sensitive lookalikes), a non-positive or
non-numeric notional, or a bad transaction count raises `ValueError`. An unmodelled chain must never
default to a plausible-looking zero cost.

The headline consequence: at a $1 notional, Ethereum's round trip costs $4.00 — **400%** of the trade.
No $1 Ethereum strategy can survive its own gas. That is not a market opinion; it is arithmetic, and the
gate says it before the money is committed.

## Key derivations & formulas

### One transaction

```text
tx_cost_usd(chain, notional) =
    FLAT_COST_USD[chain]              if chain is flat-fee
    notional * PCT_COST_RATE[chain]   if chain is percentage-fee
```

Flat cost does not depend on notional at all. Percentage cost scales linearly: on Hyperliquid, $100 →
$0.035 per tx; $1000 → $0.35 per tx.

### Round trip

```text
round_trip_cost_usd(chain, notional, txs=4) = tx_cost_usd(chain, notional) * txs
round_trip_cost_pct(chain, notional, txs=4) = round_trip_cost_usd / notional * 100
```

Step by step for a flat chain, this simplifies:

```text
round_trip_cost_pct = (fee * txs) / notional * 100
```

— a hyperbola in `notional`: double the size, halve the cost percentage. The dollar cost never moves.

For the percentage chain the notional cancels:

```text
round_trip_cost_pct = rate * txs * 100
                    = 0.00035 * 4 * 100
                    = 0.14%
```

— a constant at *every* notional, from $0.01 to $1,000,000. That single algebraic difference — fee fixed
in dollars vs fee proportional to size — is why flat chains have a minimum viable size and Hyperliquid
does not.

### The gate

```text
allowed  <=>  round_trip_cost_pct <= limit_pct      (default 25.0)
blocked  <=>  round_trip_cost_pct >  limit_pct
```

`GateResult` carries `(allowed, cost_usd, cost_pct, reason, chain, notional)` and also unpacks as the
4-tuple `(allowed, cost_usd, cost_pct, reason)`.

### Minimum viable notional

Solve the flat-chain gate at equality:

```text
(fee * txs) / notional * 100 = limit_pct
notional_min = (fee * txs) / (limit_pct / 100)
```

With `txs = 4` and `limit_pct = 25`:

- solana: `0.004 / 0.25 = $0.016`
- base: `0.12 / 0.25 = $0.48`
- ethereum: `4.00 / 0.25 = $16.00`
- hyperliquid: `0.0` — the constant 0.14% passes at any size.

The implementation's general form: if a percentage chain's constant pct ever *exceeded* the limit, its
minimum would be infinity — never viable at any size. Tests verify the boundary property directly: each
minimum passes the gate, and 99% of it fails.

## Worked example

The demo's cost table — $1 / $5 / $25 across four chains:

| notional | chain | round-trip | cost % | verdict |
|----------|-------|-----------|--------|---------|
| $1.00 | solana | $0.0040 | 0.40% | ALLOWED |
| $1.00 | base | $0.1200 | 12.00% | ALLOWED |
| $1.00 | ethereum | $4.0000 | 400.00% | BLOCKED |
| $1.00 | hyperliquid | $0.0014 | 0.14% | ALLOWED |
| $5.00 | solana | $0.0040 | 0.08% | ALLOWED |
| $5.00 | base | $0.1200 | 2.40% | ALLOWED |
| $5.00 | ethereum | $4.0000 | 80.00% | BLOCKED |
| $5.00 | hyperliquid | $0.0070 | 0.14% | ALLOWED |
| $25.00 | solana | $0.0040 | 0.02% | ALLOWED |
| $25.00 | base | $0.1200 | 0.48% | ALLOWED |
| $25.00 | ethereum | $4.0000 | 16.00% | ALLOWED |
| $25.00 | hyperliquid | $0.0350 | 0.14% | ALLOWED |

Read the Ethereum column vertically: the dollar cost is pinned at $4.00 while the percentage collapses
400% → 80% → 16% purely because the denominator grew. Ethereum first passes at exactly $16 (25.00%,
allowed); just below $16 it is blocked.

Base shows the same shape lower down: blocked below $0.48, allowed at $0.48. Solana passes even a $0.10
notional. Hyperliquid at $1000 costs $1.40 — still 0.14%, still allowed.

And the fail-closed case: `cost_gate(1.0, "dogechain")` raises `ValueError: unknown chain 'dogechain';
...`.

## Trap log

- T1. **Modelling a round trip as 2 transactions.** The exit ladder fires in legs; costing only entry +
  one exit understates the worst case the gate exists to bound.
- T2. **Comparing dollar costs across
  sizes.** "$4 of fees" is fatal at $1 and fine at $25. Only the *percentage of notional* is comparable,
  which is why the gate is stated in percent.
- T3. **Assuming all fees scale.** Flat fees punish small
  trades and vanish proportionally at size; percentage fees do neither. Mixing up the two shapes inverts
  the conclusion about which chain suits small shots.
- T4. **Post-hoc gate limits.** A limit chosen
  after seeing a trade's cost is not a gate, it is a rationalisation. The 25% figure must be registered
  before the trade exists.
- T5. **A fuzzy boundary.** "Around 25% is risky" cannot be applied
  consistently by code or audited later. A sharp line — exactly 25% allowed, above blocked — can.
- T6. **Unknown chain = zero cost.** Defaulting an unmodelled chain to $0 fees makes the least-understood
  chain look the cheapest. Fail closed instead.
- T7. **Reading the minimum as a recommendation.**
  Solana's $0.016 minimum only says *this model* permits a 2-cent trade. Costs outside the model —
  slippage, spread, failed transactions — would still eat it.
- T8. **Forgetting what the gate cannot
  see.** Passing the cost gate says nothing about whether the trade has positive expectancy. It is a
  necessary screen, not a sufficient one.

## Source flags

- All fee constants, the 4-transaction round trip, and the 25% limit are from `chaincosts.py`
  (`FLAT_COST_USD`, `PCT_COST_RATE`, `ROUND_TRIP_TXS`, `GATE_LIMIT_PCT`). [S]
- The pinned results —
  Ethereum $1 blocked at 400%, Ethereum $25 allowed at 16%, the $16.00/$0.48/$0.016 minimums,
  Hyperliquid's constant 0.14% — are locked in by the toolkit's 38 tests per the README. [S]
- Note on
  display: the demo prints Solana's minimum as `$0.02` because it formats to two decimals; the precise
  computed value is $0.016. That is print rounding, not a different number. [S]
- The mirroring of the
  live bot's `chain_costs.py` and its $1 paper-mirror positions being cost-blocked on Ethereum is the
  README's live-bot account. [M]
- The hyperbola/constant reading of the two fee shapes is derived here
  from the source formulas. [R]

## One-paragraph verdict

This toolkit proves a narrow, complete piece of arithmetic: given documented per-chain fee constants, a 4-transaction worst-case round trip, and a pre-registered 25% limit, the gate's every verdict — including Ethereum blocked at 400% on $1 and allowed at 16% on $25, and Hyperliquid's size-independent 0.14% — follows by hand-checkable algebra, with exact boundary behaviour at 25%. It does not prove the fee constants match live network conditions at any given moment, and passing the gate says nothing about a trade's expectancy; it proves only that cost alone does not destroy the trade, which is precisely the question it was built to answer first.
