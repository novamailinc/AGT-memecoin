# Copy-Trading Economics — study notes

## Core ideas (teach-back)

Copy trading sounds like free alpha: a smart wallet buys, you buy the same thing seconds later, you
share the win. This toolkit runs that story as a simulation and lets the result emerge: a **leader**
buys at t0 on a seeded synthetic price path; **followers** copy at fixed lags, paying extra slippage
that grows with the lag; everyone exits on the **same exit ladder**. Over 200 seeded paths the leader
makes money and *every* follower bucket loses — increasingly with lag. The headline is produced by the
simulation and then pinned by tests, not asserted in advance [S].

The price path is built to look like a memecoin launch: starting at 1.0, with 5-second steps over 400
steps (~33 minutes), a sharp pump (drift **+3%/step** for the first 25 steps, ~2 minutes), a hard bleed
(**−1.2%/step** to step 200), then a slower bleed (**−0.4%/step**), volatility 3%/step, floored at 0.02.
The same seed always reproduces the same path [S].

The lag buckets (`LAG_BUCKETS`) pair each delay with its extra slippage — late copies chase a thinner,
more picked-over book:

| bucket | lag | steps (5s each) | slippage |
|--------|----:|----------------:|---------:|
| leader | 0   | 0               | 0.005    |
| 15s    | 15s | 3               | 0.01     |
| 1m     | 60s | 12              | 0.02     |
| 5m     | 300s| 60              | 0.05     |
| 15m    | 900s| 180             | 0.15     |

The exit ladder is identical for everyone: sell **50%** when the market price first reaches **2×** the
entry spot; let the rest ride on a **30% trailing stop** from the peak; if 2× never comes, cut
everything at a **−50% hard stop**; anything left exits at the final price. Two distinct costs therefore
hit the follower: the **information cost** (the pump is spent by the time they land — the entry price
itself is worse) and the **slippage cost** (they pay more over spot for the same fills). The toolkit's
tests separate them: lag *alone*, with slippage zeroed or equalised, already costs the follower [S].

This mirrors why the live bot demoted smart money to watchlist-only: tracked-wallet buys still fire
cards and get journaled and graded, but smart-money agreement adds **zero score** in every entry path,
and re-enabling it requires *follower-grade* evidence, not leader-grade evidence [M].

## Key derivations & formulas

**Effective entry and proceeds.** For entry index `i` and slippage `s`:

```
spot_entry      = path[i]
effective_entry = spot_entry × (1 + s)
```

Ladder triggers are read off the **market price relative to entry spot** (`price / spot_entry`), but
every exit's proceeds are divided by the **effective entry**:

```
proceeds += fraction_sold × (exit_price / effective_entry)
return   = proceeds − 1.0        (per 1.0 unit invested)
```

That split is the honest accounting: triggers describe what the market did; the divisor describes what
*you* paid. Reading triggers off the effective entry would hide slippage inside the ladder; dividing by
spot would pretend slippage never happened [S].

**Ladder walk**, per step after entry, tracking the running peak:

```
if not sold_half and price/spot ≥ 2.0:
    sell 0.5 at this price; sold_half = True; continue
elif sold_half and price ≤ peak × (1 − 0.30):
    sell the remaining 0.5 (trailing stop); stop
elif not sold_half and price/spot ≤ 0.50:
    sell everything (hard stop); stop
at path end: sell any remainder at the final price
```

Note the hard stop only exists in the "2× never came" branch, and the trailing stop only exists after
the half has been sold — the ladder's two halves live different lives, which is what lets a follower
occasionally beat the leader on a single path (the leader may have banked half and trailed out while a
later entrant's position rides a second wind) even though the aggregate is brutal [S].

**Aggregation.**

```
expectancy(returns) = mean return        (empty/None → ValueError)
win_rate(returns)   = share of returns > 0
lag_penalty(bucket) = expectancy(bucket) − expectancy(leader)
is_copying_viable   ⇔ expectancy(bucket) > 0   (False for all here)
```

`simulate_copy` fails closed: paths with < 2 prices, any non-positive price, an entry index at/past the
last price, or slippage outside [0, 1) all raise `ValueError` [S].

## Worked example

Expectancy across **200 seeded paths**, verbatim from the demo [S]:

| bucket | expectancy | viable | penalty vs leader |
|--------|-----------:|--------|------------------:|
| leader | **+31.2%** | —       | —                 |
| 15s    | **−6.2%**  | False   | −37.4 pts         |
| 1m     | **−50.3%** | False   | −81.5 pts         |
| 5m     | **−53.4%** | False   | −84.6 pts         |
| 15m    | **−55.7%** | False   | −86.9 pts         |

Read the shape, not just the signs. The **15s → 1m cliff** (−6% → −50%) is the pump expiring: the pump
lasts ~25 steps (125s), so a 12-step (60s) follower enters halfway up a move that is about to bleed at
−1.2%/step, and the −50% hard stop does the rest. The **5m / 15m compression** (−53% vs −56%) is the
ladder's floor: once you are deep in the bleed, the hard stop and the price floor bound how much worse
"later" can get, so extra lag stops mattering — stop design, not timing, sets the tail.

Sanity cases from the tests [S]: on a perfectly flat path a copy loses exactly its slippage (proceeds
are spot-priced but divided by the inflated entry); on a clean double the ladder pays in full; returns
are bounded above total loss (never below −100%).

## Trap log

- T1. Citing the leader's record as the copy product's record. The leader's +31.2% and the 15-second
  copy's −6.2% are *different trades* that happen to share a signal; marketing the first as the second
  is the entire copy-trading sales trick. - T2. Blaming slippage alone. Slippage at 15s is only 1%, yet
  the bucket is already negative: the dominant early cost is the moved price (stale information), with
  slippage compounding it. By 1m the entry-point decay dwarfs the slippage line entirely. - T3. Quoting
  win rate as viability. "The leader wins often" says nothing about the follower's expectancy; the
  toolkit's win rates collapse with lag, and viability is defined on expectancy > 0, not on the leader's
  hit rate. - T4. Assuming per-path monotonicity. Individual paths exist where a follower beats the
  leader (ladder path-dependence); the claim is aggregate and monotone *on average* — a single anecdotal
  copy win refutes nothing. - T5. Reading the 5m/15m closeness as "lag stops mattering after 5 minutes,
  so 15m is fine". Both are catastrophic; the compression is the hard stop bounding losses, i.e. the
  ladder rescuing the tail, not the entry improving. - T6. Crediting the ladder for the leader's edge.
  Everyone runs the *same* ladder; the only differences are entry step and slippage. The experiment is
  controlled precisely so "better exits" cannot explain the gap. - T7. Extrapolating to zero lag. A
  hypothetical 1-second, leader-grade-slippage copy is outside the tested buckets; the model's path (a
  clean 2-minute pump, no front-running, no copy impact of its own) is most fragile exactly there — in
  real markets the copier's own flow and adversarial wallets farming copiers are first-order effects
  this lab does not model.

## Source flags

- Path model (drifts, phases, volatility, floor, 400 × 5s steps), lag buckets and slippages, ladder
  rules, the trigger/divisor split, and all headline figures (+31.2 / −6.2 / −50.3 / −53.4 / −55.7,
  penalties) are from `copyecon.py`, re-verified on every test run by `test_copyecon.py` (38 tests) [S].
  - The cliff/compression reading is derived from the path and ladder parameters [R] over the source
  numbers [S]. - The live-bot watchlist-only demotion, zero smart-money score, and the roughly −41%
  delayed-copying research figure it echoes are per the README [M]; the lab's own numbers are the
  synthetic ones above, not that figure [S].

## One-paragraph verdict

This toolkit proves, inside a controlled simulation where leader and followers share signal and exit
ladder, that delayed copying is negative at *every* tested lag and worsens monotonically — +31.2% for
the leader against −6.2% at just 15 seconds — because the follower buys the top of a move the leader
created and pays extra slippage to inherit the bleed. It does **not** prove copying can never work
(sub-second execution, different path shapes, or leaders whose edge is selection rather than speed are
outside the model), and its paths are synthetic with tuned phases; what it establishes is the burden of
proof — anyone selling or enabling copying must produce *follower-grade* expectancy, because the
leader's record is not evidence about the follower's trade.
