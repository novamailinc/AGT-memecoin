# Exit Ladder Replay — study notes

## Core ideas (teach-back: explain it as if teaching a smart beginner; the actual concepts from THIS toolkit, with its real numbers/thresholds)

Entry quality gets the glory, but exits decide what you keep.
This lab in `replay.py` walks a synthetic price path bar by bar
— strictly in time order, no look-ahead — and applies an exit
ladder to it, so a policy is judged by realised multiples
instead of argued about [S].

The motivating leak is the round-tripper: a coin that doubles
(peak 2.6x), then fades all the way back through the stop. If
you simply hold, you close at 0.5x — a loser on a coin that
doubled. The user's ladder turns that identical path into a
1.58x close by harvesting on the way up. The exit, not the
entry, made the difference [S].

The user's ladder, `USER_LADDER` [S]:

- 50% sold at 2.0x (take-profit).
- 25% sold at 3.0x (take-profit).
- 25% trailing remainder: trail 30% below the running peak,
  but *armed only once price reaches 2.0x*.
- Hard stop 0.5x: price at/below it exits everything at once.
- Time cap 1440 minutes (24h).

The arm is the subtle part. Without it, a coin that never ran
would "trail" out at, say, 0.77x on its first dip — a fake exit
that also breaks the hard stop's clean meaning. Below 2.0x the
hard stop alone protects the trailing tranche [S].

Two comparison ladders frame it. `FAST_HARVEST`: 50% at 1.5x,
25% at 2.0x, 25% trailing at 20% armed at 1.5x, stop 0.6x,
cap 240 min. `HOLD_TO_STOP`: one 100% hold tranche, stop 0.5x,
cap 1440 min — the harvest-nothing baseline [S].

Fill conventions are deterministic and conservative: TPs fill
at their target price (not the overshooting bar's price),
trailing exits fill at `peak × (1 − trail_pct)` off the
*running* peak, hard stops fill at the stop price even on a gap
down, and time-cap/path-end exits fill at that bar's price. A
fill only ever uses bars up to and including the current bar —
the test suite includes traps that would catch a future-peeking
engine [S].

## Key derivations & formulas (the math the toolkit implements, shown step by step where non-trivial)

Realised multiple is the fraction-weighted sum of leg exits
(all fixtures use entry p0 = 1.0, so prices are multiples) [S]:

    realized_mult = Σ fraction_i × exit_price_i / p0
    legs always sum to 100% of the position (test-pinned)

Per-bar evaluation order in the engine [S]:

    1. past time cap → never seen (bars beyond the cap do not
       exist for the replay; a post-cap crash changes nothing)
    2. hard stop: price ≤ 0.5 × p0 → close ALL at stop price
    3. take-profits: price ≥ target × p0 → close that tranche
       at the target price
    4. trailing: if armed (peak ≥ arm_mult × p0) and
       price ≤ peak × (1 − trail_pct) → close at trail price
    5. at t ≥ cap → close all at that bar's price (time_cap);
       at path end → close remainder at last price (path_end)

Custom ladders are validated before any bar replays:
`validate_ladder` requires tranche fractions summing to 1.0, TP
targets > 1.0, trail percentages in (0, 1), hard stop in (0, 1),
and a positive time cap — violations raise `ValueError` [S].

Path analytics (after-the-fact, running quantities only) [S]:

    peak_multiple        = max(price) / p0
    max_drawdown         = max over bars of
                           (running_peak − price) / running_peak
                           — never measured off a future peak
    time_above_multiple  = discrete-bar interval time spent
                           at/above a multiple
    first_crossing_time  = first bar at/above the multiple,
                           or None

Batch aggregation: `replay_batch` returns `mean_mult`,
`win_rate`, `round_trippers`, `median_time_to_peak_min`; an
empty batch returns zeros rather than crashing. A result is a
round-tripper when it peaked ≥2x yet realised < 1.0 [S].

## Worked example (concrete numbers taken from the toolkit's demo/fixtures)

Verbatim from `demo.py`, all paths p0 = 1.0 [S]:

round_tripper (peak 2.60x at t=50):

- user_ladder 1.580x — legs: 50%@2.00x (tp), 25%@1.82x
  (trail), 25%@0.50x (hard_stop).
  Check: 0.5×2.00 + 0.25×1.82 + 0.25×0.50
       = 1.000 + 0.455 + 0.125 = 1.580 ✓ [R]
- fast_harvest 1.770x; hold_to_stop 0.500x. The leak, priced.

clean_runner (peak 5.00x at t=60):

- user_ladder 2.625x — 50%@2.00, 25%@3.00, 25%@3.50 (trail).
- fast_harvest 2.250x; hold_to_stop 3.500x (path_end). Here
  holding wins — the ladder pays for round-trip insurance
  with some runner upside. That trade-off is the honest
  finding, not a defect [R].

straight_rug (peak 1.10x): user_ladder 0.500x (all three
tranches out at the stop), fast_harvest 0.600x (its stop is
0.6), hold_to_stop 0.500x.

slow_grinder (peak 2.05x at t=1200): user_ladder 1.900x —
50%@2.00 (tp), then 25% + 25% at 1.80x on the 1440-min cap;
fast_harvest 1.200x (everything out at its 240-min cap);
hold_to_stop 1.800x (time_cap).

Batch over all 4 paths, user_ladder: mean 1.65125, win rate
0.75, round-trippers 0, median time-to-peak 55.0 min [S].

## Trap log (numbered T1, T2, … — the failure modes/misconceptions this toolkit exists to catch, each 1–3 lines)

- T1. Look-ahead. Pricing any fill off a later bar fabricates
  profit. The suite truncates paths at the exit bar and moves
  spikes after the stop to prove the engine cannot see them.
- T2. Trailing from entry. An unarmed trail exits noise dips
  (0.77x) and muddies the stop; the 2.0x arm exists so only a
  proven runner gets a trailing remainder.
- T3. Filling TPs at the bar price. A bar that gaps to 4x
  would flatter a 2x TP; filling at target is the conservative,
  reproducible convention.
- T4. Measuring drawdown off the final peak. Max drawdown uses
  the running peak only — the future peak was unknowable then.
- T5. Judging a ladder on one path. Hold beats the ladder on
  the clean runner and loses catastrophically on the
  round-tripper; batches, not anecdotes, judge policies.
- T6. Forgetting legs must sum to 100%. A ladder whose
  tranches sum to 0.9 silently holds 10% forever;
  `validate_ladder` rejects it before replay.
- T7. Seeing bars past the time cap. The grinder's post-cap
  fate is unknowable to the position — it already exited.
- T8. Sloppy path data. Empty paths, non-positive/NaN prices,
  a start other than t=0, or non-increasing timestamps all
  raise — a replay on bad bars proves nothing.

## Source flags (mark key claims [S] = proven in the toolkit's own tests/source, [R] = reasoned/derived here, [M] = mirrors live-bot practice per the README)

- Ladder definitions (50%@2x, 25%@3x, 25% trail 30% armed
  2.0x, stop 0.5, cap 1440; fast-harvest and hold parameters),
  fill conventions, no-look-ahead traps, leg-sum invariant:
  [S] — 44 tests.
- All demo multiples and batch stats (1.580x leak fix, 2.625x
  runner, mean 1.65125): [S] — verbatim captured output.
- The live mirror/CEO ledgers running the user's ladder on
  every fired card, plus the two-tier leg structure: [M] —
  per README; the lab isolates the exit policy only.

## One-paragraph verdict (what this toolkit proves and what it does NOT prove)

This toolkit proves an exit ladder changes outcomes on
identical entries: the user's ladder converts a doubling-then-
dying path from 0.5x to 1.58x, at the measurable cost of
capping a clean runner at 2.625x versus 3.5x for holding —
with no-look-ahead enforced by adversarial tests across 44
cases [S]. It does not prove the ladder is optimal or that
these multiples occur live: the four paths are synthetic,
fills ignore liquidity, fees, and slippage, and real bar data
is messier than fixtures — the lab prices *policies*, not
markets [R].
