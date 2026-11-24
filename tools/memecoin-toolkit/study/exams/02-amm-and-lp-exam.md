# Exam — AMM and LP Lock Checks

- **Total questions:** 22
- **Points:** 1 per question (22 points total)
- **PASS BAR: ≥85% — minimum 19 of 22 correct** (0.85 × 22 = 18.7, rounded up)
- **Conditions:** Closed-book. Synthetic data only. Show working on compute-it questions.
- **Source:** Answerable from the `02-amm-and-lp` README and `amm.py` alone.

---

## QUESTIONS

**Q1 (MCQ).** `get_amount_out` computes output as:
A. `reserve_out × amount_in / reserve_in`, fee ignored
B. `reserve_out × amount_in_after_fee / (reserve_in + amount_in_after_fee)`, fee taken from the input
C. `reserve_out − amount_in_after_fee`
D. Spot price × amount_in, always

**Q2 (MCQ).** The default fee for `get_amount_out` / `Pool` is:
A. 100 bps
B. 0 bps
C. 25 bps
D. 90 bps

**Q3 (Compute).** `get_amount_out(1, 100, 1_000_000)` at the default fee. Give the output to 1 decimal place.

**Q4 (True/False + justification).** "Slippage is always ≥ price impact for the same trade." True or false? Justify.

**Q5 (Short answer).** Why are price impact and slippage kept as two separate numbers, and which one should a min-out guard be built from?

**Q6 (Compute).** On any constant-product pool, price impact (fee excluded) when `amount_in == reserve_in`, and when `amount_in == reserve_in / 4`. Give both percentages.

**Q7 (MCQ).** A swap whose output is below `min_out`:
A. Executes at the worse price anyway
B. Raises `InsufficientOutputError` (a `ValueError`) and is refused
C. Returns 0 silently
D. Is clamped up to `min_out`

**Q8 (MCQ).** `ensure_min_out(amount_out, min_out=None)`:
A. Raises — a guard is mandatory
B. Disables the guard and returns the amount; a negative `min_out` raises `ValueError`
C. Treats `None` as `min_out = 0` and raises on equality
D. Returns `None`

**Q9 (Compute).** `impermanent_loss_pct(4.0)` and `impermanent_loss_pct(0.25)` and `impermanent_loss_pct(1.0)`. Give all three.

**Q10 (MCQ).** `verify_lp_lock` counts as "secured":
A. Burned LP only
B. Burned + locker-held LP; deployer-held LP is reported but never counted
C. Burned + locker + deployer LP
D. Any LP not in a user wallet

**Q11 (Compute).** LP total supply 1000. (a) Burn 600, lockers hold 300, deployer holds 100 → verdict and secured %? (b) Burn 950, lockers 0 → verdict? (c) Burn 400, lockers 100, deployer 500 → verdict and secured %?

**Q12 (MCQ).** Secured of exactly 90.0% at the default threshold gives:
A. `"unlocked"` — the threshold is strict
B. `"locked"`, passed=True — verdict is locked iff secured ≥ threshold
C. `"unverifiable"`
D. Passed=False but verdict `"locked"`

**Q13 (True/False + justification).** "If locker balances were never fetched (`None`), `verify_lp_lock` should return `"unlocked"`." True or false? Justify — including why `"unlocked"` is also wrong.

**Q14 (MCQ).** Balances claiming 60% secured + 60% deployer-held (120% of supply in total) produce:
A. `"locked"`
B. `"unlocked"`
C. `"unverifiable"` with passed=False — the fixture contradicts itself
D. A `ValueError`

**Q15 (Short answer).** Why does LP in the deployer wallet not count toward "locked" even if the deployer promises not to touch it?

**Q16 (Compute).** `lp_share_pct(250, 1000)` and `pro_rata_amounts(250, 1000, reserve_x=100, reserve_y=1_000_000)`.

**Q17 (MCQ).** `get_amount_in` with `amount_out` equal to the whole `reserve_out`:
A. Returns `reserve_in`
B. Returns infinity
C. Raises `ValueError` — an output at/beyond the reserve can never be bought
D. Returns 0

**Q18 (True/False + justification).** "A two-hop `chained_quote` can fill better than the second hop's naive spot quote suggests, because each hop only charges impact, not fees." True or false? Justify.

**Q19 (MCQ).** In the demo sandwich estimate, the same attack on a 10× thinner pool at the same spot price cost the victim:
A. Less — thin pools move less
B. The same — spot price is all that matters
C. Much more (29.46% extra loss vs 3.86% on the deep pool)
D. Nothing — sandwiches only work on deep pools

**Q20 (Short answer).** In `estimate_sandwich`, why must the three swaps (front-run, victim, attacker sell-all) each run on the pool state left by the previous one?

**Q21 (MCQ).** `k` (reserve_x × reserve_y) after a swap with a fee:
A. Is exactly preserved
B. Strictly grows — part of the input stays in the pool as the fee
C. Strictly shrinks
D. Becomes 0

**Q22 (MCQ).** A `threshold_pct` of 0, 101, or `None` for `verify_lp_lock`:
A. Is accepted
B. Raises `ValueError` — threshold must lie in `(0, 100]`
C. Defaults to 90.0
D. Returns `"unverifiable"`

---

## ANSWER KEY

- **Q1: B** — Constant-product quote with the fee taken from the input side.
- **Q2: C** — 25 bps is the documented default.
- **Q3: 9,876.5 tokens.** Working: in_after_fee = 1 × (1 − 0.0025) = 0.9975; out = 1,000,000 × 0.9975 / (100 + 0.9975) = 997,500 / 100.9975 = 9,876.48 ≈ 9,876.5.
- **Q4: True** — Slippage = impact + fee effect against the same spot quote; the fee can only make the actual fill worse than the curve-only fill, never better.
- **Q5:** Impact is the pool's shape (curve movement alone, fee excluded); slippage is what the trader actually pays (fee included). A min-out guard must be built from slippage / the actual quote — guarding on impact alone would promise fills the fee makes impossible.
- **Q6: 50% and 20%.** Working: in = R → curve out = R_out × R/(R+R) = 0.5·R_out vs spot-expected R_out → 50%. In = R/4 → out = R_out × 0.25/1.25 = 0.20·R_out vs spot-expected 0.25·R_out → (0.25−0.20)/0.25 = 20%.
- **Q7: B** — Fail-closed refusal via `InsufficientOutputError`.
- **Q8: B** — `None` disables; zero and exactly-at guards pass; negative raises.
- **Q9: −20%, −20%, 0%.** Working: 2√r/(1+r) − 1: r=4 → 4/5 − 1 = −0.20; r=0.25 → 1/1.25 − 1 = −0.20 (symmetric under r → 1/r); r=1 → 2/2 − 1 = 0.
- **Q10: B** — Only burned + locker-held counts; deployer LP is reported separately and never secured.
- **Q11: (a) "locked", 90.0%** ((600+300)/1000 — exactly at threshold, ≥ passes; deployer's 100 ignored). **(b) "locked"** (95.0% ≥ 90%). **(c) "unlocked", 50.0%** ((400+100)/1000; deployer's 500 does not count).
- **Q12: B** — The comparison is ≥ threshold, so exactly 90.0% locks.
- **Q13: False** — Missing inputs return `"unverifiable"`, passed=False. `"unlocked"` is also wrong because it asserts a measured fact (LP is movable) when nothing was measured; unverifiable keeps "we don't know" distinct from both verdicts while still blocking.
- **Q14: C** — Each balance alone or all together exceeding supply → `"unverifiable"`, never a guessed "locked".
- **Q15:** Because the deployer can pull/move it at any time — a promise is not a lock. Only burned (gone) or locker-held (contractually immobile) LP is verifiably secured.
- **Q16: 25.0%; (25.0, 250,000).** Working: 250/1000 = 25%; pro-rata = 0.25 × (100, 1,000,000).
- **Q17: C** — At/beyond-reserve outputs raise `ValueError`.
- **Q18: False** — Each hop charges its OWN fee AND impact, and each hop's output feeds the next, so the route fills worse than a naive spot reading of any single leg; the two-hop quote equals the manual leg-by-leg composition.
- **Q19: C** — Demo: deep (100 SOL) 3.86% extra loss vs thin (10 SOL) 29.46%; same spot, far less depth to absorb the front-run.
- **Q20:** Because each swap moves the reserves and price the next swap faces; running them on the original state would erase exactly the price manipulation the simulation measures.
- **Q21: B** — At zero fee k is exactly preserved; with a fee, k strictly grows.
- **Q22: B** — Threshold must be in `(0, 100]`; invalid values raise rather than defaulting.

---

## GRADING RUBRIC

- **MCQ (Q1, Q2, Q7, Q8, Q10, Q12, Q14, Q17, Q19, Q21, Q22):** 1 point for the correct letter only.
- **True/False + justification (Q4, Q13, Q18):** 1 point only if verdict AND justification are both correct.
- **Short answer (Q5, Q15, Q20):** 1 point if the mechanism in the key is stated.
- **Compute-it (Q3, Q6, Q9, Q11, Q16):** 1 point per question; all sub-parts numerically correct (tolerance ±0.1 unless exact % stated). Working must be shown for Q3 and Q6.
- **Pass rule:** ≥85% → **at least 19 of 22 correct** passes.
- **On a fail:** Re-study the toolkit note/README and the missed sections of `amm.py`, then retake the full exam. Do not treat LP verdicts or quotes from this toolkit as trustworthy in your own reasoning until a retake passes.
