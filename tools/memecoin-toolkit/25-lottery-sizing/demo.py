"""Scenario walkthrough for the lottery-sizing lab. Synthetic only."""
from sizing import (expectancy, expected_log_growth, growth_comparison,
                    kelly_fraction, prob_of_loss, ruin_probability)

LOTTERY = [(0.90, 0.0), (0.10, 15.0)]  # 90% total loss, 10% a 15x


def main() -> None:
    print("=== Lottery sizing lab (memecoin outcome distributions) ===")
    print("Distribution: 90% -> 0x (total loss), 10% -> 15x")
    print(f"Expectancy per $1 shot: {expectancy(LOTTERY):+.2f} "
          f"(probability of loss: {prob_of_loss(LOTTERY):.0%})")

    print("\n-- Kelly sizing --")
    kelly = kelly_fraction(LOTTERY)
    print(f"Full-Kelly fraction of bankroll: {kelly:.4f} "
          f"(${kelly * 100:.2f} per shot on a $100 bankroll)")
    print("The CEO discipline bets $1 on $100 = a 0.0100 flat fraction,")
    print("deliberately below Kelly: smaller growth, far smaller swings.")

    print("\n-- Expected log growth: flat vs Kelly vs 2x Kelly --")
    comparison = growth_comparison(LOTTERY)
    for name in ("flat", "kelly", "double_kelly"):
        fraction, growth = comparison[name]
        print(f"{name:<13} f={fraction:.4f}  E[log growth]/bet = {growth:+.6f}")
    print("2x Kelly grows SLOWER than Kelly despite the same +50% expectancy.")

    print("\n-- Overbetting far past Kelly turns growth negative --")
    print(f"f=0.50: E[log growth]/bet = "
          f"{expected_log_growth(0.50, LOTTERY):+.6f} "
          f"(expectancy is still {expectancy(LOTTERY):+.2f} per $1)")

    print("\n-- Seeded Monte Carlo ruin (flat $1 shots, $100 bankroll) --")
    ruin = ruin_probability(LOTTERY, bankroll=100.0, bet=1.0,
                            n_bets=200, n_sims=200, seed=42)
    print(f"Ruin probability over 200 shots (seed 42): {ruin:.2f}")
    thin = ruin_probability(LOTTERY, bankroll=10.0, bet=1.0,
                            n_bets=200, n_sims=200, seed=42)
    print(f"Same shots on a $10 bankroll (seed 42):    {thin:.2f}")

    print("\n-- Fail-closed --")
    try:
        expectancy([(0.5, 0.0), (0.4, 15.0)])
    except ValueError as exc:
        print(f"Probabilities summing to 0.9 correctly rejected: {exc}")


if __name__ == "__main__":
    main()
