# Exam — Token Lifecycle

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR:** ≥85% — at least **17 of 20** correct
- **Conditions:** Closed-book. Synthetic data only. Answer from the toolkit's README/module alone.

---

## QUESTIONS

### Multiple choice (Q1–Q8) — choose the one correct option

**Q1.** `classify()` applies its rules in which order?
- A. BONDING check → collapse → validity → RUNNER → FADING
- B. Missing/invalid metric → `UNKNOWN`; collapse → `DEAD`; curve < 100% → `BONDING`; RUNNER test; FADING test; otherwise `GRADUATED`
- C. RUNNER test first, then validity, then collapse
- D. Age first: young tokens are always `BONDING`

**Q2.** A graduated snapshot is `RUNNER` exactly when:
- A. Momentum ≥ 15%, liquidity ≥ $25,000, mcap ≥ $50,000, holder trend ≥ −5%
- B. Momentum > 15%, liquidity > $25,000, any mcap, holders rising
- C. Momentum ≥ 10%, liquidity ≥ $10,000, mcap ≥ $50,000, holder trend ≥ 0%
- D. Curve complete and price rising, regardless of liquidity or mcap

**Q3.** Liquidity alone forces `DEAD` (at any age, graduated or not) when it is:
- A. Below $10,000
- B. Below $25,000
- C. Below $1,000
- D. Exactly zero

**Q4.** A graduated snapshot is `FADING` when **any** of these holds:
- A. Momentum ≤ −10%, or holder trend ≤ −15%, or liquidity < $10,000
- B. Momentum ≤ −85% and holder trend ≤ −30%
- C. Momentum < 15% (i.e. it merely fails the RUNNER test)
- D. Age exceeds 24 hours

**Q5.** The transition `DEAD → RUNNER` is:
- A. Illegal — dead is final
- B. Legal and unremarkable ("ok")
- C. Legal but flagged `"suspicious"` — dormant revivals happen and deserve a second look
- D. Legal only via `FADING`

**Q6.** The transition `FADING → RUNNER` is:
- A. Illegal
- B. Legal and normal — a revival, flagged "ok"
- C. Legal but flagged `"suspicious"`
- D. Impossible because `FADING` can only go to `DEAD`

**Q7.** Which of these transitions is illegal?
- A. `BONDING → DEAD`
- B. `GRADUATED → FADING`
- C. `RUNNER → GRADUATED`
- D. `FADING → DEAD`

**Q8.** `is_runner_phase()` is True for:
- A. `RUNNER` and `GRADUATED`
- B. `RUNNER` only — `UNKNOWN` is never a huntable runner
- C. Any phase except `DEAD`
- D. `RUNNER`, and `UNKNOWN` when data is merely delayed

### True / False with justification (Q9–Q12) — state True or False AND justify in one sentence

**Q9.** A snapshot with one missing metric should be classified by treating the missing value as zero.

**Q10.** A token may legally die (`BONDING → DEAD`) without ever graduating.

**Q11.** A snapshot with curve completion 101% and otherwise perfect runner numbers classifies as `RUNNER`.

**Q12.** In `time_in_phase`, the last timeline point owns the interval from its timestamp onward indefinitely.

### Short answer (Q13–Q17)

**Q13.** Collapse to `DEAD` (with liquidity still high) requires two conditions together. Name them, and explain why price collapse alone is not enough.

**Q14.** A pre-graduation token prints enormous momentum and liquidity numbers. Why is it still `BONDING`, and why does that ordering matter to a hunting lane?

**Q15.** The zombie fixture sits in `FADING` for two days without dying. Why is "not dead yet" not a buy signal?

**Q16.** What does `classify_timeline()` do when timestamps are non-monotonic, and why?

**Q17.** What does an empty `snapshot_issues()` result mean, and what does `classify_with_reasons()` report for an `UNKNOWN` snapshot?

### Compute it (Q18–Q20) — show your working

**Q18.** Classify: (a) curve 100%, liquidity $30,000, mcap $60,000, momentum +20%, holder trend −2%; (b) the same snapshot but momentum +14% (all else unchanged, no FADING trigger present).

**Q19.** For the DACITY-shape timeline — points at t = 0 (BONDING), 20 (BONDING), 38 (GRADUATED), 55 (RUNNER), 80 (FADING), 105 (DEAD), with no `end_minutes` — compute `time_in_phase` for every phase.

**Q20.** Classify: (a) curve 100%, liquidity $80,000, mcap $500,000, momentum −90%, holder trend −40%; (b) the same snapshot but holder trend −10%.

---

## ANSWER KEY

**Q1. B** — Validity first (fail-closed `UNKNOWN`), then collapse, then bonding, then the runner test, then fading, with `GRADUATED` as the remainder.

**Q2. A** — All four runner conditions together, at exactly those thresholds, on a graduated token.

**Q3. C** — Liquidity < $1,000 is `DEAD` outright (the second collapse route needs price *and* holder flight).

**Q4. A** — Any one of the three fading triggers suffices; merely failing the RUNNER test (option C) leaves a token `GRADUATED`.

**Q5. C** — `DEAD → RUNNER` is in the legal table but is the one transition flagged `"suspicious"`.

**Q6. B** — `FADING → RUNNER` is a normal revival and flags "ok".

**Q7. C** — No phase returns to `BONDING`, and `RUNNER` never returns to `GRADUATED`; the other three options are all legal.

**Q8. B** — Only `RUNNER`; `UNKNOWN` is a feed gap, not a phase, and must never be treated as an opportunity.

**Q9. False** — A missing (or `None`/NaN/invalid) metric yields `UNKNOWN`, never a guessed phase built on a substituted zero.

**Q10. True** — `BONDING → DEAD` is in `LEGAL_TRANSITIONS`; a pre-graduation collapse is a normal way for a launch to end.

**Q11. False** — Curve completion outside [0, 100] is invalid input and yields `UNKNOWN`, whatever the other numbers say.

**Q12. False** — The last point owns nothing unless `end_minutes` is supplied (and `end_minutes` before the last point raises `ValueError`); otherwise its duration would be invented and unbounded.

**Q13.** Momentum ≤ −85% **and** holder trend ≤ −30% together. Price alone can be a wick, a thin-pool print, or a single dump in a token whose holders are staying; requiring holders to be fleeing at the same time distinguishes an actual collapse (both price and holder flight) from a violent but survivable move.

**Q14.** The curve-completion check runs before the runner test: curve < 100% is `BONDING` regardless of the other numbers. A hunting lane must not fire on unproven, still-bonding tokens — graduation is the event that separates "numbers on a curve" from a tradeable market, so runner-grade prints before graduation do not promote the token.

**Q15.** `FADING` means the graduated token is leaving: weakening momentum, holders, or liquidity, with none of the runner conditions met. Surviving a long time in that state (the zombie) is persistence of decay, not strength — the lane hunts `RUNNER` only, and "hasn't died" is not one of the runner conditions.

**Q16.** It raises `ValueError`. Phase sequences and durations are only meaningful in time order; silently sorting or accepting reversed timestamps would manufacture transitions and time-in-phase numbers that never happened.

**Q17.** Empty issues means the snapshot is usable (all six metrics present and valid). For `UNKNOWN`, `classify_with_reasons` returns the phase plus the exact tuple of missing/invalid metrics that caused it.

**Q18.** (a) **RUNNER** — graduated (curve 100%), momentum 20 ≥ 15, liquidity 30k ≥ 25k, mcap 60k ≥ 50k, holder trend −2 ≥ −5: all four runner conditions hold. (b) **GRADUATED** — momentum 14 < 15 fails the runner test, and no fading trigger holds (momentum > −10, holders > −15, liquidity ≥ $10k), so it is graduated-but-undecided.

**Q19.** Each point owns the interval to the next; the last owns nothing. BONDING: t0→t38 = **38m**; GRADUATED: 38→55 = **17m**; RUNNER: 55→80 = **25m**; FADING: 80→105 = **25m**; DEAD: **0m** (last point, no end given). Total = 105m, the timeline span.

**Q20.** (a) **DEAD** — momentum −90 ≤ −85 and holder trend −40 ≤ −30 is the collapse route, even though liquidity ($80k) is far above the $1,000 floor. (b) **FADING** — holder trend −10 breaks the collapse pair (needs ≤ −30), the token is graduated, the runner test fails, and momentum −90 ≤ −10 triggers `FADING`.

---

## GRADING RUBRIC

- **Multiple choice (Q1–Q8):** 1 point for the correct letter only; no partial credit.
- **True/False with justification (Q9–Q12):** 1 point only if BOTH the verdict and the justification are correct. A correct verdict with a wrong or missing reason scores 0.
- **Short answer (Q13–Q17):** 1 point if the answer states the mechanism named in the key (wording may differ). Answers that name a phase without the deciding conditions score 0.
- **Compute it (Q18–Q20):** 1 point for the correct phase/duration(s) with working shown against the rule order and thresholds. Multi-part questions require every part correct.
- **Pass rule:** ≥85% = **17 or more of 20**. 16 or fewer is a fail.
- **On a fail:** re-study the toolkit note/README, hand-classify all four fixture timelines, and retake the full exam (not just the missed questions) no sooner than the next study session.
