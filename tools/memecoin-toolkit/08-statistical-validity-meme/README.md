# Statistical Validity for Memecoin Claims (lab)

A from-scratch, pure-stdlib lab that answers three questions memecoin strategy claims usually skip: what is the expectancy per dollar, how impressive does pure noise look once you pick the best of hundreds of variants, and how many closes would you need before a claimed win-rate edge is even detectable. Everything is seeded, so every number in this file reproduces exactly.

## What it does

- Computes expectancy from realized multiples and the breakeven win rate for a given average win multiple.
- Runs a p-hacking demonstration: 500 pure-noise "strategies" at a true 30% win rate, crowns the best in-sample performer, then re-tests that same strategy on fresh out-of-sample data where it regresses toward 30%.
- Computes required sample size and achieved power for a one-sided win-rate test with the normal approximation.
- Pins the base rates (`BASE_RATES`) that any memecoin claim must survive: launch graduation rate, launches per $1M token, and the share of wallets profitable over 90 days.
- Summarises a batch of closes beyond the mean (win rate excluding breakeven, median, profit factor, loss streaks, cumulative-profit drawdown, standard error), attaches uncertainty to a win rate (Wilson interval, exact binomial p-value, seeded bootstrap expectancy interval), and prices a claimed edge in Kelly terms — zero exactly at the breakeven win rate.

## Files

- `statslab.py` — `expectancy(outcomes) -> float` (mean − 1 per $1; empty list, a negative multiple, or a non-finite value raises `ValueError`, fail-closed — all outcome-taking functions share that validation). `breakeven_wr(payoff_mult) -> float` = `1 / payoff_mult` (win pays W−1, loss pays −1; non-positive or non-finite payoff raises). Outcome summaries: `win_rate(outcomes)` (share strictly above 1.0 — breakeven is *not* a win), `median_multiple(outcomes)`, `profit_factor(outcomes)` (gross profit / gross loss; `inf` when there are gains and no losses, 0.0 when nothing moved), `max_consecutive_losses(outcomes)` (a 1.0 breaks the streak), `max_profit_drawdown(outcomes)` (largest fall of the cumulative-profit curve from its running peak), `standard_error(outcomes)` (sample std / √n; n < 2 raises). Inference: `wilson_interval(wins, n, alpha=0.05) -> (low, high)` (Wilson score interval, clamped to [0, 1]) and `binomial_pvalue_at_least(k, n, p0) -> float` (exact one-sided binomial p-value via `math.comb`). Sizing: `kelly_fraction(wr, payoff_mult)` = `(wr*W − 1) / (W − 1)` for the same binary model as `breakeven_wr` (0 exactly at breakeven, negative below it; payoff ≤ 1 raises), and `bootstrap_expectancy_ci(outcomes, seed=42, n_boot=2000, level=0.95) -> (low, high)` (seeded percentile bootstrap; constant outcomes collapse it to a point; `n_boot < 100` raises). `simulate_phacking(seed=42, n_strategies=500, n_in_sample=40, n_out_sample=200, true_wr=0.30) -> dict` with best in-sample / out-of-sample win rates and the inflation between them; same seed gives an identical dict. `required_n(p1=0.35, p0=0.30, alpha=0.05, power=0.8) -> int` using `n = (z_a*sqrt(p0(1-p0)) + z_b*sqrt(p1(1-p1)))^2 / (p1-p0)^2`, rounded up; `achieved_power(n, p0, p1, alpha) -> float` inverts it via the normal CDF. `p1 <= p0`, probabilities outside (0, 1), or `alpha` outside (0, 1) raise `ValueError`. Constant `BASE_RATES` = graduation_rate (0.0063, 0.017), one_in_launches_to_1m 4200, wallets_profitable_90d 0.0625.
- `demo.py` — prints expectancy, breakeven rates, the p-hacking result, power figures, and base rates. Runs clean, exit 0.
- `realdata.py` — real-data loader: streams the live paper bot's journaled closes (`backtest_dataset.jsonl` paper records plus the mirror/CEO/steady/stacker lane ledgers, read-only, fields whitelisted — no wallet data), reconstructs realized multiples per source, and summarises them with this toolkit's own functions: expectancy, WR at ≥1.5x / >1.0x / ≥1.0x, rug rate (recorded flag where present, else the documented ≤0.05x proxy), Wilson interval, and `required_n` against a 30% null (reported as `None` with a note when the observed WR does not beat the null — no edge to power for). `sample_paper_closes.jsonl` is a small fixture of real closes the adapter tests run against.
- `test_statslab.py` — pytest suite (see below).
- `test_realdata.py` — adapter tests against the sample fixture, all numbers hand-computed from it.

## Test suite

49 tests, all passing (40 in `test_statslab.py`, 9 in `test_realdata.py`). Coverage: expectancy hand examples (including the empty-list failure), breakeven win rates (2x → 0.5, 4x → 0.25, 10x → 0.1, plus an EV-consistency check), the pinned required-sample value (534 closes for 35% vs 30% at alpha 0.05 / power 0.8, inside the sane 500–700 range), achieved power inverting required_n (≈0.8 at n=534, blind at 50 closes, >0.99 at 2000), the p-hacking result (best in-sample WR > 0.45 while its out-of-sample WR is lower and the mean in-sample WR sits at the true 30%), seed determinism, and the exact `BASE_RATES` contents. Added: outcome summaries with hand values and edge cases (win rate excluding breakeven, median vs mean under a single 20x outlier, profit factor including the `inf` / no-movement / no-gains edges, loss streaks broken by a 1.0, cumulative-profit drawdown from the running peak, standard error exactly 1.0 on [1, 3] and failing at n < 2); shared fail-closed outcome validation (negative / NaN / inf / `None` multiples raise everywhere); breakeven edges (payoff 1 → 1.0, payoff 0.5 → an impossible 2.0) and invalid payoffs; Kelly hand values, zero exactly at breakeven across five payoffs, and rejection of payoffs ≤ 1; the win-rate-vs-breakeven ⇔ expectancy-sign consistency at 35% and 30%; `required_n` monotonicity with pinned values for a smaller effect (3286), laxer alpha (392), and higher power (746), plus every bad-parameter region; achieved power monotonic/bounded with a second inversion (191 closes for 40% vs 30% at power 0.9) and bad-alpha/n rejection; Wilson interval boundary hand values (0/10 → (0, ~0.278), 10/10 → (~0.722, 1)), narrowing with n, nesting by confidence level, and impossible counts; exact binomial p-values by hand (10/10 at p=0.5 is 1/1024), monotonicity in k, and the 40/100-beats-a-30%-null case; a cross-check that Wilson and the exact test tell the same story at 60/100 and 32/100; the bootstrap CI collapsing on constant outcomes, bracketing 0 on the demo sample (seed-pinned endpoints), widening with level, and failing closed; p-hacking parameter echoing, the best ≥ mean invariant across seeds, and bad counts/rates; and `BASE_RATES` probability-shape invariants.

## Captured output

```
Statistical Validity Lab — seeded, deterministic, pure noise is labelled

expectancy([2.5, 0.0, 1.2, 0.4, 3.0, 0.0, 0.8]) = +0.129 per $1 staked
breakeven WR at 2x payoff: 0.50, at 4x: 0.25, at 10x: 0.10

p-hacking demo: 500 pure-noise strategies, true WR 30%
  mean in-sample WR : 0.297
  best in-sample WR : 0.500 (strategy #25) <- looks like a discovery
  same strategy OOS : 0.360 <- regresses to the true rate
  in-sample inflation: +0.140

power: closes needed to tell 35% WR from 30% WR (alpha 0.05 one-sided, power 0.8): 534
  achieved power at 100 closes: 0.297
  achieved power at 534 closes: 0.800

base rates a claim must survive:
  graduation_rate: (0.0063, 0.017)
  one_in_launches_to_1m: 4200
  wallets_profitable_90d: 0.0625
```

## What this mirrors in the live bot

The live operation pre-registers lane verdicts in closes — a lane earns a verdict at 20 closes, a go/no-go gate at 200 — and the nightly learner tunes weights on measured outcomes. This lab is the arithmetic underneath those gates: it shows why a lane with a hot first week proves nothing (the best of 500 noise strategies "wins" at 50%), and why distinguishing a real 35% win rate from a 30% base rate takes ~534 closes, not 50. The base rates mirror the standing facts the bot's design assumes: only ~0.6–1.7% of launches graduate and only 6.25% of wallets are profitable over 90 days.

## Self-exam

1. A strategy averages a 3x win multiple. What win rate does it need just to break even, and how is that derived?
2. In the p-hacking demo the crowned strategy scores 0.500 in-sample and 0.360 out-of-sample. Which number is closer to its true skill, and why is neither exactly 0.30?
3. Why does `required_n` grow so fast as p1 approaches p0 — what happens to the formula's denominator?
4. At 100 closes, achieved power for the 35%-vs-30% test is about 0.297. What does that mean for a verdict passed at 100 closes?
5. Why does `expectancy` raise on an empty list instead of returning 0.0?
