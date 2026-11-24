# Time to Peak — study notes

## Core ideas (teach-back)

"How long does a winner take to peak?" sounds like one question. This lab's whole point is that it is
two.

There are two populations of peak times, in minutes from entry, and they run on different clocks:

- **(a) `peaked_2x`** — paper closes that eventually peaked at ≥2x. They peak *slowly*: median ~53 min,
  mean ~74 min (see the flag below on the fixture's exact mean).
- **(b) `post_graduation`** — peaks
  right after graduation. They peak almost *immediately*: median ~5 min.

Pool the two and the pooled median describes neither. It lands somewhere in the middle — too slow for
the fast population, lethally fast for the slow one. Any decision built on it, above all a *time stop*,
inherits the same defect: a stop tuned on the fast clock amputates the slow population's winners before
they arrive.

So the lab makes population a *type*, not a comment. Samples travel as `TaggedSamples(population,
values)`, a frozen dataclass that refuses unknown population tags and empty values. Pooling is a named
operation, `mix_populations(...)`, and pooling two different populations without an explicit
`allow_mix=True` raises `MixedPopulationError` (a `ValueError`). `mix_warning(...)` returns the warning
string instead when pooling would mix, and `None` otherwise — the same check in advisory form.

On top of tagged samples sit the ordinary statistics:

- `median`, `mean`
- `percentile(values, p)` — linear interpolation, `p` in `[0, 100]`
- `histogram(values, bucket_edges)` — counts per `[edge, next_edge)` bucket, with values above the last
  edge overflowing into the last bucket
- `stats(values)` — `n, median, mean, min, max, p25, p75, p90`

And the two decision-shaped tools:

- `prob_peak_after(values, t)` — the empirical probability that the peak arrives *after* T minutes. This
  is the survival function of the sample, and it is exactly the number a time stop trades against: a
  stop at T sacrifices (roughly) this share of eventual winners.
- `recommend_time_stop(values,
  max_miss_rate=0.10)` — the smallest stop that misses at most the chosen share of peaks.

Fail-closed throughout: empty samples, negative times, `None`, out-of-range percentiles, and
non-increasing bucket edges all raise `ValueError`.

## Key derivations & formulas

### Percentile by linear interpolation

For a sorted sample `s` of size `n`:

```text
rank = (p / 100) * (n − 1)
lo = floor(rank),  hi = ceil(rank),  frac = rank − lo
percentile = s[lo] * (1 − frac) + s[hi] * frac
```

A single-value sample returns that value. The 0th percentile is the minimum, the 100th the maximum.

### Median as a special case

```text
n odd:   median = s[n // 2]
n even:  median = (s[n//2 − 1] + s[n//2]) / 2
```

### Survival / peak-after probability

```text
prob_peak_after(values, T) = #{v in values : v > T} / n
```

`survival_curve(values, thresholds)` is the vector form. It is non-increasing in T by construction — a
test pins that monotonicity, because a "survival curve" that rises would expose a counting bug.

### Time-stop recommendation

To miss at most `max_miss_rate` of peaks, choose the quantile that leaves exactly that share above it:

```text
recommend_time_stop(values, max_miss_rate)
    = percentile(values, (1 − max_miss_rate) * 100)
```

With the default `max_miss_rate = 0.10`, that is the 90th percentile. `max_miss_rate` must lie strictly
in `(0, 1)` — 0 would demand the sample maximum (or beyond), 1 would permit missing everything.

Why a percentile and not the observed maximum? The max is a single-sample extreme: it grows with sample
size, is dominated by the one strangest trade, and would set a stop so generous it stops nothing. The
p90 prices the trade-off explicitly: accept missing ~10% of peaks in exchange for a stop that actually
binds.

### The fixtures are lognormal — and that shape is the lesson

Both generators draw from a lognormal distribution:

```text
peaked_2x:  mu = ln(53), sigma = 0.82
post_grad:  mu = ln(5),  sigma = 0.70
```

For a lognormal, `median = exp(mu)` and `mean = median * exp(sigma^2 / 2)`, so the mean *sits above* the
median by a factor of about `exp(0.82^2 / 2) ≈ 1.40` for population A. The median–mean gap is not noise;
it is the signature of a right-skewed distribution with a long slow tail — most winners peak around an
hour, a few take many hours. Floors keep samples sane: `max(0.5, ...)` minutes for A, `max(0.1, ...)`
for B. Same seed, identical sample: A uses seed 46, B seed 43, each `n = 200` by default.

## Worked example

From `demo.py`, both seeded fixtures at `n = 200`:

```text
Population A (peaked_2x):       median 53.5 min, mean 75.7 min, p90 155.2 min
Population B (post_graduation): median  4.7 min, mean  6.0 min, p90  11.8 min
```

The medians differ by more than 11x (tests only require >5x separation — the fixtures clear it
comfortably).

Histograms on edges `[0, 10, 30, 60, 120, 240, 10000]`:

```text
A: [8, 42, 66, 54, 22, 8]     — spread across hours
B: [172, 28, 0, 0, 0, 0]      — 172 of 200 peak inside 10 minutes
```

Survival probabilities for population A:

```text
P(peak after   30 min) = 0.750
P(peak after   60 min) = 0.420
P(peak after  240 min) = 0.040
P(peak after 1440 min) = 0.000
```

Read as time-stop costs, for eventual ≥2x winners:

- a 30-minute stop sacrifices ~75% of them;
- a 60-minute stop sacrifices ~42%;
- a 24h (1440 min) stop
  sacrifices essentially none.

`recommend_time_stop` at the default 10% miss rate returns **155.2 min** — population A's p90.

And the guard, firing: `mix_populations([a, b])` raises `MixedPopulationError`, naming both populations
and refusing the pool without `allow_mix=True`.

## Trap log

- T1. **The pooled median.** Averaging a 5-minute clock and a 53-minute clock yields a number faithful
  to neither — and a time stop built on it that harms the slow population most.
- T2. **Letting the fast
  population set the stop.** Post-graduation peaks are the most visible and the least representative of
  held winners. A 30–60 min stop feels "responsive" and cuts 42–75% of eventual ≥2x winners.
- T3. **Reading ≈0 at 24h backwards.** `P(peak after 1440) ≈ 0` justifies *keeping* a 24h stop because it
  costs almost nothing on the slow population — not shortening the stop, which is where the cost
  explodes.
- T4. **Using the maximum as the stop.** One extreme sample would set a stop that never
  binds. A percentile prices the miss rate; the max hides it.
- T5. **Using the mean as the typical
  time.** Median 53.5 vs mean 75.7 is skew talking. Quoting the mean as "typical peak time" overstates
  the wait for the median trade and understates the tail.
- T6. **Untagged samples.** A reported "median
  time-to-peak of 20 minutes" is uninterpretable until you ask which populations were pooled and in what
  proportions. The tag is part of the data.
- T7. **Silent mixing via a plain list.** Once samples are
  pooled into a bare `list[float]`, the population information is gone and no later check can recover
  it. Hence mixing is an explicit, overridable operation, never a default.
- T8. **`allow_mix=True` as a
  rubber stamp.** The override exists for deliberate analysis, not convenience. Passing it without
  stating the mixture proportions repeats T1 with extra steps.

## Source flags

- Population definitions, tags, and all function semantics are from `timetopeak.py`; the fixture
  statistics (A: median 53.5, mean 75.7, p90 155.2; B: median 4.7, mean 6.0, p90 11.8) are the README's
  captured demo output, reproduced in its test expectations (A's mean accepted in 58–95). [S]
- Minor
  internal wobble, reported not fixed: the README's opening line and the module docstring describe
  population A's mean as "~74 min", while the seeded fixture's actual mean is 75.7 min. "~74" reads as
  an approximation of the replay finding the lab mirrors, not of the fixture itself — the numbers are
  close, but a reader checking one against the other will find a ~1.7-minute gap. [S]
- The lognormal
  mean/median factor `exp(sigma^2/2)` is standard lognormal algebra, verified here against the source's
  `mu`/`sigma` choices. [R]
- That these two clocks mirror the live bot's exit-replay findings and its
  24h time-stop reasoning is the README's mirroring account. [M]
- Coverage (41 tests passing) is as
  reported in the README. [S]

## One-paragraph verdict

This toolkit proves a discipline, backed by working code: time-to-peak is a per-population quantity, the two synthetic populations are separated by more than an order of magnitude, survival probabilities convert directly into time-stop costs (42–75% of slow winners sacrificed by a 30–60-minute stop, ~0% by a 24h stop), and a typed mixing guard makes the classic pooling error raise instead of publish. It does not prove that real peak times are lognormal with these parameters, or that the live replay values are correct — the fixtures are seeded stand-ins chosen to land near them — so the transferable result is the guard and the survival arithmetic, not the specific minutes.
