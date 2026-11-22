"""Scenario walkthrough for the wash / bot-share lab. Synthetic only."""
from wash import Trade, assess, bot_share, wash_share


def buy(w, amt, slot):
    return Trade(w, "buy", amt, slot=slot)


def sell(w, amt, slot):
    return Trade(w, "sell", amt, slot=slot)


def main() -> None:
    print("=== Wash & bot-share lab (synthetic trade streams) ===")

    clean = [buy(f"wallet{i}", 1.0 + 0.1 * i, 100 + i) for i in range(12)]
    print("\n-- Clean stream (12 distinct real buyers) --")
    print(f"wash_share={wash_share(clean):.2f} bot_share={bot_share(clean):.2f}")
    print(f"assess -> {assess(clean)}")

    wash = []
    for i in range(6):
        wash.append(buy(f"w{i}", 2.0, 200 + 2 * i))
        wash.append(sell(f"w{i}", 2.0, 201 + 2 * i))
    print("\n-- Wash stream (6 wallets buy and sell the same size) --")
    print(f"wash_share={wash_share(wash):.2f} bot_share={bot_share(wash):.2f}")
    print(f"assess -> {assess(wash)}")

    bots = [buy(f"bot{i}", 0.01, 300 + i) for i in range(12)]
    print("\n-- Bot stream (12 dust-sized buys) --")
    print(f"wash_share={wash_share(bots):.2f} bot_share={bot_share(bots):.2f}")
    print(f"assess -> {assess(bots)}")

    print("\n-- Missing / thin data (fail-open fingerprint) --")
    print(f"assess(None) -> {assess(None)}")
    print(f"assess(3 trades) -> {assess(clean[:3])}")
    print("unknown is neither a pass nor a veto: no score is reported.")

    print("\n-- Fail-closed --")
    try:
        wash_share([])
    except ValueError as exc:
        print(f"Empty stream correctly rejected: {exc}")


if __name__ == "__main__":
    main()
