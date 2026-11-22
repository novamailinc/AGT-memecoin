"""Demo: user-holder concentration vs the naive (wrong) reading."""
from holders import analyze

SUPPLY = 1_000_000_000

REGISTRY = {
    "Pool111": {"owner_program": True, "on_curve": False},
    "Curve111": {"owner_program": True, "on_curve": False},
    "Whale111": {"owner_program": False, "on_curve": True},
    "UserA111": {"owner_program": False, "on_curve": True},
    "UserB111": {"owner_program": False, "on_curve": True},
    "UserC111": {"owner_program": False, "on_curve": True},
}


def show(title, holders):
    result = analyze(holders, SUPPLY, REGISTRY)
    print(f"{title}")
    if holders:
        naive_top1 = max(h["amount"] for h in holders) / SUPPLY * 100
        print(f"  naive top-1 (pool included): {naive_top1:.1f}% of supply")
    else:
        print("  naive top-1 (pool included): n/a (no holders)")
    if result.top1_user_pct is None:
        print(f"  user top-1: n/a | top-10 users: n/a | verdict: {result.verdict}")
    else:
        print(f"  user top-1: {result.top1_user_pct:.1f}% | "
              f"top-10 users: {result.top10_user_pct:.1f}% | "
              f"verdict: {result.verdict} (passed={result.passed})")
    print(f"  note: {result.reason}; excluded={result.excluded_count}, "
          f"unknown={result.unknown_count}")
    print()


def main():
    print("Holder concentration lab -- pools are not whales")
    print("=" * 52)
    show("Scenario 1: big pool, spread-out users", [
        {"address": "Pool111", "amount": 400_000_000},
        {"address": "Whale111", "amount": 60_000_000},
        {"address": "UserA111", "amount": 30_000_000},
        {"address": "UserB111", "amount": 20_000_000},
        {"address": "UserC111", "amount": 10_000_000},
    ])
    show("Scenario 2: one user wallet quietly holds the majority", [
        {"address": "Pool111", "amount": 100_000_000},
        {"address": "Whale111", "amount": 520_000_000},
        {"address": "UserA111", "amount": 30_000_000},
        {"address": "UserB111", "amount": 20_000_000},
    ])
    show("Scenario 3: no data at all (fail-closed)", [])


if __name__ == "__main__":
    main()
