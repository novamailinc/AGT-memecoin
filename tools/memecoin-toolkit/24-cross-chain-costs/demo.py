"""Scenario walkthrough for the cross-chain costs lab. Synthetic only."""
from chaincosts import cost_gate, min_viable_notional, render_cost_table


def main() -> None:
    print("=== Cross-chain cost lab (flat / percentage tx cost model) ===")
    print("Worst-case round trip = 4 txs (1 entry + up to 3 exit legs)")
    print("Pre-registered gate: block when round-trip cost > 25% of notional")

    print("\n-- Cost table: $1 / $5 / $25 across four chains --")
    print(render_cost_table())

    print("\n-- Pinned gate decisions --")
    for notional, chain in ((1.0, "ethereum"), (25.0, "ethereum"),
                            (1.0, "solana"), (1.0, "base"),
                            (1.0, "hyperliquid"), (1000.0, "hyperliquid")):
        result = cost_gate(notional, chain)
        print(f"${notional:>7.2f} on {chain:<12} cost ${result.cost_usd:.4f} "
              f"({result.cost_pct:.2f}%) -> "
              f"{'ALLOWED' if result.allowed else 'BLOCKED'}")

    print("\n-- Minimum viable notional per chain (25% gate) --")
    for chain in ("solana", "base", "ethereum", "hyperliquid"):
        print(f"{chain:<12} min viable notional: "
              f"${min_viable_notional(chain):.2f}")

    print("\n-- Fail-closed --")
    try:
        cost_gate(1.0, "dogechain")
    except ValueError as exc:
        print(f"Unknown chain correctly rejected: {exc}")


if __name__ == "__main__":
    main()
