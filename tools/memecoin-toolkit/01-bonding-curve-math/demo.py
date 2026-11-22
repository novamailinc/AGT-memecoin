"""Scenario walkthrough for the bonding-curve lab. Synthetic only."""
from curve import Curve


def main() -> None:
    curve = Curve()
    print("=== Bonding curve lab (synthetic pump.fun-style curve) ===")
    print(f"Virtual reserves: {curve.virtual_token_reserves:,.0f} tokens / "
          f"{curve.virtual_sol_reserves:.1f} SOL")
    print(f"Spot price: {curve.spot_price_sol():.10f} SOL per token")
    print(f"Spot quote: {curve.spot_price_tokens_per_sol():,.0f} tokens per SOL")

    print("\n-- A small buy --")
    tokens = curve.tokens_received_for_sol(1.0)
    print(f"1.0 SOL buys {tokens:,.0f} tokens (fee + impact included)")
    print(f"Cost to buy those tokens back exactly: "
          f"{curve.cost_to_buy_tokens(tokens):.4f} SOL")

    print("\n-- Buying 1% of supply --")
    cost_1pct = curve.cost_to_buy_percent_of_supply(1.0)
    print(f"1% of supply = {curve.total_supply * 0.01:,.0f} tokens")
    print(f"Cost: {cost_1pct:.4f} SOL")
    print(f"Price impact of that buy: "
          f"{curve.price_impact_of_buy_tokens(curve.total_supply * 0.01):.2f}%")
    print(f"Slippage vs spot quote: "
          f"{curve.slippage_buy_tokens(curve.total_supply * 0.01):+.2f}%")

    print("\n-- Price rises after a bigger buy --")
    after = curve.curve_after_buy_sol(10.0)
    print(f"After a 10 SOL buy, spot is {after.spot_price_sol():.10f} SOL "
          f"(was {curve.spot_price_sol():.10f})")

    print("\n-- Round trip --")
    bought = curve.tokens_received_for_sol(5.0)
    back = curve.curve_after_buy_sol(5.0).sell_proceeds_sol(bought)
    print(f"Buy with 5.0 SOL -> {bought:,.0f} tokens -> sell all -> {back:.4f} SOL")
    print(f"Round-trip loss: {(5.0 - back) / 5.0 * 100:.2f}% (fees only)")

    print("\n-- Graduation --")
    for collected in (0.0, 42.5, 85.0, 120.0):
        print(f"Real SOL collected {collected:6.1f}: "
              f"{curve.completion_pct(collected):5.1f}% complete, "
              f"graduated={curve.is_graduated(collected)}")

    print("\n-- Fail-closed --")
    try:
        curve.tokens_received_for_sol(0)
    except ValueError as exc:
        print(f"Zero buy correctly rejected: {exc}")


if __name__ == "__main__":
    main()
