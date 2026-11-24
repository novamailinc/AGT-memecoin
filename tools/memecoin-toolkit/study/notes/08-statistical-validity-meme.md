# Statistical Validity for Memecoin Claims — study notes

## Core ideas (teach-back: explain it as if teaching a smart beginner; the actual concepts from THIS toolkit, with its real numbers/thresholds)

Every memecoin claim sounds like "my strategy wins 40% of the
time." This lab in `statslab.py` asks the three questions such
claims usually skip: what is the expectancy per dollar, how
good does pure noise look once you pick the best of hundreds of
variants, and how many closes would you need before the claimed
edge is even detectable? Everything is seeded, so every number
reproduces exactly [S].

Expectancy first. A list of realised multiples (2.5 means you
got 2.5× your stake back) has expectancy = mean − 1, per $1
staked. It is the only summary that cannot be flattered by
cherry-picking a win rate: a strategy can win often and still
lose money, or win rarely and print. Win rate alone never
settles anything; win rate *times payoff* does [S].

The p-hacking demonstration is the heart of the lab. Generate
500 "strategies" that are pure noise — true win rate 30%, 40
in-sample trades each. Crown the best in-sample performer. It
"wins" at 50.0%. Re-test that same strategy on 200 fresh
out-of-sample trades: it scores 0.360, regressing toward the
true 30%. Nothing was discovered; selection manufactured the
discovery. The mean in-sample win rate sits at 0.297 — the
*average* strategy was honest all along; only the *best* lied
[S].

Power analysis then prices honesty. Telling a real 35% win
rate from a 30% base rate (one-sided α = 0.05, power 0.8)
takes 534 closes. At 100 closes the achieved power is only
~0.297 — a verdict rendered there is barely better than a
coin flip at detecting the very edge it claims to test. Small
samples do not give weak evidence; they give almost none [S].

Finally the lab pins the base rates any claim must survive in
`BASE_RATES`: only 0.63%–1.7% of launches graduate
(`graduation_rate` (0.0063, 0.017)), roughly 1 launch in 4200
reaches $1M, and only 6.25% of wallets are profitable over 90
days. A strategy story that ignores these priors starts the
race already beaten [S].

## Key derivations & formulas (the math the toolkit implements, shown step by step where non-trivial)

Expectancy and validation [S]:

    expectancy(outcomes) = mean(outcomes) − 1     (per $1)
    Empty list, a negative multiple, or a non-finite value
    raises ValueError — shared by all outcome functions.

Breakeven win rate for the binary model (win returns W× total,
loss returns 0) [S]:

    EV = wr × (W − 1) + (1 − wr) × (−1) = 0
       → wr × W = 1  →  breakeven_wr = 1 / W
    2x → 0.50;  4x → 0.25;  10x → 0.10
    (payoff 1 → 1.0; payoff 0.5 → an impossible 2.0)

Kelly fraction for the same model [S]:

    kelly = (wr × W − 1) / (W − 1)
    Zero exactly at the breakeven win rate, negative below it;
    payoff ≤ 1 raises.

Required sample size (normal approximation, one-sided) [S]:

    n = ( z_a·√(p0(1−p0)) + z_b·√(p1(1−p1)) )² / (p1 − p0)²
    rounded up. Pinned values: 35% vs 30%, α .05, power .8
    → 534; a smaller effect (p1 nearer p0) → 3286; laxer α
    → 392; power 0.9 → 746. p1 ≤ p0 raises.
    `achieved_power` inverts it via the normal CDF: ≈0.8 at
    n = 534, >0.99 at 2000, ~blind at 50.

Note the denominator: n grows with 1/(p1−p0)². Halving the
effect you hunt *quadruples* the closes required [R].

Uncertainty tools [S]:

    wilson_interval(wins, n) — score interval clamped to
    [0, 1]; 0/10 → (0, ~0.278); 10/10 → (~0.722, 1);
    narrows with n, nests by confidence level.
    binomial_pvalue_at_least(k, n, p0) — exact one-sided
    p-value via math.comb; 10/10 at p = 0.5 is 1/1024.
    bootstrap_expectancy_ci(outcomes, seed=42, n_boot=2000)
    — seeded percentile bootstrap; constant outcomes collapse
    it to a point; n_boot < 100 raises.

Outcome summaries: `win_rate` counts only multiples strictly
above 1.0 (breakeven is not a win); `profit_factor` is gross
profit / gross loss (inf with gains and no losses, 0.0 when
nothing moved); a 1.0 breaks a loss streak in
`max_consecutive_losses`; `max_profit_drawdown` measures the
cumulative-profit curve against its *running* peak [S].

Deflation preview: `deflated_expectancy` lives in the rigor
lab, but the same selection logic — best-of-many regressing —
is what `simulate_phacking` demonstrates empirically here [R].

## Worked example (concrete numbers taken from the toolkit's demo/fixtures)

Verbatim from `demo.py` [S]:

    expectancy([2.5, 0.0, 1.2, 0.4, 3.0, 0.0, 0.8]) = +0.129
    per $1 staked
    breakeven WR at 2x: 0.50; at 4x: 0.25; at 10x: 0.10

    p-hacking (500 noise strategies, true WR 30%):
      mean in-sample WR : 0.297
      best in-sample WR : 0.500 (strategy #25)
      same strategy OOS : 0.360
      in-sample inflation: +0.140

    power: closes needed for 35% vs 30% = 534
      achieved power at 100 closes: 0.297
      achieved power at 534 closes: 0.800

    base rates: graduation (0.0063, 0.017);
      1 in 4200 launches to $1M; wallets profitable 6.25%

Hand-checks pinned by tests [S]: median vs mean under a single
20x outlier (the mean flatters; the median doesn't); standard
error exactly 1.0 on [1, 3]; Wilson and the exact binomial
test telling the same story at 60/100 (evidence) and 32/100
(no evidence); Kelly zero at breakeven across five payoffs;
and the consistency law — win rate above breakeven ⇔ positive
expectancy — verified at 35% and 30%.

## Trap log (numbered T1, T2, … — the failure modes/misconceptions this toolkit exists to catch, each 1–3 lines)

- T1. Reporting win rate without payoff. 60% winners at 1.5x
  average loses to 30% winners at 4x; expectancy is the unit.
- T2. Crowning the best of many variants. Best-of-500 noise
  "wins" at 50% in-sample and 36% out-of-sample. Selection is
  a claim multiplier, not a discovery.
- T3. Verdicts on 50 closes. Detecting 35% vs 30% needs 534;
  at 100 closes power is ~0.30 — the test mostly cannot see
  the edge it is judging.
- T4. Counting breakeven as a win. `win_rate` is strictly
  > 1.0; a 1.0 also breaks loss streaks but earns nothing.
- T5. Trusting the mean under outliers. One 20x in a small
  batch drags expectancy; check the median and profit factor.
- T6. Returning 0.0 for no data. `expectancy([])` raises —
  "no evidence" must never render as "zero edge".
- T7. Ignoring base rates. With 6.25% of wallets profitable
  over 90 days, the null hypothesis is harsh; extraordinary
  hit rates need extraordinary sample sizes.
- T8. Betting Kelly on a sub-breakeven rate. The fraction is
  exactly 0 at breakeven and negative below — the formula's
  answer to "how much?" is often "nothing".

## Source flags (mark key claims [S] = proven in the toolkit's own tests/source, [R] = reasoned/derived here, [M] = mirrors live-bot practice per the README)

- Expectancy/breakeven/Kelly formulas, 534-close requirement,
  power inversions, Wilson/binomial values, BASE_RATES
  contents: [S] — 40 tests, seeded and deterministic.
- Demo figures (+0.129 expectancy; best 0.500 → OOS 0.360;
  +0.140 inflation): [S] — verbatim captured output.
- Live pre-registered gates (verdict at 20 closes, go/no-go
  at 200) and nightly-learner tuning: [M] — the lab is the
  arithmetic underneath them, per README.

## One-paragraph verdict (what this toolkit proves and what it does NOT prove)

This toolkit proves the arithmetic of scepticism: expectancy
and breakeven rates by hand-checkable formulas, the exact
sample sizes real edges require (534 closes for a 5-point win
rate edge), and an empirical demonstration that best-of-500
noise fabricates a 50% "winner" — across 40 seeded tests [S].
It does not prove any live lane has or lacks an edge: the
binary payoff model is a simplification, the normal
approximation is just that, and applying these tools to real
closes still needs the closes themselves — honestly recorded,
which is the neighbouring labs' problem [R].
