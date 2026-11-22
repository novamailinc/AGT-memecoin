# Cross-Chain Costs (lab)

A from-scratch, pure-stdlib lab for the per-chain transaction cost model behind a pre-registered cost gate: what a position's worst-case round trip actually costs on each chain, and at what size the cost model alone destroys the trade. Everything here is synthetic — no network, no live bot code, no live fee quotes — every figure is a documented model constant that can be checked by hand.

## What it does

Module `chaincosts.py` — the cost model and the gate:

- Model constants: `FLAT_COST_USD = {"solana": 0.001, "base": 0.03, "ethereum": 1.00}` (flat per-transaction fees) and `PCT_COST_RATE = {"hyperliquid": 0.00035}` (0.035% of notional per transaction); `CHAINS = ("solana", "base", "ethereum", "hyperliquid")`; a worst-case round trip is `ROUND_TRIP_TXS = 4` transactions (1 entry + up to 3 exit legs, because the exit ladder can fire in pieces); the gate limit is `GATE_LIMIT_PCT = 25.0`.
- `tx_cost_usd(chain, notional) -> float` — the cost of one transaction: the flat fee on flat chains (independent of notional), or `notional * rate` on Hyperliquid. Fail-closed: an unknown chain or a non-positive/non-numeric notional raises `ValueError`.
- `round_trip_cost_usd(chain, notional, txs=4) -> float` / `round_trip_cost_pct(chain, notional, txs=4) -> float` — the worst-case round-trip cost in dollars and as a percentage of notional. Fail-closed on a non-positive or non-integer transaction count.
- `cost_gate(notional, chain, txs=4, limit_pct=25.0) -> GateResult` — the pre-registered rule: **blocked iff the worst-case round-trip cost exceeds 25% of the notional**; exactly 25% is allowed. `GateResult(allowed, cost_usd, cost_pct, reason, chain, notional)` also unpacks as the 4-tuple `(allowed, cost_usd, cost_pct, reason)`, and `reason` names the chain, the dollar cost, the percentage, and the limit, for both verdicts.
- `min_viable_notional(chain, limit_pct=25.0, txs=4) -> float` — the smallest notional that passes the gate: `txs * fee / limit` on flat chains ($16.00 for Ethereum, $0.48 for Base, $0.016 for Solana), and `0.0` on Hyperliquid, whose percentage cost scales with the trade so no floor exists.
- `cost_table(notionals=(1.0, 5.0, 25.0), chains=CHAINS) -> list[dict]` — one gate row per (notional, chain) pair; `render_cost_table(...) -> str` renders the same grid as fixed-width text.

Pinned results the tests lock in: Ethereum at $1 is **blocked at 400%** ($4.00 round trip); Ethereum at $25 is **allowed at 16%**; Solana (0.40%) and Base (12.00%) pass at $1; Hyperliquid's round-trip percentage is a constant 0.14% at every notional, so it is never blocked by this gate at any size.

`demo.py` renders the $1/$5/$25 × 4-chain cost table, lists the pinned gate decisions and per-chain minimum viable notionals, and shows the unknown-chain rejection.

## Test suite

38 tests, all passing (`pytest -q`: "38 passed"). Covered: the flat and percentage model constants and the 4-transaction / 25% defaults; per-transaction costs on flat chains being independent of notional and Hyperliquid's scaling linearly ($100 → $0.035, $1000 → $0.35); round-trip dollar costs for all four chains and custom transaction counts; flat-chain cost percentages falling as notional grows, and Hyperliquid's percentage being exactly constant across notionals from $1 to $1,000,000; all four pinned gate results (Ethereum $1 blocked at 400%, Ethereum $25 allowed at 16%, Solana and Base passing at $1, Hyperliquid passing at $1 at 0.14%); the gate boundary — exactly 25% (Ethereum at $16) allowed, just above it blocked; `GateResult` unpacking as a 4-tuple; the reason string naming chain and limit for both verdicts; the block threshold scaling with notional (Base blocked below a $0.48 notional, allowed at it); Solana passing even a $0.10 notional; Hyperliquid never blocked at any size from $0.01 to $1M; minimum viable notionals for all four chains, verified to actually pass the gate while 99% of the minimum fails; the cost table's shape (12 rows for the default grid), its custom-input behaviour, the rendered table containing all chains and both verdicts, and the rendered Ethereum-$1 row showing 400.00% BLOCKED; and fail-closed rejection of unknown chains (including case-sensitive lookalikes), bad notionals, bad transaction counts, and bad gate limits.

## Captured output

Verbatim output of `python demo.py`:

```
=== Cross-chain cost lab (flat / percentage tx cost model) ===
Worst-case round trip = 4 txs (1 entry + up to 3 exit legs)
Pre-registered gate: block when round-trip cost > 25% of notional

-- Cost table: $1 / $5 / $25 across four chains --
 notional chain         round-trip    cost %  verdict
$    1.00 solana       $    0.0040     0.40%  ALLOWED
$    1.00 base         $    0.1200    12.00%  ALLOWED
$    1.00 ethereum     $    4.0000   400.00%  BLOCKED
$    1.00 hyperliquid  $    0.0014     0.14%  ALLOWED
$    5.00 solana       $    0.0040     0.08%  ALLOWED
$    5.00 base         $    0.1200     2.40%  ALLOWED
$    5.00 ethereum     $    4.0000    80.00%  BLOCKED
$    5.00 hyperliquid  $    0.0070     0.14%  ALLOWED
$   25.00 solana       $    0.0040     0.02%  ALLOWED
$   25.00 base         $    0.1200     0.48%  ALLOWED
$   25.00 ethereum     $    4.0000    16.00%  ALLOWED
$   25.00 hyperliquid  $    0.0350     0.14%  ALLOWED

-- Pinned gate decisions --
$   1.00 on ethereum     cost $4.0000 (400.00%) -> BLOCKED
$  25.00 on ethereum     cost $4.0000 (16.00%) -> ALLOWED
$   1.00 on solana       cost $0.0040 (0.40%) -> ALLOWED
$   1.00 on base         cost $0.1200 (12.00%) -> ALLOWED
$   1.00 on hyperliquid  cost $0.0014 (0.14%) -> ALLOWED
$1000.00 on hyperliquid  cost $1.4000 (0.14%) -> ALLOWED

-- Minimum viable notional per chain (25% gate) --
solana       min viable notional: $0.02
base         min viable notional: $0.48
ethereum     min viable notional: $16.00
hyperliquid  min viable notional: $0.00

-- Fail-closed --
Unknown chain correctly rejected: unknown chain 'dogechain'; expected one of ('solana', 'base', 'ethereum', 'hyperliquid')
```

## What this mirrors in the live bot

This mirrors `daemon/launch_scanner/chain_costs.py` in the live bot and the pre-registered 25% cost gate applied to the $1 paper-mirror positions: every fired card opens a $1 paper position per chain, the modelled worst-case round-trip cost (entry plus the exit ladder's legs, with per-chain gas/fee models — Solana $0.001, Base $0.03, Ethereum $1.00 per transaction, Hyperliquid 0.035% of notional) is compared against 25% of the notional, and entries are blocked with the math logged when the cost alone would eat the trade. The model's productive result stands as-is: at $1, Ethereum is cost-blocked at 400%, so no $1 Ethereum strategy can survive its own gas — the gate says so before any market risk is taken. It is a teaching/checking model of that cost discipline only; it shares no code with the live bot.

## Self-exam

1. Why is the round trip modelled as 4 transactions rather than the 2 (one entry, one exit) a simple trade would suggest?
2. Ethereum at $1 is blocked at 400% while Hyperliquid can never be blocked by this gate at any size — what structural difference between the two fee models causes that?
3. The gate blocks *before* entry on a cost model rather than measuring realised costs afterwards — why does pre-registration of the 25% rule matter for that to be honest?
4. Exactly 25% is allowed and 25.01% is blocked: what does the position at the Ethereum $16 boundary actually pay, and why is a sharp boundary preferable here to a warning zone?
5. Solana's minimum viable notional is $0.016 — does that mean $0.02 Solana positions are a good idea? What costs does this model *not* capture that would still eat them?
6. If Ethereum gas doubled in the model to $2.00 per transaction, which rows of the $1/$5/$25 table change verdict, and what does the new minimum viable Ethereum notional become?
