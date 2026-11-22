# Time to Peak (lab)

A from-scratch, pure-stdlib lab for the distribution of *when* a winning token peaks — measured in minutes from entry. Everything here is synthetic — no network, no live bot code, no real token data. Its central discipline is that there are two different populations that must never be conflated: (a) peaked≥2x paper closes, which peak slowly (median ~53 min, mean ~74 min), and (b) post-graduation peaks, which peak almost immediately (median ~5 min). Pooling them produces a median that describes neither.

## What it does

Module `timetopeak.py` — population tags `POP_PEAKED_2X = "peaked_2x"` and `POP_POST_GRAD = "post_graduation"`, a frozen `TaggedSamples(population, values)` dataclass, and plain functions over minute lists:

- `median(values) -> float` / `mean(values) -> float` / `percentile(values, p) -> float` (linear interpolation, `p` in `[0, 100]`) / `histogram(values, bucket_edges) -> list[int]` (counts per `[edge, next_edge)` bucket, overflow into the last bucket) / `stats(values) -> dict` with `n, median, mean, min, max, p25, p75, p90`. Fail-closed: every function raises `ValueError` on an empty sample, a negative time, `None`, or an out-of-range percentile / non-increasing bucket edges; `TaggedSamples` raises on an unknown population tag or empty values.
- `stats_for_population(tagged) -> dict` — `stats()` plus the population tag. Raises `ValueError` on anything that is not a `TaggedSamples`.
- `mix_populations(tagged_list, allow_mix=False) -> list[float]` — pools tagged samples. Fail-closed: pooling two different populations without `allow_mix=True` raises `MixedPopulationError` (a `ValueError`); `mix_warning(tagged_list) -> str | None` returns a warning string instead when pooling would mix, `None` otherwise.
- `prob_peak_after(values, t_minutes) -> float` — empirical P(peak occurs after T), for choosing a time stop; `prob_peak_after_population(tagged, t)` and `survival_curve(values, thresholds) -> list[float]` are the tagged / vector forms. Raises `ValueError` on negative T or empty inputs.
- `recommend_time_stop(values, max_miss_rate=0.10) -> float` — the `(1 − max_miss_rate)` percentile: the smallest stop that misses at most that share of peaks. Raises `ValueError` unless `0 < max_miss_rate < 1`.
- Fixtures: `generate_peaked_2x_samples(seed=46, n=200)` — seeded lognormal landing at median 53.5 min / mean 75.7 min; `generate_post_grad_samples(seed=43, n=200)` — median 4.7 min. Same seed reproduces the identical sample.

`demo.py` prints both populations' stats, histograms, survival probabilities at 30/60/240/1440 min, a recommended time stop, and the mixing guard firing.

## Test suite

41 tests, all passing (`pytest -q`: "41 passed"). Covered: median/mean/percentile hand values (odd, even, single-value, 0th/100th percentile); histogram bucketing including last-bucket overflow and invalid edges; stats key set and percentile ordering; fail-closed `ValueError` on empty, negative, `None`, unknown-population, and bad-percentile inputs; the seeded fixtures landing near their stated medians/means (peaked≥2x median ≈53, mean in 58–95; post-graduation median ≈5) and differing from each other by >5×; fixture determinism and tags; the mixing guard (same-population pooling OK, cross-population raises, `allow_mix=True` pools, warning helper agrees); `prob_peak_after` extremes, monotonicity, the <5% tail past 24h, and the survival curve; and the time-stop recommender honouring its miss-rate bound plus rejecting rates of 0 and 1.

## Captured output

Verbatim output of `python demo.py`:

```
=== Time-to-peak lab (synthetic, two populations) ===
Population A (peaked_2x): n=200 median=53.5 min mean=75.7 min p90=155.2 min
Population B (post_graduation): n=200 median=4.7 min mean=6.0 min p90=11.8 min
Histogram A (edges [0, 10, 30, 60, 120, 240, 10000]): [8, 42, 66, 54, 22, 8]
Histogram B (edges [0, 10, 30, 60, 120, 240, 10000]): [172, 28, 0, 0, 0, 0]
P(peak after   30 min | peaked>=2x) = 0.750
P(peak after   60 min | peaked>=2x) = 0.420
P(peak after  240 min | peaked>=2x) = 0.040
P(peak after 1440 min | peaked>=2x) = 0.000
Recommended time stop (miss<=10%, pop A): 155.2 min

-- Fail-closed: mixing populations --
Mixed pooling correctly rejected: refusing to pool populations ['peaked_2x', 'post_graduation'] without allow_mix=True; peaked>=2x closes and post-graduation peaks are different distributions
```

## What this mirrors in the live bot

This mirrors the live bot's exit-replay findings and its 24h time-stop reasoning: replaying closed paper trades showed that trades which eventually peaked at ≥2x took a median of ~53 minutes (mean ~74) to get there, while post-graduation peaks arrive in ~5 minutes — two different clocks. A time stop tuned on the fast population would amputate the slow one, and the pooled median would justify a stop that fits neither. The lab's `prob_peak_after` is the arithmetic behind keeping a generous 24h ceiling for the slow population: on the peaked≥2x distribution essentially nothing is still coming after 24h, so the stop costs almost nothing there, while a 30- or 60-minute stop would cut away 42–75% of eventual winners. It is a teaching/checking model only; it shares no code with the live bot.

## Self-exam

1. Why does pooling the peaked≥2x closes with the post-graduation peaks produce a median that describes neither population, and what does the guard do about it?
2. The peaked≥2x fixture has median 53.5 but mean 75.7. What does that gap tell you about the shape of the distribution, and which number should drive a time stop?
3. If P(peak after 60 min) is 0.42 for the peaked≥2x population, what fraction of eventual ≥2x winners does a 60-minute time stop sacrifice?
4. Why is P(peak after 1440 min) ≈ 0 the justification for a 24h time stop rather than an argument for a much shorter one?
5. `recommend_time_stop` returns a percentile, not the maximum observed peak time. Why is the max the wrong choice?
6. A new lane reports a median time-to-peak of 20 minutes pooled across all its closes. Before believing it, what two questions should you ask about how the sample was tagged?
