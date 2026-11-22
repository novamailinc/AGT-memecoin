# Bonding Curve Math (lab)

A from-scratch, pure-stdlib lab for the math of a pump.fun-style Solana launch: a constant-product curve over *virtual* reserves. Everything here is synthetic — no network, no live bot code, no real token data — so every number can be checked by hand and every failure mode can be exercised safely.

## What it does

Module `curve.py` — one frozen dataclass, `Curve(virtual_token_reserves=1.073e9, virtual_sol_reserves=30.0, total_supply=1e9, fee_bps=100, graduation_target_sol=85.0)`:

- `Curve.spot_price_sol() -> float` — SOL per token, `virtual_sol / virtual_tokens`. Fail-closed: construction raises `ValueError` on non-positive reserves/supply/target or `fee_bps` outside `[0, 10000)`.
- `Curve.spot_price_tokens_per_sol() -> float` — the reciprocal quote.
- `Curve.tokens_received_for_sol(sol_amount) -> float` — tokens out for a gross SOL buy, fee and impact included. Raises `ValueError` on non-positive `sol_amount`.
- `Curve.cost_to_buy_tokens(token_amount) -> float` — gross SOL to receive exactly that many tokens; the exact inverse of `tokens_received_for_sol`. Raises `ValueError` on non-positive amounts or a buy that would drain the virtual reserve.
- `Curve.cost_to_buy_percent_of_supply(percent) -> float` — cost to buy a % of total supply. Raises `ValueError` unless `0 < percent <= 100`.
- `Curve.sell_proceeds_sol(token_amount) -> float` — SOL back for selling tokens into the *current* curve state (chain `curve_after_buy_sol` first for a true round trip). Raises `ValueError` on non-positive amounts.
- `Curve.curve_after_buy_sol(sol_amount) -> Curve` / `Curve.curve_after_sell_tokens(token_amount) -> Curve` — new curve states after a trade; the original is immutable.
- `Curve.price_impact_of_buy_tokens(token_amount) -> float` / `Curve.price_impact_of_sell_tokens(token_amount) -> float` — % the average fill price sits above/below spot; always ≥ 0, grows with size.
- `Curve.slippage_buy_tokens(token_amount) -> float` / `Curve.slippage_sell_tokens(token_amount) -> float` — signed % difference between the naive spot quote and the actual fill; positive means a worse fill than quoted.
- `Curve.completion_pct(real_sol_collected) -> float` — % of the graduation target collected, clamped to `[0, 100]`. Raises `ValueError` on negative input.
- `Curve.is_graduated(real_sol_collected) -> bool` — True iff collected ≥ target (default ~85 SOL). Raises `ValueError` on negative input.
- `Curve.remaining_sol_to_graduation(real_sol_collected) -> float` — SOL still needed to reach the target; 0 once graduated, never negative. Raises `ValueError` on negative input.
- `Curve.market_cap_sol() -> float` — spot market cap in SOL: spot price × total supply.
- `Curve.effective_buy_price_sol(sol_amount) -> float` / `Curve.effective_sell_price_sol(token_amount) -> float` — the average fill price actually paid/received per token at that trade size (fee + impact included); the buy price always sits above spot, the sell price below, and both worsen with size.
- `Curve.round_trip_loss_pct(sol_amount) -> float` — % lost buying with that SOL and immediately selling everything back, priced on the post-buy curve; exactly 0 at zero fee, ~the fee twice otherwise.
- `Curve.sol_to_reach_spot_price(target_price_sol) -> float` — gross SOL of buys needed to push the spot price to a target (from `sqrt(k · target)`). Raises `ValueError` unless the target is positive and strictly above the current spot — buys cannot move the price down or sideways.
- `Curve.simulate_buys(sol_amounts) -> (float, Curve)` / `Curve.simulate_sells(token_amounts) -> (float, Curve)` — apply a sequence of trades leg by leg, each on the state the previous leg left; returns the total tokens/SOL and the final curve. An empty sequence is a no-op; a non-positive leg (or `None` instead of a sequence) raises `ValueError`.

Fee model: `fee_bps` is skimmed from the SOL side on both buys and sells, so a same-size buy-then-sell round trip loses only the fee (twice) — never a hidden spread.

`demo.py` walks through a spot quote, a small buy, the cost of 1% of supply, a round trip, and graduation progress on the default curve.

## Test suite

43 tests, all passing (`pytest -q`: "43 passed"). Covered: spot price from virtual reserves; price rises with buys and falls with sells; cost to buy 1% of supply against a hand-computed constant-product value (1.010101… SOL on a zero-fee 1M-token / 100-SOL fixture); buy cost and tokens-received being exact inverses; per-SOL rate degrading with trade size; a round trip losing only the fee (~1.99% at 100 bps) and exactly 0 at zero fee; completion % math including clamping past 100%; the graduation threshold boundary; slippage sign and growth with size; price impact monotonic in size; and `ValueError` on zero/negative/`None` amounts, bad reserves, bad fees, and out-of-range percentages. Added in the deepening pass: the invariant `k` preserved across buys and sells, curve states as immutable snapshots (frozen dataclass, originals never move), spot quotes as exact reciprocals, a zero-fee buy→sell restoring the original reserves exactly; percent-of-supply cost strictly increasing and convex (the second 1% costs more than the first), monotonicity of tokens-received and cost, buying 100% of supply succeeding only with reserve headroom (~415.11 SOL on the default curve) and raising on a supply==reserves fixture, a huge buy approaching but never draining the virtual reserve (~32.5 tokens left after a 1B-SOL buy), sell proceeds bounded above by the virtual SOL reserve × (1 − fee); fee behaviour — cost and output strictly ordered across 0/100/500 bps, `fee_bps=9999` still quoting its hand-computed ~3,576.65 tokens, negative fee rejected, dust-trade impact ≈ the fee alone, and slippage being numerically identical to price impact in both directions; the new helpers — market cap (== virtual SOL when supply == reserves), effective buy/sell prices bracketing spot and worsening with size, `round_trip_loss_pct` matching the manual computation, `sol_to_reach_spot_price` round-tripping to the target price (41.421356… SOL to double the small fixture's spot) and rejecting at/below-spot, zero, and `None` targets, `remaining_sol_to_graduation` including its never-negative clamp, a custom graduation target end-to-end; trade sequences — split buys [1, 1] equalling one combined 2-SOL buy (linear fees), empty-sequence no-op, sequential sells matching a manual leg-by-leg loop; and the fail-closed surface of the derived methods (`curve_after_*`, impact on a draining amount, `None` graduation inputs, bad `total_supply`/`graduation_target_sol`).

## Captured output

Verbatim output of `python demo.py`:

```
=== Bonding curve lab (synthetic pump.fun-style curve) ===
Virtual reserves: 1,073,000,000 tokens / 30.0 SOL
Spot price: 0.0000000280 SOL per token
Spot quote: 35,766,667 tokens per SOL

-- A small buy --
1.0 SOL buys 34,277,832 tokens (fee + impact included)
Cost to buy those tokens back exactly: 1.0000 SOL

-- Buying 1% of supply --
1% of supply = 10,000,000 tokens
Cost: 0.2851 SOL
Price impact of that buy: 1.96%
Slippage vs spot quote: +1.96%

-- Price rises after a bigger buy --
After a 10 SOL buy, spot is 0.0000000495 SOL (was 0.0000000280)

-- Round trip --
Buy with 5.0 SOL -> 151,969,957 tokens -> sell all -> 4.9005 SOL
Round-trip loss: 1.99% (fees only)

-- Graduation --
Real SOL collected    0.0:   0.0% complete, graduated=False
Real SOL collected   42.5:  50.0% complete, graduated=False
Real SOL collected   85.0: 100.0% complete, graduated=True
Real SOL collected  120.0: 100.0% complete, graduated=True

-- Fail-closed --
Zero buy correctly rejected: sol_amount must be positive, got 0
```

## What this mirrors in the live bot

This mirrors the launch-discovery side of the live paper bot's `daemon/launch_scanner` pipeline: the part that reasons about pump.fun launches still on the bonding curve — what a buy would actually fill at (not the spot quote), how far a launch is from graduating to a DEX pool, and why quoted prices overstate what a position would really get. It is a teaching/checking model of that math only; it shares no code with the live scanner.

## Self-exam

1. Why does a bonding curve quote prices from virtual reserves instead of the real SOL actually collected?
2. If you buy tokens and immediately sell them all back, why must you price the sell on the post-buy curve state to measure the true round-trip cost?
3. Why does the per-SOL token rate get worse as the buy size grows, even before any fee is applied?
4. What changes in the curve's behaviour at the moment real SOL collected crosses the graduation target?
5. A trader compares a fill against the spot quote and calls the difference "slippage" — what two separate effects are bundled into that one number here?
