# Exam — Smart-Wallet Grading

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR:** ≥85% — at least **17 of 20** correct
- **Conditions:** Closed-book. Synthetic data only. Answer from the toolkit's README/module alone.

---

## QUESTIONS

### Multiple choice (Q1–Q8) — choose the one correct option

**Q1.** Which lag gains does `follow()` accept?
- A. Any non-negative float
- B. Exactly `LAG_GAINS = (0.15, 0.50, 1.00)`; anything else raises `ValueError`
- C. Exactly `(0.10, 0.25, 0.50)`
- D. Any value in the open interval (0, 1)

**Q2.** How is a copier's entry price determined?
- A. Exactly entry price × (1 + lag gain)
- B. The average of the prints around the target level
- C. The first path print at or above the target level — overshoot is charged to the copier
- D. The wallet's own entry price, adjusted by a fixed fee

**Q3.** The lag level is never reached on the price path. What happens?
- A. The copier enters at the last print anyway
- B. The copier enters at the highest print
- C. The follow returns `None` — a skip, never a chased entry
- D. A `ValueError` is raised

**Q4.** In plain `follow()`, when does the copier exit?
- A. At the peak of the path
- B. At the last path point
- C. 60 seconds after entry
- D. At a fixed 2x take-profit

**Q5.** Before any "several wallets agree" signal is counted, wallets are collapsed into independent actors by what key?
- A. Mint
- B. Entry timestamp, to the nearest minute
- C. Funder — wallets sharing a funder count as one actor
- D. Wallet name prefix

**Q6.** `grade_weight()` clamps its result to which interval, and what weight does the neutral prior (0.0) produce?
- A. [0, 2]; neutral weight 0.0
- B. [0.5, 1.5]; neutral weight 1.0
- C. [0.5, 1.5]; neutral weight 0.5
- D. [−1, 1]; neutral weight 0.0

**Q7.** A wallet is auto-muted exactly when:
- A. `n_follows >= 5 and avg_realized <= -0.10`
- B. `n_follows >= 10 and avg_realized < -0.20`
- C. `n_follows >= 10 and avg_realized <= -0.20`
- D. Any single follow loses more than 50%

**Q8.** `rank_wallets()` sorts deterministically by:
- A. Name, then weight, then follow count
- B. Weight, then follow count, then name
- C. Follow count only
- D. Average realised fraction, then name

### True / False with justification (Q9–Q12) — state True or False AND justify in one sentence

**Q9.** Path points printed before the wallet's own buy can trigger a copier's follow entry.

**Q10.** A wallet with no known funder is merged into the largest pack, to be safe.

**Q11.** `follow_with_stop()` accepts `stop_fraction = 1`, meaning "exit only at zero".

**Q12.** `median_realized()` on an empty result set returns 0.0.

### Short answer (Q13–Q17)

**Q13.** This lab grades a wallet by the copier's result, not the wallet's own result. Why — what does the wallet's own hold-to-the-end fraction fail to capture?

**Q14.** On the demo path (1.00 → 1.20 → 1.60 → 2.10 → 1.40 → 0.40), why does the +15% copier pay 1.20 rather than exactly 1.15?

**Q15.** What does `lag_cost_fraction()` measure, and in what case is it `None`?

**Q16.** What is the shrinkage prior strength `m = 10` doing to a wallet with only 2 follows, and why is that desirable before the grade becomes a scoring weight?

**Q17.** Define `independence_ratio()`. What does it return for an empty wallet set?

### Compute it (Q18–Q20) — show your working

**Q18.** Using `grade_weight(n, avg, prior=0.0, m=10)`: compute the weight for a new wallet with n = 2 follows averaging +0.90.

**Q19.** Compute the weight for n = 12 follows averaging −0.30, and state whether the wallet is muted.

**Q20.** On the demo path above, the wallet buys at 1.00 and the path ends at 0.40. Compute (a) the +50% copier's realised multiple and realised fraction, (b) the wallet's own realised fraction, and (c) the lag cost at the +50% lag.

---

## ANSWER KEY

**Q1. B** — Only the three documented lag gains are legal; an undocumented lag raises `ValueError` (and that propagates through `simulate_follows`).

**Q2. C** — Entry is the first print at/above the target (an exact print counts); the copier pays the print, not the target.

**Q3. C** — Fail-closed: an unreached level is a skip (`None`); an empty path is likewise a skip.

**Q4. B** — Every plain follow exits at the last path point, which is why a run-then-die path punishes larger lags.

**Q5. C** — `group_wallets` keys on funder; `convergence_count` is the number of distinct funders.

**Q6. B** — `weight = clamp(1.0 + shrunk, 0.5, 1.5)` and prior 0.0 gives shrunk = 0 at n = 0, i.e. weight 1.0.

**Q7. C** — Muted iff `n_follows >= 10 and avg_realized <= -0.20`; exactly 10 at exactly −0.20 is muted.

**Q8. B** — Weight first, then follow count (evidence), then name as the final tie-break.

**Q9. False** — Pre-buy path points are explicitly ignored; only prints at/after the wallet's buy are considered.

**Q10. False** — Fail-closed in the other direction: a funder-less (or empty-funder) wallet is treated as its own funder and never merged; unknown funding never creates a pack.

**Q11. False** — `stop_fraction` must lie in the open interval (0, 1); both 0 and 1 are rejected.

**Q12. False** — It returns `None` on empty input; there is no median of nothing.

**Q13.** The wallet's own fraction assumes the wallet's entry price and timing. A copier necessarily arrives later, pays a higher (lag-triggered) entry, and holds to the same end — on a run-then-die path the wallet can show a gain while every lagged copy loses. Grading the wallet by its own result would certify signals that lose money for anyone who could actually act on them.

**Q14.** The target is 1.00 × 1.15 = 1.15, but entry is the first *print* at or above the target. The prints after entry are 1.20, 1.60, … — the first print ≥ 1.15 is 1.20, and the overshoot (1.20 vs 1.15) is charged to the copier.

**Q15.** It is the wallet's own realised fraction minus the copier's realised fraction at that lag — the fraction of the move the delay cost. It is `None` when the copier never entered (lag level unreached), because there is no copier fraction to subtract.

**Q16.** Shrunk average = (n·avg + m·prior)/(n + m); with n = 2 and m = 10 the prior contributes 10/12 of the blend, so two lucky (or unlucky) follows move the weight only slightly from neutral 1.0. This stops thin evidence from producing extreme weights; the weight earns its distance from 1.0 only as follow count grows.

**Q17.** Distinct funders ÷ number of wallets (independent actors per wallet). For an empty set it returns 0.0.

**Q18.** shrunk = (2 × 0.90 + 10 × 0) / (2 + 10) = 1.8 / 12 = 0.15. weight = 1.0 + 0.15 = **1.150** (inside the clamp).

**Q19.** shrunk = (12 × −0.30) / 22 = −3.6 / 22 ≈ −0.1636. weight = 1.0 − 0.1636 ≈ **0.836**. Muted: n = 12 ≥ 10 and avg −0.30 ≤ −0.20 → **muted = True**.

**Q20.** (a) +50% target = 1.50; first print ≥ 1.50 is 1.60; exit 0.40. Realised multiple = 0.40 / 1.60 = **0.25x**; realised fraction = 0.25 − 1 = **−0.75**. (b) Wallet fraction = 0.40 / 1.00 − 1 = **−0.60**. (c) Lag cost = −0.60 − (−0.75) = **0.15** (the delay cost the copier 15 percentage points of fraction versus the wallet).

---

## GRADING RUBRIC

- **Multiple choice (Q1–Q8):** 1 point for the correct letter only; no partial credit.
- **True/False with justification (Q9–Q12):** 1 point only if BOTH the verdict and the justification are correct. A correct verdict with a wrong or missing reason scores 0.
- **Short answer (Q13–Q17):** 1 point if the answer states the mechanism named in the key (wording may differ). Answers that restate the question or give a plausible-but-different mechanism score 0.
- **Compute it (Q18–Q20):** 1 point for the correct final value(s) with working shown that follows the toolkit's formula. Correct working with a single arithmetic slip scores 0 — recompute before submitting. Multi-part questions require every part correct.
- **Pass rule:** ≥85% = **17 or more of 20**. 16 or fewer is a fail.
- **On a fail:** re-study the toolkit note/README, re-derive every missed computation by hand, and retake the full exam (not just the missed questions) no sooner than the next study session.
