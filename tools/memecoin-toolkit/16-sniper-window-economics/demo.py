"""Scenario walkthrough for the sniper-window lab. Synthetic only."""
from sniperwindow import (
    break_even_delay, copy_window_expectancy, entry_multiple,
    expectancy_table, fraction_of_move_remaining, late_entry_return_pct,
    net_return_at_peak_pct,
)


def main() -> None:
    print("=== Sniper-window economics lab (synthetic launches) ===")
    print("Model: 10x peak over a 120s geometric rise, 180s decay half-life,")
    print("4% round-trip cost. Peak exits are granted generously where used.")

    print("\n-- What your delay costs you (best case: you exit at the peak) --")
    for delay in (1.0, 2.0, 15.0, 30.0, 60.0, 120.0):
        print(f"delay {delay:6.1f}s  entry {entry_multiple(delay):5.2f}x  "
              f"move left {fraction_of_move_remaining(delay) * 100:5.1f}%  "
              f"net at peak {net_return_at_peak_pct(delay):+7.1f}%")

    print("\n-- The copy window (enter late, hold 60s) --")
    for delay in (120.0, 300.0, 600.0):
        print(f"delay {delay:6.1f}s  net {late_entry_return_pct(delay):+6.1f}% "
              "(price is decaying the whole hold)")

    print("\n-- Seeded expectancy over synthetic launches (400 per delay) --")
    for row in expectancy_table():
        print(f"delay {row.delay_seconds:6.1f}s  [{row.bucket:<19}] "
              f"mean net {row.mean_net_return_pct:+8.1f}%  "
              f"win rate {row.win_rate_pct:5.1f}%")
    print(f"Copy-window (2-10 min) mean expectancy: {copy_window_expectancy():+.1f}%")

    print("\n-- Break-even solver --")
    print(f"Latest best-case break-even entry: {break_even_delay():.1f}s "
          "(and that still assumes you sell the exact peak)")


if __name__ == "__main__":
    main()
