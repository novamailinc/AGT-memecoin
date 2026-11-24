"""Scenario walkthrough for the copy-trading economics lab. Synthetic only."""
from copyecon import expectancy_by_lag, is_copying_viable, lag_penalty


def main() -> None:
    print("=== Copy-trading economics lab (seeded synthetic paths) ===")
    print("Leader buys at t0; followers copy at a lag, paying extra slippage.")
    print("Exit ladder: 50% at 2x, rest on a 30% trail, -50% hard stop.")

    seeds = range(200)
    table = expectancy_by_lag(seeds)
    print(f"\n-- Expectancy per bucket across {len(list(seeds))} seeded paths --")
    print(f"leader: {table['leader']:+.1%}")
    for label in ("15s", "1m", "5m", "15m"):
        print(f"copy at {label:>4}: {table[label]:+.1%}  "
              f"(viable: {is_copying_viable(seeds, label)})")

    print("\n-- Penalty vs the leader --")
    for label, penalty in lag_penalty(seeds).items():
        print(f"copy at {label:>4}: {penalty:+.1%} vs leader")

    print("\nDelayed copying is negative at every lag and worsens with lag.")
    print("The information is stale and the price has already moved.")


if __name__ == "__main__":
    main()
