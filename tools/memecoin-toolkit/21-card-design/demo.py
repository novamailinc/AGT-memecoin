"""Scenario walkthrough for the card-design lab. Synthetic only."""
from cardcheck import validate

MINT = "7xK9mQ2vR8tY4wZ6aB3cD5eF7gH9jK2mN4pQ6rS8tU1v"


def full_card():
    return {
        "chain": "solana",
        "mint": MINT,
        "mcap_usd": 250_000.0,
        "fdv_usd": 250_000.0,
        "liquidity_usd": 60_000.0,
        "context_line": "📊 $250k mcap · $60k liq · 120 holders, rising 5m",
        "safety_line": "🛡 Safety: PASS · score 42/100 · no hard fails",
        "supply_line": "Supply 1.0B",
        "provenance_line": "⛓ Solana runner feed",
        "exit_hint": "Exit: Solana ladder, sell on Raydium — 50% at 2x",
        "buttons": ["📋 Copy CA", "📍 I bought this"],
        "is_pick": True,
    }


def main() -> None:
    print("=== Card design lab (synthetic signal cards) ===")

    card = full_card()
    print("\n-- A complete pick card --")
    print(f"validate -> {validate(card)} (empty means valid)")

    print("\n-- Cards each missing one element --")
    for label, change in (
        ("no safety line", {"safety_line": ""}),
        ("no copy-CA button", {"buttons": ["📍 I bought this"]}),
        ("no supply line", {"supply_line": ""}),
        ("pick without the track button", {"buttons": ["📋 Copy CA"]}),
        ("a bare CA and nothing else", None),
    ):
        if change is None:
            broken = {"mint": MINT}
        else:
            broken = {**card, **change}
        print(f"{label}: {validate(broken)}")

    print("\n-- An invalid CA --")
    bad = {**card, "mint": "0OIl-not-base58"}
    print(f"validate -> {validate(bad)}")
    print("\nNo bare CAs: a card that cannot be judged cannot be sent.")


if __name__ == "__main__":
    main()
