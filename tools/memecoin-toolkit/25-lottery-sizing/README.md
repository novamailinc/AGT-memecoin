# Lottery Sizing (lab)

A from-scratch, pure-stdlib lab for sizing positions against memecoin outcome distributions — most shots return 0, a few return large multiples. It covers discrete-distribution expectancy, full-Kelly sizing, expected-log growth comparisons, and seeded Monte Carlo ruin probabilities for flat bets. Everything here is synthetic — no network, no live bot code, no real trade history — and every pinned number is either hand-derived in the tests or produced by a seeded, exactly reproducible simulation.

## What it does

Module `sizing.py` — a distribution is a list of `(probability, gross_multiple)` pairs: with that probability a $1 shot returns `gross_multiple` dollars (0 = total loss, 1 = break even, 15 = a 15x):

- `validate_distribution(outcomes) -> list[tuple[float, float]]` — the fail-closed gate every other function runs first. Raises `ValueError` on an empty/`None` distribution, malformed pairs, a probability outside `(0, 1]`, a negative multiple, or probabilities summing to anything other than ~1 (tolerance `1e-6`). Nothing is silently renormalised; a multiple of exactly 0 is valid (it is the total-loss outcome).
- `expected_multiple(outcomes) -> float` / `expectancy(outcomes) -> float` — the mean gross multiple and the net edge per $1 staked (`expected_multiple - 1`). `variance(outcomes)`, `prob_of_loss(outcomes)` (total probability of returning less than the stake), and `prob_of_ruin_outcome(outcomes)` (total probability of the 0x outcome) complete the descriptive set.
- `expected_log_growth(fraction, outcomes) -> float` — expected log bankroll growth per bet when staking `fraction` of the bankroll: the sum of `p * log(1 - f + f * multiple)`. Fail-closed: a fraction outside `[0, 1]` raises `ValueError`, and so does any fraction/outcome combination whose bankroll factor would be ≤ 0 (e.g. `f = 1` when a total loss is possible) — the log is undefined there because ruin is possible.
- `kelly_fraction(outcomes) -> float` — the full-Kelly fraction in `[0, 1]` maximising expected log growth, found by a deterministic 200-iteration golden-section search over the concave growth curve. A distribution with no positive expectancy has Kelly exactly 0; a certain win has Kelly 1.
- `growth_comparison(outcomes, flat_fraction=0.01) -> dict` — `{"flat": (f, g), "kelly": (f, g), "double_kelly": (f, g)}`: the fractions and their expected log growths side by side, so the cost of overbetting is a number, not an opinion.
- `simulate_bankroll(outcomes, bankroll=100.0, bet=1.0, n_bets=1000, seed=0) -> float` — the final bankroll of one seeded flat-bet path (exactly reproducible for the same seed), stopping when the bankroll can no longer cover a bet. `ruin_probability(outcomes, bankroll=100.0, bet=1.0, n_bets=1000, n_sims=500, seed=0) -> float` — the seeded Monte Carlo fraction of paths that end ruined (bankroll below one bet); each simulation draws from its own stream derived deterministically from `seed`. Fail-closed on non-positive bankrolls/bets, a bet larger than the bankroll, and non-positive bet/simulation counts.

The pinned lesson, locked in by tests: for the lottery distribution `[(0.90, 0.0), (0.10, 15.0)]` (expectancy +0.50 per $1), Kelly is `1/28 ≈ 0.0357`, growth at Kelly is ≈ +0.007816 per bet, growth at 2×Kelly falls to ≈ +0.002618, and at `f = 0.50` expected log growth is *negative* (≈ −0.4159) even though expectancy is still +0.50 — overbetting a genuinely good distribution still ruins you.

`demo.py` walks through that distribution's expectancy and Kelly fraction, the flat/Kelly/2×Kelly growth comparison, the far-overbet case, and seeded ruin probabilities on $100 and $10 bankrolls.

## Test suite

45 tests, all passing (`pytest -q`: "45 passed"). Covered: distribution validation round-tripping, probability sums below and above 1 being rejected, a sum within tolerance being accepted, empty/`None` distributions, out-of-range and non-numeric probabilities and multiples, malformed pairs, and the 0x total-loss outcome being valid; expectancy for the lottery (+0.50), binary (+0.20), negative (−0.50), break-even (0.0), and certain-win (+1.0) distributions; the lottery's variance against the hand-computed 20.25; loss and ruin-outcome probabilities; expected log growth being 0 at a zero fraction and positive below Kelly; the growth ordering (Kelly above flat, Kelly above 2×Kelly) and the pinned growth values at Kelly (≈0.007816) and 2×Kelly (≈0.002618); the pinned far-overbet result (negative growth at `f = 0.50` with positive expectancy); `f = 1` raising when a total loss is possible but being fine for a certain win; bad fractions rejected; Kelly matching the analytic optima for the lottery (`1/28`) and the binary distribution (`0.10`, with its pinned growth ≈0.009712), Kelly of 0 for negative and break-even distributions, Kelly of 1 for a certain win, Kelly staying within `[0, 1]` across distributions, and the longshot distribution's Kelly satisfying the local-optimality property; the `growth_comparison` structure; Monte Carlo reproducibility for a fixed seed; ruin of exactly 0 for a certain win and exactly 1 for certain loss over a long enough horizon, for the negative distribution on a thin bankroll, and for the lottery on a full $100 bankroll over 200 shots (all pinned seeded results); thinner bankrolls and larger bets each ruining strictly more; a pinned seeded single-path bankroll (seed 7, ten shots → $90); a certain-win path growing $100 to $110; and fail-closed rejection of bad bankroll/bet/count arguments.

## Captured output

Verbatim output of `python demo.py`:

```
=== Lottery sizing lab (memecoin outcome distributions) ===
Distribution: 90% -> 0x (total loss), 10% -> 15x
Expectancy per $1 shot: +0.50 (probability of loss: 90%)

-- Kelly sizing --
Full-Kelly fraction of bankroll: 0.0357 ($3.57 per shot on a $100 bankroll)
The CEO discipline bets $1 on $100 = a 0.0100 flat fraction,
deliberately below Kelly: smaller growth, far smaller swings.

-- Expected log growth: flat vs Kelly vs 2x Kelly --
flat          f=0.0100  E[log growth]/bet = +0.004058
kelly         f=0.0357  E[log growth]/bet = +0.007816
double_kelly  f=0.0714  E[log growth]/bet = +0.002618
2x Kelly grows SLOWER than Kelly despite the same +50% expectancy.

-- Overbetting far past Kelly turns growth negative --
f=0.50: E[log growth]/bet = -0.415888 (expectancy is still +0.50 per $1)

-- Seeded Monte Carlo ruin (flat $1 shots, $100 bankroll) --
Ruin probability over 200 shots (seed 42): 0.00
Same shots on a $10 bankroll (seed 42):    0.48

-- Fail-closed --
Probabilities summing to 0.9 correctly rejected: probabilities must sum to ~1 (tolerance 1e-06), got 0.9
```

## What this mirrors in the live bot

This mirrors the CEO account's $1-shot discipline in the live bot — a dedicated $100 paper account trading the fired-card bus at a flat $1 per shot under real-money discipline — read through the expectancy math of the earlier statistical-validity toolkit (toolkit 08) applied to sizing. The flat $1 on $100 is a deliberate 1% fraction: for a lottery-like distribution whose full Kelly is around 3.6%, that is intentionally *under* Kelly, trading some growth rate for a ruin probability the seeded simulation shows is far lower — and the overbetting results here are the standing rebuttal to "the expectancy is positive, so bet bigger": at half the bankroll per shot the same +50%-expectancy distribution has negative expected log growth. It is a teaching/checking model of that sizing discipline only; it shares no code with the live bot.

## Self-exam

1. A distribution can have strongly positive expectancy and still have negative expected log growth at a large fraction — what is it about the log that makes a single total-loss outcome weigh so much more than the arithmetic mean suggests?
2. For `[(0.90, 0.0), (0.10, 15.0)]`, Kelly is `1/28` of the bankroll. Why does the optimal fraction stay so small even though a win pays 15x?
3. The flat $1-on-$100 rule bets *below* Kelly. What is given up, what is bought with that sacrifice, and how does the seeded ruin simulation make the trade-off concrete?
4. Why must `expected_log_growth` fail closed at `f = 1` when a 0x outcome is possible, rather than returning negative infinity and letting the caller decide?
5. Why does `validate_distribution` refuse to renormalise probabilities that sum to 0.9 instead of quietly scaling them to 1?
6. Ruin probability here depends on the horizon (`n_bets`) as well as the distribution and the bankroll — why can "the ruin probability of this strategy" never be a single number without those qualifiers?
