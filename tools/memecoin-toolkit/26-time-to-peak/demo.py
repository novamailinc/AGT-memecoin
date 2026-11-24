"""Demo for time-to-peak lab. Synthetic only."""
from timetopeak import (
    POP_PEAKED_2X, POP_POST_GRAD, generate_peaked_2x_samples,
    generate_post_grad_samples, stats_for_population, histogram,
    prob_peak_after, mix_populations, MixedPopulationError, recommend_time_stop,
)


def main() -> None:
    print("=== Time-to-peak lab (synthetic, two populations) ===")
    a = generate_peaked_2x_samples()
    b = generate_post_grad_samples()
    sa = stats_for_population(a)
    sb = stats_for_population(b)
    print(f"Population A ({POP_PEAKED_2X}): n={sa['n']} median={sa['median']:.1f} min mean={sa['mean']:.1f} min p90={sa['p90']:.1f} min")
    print(f"Population B ({POP_POST_GRAD}): n={sb['n']} median={sb['median']:.1f} min mean={sb['mean']:.1f} min p90={sb['p90']:.1f} min")
    edges = [0, 10, 30, 60, 120, 240, 10000]
    print(f"Histogram A (edges {edges}): {histogram(list(a.values), edges)}")
    print(f"Histogram B (edges {edges}): {histogram(list(b.values), edges)}")
    for t in (30, 60, 240, 1440):
        print(f"P(peak after {t:4d} min | peaked>=2x) = {prob_peak_after(list(a.values), t):.3f}")
    print(f"Recommended time stop (miss<=10%, pop A): {recommend_time_stop(list(a.values)):.1f} min")
    print("\n-- Fail-closed: mixing populations --")
    try:
        mix_populations([a, b])
    except MixedPopulationError as exc:
        print(f"Mixed pooling correctly rejected: {exc}")


if __name__ == "__main__":
    main()
