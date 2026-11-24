# AMM and LP Lock Checks (lab)

A from-scratch, pure-stdlib lab for the two questions asked about any Raydium / PumpSwap style pool after graduation: what will a swap actually fill at, and is the liquidity really locked? Everything is synthetic — no network, no live bot code, no real pool data — so quotes can be checked against the x·y=k formula by hand and LP fixtures can be built to sit exactly on the verdict boundary.

## What it does

Module `amm.py`:

- `get_amount_out(amount_in, reserve_in, reserve_out, fee_bps=25) -> float` — the constant-product quote with the fee taken from the input. Fail-closed: raises `ValueError` on non-positive amounts/reserves or `fee_bps` outside `[0, 10000)`.
- `Pool(reserve_x, reserve_y, fee_bps=25)` — a frozen two-sided pool; construction raises `ValueError` on non-positive reserves. `Pool.spot_price_y_per_x()` / `Pool.spot_price_x_per_y()` give the two spot quotes.
- `Pool.quote_x_for_y(amount_x_in) -> float` / `Pool.quote_y_for_x(amount_y_in) -> float` — quotes in both directions, no state change.
- `Pool.price_impact_pct(amount_x_in) -> float` — % worse than spot from curve movement alone, fee excluded.
- `Pool.slippage_pct(amount_x_in) -> float` — % worse than the spot quote with the fee included; always ≥ the price impact. Keeping the two separate is the point: one is the pool's shape, the other is what the trader actually pays.
- `Pool.swap_x_for_y(amount_x_in, min_out=None) -> (float, Pool)` / `Pool.swap_y_for_x(amount_y_in, min_out=None) -> (float, Pool)` — execute against successive pool states. Fail-closed: raises `InsufficientOutputError` (a `ValueError`) when the output is below `min_out`.
- `ensure_min_out(amount_out, min_out) -> float` — the standalone min-out guard: returns the amount, or raises `InsufficientOutputError` when it falls short; `min_out=None` disables the guard, a negative `min_out` raises `ValueError`.
- `get_amount_in(amount_out, reserve_in, reserve_out, fee_bps=25) -> float` — the exact-out inverse of `get_amount_out`: the gross input needed to receive exactly that output. Fail-closed: an output at or beyond the whole reserve can never be bought and raises `ValueError`, as do non-positive inputs/reserves and out-of-range fees.
- `Pool.quote_x_in_for_y_out(amount_y_out) -> float` / `Pool.quote_y_in_for_x_out(amount_x_out) -> float` — exact-out quotes in both directions, built on `get_amount_in`.
- `Pool.price_impact_pct_y_in(amount_y_in) -> float` / `Pool.slippage_pct_y_in(amount_y_in) -> float` — the sell-direction twins of the buy-direction impact/slippage pair: curve movement alone vs what the seller actually loses against spot, fee included.
- `chained_quote(amount_in, hops) -> float` — a multi-hop route quote: `hops` is a sequence of `(reserve_in, reserve_out, fee_bps)` pool triples and each hop's output feeds the next, so every leg charges its own fee and impact. Fail-closed: an empty or `None` route raises `ValueError`.
- `verify_lp_lock(lp_total_supply, burn_balance, locker_balances, deployer_balance=None, threshold_pct=90.0) -> LPVerdict` — burned plus locker-held LP as a % of total supply; LP in the deployer wallet is reported but never counted as secured. Verdict is `"locked"` (passed=True) iff secured ≥ threshold, else `"unlocked"`. Fail-closed: any missing input (`None` supply, burn balance, or locker balances), a non-positive supply, or balances exceeding the supply — each balance alone *or all of them together* claiming more than 100% of it — return `"unverifiable"` with passed=False — never a guessed "locked". A `threshold_pct` outside `(0, 100]` (including `None`) raises `ValueError`.
- `lp_share_pct(lp_balance, lp_total_supply) -> float` — an LP balance as a % of total LP supply; a balance above the supply is a contradiction and raises `ValueError`, as do a negative balance and a non-positive supply.
- `pro_rata_amounts(lp_balance, lp_total_supply, reserve_x, reserve_y) -> (float, float)` — the `(x, y)` reserves that share of the LP supply could redeem.
- `impermanent_loss_pct(price_ratio) -> float` — impermanent loss vs holding for a constant-product LP, `2·√r/(1+r) − 1` in %: always ≤ 0, exactly 0 at r = 1, and symmetric under r → 1/r. Raises `ValueError` on a non-positive ratio.
- `estimate_sandwich(pool, victim_amount_x_in, attacker_amount_x_in) -> SandwichResult` — simulates front-run buy → victim buy → attacker sell-all across successive pool states and returns the victim's extra loss in % versus the same trade alone, plus the attacker's profit in x. Raises `ValueError` on non-positive trade sizes.

`demo.py` walks through two-way quotes, impact vs slippage by trade size, a refused min-out swap, three LP verdicts, and a sandwich estimate on a deep vs a thin pool.

## Test suite

45 tests, all passing (`pytest -q`: "45 passed"). Covered: known x·y=k quote values with and without a 25 bps fee; quotes in both directions and a round trip that loses but never gains; swaps moving the pool price; price impact and slippage both growing with trade size, with slippage always ≥ impact; the min-out guard triggering above the quote and passing at it; LP verdicts — 95% burned → locked, burn + locker balances summing across the threshold, exactly 90% → locked, 40% burned with the rest deployer-held → unlocked, and four missing/inconsistent-data shapes → unverifiable with passed=False; sandwich victim loss positive, and strictly larger on a 10× thinner pool at the same spot price; and `ValueError` on invalid reserves and amounts. Added in the deepening pass: exact-out quoting — `get_amount_in` round-tripping `get_amount_out` at 0/25/100 bps plus a zero-fee hand value, rejection of outputs at/beyond the reserve and of bad fees, and the Pool exact-out methods inverting the Pool quotes in both directions; multi-hop routes — a two-hop `chained_quote` equalling the manual leg-by-leg composition, a one-hop route equalling the plain quote, the routed amount beating the second hop's naive spot quote, and empty/`None` routes raising; sell-direction impact and slippage growing with size with slippage ≥ impact and a 50% hand value for a reserve-sized sale, plus x-direction impact hand values (50% at in == reserve, 20% at a quarter of it); pool invariants — `k` exactly preserved at zero fee and strictly growing with a fee, quotes/swaps never mutating the original pool, a y→x swap moving the spot price and respecting `min_out`, and `ensure_min_out` edge cases (zero and exactly-at guards pass, a negative guard raises); LP share math — share % and pro-rata redemption hand values including full redemption, and rejection of over-supply/negative balances; impermanent loss — 0 at r = 1, exactly −20% at both r = 4 and r = 0.25, negative for any move, non-positive ratio rejected; and LP verdict depth — a custom threshold flipping the same 60%-secured fixture between locked and unlocked (including its own 60/61 boundary), a locker balance tipping 89.9% to exactly 90.0% locked, empty list/dict locker inputs and the omitted-deployer default, negative or `None` balance entries raising, secured-over-supply → unverifiable, the new total-balances-over-supply → unverifiable check (60% secured + 60% deployer claims 120%), and invalid thresholds (0, 101, `None`) raising.

## Captured output

Verbatim output of `python demo.py`:

```
=== AMM + LP lab (synthetic constant-product pool) ===
Pool: 100 SOL / 1,000,000 tokens, spot 10,000 tokens per SOL

-- Quotes both directions --
1 SOL in -> 9,876.5 tokens out
Those tokens back in -> 0.9756 SOL out (fees + impact)
Module-level check: get_amount_out(1, 100, 1_000_000) = 9,876.5

-- Impact grows with size --
Buy   0.1 SOL: price impact  0.10%, slippage  0.35%
Buy   1.0 SOL: price impact  0.99%, slippage  1.24%
Buy  10.0 SOL: price impact  9.09%, slippage  9.30%
Buy  50.0 SOL: price impact 33.33%, slippage 33.44%

-- Min-out guard --
Swap correctly refused: output 9876.482091 below min_out 9975.246912; swap refused

-- LP lock verification --
Mostly burned: verdict=locked secured=95.0% (95.0% burned+locked >= 90%)
Half deployer-held: verdict=unlocked secured=50.0% (only 50.0% burned+locked < 90% threshold)
Data missing: verdict=unverifiable secured=n/a (missing data: lp_total_supply)

-- Sandwich estimate --
deep pool (100 SOL): victim normally gets 9,876, sandwiched 9,495, extra loss 3.86%, attacker profit +0.0290 SOL
thin pool (10 SOL): victim normally gets 9,070, sandwiched 6,398, extra loss 29.46%, attacker profit +0.3052 SOL
```

## What this mirrors in the live bot

This mirrors two live paper-bot components: the LP-lock verification idea behind `daemon/launch_scanner/lp_lock.py` and the locked-liquidity scanner (`scripts/locked-liq-scan`), which only cards coins whose LP is verifiably burned/locked — the ≥90% secured rule and the fail-closed "unverifiable" verdict are the same shape of check — and the honest-fill quoting used for paper entries on graduated Raydium/PumpSwap pools, where impact, slippage, and a min-out refusal decide whether a fill was real. It is a teaching/checking model only; it shares no code with the live bot.

## Self-exam

1. Why are price impact and slippage different numbers for the same trade, and which one should a min-out guard be built from?
2. Why does LP sitting in the deployer wallet not count toward "locked", even if the deployer promises not to touch it?
3. What should a checker return when the locker balances were never fetched — and why is returning "unlocked" there also wrong?
4. Why does the same sandwich attack cost the victim so much more in a thin pool than in a deep pool at the same spot price?
5. In the sandwich simulation, why must each of the three swaps run on the pool state left by the previous one?
