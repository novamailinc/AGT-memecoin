"""Demo walkthrough for grading.py -- run: python demo.py"""

from grading import (
    convergence_count,
    follow,
    grade_wallet,
    simulate_follows,
    wallet_stats,
)

PATH = [(0, 1.00), (1, 1.20), (2, 1.60), (3, 2.10), (4, 1.40), (5, 0.40)]
EVENT = {"wallet": "W-ALPHA", "mint": "SYNTH", "t": 0, "price": 1.00}


def main() -> None:
    print("=== Smart-wallet grading demo (synthetic, paper only) ===")
    print("\n-- One smart buy, three copier lags, path runs then dies --")
    print("path: 1.00 -> 1.20 -> 1.60 -> 2.10 -> 1.40 -> 0.40 (exit)")
    for lag in (0.15, 0.50, 1.00):
        result = follow(EVENT, PATH, lag)
        print(
            f"lag +{lag:.0%}: entry ${result['entry_price']:.2f} "
            f"at t={result['entry_t']} -> realized {result['realized_mult']:.3f}x"
        )

    print("\n-- A signal that never reaches the +15% follow level --")
    flat = [(0, 1.00), (1, 1.05), (2, 1.08)]
    skipped = follow(EVENT, flat, 0.15)
    print(f"follow result: {skipped} (skipped, not chased)")

    print("\n-- Pack independence: 4 wallets, 2 funders --")
    wallets = {"A": "FUNDER-1", "B": "FUNDER-1", "C": "FUNDER-1", "D": "FUNDER-2"}
    print(f"wallets={len(wallets)}, independent actors={convergence_count(wallets)}")

    print("\n-- Grades: shrinkage weight and auto-mute --")
    results = simulate_follows([EVENT], {"SYNTH": PATH}, 0.15)
    for wallet, (n, avg) in wallet_stats(results).items():
        print(f"{wallet}: n={n}, avg realized {avg:+.1%} from this one follow")
    for label, n, avg in (
        ("new wallet, 2 lucky follows", 2, 0.90),
        ("proven wallet, 50 follows", 50, 0.90),
        ("copier trap, 12 follows", 12, -0.30),
        ("small sample, 9 bad follows", 9, -0.50),
    ):
        grade = grade_wallet(n, avg)
        print(
            f"{label}: weight={grade['weight']:.3f}, muted={grade['muted']}"
        )
    print("\nDemo complete.")


if __name__ == "__main__":
    main()
