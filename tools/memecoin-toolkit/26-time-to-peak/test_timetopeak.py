import pytest
from timetopeak import *

def test_median_odd():
    assert median([1, 2, 3]) == 2

def test_median_even():
    assert median([1, 2, 3, 4]) == 2.5

def test_median_single():
    assert median([7.0]) == 7.0

def test_mean_basic():
    assert mean([2, 4, 6]) == pytest.approx(4.0)

def test_mean_single():
    assert mean([5.0]) == 5.0

def test_percentile_median_matches():
    assert percentile([1, 2, 3, 4, 5], 50) == pytest.approx(3.0)

def test_percentile_bounds():
    assert percentile([10, 20, 30], 0) == 10
    assert percentile([10, 20, 30], 100) == 30

def test_percentile_single_value():
    assert percentile([9.0], 90) == 9.0

def test_percentile_invalid_p():
    with pytest.raises(ValueError):
        percentile([1, 2], 101)
    with pytest.raises(ValueError):
        percentile([1, 2], -1)

def test_empty_raises():
    with pytest.raises(ValueError):
        median([])
    with pytest.raises(ValueError):
        mean([])
    with pytest.raises(ValueError):
        stats([])

def test_negative_raises():
    with pytest.raises(ValueError):
        median([-1, 2])
    with pytest.raises(ValueError):
        TaggedSamples(population=POP_PEAKED_2X, values=(-1.0,))

def test_unknown_population_raises():
    with pytest.raises(ValueError):
        TaggedSamples(population="nope", values=(1.0,))

def test_empty_tagged_raises():
    with pytest.raises(ValueError):
        TaggedSamples(population=POP_PEAKED_2X, values=())

def test_histogram_basic():
    assert histogram([1, 2, 3, 4], [0, 3, 5]) == [2, 2]

def test_histogram_last_bucket_overflow():
    assert histogram([1, 99], [0, 10, 20]) == [1, 1]

def test_histogram_invalid_edges():
    with pytest.raises(ValueError):
        histogram([1], [0])
    with pytest.raises(ValueError):
        histogram([1], [5, 5])

def test_histogram_below_edge_raises():
    with pytest.raises(ValueError):
        histogram([-1], [0, 10])

def test_stats_keys():
    s = stats([10, 20, 30, 40])
    assert s["n"] == 4 and s["median"] == 25 and s["min"] == 10 and s["max"] == 40

def test_stats_ordering():
    s = stats([1, 5, 10, 50, 100])
    assert s["p25"] <= s["median"] <= s["p75"] <= s["p90"]

def test_peaked_fixture_median_near_53():
    a = generate_peaked_2x_samples()
    assert stats_for_population(a)["median"] == pytest.approx(53, abs=12)

def test_peaked_fixture_mean_near_74():
    a = generate_peaked_2x_samples()
    m = stats_for_population(a)["mean"]
    assert 58 <= m <= 95

def test_postgrad_fixture_median_near_5():
    b = generate_post_grad_samples()
    assert stats_for_population(b)["median"] == pytest.approx(5, abs=2.5)

def test_populations_differ():
    a = generate_peaked_2x_samples(); b = generate_post_grad_samples()
    assert stats_for_population(a)["median"] > 5 * stats_for_population(b)["median"]

def test_fixture_determinism():
    assert generate_peaked_2x_samples().values == generate_peaked_2x_samples().values
    assert generate_post_grad_samples().values != generate_peaked_2x_samples().values

def test_fixture_population_tags():
    assert generate_peaked_2x_samples().population == POP_PEAKED_2X
    assert generate_post_grad_samples().population == POP_POST_GRAD

def test_mix_same_population_ok():
    a = generate_peaked_2x_samples(n=10)
    pooled = mix_populations([a, a])
    assert len(pooled) == 20

def test_mix_different_raises():
    a = generate_peaked_2x_samples(n=10); b = generate_post_grad_samples(n=10)
    with pytest.raises(MixedPopulationError):
        mix_populations([a, b])

def test_mix_override_allows():
    a = generate_peaked_2x_samples(n=10); b = generate_post_grad_samples(n=10)
    assert len(mix_populations([a, b], allow_mix=True)) == 20

def test_mix_empty_raises():
    with pytest.raises(ValueError):
        mix_populations([])

def test_mix_warning():
    a = generate_peaked_2x_samples(n=5); b = generate_post_grad_samples(n=5)
    assert mix_warning([a, b]) is not None
    assert mix_warning([a]) is None
    assert mix_warning([a, a]) is None

def test_prob_after_extremes():
    vals = [10, 20, 30]
    assert prob_peak_after(vals, 0) == 1.0
    assert prob_peak_after(vals, 100) == 0.0
    assert prob_peak_after(vals, 20) == pytest.approx(1/3)

def test_prob_after_negative_t_raises():
    with pytest.raises(ValueError):
        prob_peak_after([1, 2], -1)

def test_prob_after_monotonic():
    vals = list(generate_peaked_2x_samples().values)
    ps = [prob_peak_after(vals, t) for t in (10, 30, 60, 120, 300)]
    assert ps == sorted(ps, reverse=True)

def test_prob_24h_small_for_peaked():
    a = generate_peaked_2x_samples()
    assert prob_peak_after(list(a.values), 1440) < 0.05

def test_survival_curve():
    sc = survival_curve([10, 20, 30], [5, 15, 25])
    assert sc == [1.0, pytest.approx(2/3), pytest.approx(1/3)]

def test_survival_empty_thresholds_raises():
    with pytest.raises(ValueError):
        survival_curve([1], [])

def test_recommend_time_stop():
    vals = list(range(1, 101))
    t = recommend_time_stop(vals, max_miss_rate=0.10)
    assert prob_peak_after(vals, t) <= 0.11

def test_recommend_invalid_rate():
    with pytest.raises(ValueError):
        recommend_time_stop([1, 2], max_miss_rate=0)
    with pytest.raises(ValueError):
        recommend_time_stop([1, 2], max_miss_rate=1.0)

def test_stats_for_population_includes_tag():
    a = generate_peaked_2x_samples(n=20)
    assert stats_for_population(a)["population"] == POP_PEAKED_2X

def test_stats_for_population_wrong_type():
    with pytest.raises(ValueError):
        stats_for_population([1, 2, 3])

def test_prob_population_wrapper():
    a = generate_post_grad_samples(n=50)
    assert 0 <= prob_peak_after_population(a, 5) <= 1
