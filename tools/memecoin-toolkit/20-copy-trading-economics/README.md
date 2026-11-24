# Copy-Trading Economics (lab)

A from-scratch, pure-stdlib lab for the economics of copying a smart-wallet cluster: a leader buys at t0 on a seeded synthetic memecoin price path, followers pile in at fixed lags paying extra slippage, and everyone exits on the same simple ladder. Everything here is synthetic — no network, no live bot code, no real token data — and the headline result is *produced by the simulation*, then pinned by tests, not asserted in advance.

## What it does

Module `copyecon.py`:

- `generate_price_path(seed, steps=400) -> list[float]` — a seeded, deterministic synthetic path starting at 1.0 with 5-second steps: a sharp pump (drift +3%/step for the first 25 steps, ~2 minutes), a hard bleed (−1.2%/step to step 200), then a slower bleed (−0.4%/step), volatility 3%/step, floored at 0.02. Raises `ValueError` on a `None` seed or fewer than 10 steps. The same seed always reproduces the same path.
- `lag_to_steps(lag_seconds) -> int` — lag → path steps at 5 s/step (15 s → 3, 1 m → 12, 5 m → 60, 15 m → 180). Raises `ValueError` on negative input.
- `LAG_BUCKETS` — `{"15s": (15, 0.01), "1m": (60, 0.02), "5m": (300, 0.05), "15m": (900, 0.15)}`: each lag's extra slippage, growing with the lag; the leader pays 0.005.
- `simulate_copy(path, entry_index, slippage) -> float` — one copy trade's return fraction, with the exit ladder implemented inline: sell 50% when the market price first reaches 2× the entry spot, let the rest ride on a 30% trailing stop from the peak, cut everything at a −50% hard stop if 2× never comes, and exit anything left at the final price. Ladder triggers are read off the *market* price relative to entry spot, but proceeds are divided by the slippage-inflated effective entry price, so slippage drags every exit, not just the entry. Fail-closed: raises `ValueError` on a path with < 2 prices, any non-positive price, an entry index at/past the last price, or slippage outside `[0, 1)`.
- `simulate_leader(path) -> float` / `simulate_follower(path, lag_label) -> float` — the leader (t0, lowest slippage) and one follower bucket. Raises `ValueError` on an unknown bucket label.
- `expectancy(returns) -> float` / `win_rate(returns) -> float` — mean return and share of positive returns. Both raise `ValueError` on empty/`None` input.
- `expectancy_by_lag(seeds=range(200), lags=None) -> dict` — mean return for `"leader"` plus each lag bucket across the seeded paths. Raises `ValueError` on empty seeds or an unknown bucket.
- `lag_penalty(seeds) -> dict` — follower expectancy minus leader expectancy, per bucket.
- `is_copying_viable(seeds, lag_label) -> bool` — `True` only if that bucket's expectancy is positive. It is `False` for every bucket here.

The honest result, over 200 seeded paths: leader **+31.2%**, 15 s copy **−6.2%**, 1 m **−50.3%**, 5 m **−53.4%**, 15 m **−55.7%** — delayed copying is negative at *every* lag and worsens monotonically, because by the time a follower's transaction lands the pump is spent and the bleed is theirs.

`demo.py` prints the expectancy table and the per-lag penalties.

## Test suite

38 tests, all passing (`pytest -q`: "38 passed"). Covered: path shape and determinism (starts at 1.0, same seed = same path, pumps then bleeds on average, custom lengths, seed/step validation); lag plumbing (all four step conversions, slippage strictly growing with lag and above the leader's); the ladder trade-by-trade on hand-built paths (flat path loses exactly the slippage, a clean double pays the full ladder, the trailing stop exits the rest after a peak, the hard stop cuts at −50% and includes the slippage drag, returns bounded above total loss); all `simulate_copy` validation paths; statistics helpers on hand values; and the simulated headline result re-verified on every run — leader expectancy positive, *every* follower bucket negative, expectancy strictly worsening across leader → 15 s → 1 m → 5 m → 15 m, the 15-minute copy below −40%, lag penalties negative and deepening, follower win rates collapsing with lag, copying viable at no lag, lag alone (zero slippage, equal-slippage aggregate) already costing the follower, and the expectancy table deterministic per seed set.

## Captured output

Verbatim output of `python demo.py`:

```
=== Copy-trading economics lab (seeded synthetic paths) ===
Leader buys at t0; followers copy at a lag, paying extra slippage.
Exit ladder: 50% at 2x, rest on a 30% trail, -50% hard stop.

-- Expectancy per bucket across 200 seeded paths --
leader: +31.2%
copy at  15s: -6.2%  (viable: False)
copy at   1m: -50.3%  (viable: False)
copy at   5m: -53.4%  (viable: False)
copy at  15m: -55.7%  (viable: False)

-- Penalty vs the leader --
copy at  15s: -37.4% vs leader
copy at   1m: -81.5% vs leader
copy at   5m: -84.6% vs leader
copy at  15m: -86.9% vs leader

Delayed copying is negative at every lag and worsens with lag.
The information is stale and the price has already moved.
```

## What this mirrors in the live bot

This mirrors why the live bot demoted smart money to watchlist-only. The research behind that decision found delayed cluster copying averaging roughly −41% — the wallets being copied are speed demons whose edge *is* the entry price, and a follower necessarily buys the top of the move they created, pays extra slippage for the privilege, and inherits the bleed. In the live bot, tracked-wallet buys still fire cards, still get journaled and graded, and still display as a 🧠 line — but smart-money agreement adds zero score in every entry path, and re-enabling it requires follower-grade evidence, not leader-grade evidence. This lab is that argument in miniature: the leader's record and the follower's record are different trades. It shares no code with the live bot.

## Self-exam

1. The leader's expectancy is +31% and the 15-second copy already loses money. Which two distinct costs turn the same signal negative that fast, and which one dominates by the 1-minute bucket?
2. Why are the ladder triggers read off the market price relative to entry *spot*, while proceeds are divided by the slippage-inflated entry — what would each simpler choice hide?
3. Per-path, a follower sometimes beats the leader on the same path (the aggregate test had to replace a per-path one). What ladder behaviour makes that possible?
4. The 5 m and 15 m buckets are close together (−53% vs −56%) compared with the 15 s → 1 m cliff. What in the exit ladder compresses the tail buckets, and what does that imply about stop design for late entries?
5. If a copy system could execute at 1-second lag with leader-grade slippage, what does this model predict — and which of its assumptions would you distrust first in the real market?
6. Why is "the leader wins 62% of the time" not evidence that copying the leader is profitable, even before slippage?
