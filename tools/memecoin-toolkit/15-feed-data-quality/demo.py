"""Scenario walkthrough for the feed-data-quality lab. Synthetic only."""
from feedquality import (
    MINT, NOW_MS, disagreement_payload, dust_first_payload, messy_payload,
    select_pair,
)


def main() -> None:
    print("=== Feed data quality lab (synthetic DexScreener-style payloads) ===")

    print("\n-- The pairs[0] trap --")
    payload = dust_first_payload()
    naive = payload[0]
    print(f"pairs[0]: {naive['pairAddress']} liquidity ${naive['liquidity']['usd']:.0f} "
          f"price ${naive['priceUsd']}")
    selection = select_pair(payload, MINT, NOW_MS)
    print(f"selector: {selection.pair.pair_address} "
          f"liquidity ${selection.pair.liquidity_usd:,.0f} "
          f"price ${selection.pair.price_usd:.2f}")
    print(f"candidates considered: {selection.candidates_considered}, "
          f"price disagreement: {selection.disagreement_pct:.1f}%")

    print("\n-- Messy payload --")
    messy = select_pair(messy_payload(), MINT, NOW_MS)
    print(f"selected: {messy.pair.pair_address} "
          f"liquidity ${messy.pair.liquidity_usd:,.0f}")
    print(f"malformed skipped: {messy.malformed_skipped}, "
          f"duplicates dropped: {messy.duplicates_dropped}, "
          f"stale rejected: {messy.stale_rejected}")

    print("\n-- Fail-closed: unverifiable price --")
    print(f"Disagreeing pairs select: {select_pair(disagreement_payload(), MINT, NOW_MS)}")
    print(f"Empty payload selects: {select_pair([], MINT, NOW_MS)}")


if __name__ == "__main__":
    main()
