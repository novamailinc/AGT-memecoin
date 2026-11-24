# Smart-Wallet Grading (lab)

A from-scratch, pure-stdlib lab that grades tracked "smart" wallets the honest way: not by what the wallet itself made, but by what a copier arriving late would have made. It simulates follows at three fixed lag gains against a synthetic price path, corrects for wallet packs that share one funder, shrinks small samples toward a neutral prior before turning them into a weight, and auto-mutes wallets whose followers reliably lose. All wallets, mints and prices are invented fixtures.

## What it does

- Replays a wallet's buy through a price path and enters the copier only when price first reaches +15%, +50% or +100% over the wallet's entry; if the level never comes, there is no follow at all.
- Exits every copier at the last path point, so a run-then-die path punishes larger lags exactly the way a real copy delay would.
- Collapses wallets sharing a funder into a single independent actor before any "several wallets agree" signal is counted.
- Converts a wallet's follow record into a weight in [0.5, 1.5] with shrinkage, and mutes a wallet once it has enough follows and a bad enough average.

## Files

- `grading.py` — `follow(event, price_path, lag_gain) -> dict | None`: `event` is `{wallet, mint, t, price}`, `price_path` is `[(t, price), ...]`, `lag_gain` must be one of `LAG_GAINS = (0.15, 0.50, 1.00)` (anything else raises `ValueError`); returns entry/exit plus `realized_mult = exit_price / entry_price` and `realized_fraction`, or `None` when the lag level is never reached. The entry price is the first path print at/above the target (an exact print counts), so overshoot is charged to the copier. Fail-closed: an unreached level is a skip, never a chased entry, pre-buy path points are ignored, and an empty path is a skip.
- `grading.py` — `follow_all_lags(event, price_path) -> {lag: result | None}` over exactly `LAG_GAINS`; `wallet_outcome_fraction(event, price_path) -> float | None` (the wallet's own hold-to-the-end fraction, the baseline copiers are graded against); `lag_cost_fraction(event, price_path, lag_gain) -> float | None` (wallet fraction minus copier fraction; `None` when the copier never entered); `follow_with_stop(event, price_path, lag_gain, stop_fraction) -> dict | None` (same entry, but exits at the first later print at/below `entry * (1 - stop_fraction)`, flagged `stopped_out`; `stop_fraction` must be in (0, 1)).
- `grading.py` — `simulate_follows(events, paths_by_mint, lag_gain) -> list` (skips dropped) and `wallet_stats(follow_results) -> {wallet: (n, avg_realized_fraction)}`; `median_realized(follow_results) -> float | None` gives the median realised fraction (None on empty).
- `grading.py` — `group_wallets(wallets_with_funders) -> {funder: [wallet, ...]}` accepting a dict, dict rows, or `(wallet, funder)` pairs, with a funder-less (or empty-funder) wallet treated as its own funder and duplicate wallet rows de-duplicated; `convergence_count(wallets_with_funders) -> int` is the number of distinct funders; `independence_ratio(...) -> float` is actors per wallet (0.0 when empty); `meets_independence(..., min_actors) -> bool`; `largest_pack(...) -> (funder, wallets) | None` (ties broken alphabetically). Fail-closed: unknown funding never merges wallets into a pack.
- `grading.py` — `grade_weight(n_follows, avg_realized, prior=0.0, m=10) -> float`: `shrunk = (n * avg_realized + m * prior) / (n + m)`, `weight = clamp(1.0 + shrunk, 0.5, 1.5)`, so the neutral prior maps to weight 1.0; negative `n_follows`/`m` raise `ValueError`.
- `grading.py` — `is_muted(n_follows, avg_realized) -> bool`: muted iff `n_follows >= 10 and avg_realized <= -0.20`; `grade_wallet(n_follows, avg_realized, prior=0.0) -> dict` bundles weight and mute flag; `grade_all(stats, prior=0.0) -> dict` grades a whole `wallet_stats` mapping; `rank_wallets(stats, prior=0.0) -> [(wallet, grade)]` sorts by weight, then follow count, then name, deterministically.
- `demo.py` — scenario walkthrough: lag decay on a dying path, a skipped follow, a 4-wallet/2-funder pack, and four grade examples.
- `test_grading.py` — the pytest suite below.

## Test suite

45 tests, all passing. Coverage: entry at the first path price reaching the lag target (an exactly-on-target print counts), lag decay on a rising-then-dying path (realised at +15% entry strictly greater than at +50% and +100%), a follow skipped when the level is never reached and on an empty path, entry on the very last path point realising exactly 1.0x, overshoot charged to the copier (entry is the print, not the target), rejection of an undocumented lag gain (also propagated through `simulate_follows`), prices before the wallet's buy ignored, `follow_all_lags` keys and all-skip shape, the wallet's own outcome fraction as baseline (including no-point-after-the-buy), lag cost quantified for two lags and `None` on a skip, a stop-loss follow exiting on the first breach, beating hold-to-the-end on a collapsing path, matching plain `follow` when never triggered, and rejecting stop fractions of 0 and 1, simulation/aggregation across wallets plus empty stats, median realised for odd/even/empty result sets, a 3-wallet single-funder pack counting as 1 independent actor, two funders counting as 2, funder-less and empty-string-funder wallets standing alone, duplicate wallet rows de-duplicated, independence ratios of 0.25 / 1.0 / empty, the independence threshold and its validation, largest pack with an alphabetical tie-break, shrinkage pulling a small-n grade toward the prior (exact values checked), clamping at 0.5 and 1.5, a non-zero prior, `m=0` removing the prior (and the n=0, m=0 corner returning the prior), weight monotonic in both average and follow count, negative counts rejected, `grade_all`/`rank_wallets` ordering including the evidence-then-name tie-break, the grade bundle's internal consistency, and the mute boundary — exactly 10 follows at exactly −0.20 muted, 9 follows never muted, −0.19 not muted.

## Captured output

Verbatim output of `python demo.py` (exit 0):

```
=== Smart-wallet grading demo (synthetic, paper only) ===

-- One smart buy, three copier lags, path runs then dies --
path: 1.00 -> 1.20 -> 1.60 -> 2.10 -> 1.40 -> 0.40 (exit)
lag +15%: entry $1.20 at t=1 -> realized 0.333x
lag +50%: entry $1.60 at t=2 -> realized 0.250x
lag +100%: entry $2.10 at t=3 -> realized 0.190x

-- A signal that never reaches the +15% follow level --
follow result: None (skipped, not chased)

-- Pack independence: 4 wallets, 2 funders --
wallets=4, independent actors=2

-- Grades: shrinkage weight and auto-mute --
W-ALPHA: n=1, avg realized -66.7% from this one follow
new wallet, 2 lucky follows: weight=1.150, muted=False
proven wallet, 50 follows: weight=1.500, muted=False
copier trap, 12 follows: weight=0.836, muted=True
small sample, 9 bad follows: weight=0.763, muted=False

Demo complete.
```

## What this mirrors in the live bot

The live bot keeps a curated smart-wallet registry, weights each buying wallet by a reconciled, shrinkage-adjusted grade when it scores candidates, and treats a cluster of wallets funded by one operator as a single pack rather than independent confirmation — a confirmed multi-wallet pack sits in that registry today. It also learned the hard way that smart-money agreement is a display signal, not proof a late copier profits, which is exactly the question this lab's lag simulation asks. This is an original re-implementation on invented data; no registry, grades or live code were read or reused.

## Self-exam

1. On the demo path, why does every copier lose money even though the wallet bought at 1.00 and the token more than doubled?
2. What entry price does the +15% copier actually pay, and why is it not exactly 1.15?
3. Three wallets buy the same mint within a minute. What extra fact decides whether that is strong convergence or just one actor?
4. A wallet averages +90% over 2 follows and another averages +90% over 50. Why do their weights differ, and what are the two weights?
5. Why does the mute rule refuse to fire at 9 follows averaging −50%, and what risk is that refusal accepting?
