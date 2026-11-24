# Exam — Time to Peak

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR: ≥85% — at least 17 of 20 correct**
- **Conditions:** Closed-book. Synthetic data only. All answers are derivable from the toolkit's README / `timetopeak.py` alone.
- **Types:** Multiple-choice (A–D), true/false-with-justification, short-answer, and compute-it questions.

**The two populations (never pool them):** (A) `peaked_2x` — peaked≥2x paper closes, seeded fixture median ≈53.5 min / mean ≈75.7 min; (B) `post_graduation` — post-graduation peaks, median ≈4.7 min.

---

## QUESTIONS

**Q1 (MCQ).** The two population tags are:
A. `"winners"` and `"losers"`
B. `"peaked_2x"` and `"post_graduation"`
C. `"slow"` and `"fast"`
D. `"paper"` and `"live"`

**Q2 (Compute).** Compute `median` and `mean` of `[10, 20, 30, 40]`. Show how the even-count median is obtained.

**Q3 (Compute).** Compute `percentile([0, 10, 20, 30, 40], 90)` using this module's linear-interpolation method (rank = (p/100)·(n−1)). Show rank, the two bracketing values, and the result.

**Q4 (Compute).** Compute `histogram([5, 15, 25, 35, 100], [0, 10, 30, 60, 120])`. State the bucket rule, including where a value above the last edge would go.

**Q5 (MCQ).** `stats(values)` returns which key set?
A. `n, median, mean` only
B. `n, median, mean, min, max, p25, p75, p90`
C. `median, mean, variance`
D. `population, n, median`

**Q6 (True/False + justification).** True or false: `percentile(values, 101)` and `percentile(values, -1)` are clamped to 100 and 0. Justify.

**Q7 (Short answer).** List the fail-closed inputs shared by the stats functions: at least four distinct cases that raise `ValueError`.

**Q8 (MCQ).** `mix_populations` on one `peaked_2x` sample and one `post_graduation` sample, with `allow_mix` at its default, does what?
A. Pools them and returns a warning in the list
B. Raises `MixedPopulationError` (a `ValueError`)
C. Pools only the larger sample
D. Returns the peaked_2x sample alone

**Q9 (True/False + justification).** True or false: `mix_warning(tagged_list)` raises the same error as `mix_populations` when pooling would mix. Justify what it does instead.

**Q10 (Compute).** For the seeded peaked_2x fixture (demo output): state P(peak after 30 min), P(peak after 60 min), P(peak after 240 min), and P(peak after 1440 min).

**Q11 (Compute).** If P(peak after 60 min) = 0.42 for peaked_2x, what fraction (as a percentage) of eventual ≥2x winners does a 60-minute time stop sacrifice? And what fraction does a 30-minute stop sacrifice, using Q10?

**Q12 (Short answer).** Why is P(peak after 1440 min) ≈ 0 the justification *for* a 24h time stop rather than an argument for a much shorter one?

**Q13 (Compute).** Compute `prob_peak_after([10, 30, 60, 120], 30)`. State the comparison (strictly after vs at-or-after) your counting uses, as pinned by this result.

**Q14 (MCQ).** `recommend_time_stop(values, max_miss_rate=0.10)` returns:
A. The maximum observed peak time
B. The median
C. The (1 − max_miss_rate) percentile — here the 90th percentile
D. The mean plus one standard deviation

**Q15 (Compute).** For the demo's peaked_2x fixture, the recommended time stop at the default miss rate is 155.2 min. Which percentile of that fixture is 155.2 min (use the demo's stats line to cross-check)?

**Q16 (True/False + justification).** True or false: `recommend_time_stop` accepts `max_miss_rate = 0` and `= 1` as sensible extremes. Justify.

**Q17 (Short answer).** Population A has median 53.5 but mean 75.7. What does that gap say about the shape of the distribution, and which statistic should drive a time stop — the mean, the median, or a high percentile? Defend the choice in one sentence.

**Q18 (MCQ).** The seeded fixtures are deterministic in what sense?
A. Same seed reproduces the identical sample; different populations use different seeds (46 for peaked_2x, 43 for post-graduation) at n=200 by default
B. They are regenerated randomly on every call
C. Only the median is fixed
D. They are loaded from a data file

**Q19 (Compute).** Using the demo histograms with edges `[0, 10, 30, 60, 120, 240, 10000]`: give both populations' count lists, and the total in the first bucket ([0,10)) for each. What do the first-bucket totals show about the two clocks?

**Q20 (Short answer).** A new lane reports a pooled median time-to-peak of 20 minutes across all its closes. Before believing it, what two questions should you ask about how the sample was tagged? (From the README self-exam.)

---

## ANSWER KEY

**Q1 — B.** `POP_PEAKED_2X = "peaked_2x"`, `POP_POST_GRAD = "post_graduation"`.
**Q2 — Median 25.0; mean 25.0.** Working: even count — median is the average of the two middle values, (20+30)/2 = 25.0; mean = (10+20+30+40)/4 = 100/4 = 25.0.
**Q3 — 36.0.** Working: rank = 0.90×(5−1) = 3.6; bracketing sorted values are s[3]=30 and s[4]=40; interpolate 30×0.4 + 40×0.6 = 12 + 24 = **36.0**.
**Q4 — `[1, 2, 1, 1]`.** Buckets are [edge, next_edge): 5→[0,10); 15,25→[10,30); 35→[30,60); 100→[60,120). A value above the last edge overflows into the **last** bucket.
**Q5 — B.** `n, median, mean, min, max, p25, p75, p90`.
**Q6 — False.** `p` must be in [0, 100]; out-of-range percentiles raise `ValueError` — nothing is clamped (fail-closed).
**Q7 —** Any four: empty sample; a negative time; `None` input/values; an out-of-range percentile; non-increasing bucket edges (also: fewer than 2 bucket edges; an unknown population tag / empty values on `TaggedSamples`; a non-`TaggedSamples` argument to `stats_for_population`).
**Q8 — B.** Pooling two different populations without `allow_mix=True` raises `MixedPopulationError`. With `allow_mix=True` it pools — the override must be explicit.
**Q9 — False.** `mix_warning` does not raise; it *returns a warning string* when pooling would mix, and `None` otherwise — the non-raising companion to the guard.
**Q10 — 30 min: 0.750; 60 min: 0.420; 240 min: 0.040; 1440 min: 0.000.** (Demo output, peaked_2x, n=200; the <5% tail past 24h is also test-pinned.)
**Q11 — 60-min stop sacrifices 42%** of eventual winners (exactly the share peaking after 60). **30-min stop sacrifices 75%.**
**Q12 —** Because essentially nothing is still coming after 24h for the slow population, a 24h stop costs almost nothing there (misses ≈0% of eventual winners) while still bounding the position — whereas the same arithmetic shows a 30- or 60-minute stop would cut away 42–75% of winners. The ≈0 tail justifies keeping the generous ceiling, not shortening it.
**Q13 — 0.5.** Values strictly after 30 are 60 and 120: 2/4 = **0.50**. The value equal to 30 is not counted — the comparison is strictly `> t`.
**Q14 — C.** It returns the (1 − max_miss_rate) percentile — the smallest stop that misses at most that share of peaks; at 0.10 that is the 90th percentile.
**Q15 — The 90th percentile (p90).** Cross-check: the demo stats line gives `p90 = 155.2 min` for population A, identical to the recommended stop at the default 10% miss rate.
**Q16 — False.** It raises `ValueError` unless `0 < max_miss_rate < 1`; both 0 and 1 are rejected (0 would demand missing nothing — the max, not a percentile rule; 1 would miss everything).
**Q17 —** Mean > median indicates a right-skewed / long-tailed distribution (a minority of very late peaks drags the mean up). A **high percentile** (via the miss-rate rule) should drive the stop, because a stop is a tail guarantee — the share of winners you are willing to miss — not a central-tendency statement; neither the median (would miss ~50%) nor the mean encodes that.
**Q18 — A.** Same seed → identical sample; defaults are seed 46 / n=200 for peaked_2x and seed 43 / n=200 for post-graduation, landing at median 53.5 / mean 75.7 and median 4.7 respectively, differing by >5×.
**Q19 — A (peaked_2x): `[8, 42, 66, 54, 22, 8]`; B (post_graduation): `[172, 28, 0, 0, 0, 0]`.** First bucket [0,10): A has **8**, B has **172**. Post-graduation peaks are almost all inside 10 minutes, while peaked_2x closes barely begin there — two different clocks, which is why pooling them describes neither.
**Q20 —** (1) Which populations were pooled / were the closes tagged by population at all (peaked≥2x closes vs post-graduation peaks vs everything)? (2) Was the sample restricted to the population the stop will actually govern (e.g. only eventual ≥2x winners), or does the 20-minute figure mix populations / include non-winners — i.e. is it a pooled median that describes neither population?

---

## GRADING RUBRIC

- **MCQ (Q1, Q5, Q8, Q14, Q18):** 1 point for the correct letter only.
- **True/False + justification (Q6, Q9, Q16):** 1 point only if verdict and justification both match the key.
- **Short-answer (Q7, Q12, Q17, Q20):** 1 point if every element in the key is present and correct.
- **Compute-it (Q2, Q3, Q4, Q10, Q11, Q13, Q15, Q19):** 1 point for the exact numbers in the key with working where asked; Q3 and Q4 require the stated method, not just the result.
- **Pass rule:** Pass = **≥17/20 (≥85%)**. Below 17 is a fail.
- **What a fail means:** Re-study the toolkit README (and `timetopeak.py` if needed), then retake the full exam.
