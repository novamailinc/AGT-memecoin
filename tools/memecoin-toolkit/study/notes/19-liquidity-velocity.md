# Liquidity Velocity — study notes

## Core ideas (teach-back)

In a launch's first minutes, the most visible question is "how fast
is money arriving?" This toolkit measures that pace from a stream
of synthetic `Flow(ts, sol_in)` events — seconds since launch and
SOL in — and then asks the uncomfortable follow-up: *even if the
pace predicts what happens next, is following it worth money?* The
two answers are different, and keeping them separate is the whole
lesson [S].

The pace statistics:

- **Inflow rate** — total SOL in divided by the stream's span
  (first to last event), in SOL/minute.
- **Trade rate** — trades per minute over the same span.
- **Trades-normalised velocity** — inflow per minute per 100
  trades, a size-adjusted pace comparable between launches with
  different trade counts.
- **Progress rate** — inflow rate expressed as bonding-curve
  completion percentage points per minute against the graduation
  target (default **85 SOL**).
- **Minutes to graduation** — remaining SOL to target at the
  current rate; `None` once the target is already collected
  ("already there" is a different fact from "0 minutes away").

Rates are banded: **slow** < 0.5, **steady** < 2.0, **fast** < 5.0,
**igniting** ≥ 5.0 SOL/min (a rate exactly at a boundary takes the
higher band). A 0–100 **graduation score** combines inflow pace
(up to 60 points across those bands) with trade pace (up to 40);
a launch is predicted to graduate iff score ≥ **60** (default
threshold). Below **10 events** (`MIN_TRADES`) the predictor fails
closed: score `None`, status `"unknown"`, no call made. The
low-level rate functions are stricter still — they raise
`ValueError` on empty, single-event, or zero-duration streams
rather than dividing by nothing [S].

Then the honest part. On seeded calibration samples the velocity
cut genuinely classifies graduation well (**0.97 accuracy**). But
the calibrated *gross* edge of following the predictor is only
**+3.0%**, and a realistic memecoin round trip costs ~**4%** — so
the edge after costs is **−1.0%** and the predictor does **not**
clear breakeven. Predicting an event is not the same as profiting
from it [S]. This mirrors a core live-program finding: velocity
predicts graduation, graduated tokens still mostly bleed, and lanes
stay paper-only until a *ledger* (not a predictor) shows positive
expectancy after costs [M].

## Key derivations & formulas

Span convention first: input is sorted by timestamp, and

```
duration_minutes = (last_ts − first_ts) / 60
inflow_rate      = total_inflow / duration_minutes        [SOL/min]
trade_rate       = n_events / duration_minutes            [trades/min]
normalised_vel   = inflow_rate / n_events × 100
mean_trade_size  = total_inflow / n_events
progress_rate    = inflow_rate / target_sol × 100         [pts/min]
minutes_to_grad  = (target − collected) / inflow_rate     (None if collected ≥ target)
```

**Graduation score**, step by step [S]:

```
inflow points:  rate ≥ 5.0 → 60 | ≥ 2.0 → 45 | ≥ 0.5 → 25 | else 5
trade points:   tpm  ≥ 20  → 40 | ≥ 10  → 30 | ≥ 5   → 18 | ≥ 2 → 8 | else 0
score = min(100, inflow_points + trade_points)
predict will_graduate ⇔ score ≥ threshold (default 60), n ≥ 10 events
```

**Calibration.** Seeded samples (seed 7, 60 per class): graduates
drawn around **4.0 SOL/min** (σ 1.2), non-graduates around **0.8**
(σ 0.5), floored at 0.05. A rate cut at 2.0 SOL/min classifies them
with accuracy **0.97** [S].

**The cost reckoning** — the derivation the toolkit exists for:

```
calibrated outcome: 55 winners at +30%, 45 losers at −30%
gross_edge = (55 × 0.30 + 45 × (−0.30)) / 100
           = (16.5 − 13.5) / 100 = +0.030

edge_after_costs = gross_edge − round_trip_cost
                 = 0.030 − 0.040 = −0.010
clears_breakeven ⇔ edge_after_costs > 0   (strictly; exactly at
                 cost does NOT clear)  → False
breakeven_gross_edge = round_trip_cost = 0.04
```

At a 2% cost the same predictor *would* clear (+0.010); at 6% it
would need a gross edge above 6%. The verdict lives in the cost
line, not the accuracy line [R, arithmetic on S].

## Worked example

Demo streams, 20 events each [S]:

- **Fast launch** — 3.0 SOL every 10s: total 60 SOL over 190s
  (3.167 min). Inflow rate `60 / 3.167 = 18.95 SOL/min`
  (**igniting**); trade rate `20 / 3.167 = 6.32/min`; progress
  rate `18.95 / 85 × 100 = 22.29 pts/min`. Score: 60 (inflow) +
  18 (trade pace 6.32 ≥ 5) = **78** → `will_graduate: True`.
- **Slow launch** — 0.1 SOL every 60s: total 2 SOL over 1,140s
  (19 min). Inflow `2 / 19 = 0.11 SOL/min` (**slow**); trade rate
  1.05/min; progress 0.12 pts/min. Score: 5 + 0 = **5** →
  `will_graduate: False`.
- **Sparse** — 3 events of 3 SOL in 20s: the raw rate would be
  enormous, but 3 < 10 events → `{'status': 'unknown', 'score':
  None, 'will_graduate': None}`. No confident score from thin air.

Calibration vs costs, verbatim from the demo: accuracy **0.97**,
gross edge **+0.030**, round-trip ~4%, edge after costs **−0.010**,
clears breakeven **False** [S].

## Trap log

- T1. Confusing prediction accuracy with profit. 0.97 accuracy
  measures event classification (did it graduate?); it says
  nothing about post-graduation returns, which is where the money
  is decided.
- T2. Scoring thin streams. Three events totalling 90 SOL in 20s
  produce a huge naive rate and zero evidence; the `MIN_TRADES`
  floor exists so a pace print cannot impersonate a pattern.
- T3. Dividing by a fixed window instead of the stream span. The
  span convention flatters streams whose events cluster at the
  edges; know which clock your rate is on before comparing two
  launches. (The toolkit documents its choice; the trap is
  forgetting it.)
- T4. Returning 0 for "already graduated". `minutes_to_graduation`
  returns `None` at the target — collapsing "done" into "instant"
  would corrupt any downstream ETA logic.
- T5. Treating exactly-at-cost as breakeven-clearing.
  `clears_breakeven` is strictly `> 0`: an edge equal to costs is
  a zero, and zero-edge trading still pays the risk.
- T6. Trusting raw inflow rate where pace is manufactured. A few
  huge trades inflate SOL/min while trade pace stays dead; the
  normalised velocity and the two-component score exist to catch
  the disagreement. (Cross-check with the wash detectors —
  pace can be bought.)
- T7. Lowering the graduation threshold to "catch more winners".
  The threshold trades classification against edge; the pinned
  result is that even the *calibrated* predictor's edge dies at
  the cost line, so loosening the gate buys losses faster.

## Source flags

- Rate definitions and span convention, bands (0.5 / 2.0 / 5.0),
  score point tables, `MIN_TRADES = 10`, target 85 SOL, calibration
  distributions and 0.97 accuracy, and the pinned cost figures
  (+0.030 / −0.010 / False) are from `liqvelocity.py`, covered by
  `test_liqvelocity.py` (39 tests) [S].
- The 2%- and 6%-cost what-ifs are arithmetic on the source
  formula [R].
- The live-bot parallel (ignition metrics on the feeds; the
  predictor-vs-ledger paper-only stance) is per the README [M].

## One-paragraph verdict

This toolkit proves two things at once, which is its value: early
inflow velocity really does predict graduation (0.97 calibration
accuracy, hand-checkable rates and scores), *and* that predictive
power is commercially worthless at the calibrated edge, because
+3% gross cannot pay a ~4% round trip. It does **not** prove
velocity is useless — as a filter or a timing cue inside a strategy
whose edge comes from elsewhere it may earn its keep — nor that
real launches match the synthetic calibration (graduates at 4.0 vs
0.8 SOL/min is a lab distribution); it proves the narrower, harder
point that "we can predict the event" must never be submitted as
evidence for "we can profit from the event".
