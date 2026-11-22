"""Demo: five rug detectors over synthetic lifecycles."""
from ruglab import run_detectors

CLEAN = [
    {"ts": 0, "price": 1.0, "liquidity_usd": 100_000, "holders": 500,
     "supply": 1_000_000, "mint_authority_active": False, "sell_sim_ok": True,
     "buy_vol": 4_000, "sell_vol": 3_000, "unique_buyers": 180, "trades": 300},
    {"ts": 1800, "price": 1.3, "liquidity_usd": 120_000, "holders": 560,
     "supply": 1_000_000, "mint_authority_active": False, "sell_sim_ok": True,
     "buy_vol": 5_000, "sell_vol": 3_500, "unique_buyers": 210, "trades": 340},
    {"ts": 3600, "price": 1.5, "liquidity_usd": 130_000, "holders": 610,
     "supply": 1_000_000, "mint_authority_active": False, "sell_sim_ok": True,
     "buy_vol": 5_500, "sell_vol": 4_000, "unique_buyers": 230, "trades": 360},
]

LP_RUG = [
    dict(CLEAN[0]),
    {"ts": 1800, "price": 0.9, "liquidity_usd": 90_000, "holders": 480,
     "supply": 1_000_000, "mint_authority_active": False, "sell_sim_ok": True,
     "buy_vol": 2_000, "sell_vol": 6_000, "unique_buyers": 90, "trades": 250},
    {"ts": 3000, "price": 0.2, "liquidity_usd": 8_000, "holders": 300,
     "supply": 1_000_000, "mint_authority_active": False, "sell_sim_ok": True,
     "buy_vol": 200, "sell_vol": 5_000, "unique_buyers": 30, "trades": 120},
]


def show(title, lifecycle):
    print(title)
    for name, (hit, evidence) in run_detectors(lifecycle).items():
        print(f"  [{'X' if hit else ' '}] {name}: {evidence}")
    print()


def main():
    print("Rug taxonomy lab -- five patterns, synthetic lifecycles")
    print("=" * 56)
    show("Scenario 1: healthy lifecycle (expect: nothing fires)", CLEAN)
    show("Scenario 2: liquidity pulled, price collapses", LP_RUG)


if __name__ == "__main__":
    main()
