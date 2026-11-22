"""Demo: freeze a gate, walk forward, measure a plateau, deflate, peek."""
from rigor import (PreRegistration, deflated_expectancy, lookahead_experiment,
                   parameter_plateau, walk_forward)


def main() -> None:
    print("Backtesting Rigor — five defences against fooling yourself")
    reg = PreRegistration(
        gate="momentum lane: WR >= 35% and expectancy > 0 over 200 closes",
        thresholds={"min_wr": 0.35, "min_expectancy": 0.0, "min_closes": 200},
        registered_at="2026-10-20")
    print(f"\nregistered: {reg.gate} at {reg.registered_at}, frozen={reg.frozen()}")
    reg.freeze()
    print(f"after freeze(): frozen={reg.frozen()}, certify()={reg.certify()}")
    try:
        reg.set_threshold("min_wr", 0.20)
    except RuntimeError as exc:
        print(f"  post-freeze tweak refused: {exc}")

    data = list(range(120))
    print("\nwalk-forward over 120 bars, 3 folds, embargo=5:")
    for train, test in walk_forward(data, n_folds=3, embargo=5):
        print(f"  train 0..{train[-1]} ({len(train)} bars), "
              f"test {test[0]}..{test[-1]} ({len(test)} bars), "
              f"gap {test[0] - train[-1] - 1} embargoed")

    plateau = {10: 0.02, 20: 0.10, 30: 0.14, 40: 0.15, 50: 0.13, 60: 0.09, 70: 0.01}
    spike = {10: 0.01, 20: 0.02, 30: 0.15, 40: 0.02, 50: 0.01}
    print(f"\nparameter plateau width: plateau-shaped {parameter_plateau(plateau)}, "
          f"spike-shaped {parameter_plateau(spike)} (1 = fragile)")

    print("\ndeflated expectancy for an observed +0.30 (100 samples):")
    for trials in (1, 10, 100, 1000):
        print(f"  after {trials:>4} trials tried: {deflated_expectancy(0.30, trials, 100):+.3f}")

    cheat, honest = lookahead_experiment(seed=42)
    print(f"\nlook-ahead injector (pure noise, seed 42):")
    print(f"  cheating signal (next bar) expectancy: {cheat:+.3f}")
    print(f"  honest signal (current bar) expectancy: {honest:+.3f}")
    print(f"  fabricated edge: {cheat - honest:+.3f} expectancy points from nothing")


if __name__ == "__main__":
    main()
