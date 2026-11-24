# Exam — Lottery Sizing

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR: ≥85% — at least 17 of 20 correct**
- **Conditions:** Closed-book. Synthetic data only. All answers are derivable from the toolkit's README / `sizing.py` alone.
- **Types:** Multiple-choice (A–D), true/false-with-justification, short-answer, and compute-it questions.

**Reference distribution (the "lottery"):** `[(0.90, 0.0), (0.10, 15.0)]` — 90% total loss, 10% a 15x gross return.

---

## QUESTIONS

**Q1 (Compute).** For the lottery distribution: compute `expected_multiple` and `expectancy` (net edge per $1 staked). Show the working.

**Q2 (Compute).** For the lottery distribution: compute `prob_of_loss` and `prob_of_ruin_outcome`. Are they equal here? Why / why not in general?

**Q3 (Compute).** The lottery's variance is pinned in the tests at 20.25. Verify it: compute E[X²], then Var = E[X²] − E[X]².

**Q4 (MCQ).** Full Kelly for the lottery is:
A. 0.50
B. 0.10
C. `1/28 ≈ 0.0357`
D. `1/14 ≈ 0.0714`

**Q5 (Compute).** On a $100 bankroll, how many dollars per shot is (a) the flat CEO fraction, (b) full Kelly for the lottery, (c) double Kelly? State each fraction first.

**Q6 (MCQ).** The pinned expected log growths for the lottery, in the order flat (0.01) / Kelly / 2×Kelly, are approximately:
A. +0.007816 / +0.004058 / +0.002618
B. +0.004058 / +0.007816 / +0.002618
C. +0.002618 / +0.007816 / +0.004058
D. All equal, because expectancy is the same

**Q7 (Compute).** Expected log growth for the lottery at `f = 0.50` is pinned at ≈ −0.4159, while expectancy is still +0.50. Write the log-growth sum `0.90·log(1−f) + 0.10·log(1−f+f·15)` at `f = 0.50` with the two bankroll factors filled in (you may state the pinned result rather than evaluating the logs by hand).

**Q8 (True/False + justification).** True or false: a distribution with strongly positive expectancy must have positive expected log growth at every fraction below 1. Justify using Q7.

**Q9 (MCQ).** For the binary distribution `[(0.60, 0.0), (0.40, 3.0)]`: expectancy and Kelly are:
A. Expectancy +0.50, Kelly 1/28
B. Expectancy +0.20, Kelly 0.10
C. Expectancy +0.20, Kelly 0.40
D. Expectancy −0.20, Kelly 0

**Q10 (Compute).** Verify the binary distribution's expectancy from its pairs, and state its pinned expected log growth at its Kelly fraction.

**Q11 (Short answer).** State `kelly_fraction` for each: a negative-expectancy distribution, a break-even distribution, a certain win. How is Kelly found in general in this module (method and iterations)?

**Q12 (True/False + justification).** True or false: `expected_log_growth(f=1, lottery)` returns −∞ so the caller can decide. Justify what it actually does and why.

**Q13 (MCQ).** `validate_distribution` on probabilities summing to 0.9:
A. Renormalises them to sum to 1
B. Accepts them — 0.9 is within tolerance
C. Raises `ValueError` — sums must be ~1 within tolerance `1e-6`
D. Drops the smallest outcome and renormalises

**Q14 (Short answer).** Which distribution inputs does `validate_distribution` reject? List at least five distinct fail-closed cases, and state the one surprising case that is *valid*.

**Q15 (MCQ).** `expected_log_growth` at fraction `f = 0` is:
A. Undefined / raises
B. Exactly 0
C. Equal to expectancy
D. Negative

**Q16 (Short answer).** The flat $1-on-$100 rule bets *below* Kelly. What is given up, what is bought, and what do the seeded ruin numbers show? Use the demo's figures: ruin over 200 shots (seed 42) on $100 vs on $10.

**Q17 (Compute).** Using the demo's seeded results: ruin probability for the lottery, flat $1 shots, 200 shots, seed 42 — on (a) a $100 bankroll and (b) a $10 bankroll. In one sentence, what do the two numbers together prove about "the ruin probability of this strategy" as a standalone phrase?

**Q18 (True/False + justification).** True or false: `simulate_bankroll` with the same seed, distribution, bankroll, bet, and `n_bets` reproduces the identical final bankroll. Justify, and give the pinned seeded single-path result from the tests (seed 7, ten shots).

**Q19 (MCQ).** Which `simulate_bankroll` / `ruin_probability` arguments are fail-closed errors?
A. Non-positive bankroll/bet, a bet larger than the bankroll, non-positive bet/simulation counts
B. Only a negative bankroll
C. Only a seed below 0
D. None — they are clamped

**Q20 (Short answer).** Why does the optimal Kelly fraction stay as small as 1/28 even though a win pays 15x? Answer in terms of the 90% total-loss probability and the log objective.

---

## ANSWER KEY

**Q1 — Expected multiple 1.50; expectancy +0.50.** Working: 0.90×0.0 + 0.10×15.0 = 1.50; expectancy = 1.50 − 1 = **+0.50** per $1.
**Q2 — `prob_of_loss` = 0.90; `prob_of_ruin_outcome` = 0.90.** Equal here because the only losing outcome is the 0x outcome. In general they differ: `prob_of_loss` is total probability of returning *less than the stake* (any multiple < 1, e.g. 0.5x), while ruin-outcome is only the probability of exactly 0x.
**Q3 —** E[X] = 1.50 (Q1). E[X²] = 0.90×0² + 0.10×15² = 0.10×225 = 22.50. Var = 22.50 − 1.50² = 22.50 − 2.25 = **20.25**. ✓
**Q4 — C.** Kelly = 1/28 ≈ 0.0357. (1/14 is double Kelly.)
**Q5 — (a) Flat: f = 0.01 → $1. (b) Kelly: f = 1/28 ≈ 0.0357 → $3.57. (c) Double Kelly: f ≈ 0.0714 → $7.14.** All on $100.
**Q6 — B.** Flat +0.004058, Kelly +0.007816, 2×Kelly +0.002618 — Kelly is the maximum, and 2×Kelly grows *slower* than Kelly despite identical expectancy.
**Q7 —** Factors: loss factor 1 − 0.50 = 0.50; win factor 1 − 0.50 + 0.50×15 = 0.50 + 7.50 = 8.0. Sum: 0.90·log(0.50) + 0.10·log(8.0) ≈ **−0.4159** (pinned: −0.415888 in the demo). The 90%-weighted log(0.5) loss term dominates.
**Q8 — False.** Q7 is the counterexample: expectancy +0.50 but expected log growth ≈ −0.4159 at f = 0.50. The log punishes large fractional losses asymmetrically — halving the bankroll weighs more than the arithmetic mean suggests — so overbetting a genuinely good distribution still ruins you.
**Q9 — B.** Expectancy: 0.60×0 + 0.40×3 = 1.20, minus 1 = +0.20; Kelly = 0.10 exactly.
**Q10 —** Expectancy: expected multiple = 0.40×3.0 = 1.20; expectancy = **+0.20**. Pinned growth at Kelly (f = 0.10): ≈ **+0.009712** per bet.
**Q11 —** Negative expectancy: **0**. Break-even: **0**. Certain win: **1**. In general Kelly is found by a deterministic **200-iteration golden-section search** over [0, 1] maximising expected log growth.
**Q12 — False.** It raises `ValueError` (fail-closed): at f = 1 with a possible 0x outcome the bankroll factor can be ≤ 0, where the log is undefined because ruin is possible. Returning −∞ and letting the caller decide would let a ruinous fraction flow downstream as a number.
**Q13 — C.** Nothing is silently renormalised; the sum must be ~1 within `1e-6`. (The demo: summing to 0.9 is "correctly rejected".)
**Q14 —** Rejects (any five): empty distribution; `None`; malformed pairs; a probability outside (0, 1]; a negative multiple; probabilities summing to anything other than ~1 (within 1e-6); non-numeric probabilities/multiples. **Valid surprise:** a multiple of exactly 0 is valid — it is the total-loss outcome.
**Q15 — B.** Staking nothing grows nothing: every factor is 1, log(1) = 0, so growth is exactly 0.
**Q16 — Given up:** some growth rate (+0.004058/bet at flat vs +0.007816 at Kelly). **Bought:** far smaller swings / far lower ruin probability. **Seeded proof:** ruin over 200 shots, seed 42 — **0.00 on $100** vs **0.48 on $10** — the same distribution and bet size, a different bankroll, a completely different ruin outcome; the under-Kelly $1-on-$100 discipline is what keeps the $100 figure at zero in the seeded run.
**Q17 — (a) 0.00; (b) 0.48.** Together they prove ruin probability is never a single number for "a strategy": it depends on the distribution, the bankroll/bet size, **and the horizon (n_bets)** — all qualifiers must be stated.
**Q18 — True.** Each simulation is seeded and exactly reproducible for the same seed (in `ruin_probability`, each sim draws from its own stream derived deterministically from `seed`). Pinned single-path result from the tests/README: seed 7, ten shots → **$90** final bankroll.
**Q19 — A.** All of those raise (fail-closed); no clamping.
**Q20 —** Because 90% of shots lose *everything staked on that shot*. Under a log objective, the repeated −log impact of near-total fractional losses grows fast with f, while the 10% win's log gain grows only logarithmically; the optimum balances them at a small fraction — the win is rare, so the stake that survives the losing streaks must stay small even at 15x. (Analytically the optimum solves to 0.5/14 = 1/28.)

---

## GRADING RUBRIC

- **MCQ (Q4, Q6, Q9, Q13, Q15, Q19):** 1 point for the correct letter only.
- **True/False + justification (Q8, Q12, Q18):** 1 point only if verdict and justification both match the key.
- **Short-answer (Q11, Q14, Q16, Q20):** 1 point if every element in the key is present and correct.
- **Compute-it (Q1, Q2, Q3, Q5, Q7, Q10, Q17):** 1 point for the exact numbers in the key with working where asked; a correct result with wrong working on Q1/Q3/Q10 scores 0.
- **Pass rule:** Pass = **≥17/20 (≥85%)**. Below 17 is a fail.
- **What a fail means:** Re-study the toolkit README (and `sizing.py` if needed), then retake the full exam.
