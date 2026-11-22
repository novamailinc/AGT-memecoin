# Exam — Backtesting Rigor for Memecoin Research

- **Total questions:** 22
- **Points:** 1 per question (22 points total)
- **PASS BAR: ≥85% — minimum 19 of 22 correct** (0.85 × 22 = 18.7, rounded up)
- **Conditions:** Closed-book. Synthetic data only. Show working on compute-it questions.
- **Source:** Answerable from the `09-backtesting-rigor-meme` README and `rigor.py` alone.

---

## QUESTIONS

**Q1 (MCQ).** The five defences in this toolkit are:
A. Freeze the gate pre-registration; embargoed time-ordered splits; parameter plateaus over spikes; multiple-testing haircut; look-ahead quantification
B. More data; more parameters; in-sample tuning; cherry-picked windows; survivorship inclusion
C. Cross-validation; bootstrapping; shrinkage; ensembling; calibration
D. Unit tests; code review; staging; canary deploys; rollback

**Q2 (MCQ).** After `freeze()`, assigning ANY attribute on a `PreRegistration` (including flipping `_frozen` itself or inventing a new field):
A. Works silently
B. Raises `RuntimeError`
C. Raises `TypeError`
D. Succeeds but invalidates `certify()`

**Q3 (MCQ).** After `freeze()`, item assignment into `reg.thresholds` (e.g. `reg.thresholds["wr"] = 0.9`):
A. Works — dicts are mutable
B. Raises `TypeError` (thresholds is exposed as a mapping proxy, which also has no `update`/`pop`/`clear`/`setdefault`)
C. Raises `RuntimeError`
D. Works but changes the fingerprint only

**Q4 (True/False + justification).** "Mutating the dict you originally passed to the constructor can change a registration after construction." True or false? Justify.

**Q5 (MCQ).** `certify()` on an UNFROZEN registration:
A. Returns True
B. Returns False
C. Raises `RuntimeError` — only a frozen registration can certify
D. Freezes it automatically and returns True

**Q6 (Short answer).** Name the exception/effect for each frozen-mutation route: (a) `set_threshold(...)`; (b) deleting an attribute; (c) `reg.thresholds.update(...)`; (d) rewriting via `reg.__dict__`.

**Q7 (MCQ).** `registration_fingerprint(reg)` is:
A. A random UUID per call
B. A canonical SHA-256 of the contents — identical contents fingerprint identically regardless of dict order; any content change changes it
C. The `registered_at` string
D. A hash of the object's memory address

**Q8 (MCQ).** `evaluate_gate(reg, metrics)` passes iff:
A. Any one threshold is met
B. Every threshold is a MINIMUM the same-named metric must meet (inclusive boundary); a missing or non-finite metric raises `ValueError`, and an unfrozen registration raises `RuntimeError`
C. The average metric exceeds the average threshold
D. Metrics merely exist

**Q9 (Compute).** Demo walk-forward: 120 bars, 3 folds, embargo 5 gives folds (train 0..29, test 35..59), (train 0..59, test 65..89), (train 0..89, test 95..119). (a) Size of each test segment? (b) Which bar indices appear in NO fold's train or test set at all? (c) How many bars does the embargo drop per fold boundary in fold 1?

**Q10 (True/False + justification).** "In `walk_forward` over 100 bars with 3 folds, the test segments tile bars 25–99 and a trailing remainder bar can exist that no fold uses." True or false? Justify briefly from the test coverage.

**Q11 (MCQ).** `check_no_leakage(train_indices, test_indices)` returns:
A. True/False
B. The gap between the disjoint, strictly time-ordered index sets; overlap, disorder, interleaving, or an empty set raises
C. The number of overlapping bars
D. 0 always for walk-forward output — it cannot be checked

**Q12 (MCQ).** `parameter_plateau(results)`: width of the contiguous region around the best parameter keeping at least ___ of the best expectancy. A width of 1 means ___.
A. 50%; a plateau
B. 80%; a spike — fragile
C. 95%; a spike
D. 80%; a plateau — robust

**Q13 (Compute).** A sweep's best expectancy is 0.15 at one setting and 0.02 at both neighbours. (a) Plateau width? (80% of best = 0.12 — check the neighbours.) (b) Conclusion in one phrase. (c) A second sweep: best 1.0, neighbours 0.85 and 0.80, next-out 0.50 on one side — width of the contiguous region around the best keeping ≥80% (the exact-80% neighbour counts)?

**Q14 (Compute).** `deflated_expectancy(observed, n_trials, n_samples) = observed − sqrt(2·ln(max(n_trials,1))) / sqrt(n_samples)`. For observed +0.30, n_samples = 100, compute the deflated expectancy at (a) 1 trial; (b) 10 trials; (c) 100 trials; (d) 1000 trials. (Values to 3 d.p.)

**Q15 (Short answer).** An observed +0.30 survives 10 trials but not 100. What changed — the strategy, or your belief in it? Answer in one or two sentences using the penalty's behaviour.

**Q16 (Compute).** `bonferroni_alpha(0.05, 100)` and `bonferroni_alpha(0.05, 1)`.

**Q17 (MCQ).** `multiple_testing_penalty(n_trials, n_samples)` grows/shrinks how?
A. Grows with samples, shrinks with trials
B. Is zero at 1 trial, grows with trials (log), shrinks with sample size (√n in the denominator)
C. Is constant
D. Grows linearly with trials

**Q18 (MCQ).** `lookahead_experiment(seed=42, n_bars=5000, sigma=0.5)` on iid noise gives approximately:
A. Cheating ≈ 0, honest ≈ 0
B. Cheating expectancy ≈ +0.403, honest ≈ −0.002 — a fabricated edge of ≈ +0.405 from nothing
C. Cheating ≈ +0.04, honest ≈ +0.40
D. Both ≈ +0.40

**Q19 (Short answer).** In `lookahead_experiment` both strategies earn the NEXT bar's return. What is the single difference between the cheating and honest signals, and why does it manufacture an edge from noise?

**Q20 (True/False + justification).** "Doubling sigma in `lookahead_experiment` leaves the fabricated edge unchanged, since it comes from timing, not volatility." True or false? Justify (the theory value is named in the tests).

**Q21 (MCQ).** `chronological_split(data, train_fraction, embargo)` raises when:
A. The fraction is 0.7
B. The fraction is outside (0, 1), or either side ends up empty; the embargoed bars belong to NEITHER side
C. The data is time-ordered
D. Never

**Q22 (MCQ).** `PreRegistration` construction is fail-closed: which construction raises `ValueError`?
A. A finite numeric threshold
B. An empty gate string, a non-dict or empty `thresholds`, or a non-finite / boolean / non-numeric threshold value
C. A `registered_at` timestamp
D. Freezing twice (freeze is idempotent, returns self)

---

## ANSWER KEY

- **Q1: A** — The five defences as listed.
- **Q2: B** — Any post-freeze attribute assignment raises `RuntimeError`; the class is slotted with no `__dict__` back door.
- **Q3: B** — Mapping proxy raises `TypeError` on item assignment/deletion and exposes no mutator methods.
- **Q4: False** — The registration keeps its OWN copy of the thresholds dict at construction, so mutating the caller's original — before or after the freeze — has no effect (and the fingerprint is verified unchanged after failed tampering).
- **Q5: C** — Unfrozen registrations refuse to certify.
- **Q6:** (a) `RuntimeError` from `set_threshold`. (b) Deleting any attribute raises `RuntimeError`. (c) No such method — the mapping proxy exposes no `update` (AttributeError if called). (d) Impossible — slotted class, there is no instance `__dict__` to rewrite.
- **Q7: B** — Canonical SHA-256, dict-order independent, content-sensitive.
- **Q8: B** — Every threshold is an inclusive minimum on the same-named metric.
- **Q9: (a) 25 bars each** (35..59, 65..89, 95..119 inclusive). **(b) Bars 90–94** — fold 1's gap (30–34) enters fold 2's train, fold 2's gap (60–64) enters fold 3's train, but fold 3's embargo gap 90–94 is in no train (fold 3 train ends at 89) and no test. **(c) 5 bars** (30, 31, 32, 33, 34 appear in neither fold-1 train nor fold-1 test).
- **Q10: True** — Test coverage pins exactly this: on 100 bars with 3 folds the test segments tile bars 25–99, and a trailing remainder bar exists that no fold uses.
- **Q11: B** — It returns the gap; any leakage shape raises.
- **Q12: B** — The plateau keeps ≥80% of best expectancy; width 1 is a spike, and a spike is fragile. (`is_robust_plateau` default min_width = 2.)
- **Q13: (a) Width 1** (0.02 < 0.12 on both sides, so the region is the best point alone). **(b) A spike — fragile; do not trust the "optimal" setting.** **(c) Width 3** — best (1.0) + 0.85 neighbour + 0.80 neighbour (exactly 80% of 1.0 counts, the boundary is inclusive in the pinned test); the 0.50 beyond stops the contiguous region on that side. (Region = the best plus both qualifying neighbours, contiguous.)
- **Q14:** Penalty = sqrt(2 ln n_trials)/10. (a) **+0.300** (ln 1 = 0, penalty 0). (b) **+0.085** (penalty ≈ 0.215). (c) **−0.003** (penalty ≈ 0.303). (d) **−0.072** (penalty ≈ 0.372).
- **Q15:** Your belief changed, not the strategy. The penalty prices the selection you performed: having tried 100 variants, a +0.30 best-of-many is what noise alone would be expected to produce, so the deflated estimate is ≈0; at 10 trials the same number is still surprising under noise.
- **Q16: 0.0005 and 0.05.** (alpha / n_trials.)
- **Q17: B** — Zero at one trial; logarithmic in trials; 1/√n_samples in size (documented assumption: per-sample std 1.0).
- **Q18: B** — Cheating +0.403 vs honest −0.002; fabricated edge +0.405.
- **Q19:** The cheating signal is computed from the NEXT bar (it peeks at the bar whose return it then "earns"), while the honest signal uses only the current bar. On iid noise the peeked signal is perfectly aligned with the return it predicts by construction, so it harvests the noise's sign — expectancy ≈ σ·√(2/π) — which is information from the future, not an edge.
- **Q20: False** — The bias scales LINEARLY in sigma (doubling sigma doubles the fake edge), matching σ·√(2/π) theory; the honest signal stays at ≈0 across seeds.
- **Q21: B** — Fraction must lie in (0, 1), neither side may be empty, and embargoed bars belong to neither side.
- **Q22: B** — All listed construction defects raise; a double freeze is explicitly fine (idempotent).

---

## GRADING RUBRIC

- **MCQ (Q1–Q3, Q5, Q7, Q8, Q11, Q12, Q17, Q18, Q21, Q22):** 1 point for the correct letter only.
- **True/False + justification (Q4, Q10, Q20):** 1 point only if verdict AND justification are both correct.
- **Short answer (Q6, Q15, Q19):** 1 point per question; Q6 needs all four routes correct.
- **Compute-it (Q9, Q13, Q14, Q16):** 1 point per question; ALL sub-parts correct (Q14 tolerance ±0.002; Q9(b) must name exactly bars 90–94).
- **Pass rule:** ≥85% → **at least 19 of 22 correct** passes.
- **On a fail:** Re-study the toolkit note/README and `rigor.py`, then retake the full exam. A gate you cannot keep frozen in the lab will not stay frozen under real pressure to ship.
