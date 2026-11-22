# Lottery Sizing — study notes

## Core ideas (teach-back)

Memecoin shots are lottery-like: most return nothing, a few return large multiples. Sizing such bets by
expectancy alone is a way to go broke while being right about the average.

This lab works with a *distribution*: a list of `(probability, gross_multiple)` pairs. A gross multiple
is what a $1 shot returns: 0 = total loss, 1 = break even, 15 = a 15x.

The canonical example, used throughout:

```text
[(0.90, 0.0), (0.10, 15.0)]     90% lose everything, 10% return 15x
```

Two different questions can be asked of a distribution:

1. **How good is one bet, on average?** — expectancy.
2. **How much of the bankroll should ride on each
  bet?** — sizing.

The trap is that they have different answers, and the second is not "as much as possible when the first
answer is positive." A bet can be genuinely good and still ruin you if it is sized too large, because
ruin is about the *path* — the sequence of losses you must survive before the wins arrive — not about
the long-run average you never live to see.

The sizing tool is expected *log* growth. Logarithms turn multiplicative bankroll changes into additive
growth, and they punish a total loss with the full weight it deserves: losing 50% of the bankroll
requires a 100% gain just to get back. The fraction that maximises expected log growth is the **Kelly
fraction**. Betting it exactly maximises long-run growth; betting more grows *slower*; betting much more
grows *negatively* — all at the same positive expectancy.

The practical discipline this mirrors is deliberately *under* Kelly: a flat $1 shot on a $100 bankroll,
a 1% fraction, where full Kelly for the canonical distribution is about 3.6%. What is given up is some
growth rate. What is bought is a ruin probability the seeded simulation shows is far lower. That trade
is a choice, and this lab makes both sides of it a number.

Every function runs validation first, and validation is fail-closed: probabilities must each lie in `(0,
1]`, multiples must be `>= 0`, and the probabilities must sum to ~1 within a tolerance of `1e-6`.
Nothing is silently renormalised — a distribution summing to 0.9 is a *broken input*, not a distribution
to be quietly scaled up, because the missing 0.1 might be exactly the outcome that matters.

## Key derivations & formulas

### Descriptive quantities

```text
expected_multiple = Σ p_i * m_i
expectancy        = expected_multiple − 1          (net edge per $1)
variance          = Σ p_i * (m_i − expected_multiple)^2
prob_of_loss      = Σ p_i  over outcomes with m_i < 1
prob_of_ruin_outcome = Σ p_i over outcomes with m_i == 0
```

For the canonical distribution:

```text
expected_multiple = 0.90*0 + 0.10*15 = 1.50
expectancy        = +0.50 per $1
variance          = 0.9*(0−1.5)^2 + 0.1*(15−1.5)^2
                  = 0.9*2.25 + 0.1*182.25 = 20.25
prob_of_loss = prob_of_ruin_outcome = 0.90
```

### Expected log growth

Staking a fraction `f` of the bankroll, one outcome with multiple `m` multiplies the bankroll by:

```text
bankroll factor = 1 − f + f*m
```

(keep `1 − f` unstaked, turn the staked `f` into `f*m`). Expected log growth per bet:

```text
g(f) = Σ p_i * log(1 − f + f * m_i)
```

Fail-closed corner: if any possible outcome makes the factor `<= 0`, the log is undefined and
`expected_log_growth` raises `ValueError` rather than returning `-inf` for the caller to mishandle. The
sharpest case is `f = 1` with a 0x outcome possible: factor `= 0`, ruin is a possible event, not a
rounding error. At `f = 0`, growth is exactly 0 — betting nothing grows nothing.

### Kelly, by hand, for the canonical distribution

```text
g(f)  = 0.9 * log(1 − f) + 0.1 * log(1 + 14f)
g'(f) = −0.9/(1 − f) + 1.4/(1 + 14f) = 0
=> 1.4(1 − f) = 0.9(1 + 14f)
=> 1.4 − 1.4f = 0.9 + 12.6f
=> 0.5 = 14f
=> f* = 1/28 ≈ 0.0357
```

The code does not use this closed form; it finds the maximum by a deterministic 200-iteration
golden-section search over the concave growth curve — general for any discrete distribution. Two guards:
no positive expectancy → Kelly is exactly 0; a certain win → Kelly is 1. If a total loss is possible,
the search stays strictly below 1, where the log is defined.

Why is the optimum so small when a win pays 15x? Because 90% of bets lose the *entire* stake. Each unit
added to `f` is lost nine times as often as it is multiplied, and the log makes the repeated full losses
compound against you.

### Pinned growth numbers

```text
f = 0.01   (flat $1 on $100)   g ≈ +0.004058
f = 1/28   (Kelly)             g ≈ +0.007816
f = 2/28   (double Kelly)      g ≈ +0.002618
f = 0.50                       g ≈ −0.4159     (expectancy still +0.50)
```

`growth_comparison(outcomes, flat_fraction=0.01)` returns exactly these three side by side as `{"flat":
(f, g), "kelly": (f, g), "double_kelly": (f, g)}`. Note the one deliberate softening: inside
`growth_comparison`, a genuinely ruinous double-Kelly reports growth as `-inf` instead of raising — the
strict raising behaviour lives in `expected_log_growth`.

### Monte Carlo ruin

`simulate_bankroll` walks one flat-bet path: each shot adds `bet * (multiple − 1)`; the path stops when
the bankroll can no longer cover a bet. `ruin_probability` runs `n_sims` such paths, each with its own
stream seeded deterministically from `seed` (`seed * 1_000_003 + sim`), and returns the fraction that
end ruined (bankroll below one bet). Same seed → exactly the same result, every time.

## Worked example

From `demo.py`, the canonical distribution again:

- Expectancy: **+0.50** per $1 shot; probability of loss 90%.
- Full Kelly: **0.0357** — $3.57 per shot
  on $100.
- The flat discipline bets $1 on $100 = **0.0100**, deliberately below Kelly.
- Growth
  comparison: flat +0.004058, Kelly +0.007816, double Kelly +0.002618 per bet. Double Kelly grows
  *slower* than Kelly despite the identical +50% expectancy.
- The far-overbet case: at `f = 0.50`,
  expected log growth is **−0.415888** per bet. The average bet still "wins" $0.50; the bankroll still
  shrinks toward ruin, because a coin-flip's worth of bankroll is destroyed on 90% of shots.

Seeded ruin (seed 42, 200 sims, flat $1 shots, 200 bets):

- On a $100 bankroll: ruin probability **0.00**.
- Same shots on a $10 bankroll: ruin probability
  **0.48**.

Same distribution, same bet size, same horizon — the bankroll alone moves ruin from nil to a coin flip.
That is why "the ruin probability of this strategy" is never a single number: it is meaningless without
the bankroll, the bet, and the horizon `n_bets` attached.

Fail-closed coda: `expectancy([(0.5, 0.0), (0.4, 15.0)])` is rejected — the probabilities sum to 0.9,
not ~1.

## Trap log

- T1. **"Expectancy is positive, so bet bigger."** At f = 0.50 the canonical +50%-expectancy
  distribution has *negative* expected log growth. Expectancy prices one bet; log growth prices the
  sequence.
- T2. **Confusing the mean with the typical path.** The arithmetic mean is dragged up by
  rare 15x wins. Most paths are long runs of total losses; sizing must survive the typical path, not the
  average one.
- T3. **Double Kelly as "aggressive but fine."** 2×Kelly already grows slower than Kelly
  (+0.002618 vs +0.007816). Past Kelly, extra size buys risk and pays back less growth — then negative
  growth.
- T4. **Quoting ruin without its qualifiers.** Bankroll, bet size, and horizon are part of the
  number. $100 → 0.00 and $10 → 0.48 are the same strategy at different depths.
- T5. **Silently
  renormalising probabilities.** Scaling a 0.9-sum to 1 invents 0.1 of probability mass and assigns it
  by fiat. Refuse the input.
- T6. **Returning −inf and letting the caller decide.** An f = 1 bet with a
  possible 0x outcome is ruinous by construction; a number-shaped non-number invites the caller to
  proceed. Raise instead.
- T7. **Dismissing 0x as an edge case.** A multiple of exactly 0 is valid
  input — it is the defining memecoin outcome — and it is the outcome that dominates every sizing
  conclusion.
- T8. **Treating a seeded simulation as an estimate of the world.** The Monte Carlo is
  exactly reproducible *given the distribution*. Its precision about ruin says nothing about whether the
  distribution itself matches reality.

## Source flags

- The distribution format, tolerance `1e-6`, golden-section Kelly search (200 iterations), and all
  function behaviours are from `sizing.py`. [S]
- The pinned numbers — Kelly `1/28 ≈ 0.0357`, growth ≈
  +0.007816 at Kelly, ≈ +0.002618 at 2×Kelly, ≈ −0.4159 at f = 0.50, variance 20.25, and the seeded ruin
  results (0.00 on $100, 0.48 on $10, seed 42) — are locked in by the toolkit's 45 tests per the README.
  [S]
- The hand derivation of `f* = 1/28` above is worked here from the source's growth formula and
  matches the search result. [R]
- The framing of the $1-on-$100 rule as the live CEO account's
  discipline is the README's mirroring account. [M]

## One-paragraph verdict

This toolkit proves the sizing half of lottery-style betting with checkable math: for a discrete outcome distribution, expectancy, expected log growth, and the Kelly fraction are computable exactly or by a deterministic search, and the central lesson is pinned numerically — overbetting a genuinely +50%-expectancy distribution first slows growth (2×Kelly) and then reverses it (f = 0.50) — while seeded Monte Carlo makes ruin a reproducible function of bankroll, bet, and horizon. It does not prove that any real memecoin distribution matches the synthetic ones, that the canonical 90%/15x shape describes live cards, or that Kelly sizing is achievable when outcomes must be estimated from noisy, small samples; it proves the discipline *given* a distribution, and that under-betting Kelly is a priced trade, not timidity.
