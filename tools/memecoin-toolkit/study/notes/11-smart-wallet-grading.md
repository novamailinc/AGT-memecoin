# Smart-Wallet Grading — study notes

## Core ideas (teach-back)

The beginner mistake this toolkit exists to kill is grading a "smart
wallet" by the wallet's own profit. A wallet that bought at $1.00 and
watched the price hit $2.10 looks brilliant — but you cannot buy its
past. The only honest question is: *what would a copier, arriving
late, have made following that buy?* [S]

So the toolkit replays every tracked buy as a copy trade. The copier
does not enter at the wallet's price. They enter only when the price
has already risen by a fixed **lag gain** — one of exactly three
documented levels, `LAG_GAINS = (0.15, 0.50, 1.00)`, i.e. +15%, +50%,
or +100% over the wallet's entry price [S]. Any other lag value is
rejected with `ValueError`; there is no "tune the lag until it looks
good" knob.

If the price never reaches the lag level, there is **no follow at
all** — `follow()` returns `None`. A skipped signal is not a loss and
not a win; it simply never happened. The copier also never chases:
the entry is the *first* price print at or after the wallet's buy
that is at/above the target, and the copier pays that print, not the
target. Overshoot is charged to the copier, honestly [S].

Every copier exits at the **last point of the price path**. That is
deliberately crude: on a run-then-die path it punishes larger lags
exactly the way a real copy delay does, because the later entrant
paid more for the same final price.

Two honesty corrections sit on top of the raw simulation:

1. **Pack independence.** Wallets that share one funder are *one*
   actor, not several. "Three wallets bought" means nothing until you
   ask how many distinct funders stand behind them. A wallet with no
   funder recorded is treated as its own funder — unknown funding
   never merges wallets into a pack (fail-closed) [S].
2. **Shrinkage grading.** A wallet's average follower outcome is
   shrunk toward a neutral prior (0.0 realised, weight 1.0) before it
   becomes a weight in **[0.5, 1.5]**. Two lucky follows do not
   outrank fifty ordinary ones.

Finally, a wallet is **auto-muted** when its followers reliably lose:
at least **10 follows** *and* an average realised fraction of
**≤ −0.20** (−20%). Both conditions, both boundaries inclusive [S].
In the live bot this mirrors the reconciled, shrinkage-adjusted
wallet grades used when scoring candidates, and the lesson that
smart-money agreement is a display signal, not proof a late copier
profits [M].

## Key derivations & formulas

**Copier entry.** For wallet entry price `P` and lag gain `g`:

```
target        = P × (1 + g)
entry_price   = first path print ≥ target, at t ≥ wallet's buy time
realized_mult = exit_price / entry_price      (exit = last path point)
realized_frac = realized_mult − 1
```

Pre-buy path points are ignored; an empty path is a skip (`None`).

**Wallet baseline and lag cost.** The wallet's own hold-to-the-end
fraction uses the same last point:

```
wallet_fraction = last_price / P − 1
lag_cost        = wallet_fraction − copier_fraction
```

`lag_cost` is `None` when the copier never entered — a skipped signal
has no lag cost to charge [S].

**Stop variant.** `follow_with_stop(..., stop_fraction)` keeps the
same entry but exits at the first later print at or below
`entry × (1 − stop_fraction)`, flagged `stopped_out`.
`stop_fraction` must lie in the open interval (0, 1): 0 would exit on
any downtick, ≥ 1 could never trigger, so both raise `ValueError`.

**Pack counting.**

```
convergence_count  = number of distinct funders
independence_ratio = distinct funders / distinct wallets   (0.0 if empty)
```

Four wallets behind one funder give ratio 0.25; four self-funded
wallets give 1.0. `largest_pack` breaks size ties alphabetically so
the answer is deterministic.

**Shrinkage weight** (`grade_weight`, defaults `prior = 0.0`,
`m = 10` pseudo-observations):

```
shrunk = (n × avg_realized + m × prior) / (n + m)
weight = clamp(1.0 + shrunk, 0.5, 1.5)
```

Step by step: with `n = 0, m = 0` the formula would divide by zero,
so the code returns the prior itself (weight 1.0 at the neutral
prior). Negative `n` or `m` raises `ValueError`. Ranking is by
weight, then follow count, then name — fully deterministic [S].

**Mute rule.**

```
muted  ⇔  n_follows ≥ 10  AND  avg_realized ≤ −0.20
```

## Worked example

Demo path (synthetic): the wallet buys at $1.00, then the path prints
`1.00 → 1.20 → 1.60 → 2.10 → 1.40 → 0.40`, exit $0.40 [S].

- Wallet's own outcome: `0.40 / 1.00 − 1 = −0.60` (−60%). Even the
  wallet loses holding to the end — the token more than doubled and
  still died.
- Lag +15%: target $1.15, first print ≥ target is **$1.20** at t=1
  (not $1.15 — overshoot is charged). Realised `0.40/1.20 = 0.333x`,
  fraction −66.7%.
- Lag +50%: entry $1.60 at t=2 → `0.40/1.60 = 0.250x`.
- Lag +100%: entry $2.10 at t=3 → `0.40/2.10 = 0.190x`.

Each extra lag strictly worsens the result on the same signal.

Grades from the demo (`m = 10`, prior 0):

- 2 follows at avg +90%: shrunk `= (2×0.90)/12 = 0.15` → weight
  **1.150**, not muted.
- 50 follows at avg +90%: shrunk `= (50×0.90)/60 = 0.75` → weight
  1.75, clamped to **1.500**, not muted. Same average, very different
  weight — that gap *is* the shrinkage doing its job.
- 12 follows at avg −30%: shrunk `= (12×−0.30)/22 ≈ −0.164` →
  weight **0.836**, and **muted** (12 ≥ 10 and −0.30 ≤ −0.20).
- 9 follows at avg −50%: weight **0.763**, but **not muted** —
  9 follows never mute, however bad the average.

Pack example: wallets A, B, C share FUNDER-1 and D has FUNDER-2, so
4 wallets collapse to **2 independent actors** [S].

## Trap log

- T1. Grading the leader instead of the follower. The wallet's P/L
  is unbuyable history; only lagged copy outcomes count.
- T2. Assuming the copier pays the exact target price. They pay the
  first print at/above it — on a fast path, far above it.
- T3. Counting a never-reached lag level as a loss (or quietly
  entering anyway). It is a skip: `None`, excluded from the stats.
- T4. Counting pack wallets as independent confirmation. Three
  wallets, one funder = one opinion, possibly one operator farming
  copiers.
- T5. Trusting a small-n average. Two +90% follows earn weight 1.15,
  not 1.5; shrinkage exists precisely for this.
- T6. Muting on vibes. Nine catastrophic follows still do not mute —
  the rule deliberately accepts that risk to avoid convicting on a
  sample too small to judge.
- T7. Using the mean alone for copier outcomes. The toolkit also
  provides the median (`median_realized`), because one 20x follow
  must not average away nine rugs.

## Source flags

- Lag gains, entry/exit rules, overshoot charging, pack semantics,
  shrinkage formula, clamp [0.5, 1.5], and the mute boundary are all
  implemented and exercised in `grading.py` / `test_grading.py`
  (45 tests) [S].
- The claim that lag cost grows with lag on run-then-die paths is
  derived from the demo arithmetic above [R].
- The parallel to the live bot's registry, pack treatment, and
  watchlist-only smart-money stance is stated in the README [M].

## One-paragraph verdict

This toolkit proves, on synthetic paths, that follower-grade is a
different number from leader-grade: lag alone can turn a doubled
token into a −67% to −81% copy outcome, pack collapse can turn
"several wallets agree" into one actor, and shrinkage plus the
10-follow / −20% mute rule give a disciplined way to weight and
silence wallets by what copying them actually pays. It does **not**
prove any real wallet is good or bad, that the three lag gains match
real copy latency, or that hold-to-the-end is the right exit — it is
a measurement instrument with honest accounting, not evidence about
any live wallet.
