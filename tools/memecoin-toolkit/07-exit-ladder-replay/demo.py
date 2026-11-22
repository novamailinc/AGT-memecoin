"""Demo: replay the four synthetic paths under all three ladders."""
from replay import DEMO_PATHS, LADDERS, peaked_ge, replay, replay_batch, time_to_peak


def main() -> None:
    print("Exit Ladder Replay — synthetic paths, p0 = 1.0, no look-ahead")
    for name, path in DEMO_PATHS.items():
        print(f"\n{name}: peak {max(p for _, p in path):.2f}x "
              f"at t={time_to_peak(path):.0f} min, peaked>=2x: {peaked_ge(path, 2.0)}")
        for ladder in LADDERS:
            r = replay(path, ladder)
            legs = ", ".join(
                f"{leg.fraction:.0%}@{leg.exit_price:.2f}x ({leg.reason})"
                for leg in r.legs)
            print(f"  {ladder:<13} realized {r.realized_mult:.3f}x  legs: {legs}")
    print("\nBatch over all 4 paths (user_ladder):")
    stats = replay_batch(list(DEMO_PATHS.values()), "user_ladder")
    for key, val in stats.items():
        print(f"  {key}: {val}")
    leak = DEMO_PATHS["round_tripper"]
    print(f"\nLeak check: round-tripper user_ladder "
          f"{replay(leak, 'user_ladder').realized_mult:.3f}x vs hold_to_stop "
          f"{replay(leak, 'hold_to_stop').realized_mult:.3f}x")


if __name__ == "__main__":
    main()
