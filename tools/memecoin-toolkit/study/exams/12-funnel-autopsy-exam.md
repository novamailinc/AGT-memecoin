# Exam — Funnel Autopsy

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR:** ≥85% — at least **17 of 20** correct
- **Conditions:** Closed-book. Synthetic data only. Answer from the toolkit's README/module alone.

---

## QUESTIONS

### Multiple choice (Q1–Q8) — choose the one correct option

**Q1.** The runner bar used everywhere in this lab is:
- A. Liquidity ≥ $10,000, mcap ≥ $25,000, momentum positive in any snapshot
- B. Liquidity ≥ $25,000, mcap ≥ $50,000, and positive momentum — all three in the same snapshot, thresholds inclusive
- C. Liquidity ≥ $25,000 or mcap ≥ $50,000, momentum positive
- D. Liquidity > $25,000, mcap > $50,000, momentum positive, averaged over the timeline

**Q2.** The classifier's precedence order (first match wins) is:
- A. rug > never-met-bar > ceiling > burned > safety-lag > caught-or-other
- B. ceiling > rug > never-met-bar > burned > safety-lag > caught-or-other
- C. never-met-bar > rug > ceiling > burned > safety-lag > caught-or-other
- D. never-met-bar > ceiling > rug > safety-lag > burned > caught-or-other

**Q3.** The DACITY-like fixture (convergence blocked once at t=1, bar met from t≈5 to t≈40, safety later known-ok, never retried) classifies as:
- A. `safety-unknown-lag`
- B. `invented-ceiling-block`
- C. `never-met-bar`
- D. `burned-one-shot-convergence`

**Q4.** The Doomsday-like fixture ($96k liq / $1.14M mcap / safety ok, blocked only by a "300% ceiling" gate reason) classifies as:
- A. `invented-ceiling-block`
- B. `gate-correctly-blocked-rug`
- C. `burned-one-shot-convergence`
- D. `caught-or-other`

**Q5.** A token meets the bar at a snapshot where its safety report failed, and its outcome is a rug. The verdict is:
- A. `safety-unknown-lag`
- B. `gate-correctly-blocked-rug`
- C. `never-met-bar`
- D. `caught-or-other`

**Q6.** Which gate-log reason strings count as a ceiling block?
- A. Only the exact string `"ceiling"`
- B. Reasons matching `ceiling` / `max-gain` / `max_gain` / `max gain` / `gain cap`, case-insensitively
- C. Any reason containing a percentage
- D. Any block issued by the momentum lane

**Q7.** For the burned-convergence rule, a retry only counts if it is:
- A. In the same minute as the block, by the same lane
- B. Strictly after the first block (a retry by a different lane does count)
- C. Accompanied by a second block
- D. Never — retries are ignored entirely

**Q8.** A case with an empty timeline (or missing keys) classifies as:
- A. `caught-or-other`
- B. `safety-unknown-lag`
- C. It raises `ValueError`
- D. `never-met-bar` (fail-closed)

### True / False with justification (Q9–Q12) — state True or False AND justify in one sentence

**Q9.** A token meets the bar if liquidity clears $25k in one snapshot, mcap clears $50k in a different snapshot, and momentum is positive in a third.

**Q10.** A failed safety report at a snapshot that is *below* the bar counts as a correct rug block.

**Q11.** When a case matches both a rug story and a ceiling reason, the rug verdict wins.

**Q12.** `summarise()` omits classes with zero cases from its result.

### Short answer (Q13–Q17)

**Q13.** What two facts, together, turn an early convergence block into `burned-one-shot-convergence` rather than `safety-unknown-lag`?

**Q14.** Why does `invented-ceiling-block` rank above `burned-one-shot-convergence` when a case matches both?

**Q15.** The DACITY-like timeline has safety `unknown` at its first bar-meeting snapshot. Why is it still not classified as `safety-unknown-lag`?

**Q16.** What single change to the Doomsday-like gate log would move its verdict to `caught-or-other`?

**Q17.** Do the reporting helpers (`explain`, `blocking_events`, `has_ceiling_block`) ever change the verdict returned by `classify`? What are they for?

### Compute it (Q18–Q20) — show your working

**Q18.** DACITY-like timeline (t, liq, mcap, momentum+, safety): (0, 3k, 18k, +, unknown), (1, 8k, 30k, +, unknown), (5, 40k, 120k, +, unknown), (10, 75k, 420k, +, known_ok), (40, 110k, 1.6M, +, known_ok). Compute the number of bar-meeting snapshots, `first_bar_time`, and `bar_duration_minutes`.

**Q19.** Decide `meets_bar` for: (a) liq $24,999, mcap $50,000, momentum positive; (b) liq $25,000, mcap $50,000, momentum positive; (c) liq $25,000, mcap $50,000, momentum flat/negative.

**Q20.** `summarise()` is run over the six fixtures (one of each class). State the resulting counts.

---

## ANSWER KEY

**Q1. B** — One bar everywhere: all three conditions at once in a single snapshot, both numeric thresholds inclusive.

**Q2. C** — Documented precedence: never-met-bar > rug > ceiling > burned > safety-lag, else caught-or-other.

**Q3. D** — Convergence fired once, was blocked, safety later became known-ok, and no retry ever came while the bar held for ~35 minutes.

**Q4. A** — The only block cites a home-grown gain ceiling on a token comfortably over the bar with safety ok.

**Q5. B** — A rug outcome, or a failed safety report at a bar-meeting snapshot, is a correct block and outranks every miss story except never-met-bar.

**Q6. B** — All five spellings match, case-insensitively; an ordinary block reason does not count.

**Q7. B** — A retry must be strictly after the first block; a same-minute retry does not un-burn the shot, a different lane's retry does, and a second block still counts as burned.

**Q8. D** — Fail-closed: an empty or bar-less case (including missing keys) is `never-met-bar`.

**Q9. False** — The three conditions must coincide in one snapshot; conditions split across prints never meet the bar.

**Q10. False** — A safety failure only counts as a rug block at a bar-meeting snapshot; below the bar it does not count.

**Q11. True** — Precedence places the rug verdict above the ceiling class: a funnel is never blamed via a miss story for keeping out a rug.

**Q12. False** — `summarise` returns counts keyed by all six classes, zeros included.

**Q13.** (1) Safety is known-ok somewhere in the case — the token *could* have been carded once safety arrived; and (2) after the first convergence block there is no retry (strictly after that block) while the token keeps meeting the bar. If safety never becomes known-ok, the same early block is instead `safety-unknown-lag`.

**Q14.** A ceiling block is a self-inflicted, specifically identifiable cause — our own invented rule stopped the token regardless of any signal timing. Precedence makes the first, most actionable story win on purpose: fix the rule you invented before blaming a one-shot signal's timing.

**Q15.** Because safety later became known-ok (at t=10 and t=40), so the burned-convergence rule matches — and it sits above `safety-unknown-lag` in precedence. `safety-unknown-lag` is reserved for cases where safety never arrives at bar-meeting snapshots at all.

**Q16.** Remove the ceiling reason — e.g. replace the gate log with a non-ceiling outcome such as "all gates passed / carded". With no ceiling block and no other miss story (no rug, no burned convergence, safety ok), the case falls through to `caught-or-other`.

**Q17.** No — reporting helpers never change the verdict. They expose the evidence behind it (bar-print counts/times, blocking events, ceiling/retry checks) so a human can audit *why* `classify` returned what it did.

**Q18.** Bar check per snapshot: t=0 fails (liq/mcap), t=1 fails (liq/mcap), t=5 passes (40k ≥ 25k, 120k ≥ 50k, +), t=10 passes, t=40 passes. **Bar-meeting snapshots = 3; first_bar_time = 5; duration = 40 − 5 = 35 minutes.**

**Q19.** (a) **False** — liquidity is $1 below the inclusive $25,000 minimum. (b) **True** — both thresholds are inclusive, so exactly-at-the-bar passes with positive momentum. (c) **False** — positive momentum is required; flat/negative momentum fails regardless of the numbers.

**Q20.** Exactly **1 in each of the six classes**: never-met-bar 1, gate-correctly-blocked-rug 1, invented-ceiling-block 1, burned-one-shot-convergence 1, safety-unknown-lag 1, caught-or-other 1 (total 6 cases).

---

## GRADING RUBRIC

- **Multiple choice (Q1–Q8):** 1 point for the correct letter only; no partial credit.
- **True/False with justification (Q9–Q12):** 1 point only if BOTH the verdict and the justification are correct. A correct verdict with a wrong or missing reason scores 0.
- **Short answer (Q13–Q17):** 1 point if the answer states the mechanism named in the key (wording may differ). Answers that name a class without the deciding facts score 0.
- **Compute it (Q18–Q20):** 1 point for the correct final value(s) with working shown against the bar/precedence rules. Multi-part questions require every part correct.
- **Pass rule:** ≥85% = **17 or more of 20**. 16 or fewer is a fail.
- **On a fail:** re-study the toolkit note/README, re-classify all six fixtures by hand, and retake the full exam (not just the missed questions) no sooner than the next study session.
