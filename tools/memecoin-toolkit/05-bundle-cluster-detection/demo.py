"""Demo: common-funder clusters -- existence is not behaviour."""
from bundle import cluster_share, evaluate, find_clusters

SUPPLY = 1_000_000_000


def show(title, wallets, balances, flows=None):
    clusters = find_clusters(wallets)
    result = evaluate(wallets, balances, SUPPLY, flows)
    print(title)
    for cluster in clusters:
        print(f"  cluster {cluster}: share={cluster_share(cluster, balances, SUPPLY):.1f}%")
    print(f"  largest cluster behaviour: {result.behavior}")
    print(f"  verdict: {result.verdict} -- {result.reason}")
    print()


def main():
    print("Bundle cluster lab -- one funder, many hats")
    print("=" * 52)
    wallets = [
        {"wallet": "W1", "funder": "FUNDER_X"},
        {"wallet": "W2", "funder": "FUNDER_X"},
        {"wallet": "W3", "funders": ["FUNDER_X", "FUNDER_Y"]},
        {"wallet": "W4", "funder": "FUNDER_Y"},
        {"wallet": "W5", "funder": "FUNDER_SOLO"},
    ]
    balances = {"W1": 80_000_000, "W2": 70_000_000, "W3": 60_000_000,
                "W4": 40_000_000, "W5": 10_000_000}
    show("Scenario 1: 25% cluster, flow data missing (FLAG, not veto)",
         wallets, balances)
    show("Scenario 2: same 25% cluster, wallets net selling (VETO)",
         wallets, balances,
         flows={"W1": {"in": 10, "out": 90}, "W2": {"in": 5, "out": 40},
                "W3": {"in": 20, "out": 15}, "W4": {"in": 0, "out": 0}})
    big = {"W1": 200_000_000, "W2": 150_000_000, "W3": 120_000_000,
           "W4": 80_000_000, "W5": 10_000_000}
    show("Scenario 3: 55% cluster (VETO on extreme share alone)",
         wallets, big)


if __name__ == "__main__":
    main()
