# Exam — Verdict Protocol

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR: ≥85% — at least 17 of 20 correct**
- **Conditions:** Closed-book. Synthetic data only — lanes are fixtures, gates are booleans, no real ledger is read. All answers are derivable from the toolkit's README / `verdict.py` alone.
- **Types:** Multiple-choice (A–D), true/false-with-justification, short-answer, and compute-it questions.

---

## QUESTIONS

**Q1 (Short answer).** State the four decision values, and the four gate names verbatim.

**Q2 (Short answer).** State the pinned thresholds: `REQUIRED_N_FULL`, `MIN_N_ANY_VERDICT`, `MIN_N_KILL`, `DEEP_NEGATIVE_EXPECTANCY` (with its unit).

**Q3 (Compute).** `required_n()` with defaults (p1 = 0.35, p0 = 0.30, alpha = 0.05, power = 0.8) returns exactly what? Write the formula it evaluates, identifying the two z-terms.

**Q4 (True/False + justification).** True or false: `required_n(p1=0.30, p0=0.35)` returns a negative sample size. Justify what it actually does, and name one other degenerate input class it rejects.

**Q5 (MCQ).** As the win-rate gap (p1 − p0) shrinks, the required sample:
A. Shrinks
B. Stays the same
C. Grows
D. Becomes zero

**Q6 (Compute — verdict matrix).** A lane is `structural_dead=True` with only 3 closes and all four gates green. Verdict? Which precedence rule decides it, and what does it outrank?

**Q7 (Compute — verdict matrix).** A lane has 19 closes, expectancy +0.30, all gates green. Verdict? And the same lane at exactly 20 closes with positive expectancy but one red gate — what *kind* of verdict can it now receive that it could not at 19? (Use the matrix: name the decision the 20-close, one-red-gate, positive-expectancy lane receives.)

**Q8 (Compute — verdict matrix).** The halted fixture: 394 closes, expectancy −0.375 per $1. Verdict and the deciding rule? Show the two numeric comparisons.

**Q9 (Compute — verdict matrix).** A lane has expectancy −0.50 at exactly 30 closes. Verdict — KILL or KEEP-MEASURING? Justify with both relevant thresholds. What changes at exactly 50 closes with expectancy −0.20 or worse — be precise about the expectancy boundary (≤ or <)?

**Q10 (Short answer).** A lane has mild negative expectancy (−0.05) at 100 closes, and the same at 600 closes. Neither is deep-negative. What verdict does the matrix give in the negative-expectancy-below-full-sample rule's terms, and what distinguishes the below-kill-sample case from this one? (State the rule (4) split as documented.)

**Q11 (Compute — verdict matrix).** The all-green fixture: 600 closes, expectancy +0.18, win rate 0.41, all four gates green. Verdict? Then the same lane at 533 closes — verdict and why; and at 534 closes — verdict.

**Q12 (True/False + justification).** True or false: dropping any *single* one of the four gates, at 600 closes with +0.20 expectancy, still allows FUND-ELIGIBLE if the other three are green. Justify.

**Q13 (True/False + justification).** True or false: a lane with *negative* expectancy can be FUND-ELIGIBLE at 1,000 closes if all four gates are green. Justify the actual rule.

**Q14 (MCQ).** A lane ran for 52 weeks but accumulated only 5 closes, all else favourable. Verdict?
A. FUND-ELIGIBLE — a year of data is enough
B. KEEP-FIXING
C. KEEP-MEASURING — calendar time never substitutes for closes
D. KILL

**Q15 (Short answer).** `LaneRecord` validation is fail-closed. List at least five distinct invalid-field cases that raise `ValueError` (include the boolean traps).

**Q16 (MCQ).** `sample_sufficient(closes, level)` for levels `"full"`, `"any"`, `"kill"` checks against which numbers, in that order?
A. 20, 50, 534
B. 534, 20, 50
C. 534, 50, 20
D. 500, 20, 50

**Q17 (Short answer).** What does `Verdict.render()` (also `__str__`) print, and what is guaranteed about reasons? Why is a reasoned refusal more useful than a bare "insufficient data"?

**Q18 (Compute — the four fixtures).** Name the verdict for each built-in fixture: `degen_v2_fixture()`, `small_sample_fixture()` (state its closes), `halted_394_fixture()`, `all_green_fixture()`.

**Q19 (Short answer).** Why does a structural kill outrank the sample-size rules — what would "keep measuring" a proven-dead window actually buy?

**Q20 (Short answer).** The evidence bar for killing (deep-negative at ≥50 closes) is lower than the bar for funding (≥534 closes, all gates green, positive expectancy). Is that asymmetry justified? Give the protocol's rationale in two sentences (what each error costs).

---

## ANSWER KEY

**Q1 — Decisions:** FUND-ELIGIBLE, KEEP-FIXING, KEEP-MEASURING, KILL. **Gates:** `sustained_positive_expectancy`, `proven_entry_strategy`, `top_picks_winning`, `hardened_execution_safety`.
**Q2 —** `REQUIRED_N_FULL = 534`; `MIN_N_ANY_VERDICT = 20`; `MIN_N_KILL = 50`; `DEEP_NEGATIVE_EXPECTANCY = -0.20` **per $1**.
**Q3 — Exactly 534.** Formula: `ceil( (z_a·√(p0(1−p0)) + z_b·√(p1(1−p1)))² / (p1−p0)² )`, where z_a is the inverse-normal at (1 − alpha) and z_b is the inverse-normal at power — the normal-approximation one-sided sample size to distinguish 35% from 30%.
**Q4 — False.** It raises `ValueError`: `p1 <= p0` is rejected (also: any of p1, p0, alpha, power outside (0, 1) raises).
**Q5 — C.** It grows — a smaller effect needs a larger sample to distinguish (the denominator (p1−p0)² shrinks).
**Q6 — KILL.** Precedence rule (1): `structural_dead` → KILL **regardless of sample size or gates** — it outranks both the <20 KEEP-MEASURING refusal and the all-green funding path.
**Q7 — 19 closes: KEEP-MEASURING** (rule 2: closes < 20 → verdict refused, however good the numbers look). **At exactly 20 closes** the lane earns a verdict; with positive expectancy and a red gate it falls through to rule (6) → **KEEP-FIXING** with the red gates listed.
**Q8 — KILL.** Rule (3): expectancy **−0.375 ≤ −0.20** (deep-negative threshold met) **and** closes **394 ≥ 50** (kill sample met). Both comparisons must be stated.
**Q9 — At 30 closes: KEEP-MEASURING, not KILL.** Although −0.50 ≤ −0.20, 30 < `MIN_N_KILL = 50`, so the deep-negative kill cannot fire yet — the kill floor prevents killing a lane on a small, noisy sample. **At exactly 50 closes with expectancy ≤ −0.20, rule (3) fires → KILL.** Boundary: the expectancy test is **≤** −0.20 (at ≥50 closes) — exactly −0.20 at exactly 50 closes kills (test-pinned).
**Q10 —** Rule (4): negative expectancy below the full sample → **KEEP-FIXING** when at/above the kill sample but not deep-negative (the 100-close and 600-close mild-negative lanes are test-pinned as KEEP-FIXING). The split: below the kill sample (<50 closes) a non-deep / negative lane is instead **KEEP-MEASURING**; between the kill floor and a deep-negative result, the verdict is fixing, not measuring and not killing.
**Q11 — 600 closes: FUND-ELIGIBLE** (all gates green + 600 ≥ 534 + positive expectancy). **533 closes: not FUND-ELIGIBLE** — one close short of the full sample; the matrix gives KEEP-MEASURING / KEEP-FIXING with the shortfall named (test-pinned: 533 measures, 534 funds). **534 closes: FUND-ELIGIBLE** (with all gates green and positive expectancy).
**Q12 — False.** FUND-ELIGIBLE requires *each* gate individually; dropping any one of the four revokes it (test-pinned gate-by-gate) — the lane becomes KEEP-FIXING with the red gate(s) named.
**Q13 — False.** Negative expectancy never funds — test-pinned at 1,000 closes. Rule (5) requires *positive* expectancy in addition to all gates green and ≥534 closes; a negative-expectancy lane is routed by rules (3)/(4) to KILL or KEEP-MEASURING / KEEP-FIXING instead.
**Q14 — C.** 5 closes < 20 → KEEP-MEASURING. The sample in a win-rate claim is made of closes, not calendar time — 52 weeks at 5 closes is still a 5-close sample.
**Q15 —** Any five: empty name; negative closes; non-int closes; **boolean closes**; non-numeric expectancy; win rate outside [0, 1]; negative weeks; **a non-bool structural flag** (e.g. a string). All raise `ValueError`.
**Q16 — B.** `"full"` → 534, `"any"` → 20, `"kill"` → 50. (Unknown levels and negative closes raise.)
**Q17 —** It prints `"<lane>: <DECISION>"` followed by one `  - <reason>` line per reason; **a verdict never renders without reasons**. A reasoned KEEP-MEASURING tells the Dec 20 decision exactly what is missing (how many closes, which threshold) and what to do next, where a bare "insufficient data" gives no actionable shortfall.
**Q18 —** `degen_v2_fixture()` → **KILL** (structural). `small_sample_fixture()` → **KEEP-MEASURING** (18 closes). `halted_394_fixture()` → **KILL** (394 closes, −0.375). `all_green_fixture()` → **FUND-ELIGIBLE** (600 closes, +0.18, all gates green).
**Q19 —** A structural kill rests on proof about the lane's *window/mechanism* (e.g. the 2–10 min sniper window is structurally dead), not on a noisy estimate that more data could overturn. Continuing to "measure" it buys no information — it only spends attention and paper capital re-proving a proven fact.
**Q20 —** Yes, on the protocol's cost asymmetry: funding a bad lane risks real money on an unproven win-rate claim, so its bar demands a sample that can actually separate 35% from 30% plus all four safety gates. Killing only stops spending attention/paper on a lane whose expectancy is already deeply negative at a non-trivial sample — continuing it has no upside case left to protect, and the 50-close floor still guards against killing on noise.

---

## GRADING RUBRIC

- **MCQ (Q5, Q14, Q16):** 1 point for the correct letter only.
- **True/False + justification (Q4, Q12, Q13):** 1 point only if verdict and justification both match the key.
- **Short-answer (Q1, Q2, Q10, Q15, Q17, Q19, Q20):** 1 point if every element in the key is present and correct; Q1 and Q2 require verbatim names/numbers.
- **Compute-it (Q3, Q6, Q7, Q8, Q9, Q11, Q18):** 1 point for the exact verdict *and* the deciding comparisons/precedence stated in the key; a bare verdict with no rule on Q6/Q8/Q9 scores 0.
- **Pass rule:** Pass = **≥17/20 (≥85%)**. Below 17 is a fail.
- **What a fail means:** Re-study the toolkit README (and `verdict.py` if needed), then retake the full exam. Below the minimum sample there is no verdict to give — including on this exam's topic itself.
