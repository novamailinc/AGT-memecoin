# Exam — Rug Taxonomy

- **Total questions:** 22
- **Points:** 1 per question (22 points total)
- **PASS BAR: ≥85% — minimum 19 of 22 correct** (0.85 × 22 = 18.7, rounded up)
- **Conditions:** Closed-book. Synthetic data only. Show working on compute-it questions.
- **Source:** Answerable from the `06-rug-taxonomy` README and `ruglab.py` alone.

---

## QUESTIONS

**Q1 (MCQ).** The five detectors are:
A. lp_pull, mint_and_dump, cant_sell, wash_skew, holder_crash
B. lp_pull, honeypot, wash_skew, bundle, holder_crash
C. mint_and_dump, cant_sell, sniper, wash_skew, lp_pull
D. holder_crash, cant_sell, tax_spike, lp_pull, mint_and_dump

**Q2 (MCQ).** `lp_pull` fires when, within the window, BOTH:
A. Liquidity falls ≥80% and volume falls ≥50%
B. Liquidity falls ≥80% and price falls ≥50% over the same snapshot pair
C. Liquidity falls >80% and holders fall ≥50%
D. Price falls ≥80% and liquidity falls ≥50%

**Q3 (Compute).** Liquidity $100,000 → $8,000 and price −80.0% within the window (the demo fixture). (a) Liquidity drop %. (b) Does `lp_pull` fire? (c) Liquidity instead falls exactly 79% with the same price collapse — fires or not? (d) Liquidity falls exactly 80% and price exactly 50% — fires or not?

**Q4 (MCQ).** The `lp_pull` window is:
A. ≤3600s, or ≤3 steps if timestamps are absent
B. ≤600s always
C. The whole lifecycle
D. ≤3 steps always, timestamps ignored

**Q5 (True/False + justification).** "A liquidity drop at exactly 3601s after the earlier snapshot cannot fire `lp_pull` with that pair, but a later in-window pair still can." True or false? Justify.

**Q6 (MCQ).** `mint_and_dump` requires ALL of:
A. Supply grows at all; price falls at all
B. Mint authority active at the inflation pair, supply grows strictly >20% between snapshots, and price at/after the inflation snapshot has fallen >50% from before
C. Mint authority revoked, supply grows >20%, price falls ≥50%
D. Supply grows ≥20% and price falls ≥50%, authority irrelevant

**Q7 (Compute).** (a) Supply grows exactly +20.0% with mint authority active, then price falls 60% — does `mint_and_dump` fire? Which comparison decides it? (b) Supply grows +20.1%, mint active, price later falls exactly 50.0% — fires or not? Which comparison decides it? (c) Same as (b) but the price fall happens BEFORE the inflation — fires or not?

**Q8 (MCQ).** `cant_sell` fires when:
A. `sell_sim_ok` is missing in any snapshot
B. `sell_sim_ok` is `0`, `"false"`, or `None` in any snapshot
C. Any snapshot has `sell_sim_ok is False` — a real boolean False, explicitly
D. Two or more snapshots lack the field

**Q9 (Short answer).** A lifecycle has no `sell_sim_ok` field anywhere. What does `cant_sell` return, and why is never firing on missing data the right choice for THIS detector? (Contrast with a fail-closed gate if you can.)

**Q10 (MCQ).** `wash_skew` fires on a snapshot where ALL of these hold:
A. Volume > $1,000; unique/trades < 0.5; holders moved < 5%
B. buy_vol + sell_vol > $10,000; unique_buyers / trades < 0.1; holder count moved < 2% versus the first snapshot
C. Volume ≥ $10,000; unique ratio ≤ 0.1; holders moved ≤ 2%
D. Volume > $10,000 alone suffices

**Q11 (Compute).** Wash boundaries — for each, state whether that single condition is met: (a) volume exactly $10,000; (b) volume $10,001; (c) unique ratio exactly 0.1; (d) unique ratio 0.099; (e) holder change exactly 2.0%; (f) holder change 1.8%; (g) holder change −1.8% (a DROP).

**Q12 (MCQ).** A snapshot shows $50,000 volume, 30 unique buyers across 900 trades, holders up 0.5% since the first snapshot. Which detector fires, and which of its conditions is closest to its threshold?
A. None fires
B. `wash_skew` fires — all three of its conditions hold
C. `lp_pull` fires
D. `holder_crash` fires

**Q13 (MCQ).** `holder_crash` fires when holders drop:
A. ≥40% in total over the lifecycle
B. ≥40% between two CONSECUTIVE snapshots
C. >40% between any two snapshots
D. ≥40% in one step, or 39% twice

**Q14 (Compute).** (a) Holders 1000 → 600 consecutively: drop % and verdict? (b) 1000 → 601: drop % and verdict? (c) Holders fall 500 → 290 over five snapshots, never more than 15% in one step: does `holder_crash` fire?

**Q15 (Compute).** `DETECTOR_WEIGHTS`: lp_pull 35, mint_and_dump 30, cant_sell 25, holder_crash 20, wash_skew 15. Compute `rug_score` for: (a) only lp_pull; (b) only wash_skew; (c) lp_pull + cant_sell + holder_crash; (d) all five firing; (e) clean lifecycle.

**Q16 (Compute).** `risk_grade(score)` bands: state the grade for scores 0, 1, 24, 25, 49, 50, 100. What does `risk_grade(101)` or `risk_grade("high")` do?

**Q17 (True/False + justification).** "NaN or infinite snapshot fields trigger detectors more easily, since they are extreme values." True or false? Justify.

**Q18 (MCQ).** `run_detectors(None)` / an empty lifecycle:
A. Raises
B. Returns five `(False, reason)` entries — all five results, always
C. Returns an empty dict
D. Returns score 100

**Q19 (Short answer).** Liquidity falls 85% in twenty minutes but price only falls 20%. Does `lp_pull` fire, and which condition saves the token — and why require BOTH conditions at all?

**Q20 (MCQ).** In the demo, the healthy lifecycle and the LP-pull lifecycle respectively trigger:
A. Nothing; exactly `lp_pull`
B. Nothing; all five
C. `wash_skew`; `lp_pull`
D. Nothing; `lp_pull` + `holder_crash`

**Q21 (True/False + justification).** "Zero or missing liquidity in a pair can fire `lp_pull` if price collapses." True or false? Justify.

**Q22 (MCQ).** The detectors are best described as:
A. One combined score that fires as a whole
B. Independent and binary — each returns `(triggered, evidence)`; `rug_score` only weighs how many fired at once
C. A ranked list where the top detector suppresses the rest
D. Machine-learned classifiers

---

## ANSWER KEY

- **Q1: A** — The five named patterns.
- **Q2: B** — Conjunction over the same pair: liquidity ≥80% down AND price ≥50% down, in-window.
- **Q3: (a) −92.0%** ((100,000−8,000)/100,000). **(b) Fires** (≥80 and ≥50, in window). **(c) Does NOT fire** — 79% is below the ≥80 line. **(d) Fires** — both thresholds are inclusive (exactly −80% / −50% fires).
- **Q4: A** — ≤3600s window; ≤3-step fallback when timestamps are absent (gap 3 fires, gap 4 does not).
- **Q5: True** — The window is measured per pair (3600 fires, 3601 does not); a later pair that IS in-window can still fire even when the first pair is out of window.
- **Q6: B** — All three: active mint authority, strictly >20% inflation, strictly >50% subsequent price fall.
- **Q7: (a) Does NOT fire** — inflation rule is STRICTLY >20%, so exactly +20.0% fails it (the price condition is satisfied but the conjunction fails). **(b) Does NOT fire** — price rule is strictly >50%, so exactly 50.0% fails it. **(c) Does NOT fire** — the price drop must be at/after the inflation snapshot; a drop before it does not count.
- **Q8: C** — Only a real boolean `False` fires; `None`, `0`, `"false"`, `1` do not.
- **Q9:** It returns not-triggered (False, with evidence saying sell simulation was ok-or-unreported at every snapshot). For a detector — evidence of a pattern — absence of the field is absence of evidence, and firing on it would label every un-simulated token a can't-sell rug. (This differs from a pass/fail safety GATE, where unknown blocks; a detector that "fires" is asserting the pattern happened.)
- **Q10: B** — All three strict conditions exactly as stated.
- **Q11: (a) NOT met** (rule is > $10,000, strict). **(b) Met.** **(c) NOT met** (rule is < 0.1, strict). **(d) Met.** **(e) NOT met** (rule is < 2%, strict). **(f) Met.** **(g) Met** — drops count as flat: the test is the ABSOLUTE holder change.
- **Q12: B** — `wash_skew`: volume $50,000 > $10,000 ✓; ratio 30/900 = 0.0333 < 0.1 ✓; holder move 0.5% < 2% ✓. All three hold, so it fires. Of the three, holder-flatness is proportionally closest to its line (0.5% is 25% of the 2% limit, vs the ratio at 33% of 0.1 and volume at 500% of $10k).
- **Q13: B** — Consecutive-pair rule only; a slow bleed totalling 40%+ does not fire.
- **Q14: (a) 40.0% drop — FIRES** (≥40 inclusive). **(b) 39.9% drop — does NOT fire.** **(c) Does NOT fire** — no single consecutive step reaches 40%; this slow bleed is a different pattern (not one of these five detectors as specified).
- **Q15: (a) 35; (b) 15; (c) 80** (35+25+20); **(d) 100** (raw sum 35+30+25+20+15 = 125, capped at 100); **(e) 0**.
- **Q16:** 0 → `"clean"`; 1 and 24 → `"watch"` (band 1–24); 25 and 49 → `"elevated"` (25–49); 50 and 100 → `"severe"` (50–100). Out-of-range or non-numeric scores raise `ValueError`.
- **Q17: False** — NaN/infinite fields are treated exactly like MISSING fields by every detector: skipped, never triggering.
- **Q18: B** — Always five non-firing results, never an exception.
- **Q19:** No, it does not fire. The price-collapse condition (≥50%) saves it: an 85% liquidity fall with only a 20% price fall is consistent with e.g. a migration or partial lock move; requiring both makes the detector assert an actual pull-and-collapse, not just a liquidity change.
- **Q20: A** — Healthy: nothing; LP-pull fixture: only `lp_pull`.
- **Q21: False** — Zero/missing liquidity pairs never fire; unusable snapshots are skipped.
- **Q22: B** — Independent binary detectors; the score layer only aggregates.

---

## GRADING RUBRIC

- **MCQ (Q1, Q2, Q4, Q6, Q8, Q10, Q12, Q13, Q18, Q20, Q22):** 1 point for the correct letter only. (Q12: letter B required.)
- **True/False + justification (Q5, Q17, Q21):** 1 point only if verdict AND justification are both correct.
- **Short answer (Q9, Q19):** 1 point if the key reasoning is stated.
- **Compute-it (Q3, Q7, Q11, Q14, Q15, Q16):** 1 point per question; ALL sub-parts must be correct — strict-vs-inclusive boundary calls are the point, no tolerance.
- **Pass rule:** ≥85% → **at least 19 of 22 correct** passes.
- **On a fail:** Re-study the toolkit note/README and `ruglab.py` threshold constants, then retake the full exam.
