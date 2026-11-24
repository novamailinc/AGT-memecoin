"""Demo: expectancy, the p-hacking trap, power, and base rates."""
from statslab import (BASE_RATES, achieved_power, breakeven_wr, expectancy,
                      required_n, simulate_phacking)


def main() -> None:
    print("Statistical Validity Lab — seeded, deterministic, pure noise is labelled")
    sample = [2.5, 0.0, 1.2, 0.4, 3.0, 0.0, 0.8]
    print(f"\nexpectancy({sample}) = {expectancy(sample):+.3f} per $1 staked")
    print(f"breakeven WR at 2x payoff: {breakeven_wr(2.0):.2f}, "
          f"at 4x: {breakeven_wr(4.0):.2f}, at 10x: {breakeven_wr(10.0):.2f}")

    hack = simulate_phacking(seed=42)
    print(f"\np-hacking demo: {hack['n_strategies']} pure-noise strategies, "
          f"true WR {hack['true_wr']:.0%}")
    print(f"  mean in-sample WR : {hack['mean_in_sample_wr']:.3f}")
    print(f"  best in-sample WR : {hack['best_in_sample_wr']:.3f} "
          f"(strategy #{hack['best_index']}) <- looks like a discovery")
    print(f"  same strategy OOS : {hack['best_out_of_sample_wr']:.3f} "
          f"<- regresses to the true rate")
    print(f"  in-sample inflation: {hack['inflation']:+.3f}")

    n = required_n(0.35, 0.30)
    print(f"\npower: closes needed to tell 35% WR from 30% WR "
          f"(alpha 0.05 one-sided, power 0.8): {n}")
    print(f"  achieved power at 100 closes: {achieved_power(100, 0.30, 0.35):.3f}")
    print(f"  achieved power at {n} closes: {achieved_power(n, 0.30, 0.35):.3f}")

    print("\nbase rates a claim must survive:")
    for key, val in BASE_RATES.items():
        print(f"  {key}: {val}")


if __name__ == "__main__":
    main()
