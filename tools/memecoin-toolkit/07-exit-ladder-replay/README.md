# Exit Ladder Replay (lab)

A from-scratch, pure-stdlib replay engine that walks a synthetic price path bar-by-bar — no look-ahead — and applies a memecoin exit ladder to it, so the value of an exit policy is measured in realized multiples instead of argued about. Four synthetic paths (round-tripper, clean runner, straight rug, slow grinder) are replayed under three ladders.

## What it does

- Replays a `(t_minutes, price)` path in strict time order; a fill only ever uses bars up to and including the current bar.
- Applies three ladders: the user's ladder (50% @ 2x, 25% @ 3x, trailing remainder), a fast-harvest ladder, and a hold-to-stop baseline.
- Fills are deterministic: take-profits at the target price, trailing exits at `peak * (1 - trail_pct)`, hard stops at the stop price (exiting everything), time-cap/path-end exits at that bar's price.
- The user ladder's trailing remainder arms only at 2.0x, so a coin that never ran is protected by the hard stop alone.
- Aggregates batches into mean multiple, win rate, round-tripper count, and median time-to-peak.
- Analyses paths and results after the fact: peak multiple, first crossing time, time spent above a multiple, maximum drawdown from the running peak, exit-reason breakdown, weighted/last exit times, round-tripper classification, and side-by-side ladder comparison — and validates custom ladders (tranches summing to the whole position) before replaying them.

## Files

- `replay.py` — `replay(path, ladder="user_ladder") -> ReplayResult` (realized multiple, peak multiple, legs). Fail-closed: empty path, non-positive or non-finite entry price/prices/timestamps, path not starting at t=0, non-increasing timestamps, or unknown ladder name all raise `ValueError`. Also `time_to_peak(path) -> float`, `peaked_ge(path, mult) -> bool`, `replay_batch(paths, ladder) -> dict` with keys `mean_mult`, `win_rate`, `round_trippers`, `median_time_to_peak_min` (empty batch returns zeros, no crash). Path analytics: `peak_multiple(path)`, `first_crossing_time(path, mult) -> float | None` (first bar at/above the multiple), `time_above_multiple(path, mult)` (discrete-bar interval convention), `max_drawdown(path)` (fraction off the *running* peak, never a future one). Result analytics: `is_round_tripper(result)`, `exit_reason_breakdown(result)` (fraction per reason, sums to 1), `weighted_exit_time(result)` / `last_exit_time(result)` (both raise on a leg-less result), `compare_ladders(path, ladders=None)` (one path under several ladders). Custom ladders are checked by `validate_ladder(ladder)`: tranche fractions must sum to 1.0, tp targets must exceed 1.0, trail percentages must lie in (0, 1), the hard stop must lie in (0, 1), and the time cap must be positive — any violation raises `ValueError` before a bar is replayed. Ladder constants `USER_LADDER`, `FAST_HARVEST`, `HOLD_TO_STOP`, `LADDERS`; fixtures `make_round_tripper()`, `make_clean_runner()`, `make_straight_rug()`, `make_slow_grinder()`, `DEMO_PATHS` (all p0 = 1.0).
- `demo.py` — prints every path under every ladder, batch stats, and the leak check. Runs clean, exit 0.
- `test_replay.py` — pytest suite (see below).

## Test suite

44 tests, all passing. Coverage: the proven leak (round-tripper: user_ladder 1.58x > 1.0 while hold_to_stop is 0.5x), hard stop firing at exactly the stop price on the rug under all three ladders, leg fills summing to 100% on every path/ladder pair, clean-runner hand math (2.625x), time-cap enforcement including a post-cap crash never being seen, the slow grinder closing at the 1440-minute cap, `time_to_peak` / `peaked_ge`, fast-harvest hand math (1.77x), batch statistics on known paths, and fail-closed input validation. Added: the trailing-arm deviation pinned three ways (a dip to the would-be trail price below 2.0x does *not* trail out, arming exactly at 2.0x fills at the trail price, 1.99x stays unarmed); no-look-ahead tests that would catch a future-informed engine (a spike after the hard stop changes nothing, bars after the final exit are never seen, truncating at the exit bar reproduces the full result, and the trail fills off the running peak — 1.40 — not the later 10x peak); fill conventions (TPs fill at target, not the overshooting bar; a gap down fills the stop at the stop price; a gap up fills each TP at its own target; inclusive stop/TP/time-cap boundaries); entry-price scaling invariance and a stop price that scales with p0; hand values for `peak_multiple`, `first_crossing_time`, `time_above_multiple`, `max_drawdown` (including running- vs future-peak), and first-peak tie-breaking; result analytics (`exit_reason_breakdown`, weighted/last exit times, round-tripper classification, `compare_ladders`, single-path and empty batches, Ladder-object equivalence); custom-ladder hand math plus `validate_ladder` rejecting six distinct malformations and fraction sums ≠ 1.0; and malformed-path rejection for NaN/inf/zero/negative prices and decreasing timestamps.

## Captured output

```
Exit Ladder Replay — synthetic paths, p0 = 1.0, no look-ahead

round_tripper: peak 2.60x at t=50 min, peaked>=2x: True
  user_ladder   realized 1.580x  legs: 50%@2.00x (tp), 25%@1.82x (trail), 25%@0.50x (hard_stop)
  fast_harvest  realized 1.770x  legs: 50%@1.50x (tp), 25%@2.00x (tp), 25%@2.08x (trail)
  hold_to_stop  realized 0.500x  legs: 100%@0.50x (hard_stop)

clean_runner: peak 5.00x at t=60 min, peaked>=2x: True
  user_ladder   realized 2.625x  legs: 50%@2.00x (tp), 25%@3.00x (tp), 25%@3.50x (trail)
  fast_harvest  realized 2.250x  legs: 50%@1.50x (tp), 25%@2.00x (tp), 25%@4.00x (trail)
  hold_to_stop  realized 3.500x  legs: 100%@3.50x (path_end)

straight_rug: peak 1.10x at t=10 min, peaked>=2x: False
  user_ladder   realized 0.500x  legs: 50%@0.50x (hard_stop), 25%@0.50x (hard_stop), 25%@0.50x (hard_stop)
  fast_harvest  realized 0.600x  legs: 50%@0.60x (hard_stop), 25%@0.60x (hard_stop), 25%@0.60x (hard_stop)
  hold_to_stop  realized 0.500x  legs: 100%@0.50x (hard_stop)

slow_grinder: peak 2.05x at t=1200 min, peaked>=2x: True
  user_ladder   realized 1.900x  legs: 50%@2.00x (tp), 25%@1.80x (time_cap), 25%@1.80x (time_cap)
  fast_harvest  realized 1.200x  legs: 50%@1.20x (time_cap), 25%@1.20x (time_cap), 25%@1.20x (time_cap)
  hold_to_stop  realized 1.800x  legs: 100%@1.80x (time_cap)

Batch over all 4 paths (user_ladder):
  mean_mult: 1.65125
  win_rate: 0.75
  round_trippers: 0
  median_time_to_peak_min: 55.0

Leak check: round-tripper user_ladder 1.580x vs hold_to_stop 0.500x
```

## What this mirrors in the live bot

The live bot's exits are the user's ladder — 50% at 2x, 25% at 3x, the rest on a trailing stop, with a hard stop and a time cap — run by the mirror/CEO ledgers on every fired card. This lab isolates that exit policy from entry quality entirely: the round-tripper fixture is the known leak, a coin that doubles and still closes a loser if nothing is harvested on the way up, and the replay shows the ladder turning that same path into a 1.58x close. It also mirrors the two-tier leg structure: independent tranches, independent exits, one position.

## Self-exam

1. Why does the trailing remainder arm at 2.0x instead of trailing from entry, and which fixture would break without the arm?
2. On the round-tripper, where exactly does the 1.58x come from, leg by leg?
3. If a bar gaps straight from 1.9x to 0.4x, which fills fire, in what order, and at what prices?
4. Why is filling a take-profit at its target price (not the bar's price) the conservative choice for a replay?
5. A path ends at t=300 with no exit triggered and no cap reached — at what price does the remainder exit, and what is that leg's reason?
