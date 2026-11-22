# Exam — Deployer Forensics

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR:** ≥85% — at least **17 of 20** correct
- **Conditions:** Closed-book. Synthetic data only. Answer from the toolkit's README/module alone.

---

## QUESTIONS

### Multiple choice (Q1–Q8) — choose the one correct option

**Q1.** The four verdicts are:
- A. `SAFE`, `RISKY`, `RUGGER`, `BANNED`
- B. `UNKNOWN`, `CLEAN`, `WATCH`, `SERIAL_RUGGER`
- C. `CLEAN`, `WATCH`, `RUGGER`, `BLACKLISTED`
- D. `UNKNOWN`, `CLEAN`, `DIRTY`, `SERIAL_RUGGER`

**Q2.** `SERIAL_RUGGER` requires exactly:
- A. ≥ 2 launches and ≥ 2 rugs
- B. ≥ 3 launches and ≥ 2 rugs
- C. A rug rate above 50%
- D. ≥ 3 rugs, regardless of launch count

**Q3.** `CLEAN` is earned only with:
- A. At least 1 launch with a known, non-rug outcome
- B. No launches at all
- C. ≥ 2 launches, every outcome known, zero rugs, and no funding/name reuse flags
- D. ≥ 2 launches and a rug rate below 25%, unknowns allowed

**Q4.** `rug_rate()` is computed over, and returns `None` when:
- A. All launches; `None` when there are no launches at all
- B. Known outcomes only; `None` when nothing is known — missing evidence is never read as 0%
- C. All launches, counting unknowns as rugs; never `None`
- D. Runner outcomes only; `None` when there are no runners

**Q5.** Which of these, on its own (with zero rugs), can trigger `WATCH`?
- A. A single launch
- B. Funding-reuse ratio ≥ 0.5 with ≥ 2 launches (or name-reuse ratio ≥ 0.5 with ≥ 2 launches)
- C. A median interval under 24 hours
- D. Using the same mint twice

**Q6.** `name_pattern()` normalises a name by:
- A. Hashing it
- B. Lowercasing, stripping punctuation, and removing trailing digits/separators — so `"MoonCat"`, `"MoonCat 2"` and `"MoonCat 2!!"` collide; an unusable name becomes `"<empty>"`
- C. Taking the first word only
- D. Removing all digits anywhere in the name

**Q7.** `should_veto()` returns True only for:
- A. `SERIAL_RUGGER` and `WATCH`
- B. `SERIAL_RUGGER` only — `WATCH` and `UNKNOWN` inform without convicting
- C. Any deployer who is not `CLEAN`
- D. Any deployer with a rug rate above 25%

**Q8.** A total stranger (no launch history at all) has verdict and veto status:
- A. `CLEAN`; veto False
- B. `WATCH`; veto True
- C. `UNKNOWN`; veto False — but unknown is never read as clean
- D. `UNKNOWN`; veto True, to be safe

### True / False with justification (Q9–Q12) — state True or False AND justify in one sentence

**Q9.** A deployer with two launches, both of which rugged, is a `SERIAL_RUGGER`.

**Q10.** A single clean launch (known outcome, no rug) earns the `CLEAN` verdict.

**Q11.** `fingerprint()` depends on the order records are supplied in.

**Q12.** `median_interval_hours` is `None` for a deployer with fewer than two launches.

### Short answer (Q13–Q17)

**Q13.** Why is a deployer with three launches and three *unknown* outcomes `UNKNOWN` rather than `CLEAN`, and what bad trade does the alternative allow?

**Q14.** Funding-source reuse alone can trigger `WATCH` with zero rugs. Why is a recycled funding wallet evidence at all, and why is it not enough to veto?

**Q15.** Name-pattern normalisation can falsely flag a legitimate creator. Give the behaviour that gets falsely flagged.

**Q16.** If `should_veto` refuses to veto a `WATCH` deployer, who or what is supposed to act on a `WATCH` instead?

**Q17.** `Launch` construction is fail-closed. List the validations it performs.

### Compute it (Q18–Q20) — show your working

**Q18.** SerialSam's fixture: 4 launches, outcomes rug / runner / rug / rug. Compute `rug_count`, `known_count`, `rug_rate`, and the verdict.

**Q19.** OneRugRita's fixture: 2 launches (t=0 "LuckyFrog", runner; t=30h "LuckyFrog 2", rug), both funded by the same wallet. Compute rug rate, funding-reuse ratio, name-reuse ratio, and the verdict.

**Q20.** HonestAda's fixture: 3 launches at t = 0h, 100h, 260h, all outcomes known, no rugs, three distinct funders, three distinct name patterns. Compute the median interval and state the verdict.

---

## ANSWER KEY

**Q1. B** — `Verdict` is exactly `UNKNOWN`, `CLEAN`, `WATCH`, `SERIAL_RUGGER`.

**Q2. B** — The serial bar is both halves: ≥ 3 launches **and** ≥ 2 rugs; failing either half alone keeps the verdict below serial.

**Q3. C** — Anything less than that full evidence set stays `WATCH`/`UNKNOWN`; a partly-unknown history is blocked from `CLEAN`.

**Q4. B** — Denominator is known outcomes only; with zero known outcomes the rate is `None`, so no caller can read missing evidence as a 0% rug rate.

**Q5. B** — Heavy reuse (ratio ≥ 0.5, on ≥ 2 launches) of either kind triggers `WATCH` on its own; so do any single rug below the serial bar and a rug rate ≥ 25% on known outcomes.

**Q6. B** — That normalisation is exactly what makes serial re-issues of one pattern collide.

**Q7. B** — Only `SERIAL_RUGGER` vetoes (and only serial ruggers appear in `blacklist_candidates`); a non-veto must never be read as "clean".

**Q8. C** — No history at all is `UNKNOWN`, not `CLEAN`; the veto does not fire, but the record carries no clearance either.

**Q9. False** — The verdict is `WATCH`: the launch-count half of the serial bar (≥ 3) is unmet, protecting against a serial conviction on thin data even when every observed launch rugged.

**Q10. False** — `CLEAN` requires ≥ 2 launches (plus all-known outcomes, zero rugs, no reuse flags); one clean launch is too thin to clear and stays `WATCH`.

**Q11. False** — The fingerprint is order-insensitive: records are sorted by timestamp first, so shuffled input gives the same fingerprint.

**Q12. True** — Cadence needs at least two launches to have an interval; below that the median interval is `None`.

**Q13.** `CLEAN` is a positive claim that requires positive evidence: known outcomes with zero rugs. Three unknowns contain no evidence of either guilt or innocence, so the fail-closed verdict is `UNKNOWN`. Reading unknowns as clean would let a serial rugger earn "clean" status simply by having their outcomes go unrecorded — and downstream entries would be taken on a clearance nobody ever established.

**Q14.** A fresh funder per launch is the normal pattern; recycling one funding wallet across launches links the launches to a single operator's treasury and is characteristic of serial launch operations, so it is real evidence of a coordinated repeat actor. But honest creators also reuse their own wallet — reuse shows coordination, not rugs — so it warrants `WATCH` (weigh it), not the conviction a veto represents.

**Q15.** A legitimate creator running an honest numbered series/sequels — e.g. launching "MoonCat", then a genuine follow-up "MoonCat 2" — collides under normalisation and accrues name-reuse, even though the reuse is branding, not re-issued rug patterns.

**Q16.** The surrounding scoring/judgement layer and the human: `WATCH` is an input to weigh (score, caution, closer review) — a warning signal, deliberately short of the hard veto that only the serial bar earns.

**Q17.** It raises `ValueError` on: an empty deployer, an empty mint, a negative `timestamp_hours`, or an outcome outside `{"rug", "runner", "flat", "unknown"}`. (`verdict_for` likewise raises on an empty deployer string.)

**Q18.** Rugs = 3, known outcomes = 4 (all four known). rug_rate = 3 / 4 = **0.75 (75%)**. Launches 4 ≥ 3 and rugs 3 ≥ 2 → **verdict `SERIAL_RUGGER`** (veto fires; blacklist candidate).

**Q19.** Rug rate = 1 rug / 2 known = **0.50 (50%)**. Funding reuse: both launches share one funder, so reused launches = 2 → ratio = 2/2 = **1.0**. Name reuse: patterns are both "luckyfrog" → distinct patterns 1 → 1 − 1/2 = **0.5**. Verdict: 1 rug but only 2 launches (< 3) → not serial → **`WATCH`** (also supported by both reuse ratios).

**Q20.** Intervals: 100 − 0 = 100h and 260 − 100 = 160h; median of {100, 160} = (100 + 160) / 2 = **130.0h**. Evidence: ≥2 launches, all known, zero rugs, no reuse → **verdict `CLEAN`** (rug rate 0.0).

---

## GRADING RUBRIC

- **Multiple choice (Q1–Q8):** 1 point for the correct letter only; no partial credit.
- **True/False with justification (Q9–Q12):** 1 point only if BOTH the verdict and the justification are correct. A correct verdict with a wrong or missing reason scores 0.
- **Short answer (Q13–Q17):** 1 point if the answer states the mechanism named in the key (wording may differ). Answers that name a verdict without the deciding evidence score 0.
- **Compute it (Q18–Q20):** 1 point for the correct final value(s) with working shown against the documented denominators and bars. Multi-part questions require every part correct.
- **Pass rule:** ≥85% = **17 or more of 20**. 16 or fewer is a fail.
- **On a fail:** re-study the toolkit note/README, recompute all three fixture reports by hand, and retake the full exam (not just the missed questions) no sooner than the next study session.
