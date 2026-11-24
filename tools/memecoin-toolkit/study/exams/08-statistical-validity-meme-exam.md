# Exam — Statistical Validity for Memecoin Claims

- **Total questions:** 22
- **Points:** 1 per question (22 points total)
- **PASS BAR: ≥85% — minimum 19 of 22 correct** (0.85 × 22 = 18.7, rounded up)
- **Conditions:** Closed-book. Synthetic data only. Show working on compute-it questions.
- **Source:** Answerable from the `08-statistical-validity-meme` README and `statslab.py` alone. All experiments are seeded/deterministic.

---

## QUESTIONS

**Q1 (MCQ).** `expectancy(outcomes)` returns:
A. The mean multiple
B. Mean − 1, i.e. profit per $1 staked; an empty list, a negative multiple, or a non-finite value raises `ValueError`
C. The median multiple
D. 0.0 on an empty list

**Q2 (Compute).** `expectancy([2.5, 0.0, 1.2, 0.4, 3.0, 0.0, 0.8])` — compute it to 3 decimal places, showing the mean first.

**Q3 (Compute).** `breakeven_wr(payoff_mult) = 1 / payoff_mult`. Compute the breakeven win rate for payoffs (a) 2x; (b) 4x; (c) 10x; (d) 3x (to 3 d.p.); (e) 1x; (f) 0.5x — and for (f), state in one phrase why the result is damning.

**Q4 (Short answer).** Derive `breakeven_wr` from the binary model: a win pays W−1 per $1, a loss pays −1. Set expected value to zero and solve for the win rate.

**Q5 (MCQ).** `win_rate(outcomes)` counts as wins:
A. Multiples ≥ 1.0 (breakeven counts)
B. Multiples strictly above 1.0 — breakeven is NOT a win
C. Multiples above the median
D. Only multiples ≥ 2.0

**Q6 (MCQ).** `profit_factor(outcomes)` when there are gains and no losses, and when nothing moved at all, is respectively:
A. 1.0 and 1.0
B. `inf` and 0.0
C. 0.0 and `inf`
D. It raises in both cases

**Q7 (True/False + justification).** "A single 1.0x (breakeven) close does not break a losing streak in `max_consecutive_losses`." True or false? Justify.

**Q8 (Compute).** `standard_error([1, 3])`: sample std of [1, 3] divided by √n. Compute it exactly. What does `standard_error` do at n < 2?

**Q9 (MCQ).** The p-hacking demo (seed 42, 500 pure-noise strategies, true WR 30%, 40 in-sample trades each) crowns a best in-sample win rate of:
A. ≈0.30 — noise cannot look good
B. 0.500, which then scores 0.360 out-of-sample (200 fresh trades) — regressing toward the true 30%
C. 0.500 in-sample and 0.500 out-of-sample
D. 0.297 — the mean

**Q10 (Short answer).** In the p-hacking demo, which number is closer to the crowned strategy's true skill — 0.500 or 0.360 — and why is neither exactly 0.30?

**Q11 (Compute).** `required_n(p1=0.35, p0=0.30, alpha=0.05, power=0.8)` — state the pinned value. Then state `achieved_power` at (a) n = 534; (b) n = 100 (approx.); (c) n = 2000 (bound).

**Q12 (MCQ).** As p1 approaches p0, `required_n` explodes because:
A. The numerator's z-values grow
B. The formula's denominator `(p1 − p0)²` shrinks toward zero while the numerator stays finite — the squared gap is in the denominator
C. Alpha must shrink
D. The normal approximation breaks

**Q13 (Short answer).** Achieved power at 100 closes for the 35%-vs-30% test is ≈0.297. What does that mean for a verdict passed at 100 closes?

**Q14 (MCQ).** `required_n` for 40% vs 30% at power 0.9 is 191 closes, versus 534 for 35% vs 30% at power 0.8. The dominant reason 191 is smaller is:
A. A larger effect size (10 points vs 5 points) shrinks the required n far more than the higher power target grows it
B. Power 0.9 is easier than 0.8
C. Rounding
D. A different alpha

**Q15 (Compute).** `kelly_fraction(wr, payoff_mult) = (wr·W − 1)/(W − 1)`. Compute: (a) wr = 0.40, W = 3; (b) wr = breakeven for W = 3 (wr = 1/3), W = 3; (c) wr = 0.30, W = 4 (sign only + value to 3 d.p.). What happens for payoff ≤ 1?

**Q16 (MCQ).** `BASE_RATES` pins: graduation_rate, one_in_launches_to_1m, wallets_profitable_90d as:
A. (0.063, 0.17), 420, 0.625
B. (0.0063, 0.017), 4200, 0.0625
C. (0.0063, 0.017), 420, 0.0625
D. (0.63, 1.7), 4200, 0.00625

**Q17 (True/False + justification).** "`wilson_interval(0, 10)` is approximately (0, 0.278) and `wilson_interval(10, 10)` is approximately (0.722, 1) — the interval never claims certainty from 10 samples." True or false? Justify (clamping).

**Q18 (Compute).** Exact binomial: the one-sided p-value `binomial_pvalue_at_least(10, 10, 0.5)` — compute it as a fraction and a decimal (4 d.p.).

**Q19 (MCQ).** `bootstrap_expectancy_ci` on constant outcomes (e.g. all closes = 1.5x):
A. Widens with level
B. Collapses to a point (low == high)
C. Raises
D. Centres on 0

**Q20 (MCQ).** `bootstrap_expectancy_ci` with `n_boot < 100`:
A. Is accepted
B. Raises `ValueError` (fail-closed); likewise `required_n` raises for `p1 ≤ p0` or probabilities/alpha outside (0, 1)
C. Defaults to 100
D. Returns (0, 0)

**Q21 (Short answer).** Why does `expectancy` raise on an empty list instead of returning 0.0?

**Q22 (True/False + justification).** "The mean in-sample win rate across the 500 noise strategies is 0.500, proving the strategies have skill." True or false? Justify with the demo numbers.

---

## ANSWER KEY

- **Q1: B** — Expectancy is mean − 1 per $1; validation is fail-closed and shared by all outcome-taking functions.
- **Q2: +0.129 per $1.** Working: sum = 7.9; mean = 7.9/7 = 1.12857…; expectancy = 1.12857 − 1 = +0.129.
- **Q3: (a) 0.50; (b) 0.25; (c) 0.10; (d) 0.333; (e) 1.0; (f) 2.0** — a win rate of 200% is impossible, so a 0.5x-payoff strategy can NEVER break even.
- **Q4:** EV = wr·(W−1) − (1−wr)·1 = wr·W − 1. Setting EV = 0 gives wr = 1/W. (Check: Kelly is 0 at exactly this wr — the consistency the tests pin.)
- **Q5: B** — Strictly above 1.0; breakeven is not a win.
- **Q6: B** — Gains with no losses → `inf`; nothing moved (no gains, no losses) → 0.0.
- **Q7: False** — A 1.0x close DOES break the streak (only closes strictly below 1.0 extend it; the streak counter resets on a non-loss, including breakeven).
- **Q8: Exactly 1.0.** Working: mean = 2; deviations ±1; sample variance = (1+1)/(2−1) = 2; sample std = √2; SE = √2/√2 = 1.0. At n < 2 it raises `ValueError`.
- **Q9: B** — Best in-sample 0.500 (strategy #25), same strategy OOS 0.360, in-sample inflation +0.140; the mean in-sample WR is 0.297 ≈ the true rate.
- **Q10:** 0.360 is closer — it is an unbiased fresh-sample estimate of a strategy whose TRUE win rate is 0.30 by construction; the 0.500 was selected precisely because it was the maximum of 500 noisy draws (selection bias). Neither is exactly 0.30 because both are finite-sample estimates subject to sampling noise (200 OOS trades still wobble around 0.30).
- **Q11: 534 closes.** (a) ≈0.800 (required_n inverts achieved_power). (b) ≈0.297. (c) > 0.99.
- **Q12: B** — n = (… )²/(p1−p0)²; halving the gap roughly quadruples n (cf. the pinned 3286 for a still-smaller effect).
- **Q13:** Even if the lane truly wins at 35% vs a 30% base rate, a 100-close test detects it only ~30% of the time — a verdict at 100 closes is mostly a coin flip dressed as evidence; "no significant edge found" at n=100 is nearly uninformative, and a "found" edge at n=100 is likely inflated.
- **Q14: A** — Effect size dominates through the squared denominator.
- **Q15: (a) 0.10** ((0.40×3 − 1)/(3−1) = 0.20/2). **(b) 0 exactly** ((1/3×3 − 1)/2 = 0 — zero at breakeven, pinned across five payoffs). **(c) +0.067** ((0.30×4 − 1)/(4−1) = 0.20/3 ≈ +0.067, positive — wr 0.30 is ABOVE the 0.25 breakeven for W=4, so Kelly is positive; the trap is assuming 30% must be sub-breakeven regardless of payoff.) Payoff ≤ 1 raises `ValueError`.
- **Q16: B** — Graduation 0.63%–1.7%, 1 in 4200 launches reaches $1M, 6.25% of wallets profitable over 90 days.
- **Q17: True** — Both intervals are clamped to [0, 1] and stay strictly interior on the uncertain side: 0/10 does not prove the rate is 0, and 10/10 does not prove it is 1.
- **Q18: 1/1024 ≈ 0.000977 → 0.0010 (4 d.p.).** Working: P(X = 10) at p = 0.5 = C(10,10)·0.5¹⁰ = 1/1024, and "at least 10" is just that term.
- **Q19: B** — Every resample of a constant list is the same list; the interval collapses to the single expectancy value.
- **Q20: B** — Both functions validate fail-closed.
- **Q21:** Because 0.0 is a meaningful measurement ("this lane exactly breaks even") and returning it for "no data" would fabricate a verdict from nothing — the same fail-closed principle as elsewhere: empty input is an error, not a zero.
- **Q22: False** — The mean in-sample WR is 0.297, essentially the true 30% — exactly what pure noise predicts. Only the SELECTED maximum (0.500) looks skilled, which is the selection effect, not skill.

---

## GRADING RUBRIC

- **MCQ (Q1, Q5, Q6, Q9, Q12, Q14, Q16, Q19, Q20):** 1 point for the correct letter only.
- **True/False + justification (Q7, Q17, Q22):** 1 point only if verdict AND justification are both correct.
- **Short answer (Q4, Q10, Q13, Q21):** 1 point if the key mechanism is stated (Q4 requires the EV = wr·W − 1 derivation).
- **Compute-it (Q2, Q3, Q8, Q11, Q15, Q18):** 1 point per question; ALL sub-parts numerically correct (tolerances: ±0.002 on Q2/Q15, exact pinned integers on Q11). **Note on Q15(c):** the correct value is +0.067 (positive) — the trap is assuming 30% must be sub-breakeven; against a 4x payoff, breakeven is 25%.
- **Pass rule:** ≥85% → **at least 19 of 22 correct** passes.
- **On a fail:** Re-study the toolkit note/README and `statslab.py`, then retake the full exam. The 534-close figure and the p-hacking result must be recallable exactly before any win-rate claim is trusted.
