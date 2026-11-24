"""Scenario walkthrough for the AMM + LP lab. Synthetic only."""
from amm import (InsufficientOutputError, Pool, estimate_sandwich,
                 get_amount_out, verify_lp_lock)


def main() -> None:
    print("=== AMM + LP lab (synthetic constant-product pool) ===")
    pool = Pool(reserve_x=100.0, reserve_y=1_000_000.0, fee_bps=25)
    print(f"Pool: {pool.reserve_x:.0f} SOL / {pool.reserve_y:,.0f} tokens, "
          f"spot {pool.spot_price_y_per_x():,.0f} tokens per SOL")

    print("\n-- Quotes both directions --")
    out = pool.quote_x_for_y(1.0)
    print(f"1 SOL in -> {out:,.1f} tokens out")
    back = pool.quote_y_for_x(out)
    print(f"Those tokens back in -> {back:.4f} SOL out (fees + impact)")
    print(f"Module-level check: get_amount_out(1, 100, 1_000_000) = "
          f"{get_amount_out(1.0, 100.0, 1_000_000.0):,.1f}")

    print("\n-- Impact grows with size --")
    for size in (0.1, 1.0, 10.0, 50.0):
        print(f"Buy {size:5.1f} SOL: price impact "
              f"{pool.price_impact_pct(size):5.2f}%, slippage "
              f"{pool.slippage_pct(size):5.2f}%")

    print("\n-- Min-out guard --")
    try:
        pool.swap_x_for_y(1.0, min_out=out * 1.01)
    except InsufficientOutputError as exc:
        print(f"Swap correctly refused: {exc}")

    print("\n-- LP lock verification --")
    cases = [
        ("Mostly burned", verify_lp_lock(1000.0, 950.0, [0.0], 50.0)),
        ("Half deployer-held", verify_lp_lock(1000.0, 400.0, [100.0], 500.0)),
        ("Data missing", verify_lp_lock(None, 950.0, [0.0], 50.0)),
    ]
    for label, verdict in cases:
        pct = "n/a" if verdict.secured_pct is None else f"{verdict.secured_pct:.1f}%"
        print(f"{label}: verdict={verdict.verdict} secured={pct} "
              f"({verdict.reasons[0]})")

    print("\n-- Sandwich estimate --")
    thin = Pool(reserve_x=10.0, reserve_y=100_000.0, fee_bps=25)
    for name, p in (("deep pool (100 SOL)", pool), ("thin pool (10 SOL)", thin)):
        result = estimate_sandwich(p, victim_amount_x_in=1.0,
                                   attacker_amount_x_in=2.0)
        print(f"{name}: victim normally gets {result.victim_out_normal:,.0f}, "
              f"sandwiched {result.victim_out_sandwiched:,.0f}, "
              f"extra loss {result.victim_extra_loss_pct:.2f}%, "
              f"attacker profit {result.attacker_profit_x:+.4f} SOL")


if __name__ == "__main__":
    main()
