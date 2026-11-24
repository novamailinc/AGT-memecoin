"""Demo walkthrough for autopsy.py -- run: python demo.py"""

from autopsy import classify
from fixtures import ALL_CASES


def main() -> None:
    print("=== Funnel autopsy demo (synthetic cases, paper only) ===")
    print("bar: liq >= $25,000 and mcap >= $50,000 and momentum positive")
    print("precedence: never-met-bar > rug > ceiling > burned > safety-lag\n")
    for case in ALL_CASES:
        verdict = classify(case)
        print(f"{case['name']:<26} outcome={case['outcome']:<7} -> {verdict}")
    print("\nReading the two headline shapes:")
    print("- DACITY-like: convergence blocked once at t=1min, bar met for")
    print("  ~35 minutes afterwards, safety later known-ok, never retried.")
    print("- Doomsday-like: $96k liq / $1.14M mcap / safety ok, blocked only")
    print("  by a gate-log reason citing a 300% gain ceiling.")
    print("\nDemo complete.")


if __name__ == "__main__":
    main()
