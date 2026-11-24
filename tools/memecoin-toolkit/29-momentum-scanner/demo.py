"""Demo for momentum-scanner lab. Synthetic only."""
from momentumscan import Pool, CooldownBook, scan, qualify, doomsday_pool, good_pool


def main() -> None:
    print("=== Momentum scanner lab (no upper gain ceiling) ===")
    safety = lambda mint: mint != "RugMint"
    pools = [
        good_pool(),
        doomsday_pool(),
        good_pool(mint="ThinMint", liquidity_usd=10_000),
        good_pool(mint="QuietMint", volume_24h=5_000),
        good_pool(mint="FlatMint", change_h24_pct=5.0),
        good_pool(mint="DumpMint", change_h1_pct=-40.0, change_m5_pct=-2.0),
        good_pool(mint="RugMint"),
        Pool(mint="StaleMint", liquidity_usd=200_000, volume_24h=300_000,
             change_h24_pct=50.0, change_h1_pct=-5.0, change_m5_pct=-1.0),
    ]
    book = CooldownBook()
    qualified, stats = scan(pools, safety_ok=safety, cooldown=book, now=1000.0)
    print(f"Qualified: {[p.mint for p in qualified]}")
    print(f"Stats: {stats}")
    print(f"Doomsday re-check (fresh book): {qualify(doomsday_pool(), safety_ok=safety, cooldown=CooldownBook(), now=0)}")
    print(f"Doomsday again within 6h (same book): {qualify(doomsday_pool(), safety_ok=safety, cooldown=book, now=2000.0)}")


if __name__ == "__main__":
    main()
