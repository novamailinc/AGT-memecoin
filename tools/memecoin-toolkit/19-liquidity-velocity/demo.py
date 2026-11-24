"""Scenario walkthrough for the liquidity-velocity lab. Synthetic only."""
from liqvelocity import (Flow, calibration_accuracy, calibrated_gross_edge,
                         clears_breakeven, edge_after_costs, inflow_rate,
                         predict_graduation, progress_rate, trade_rate,
                         velocity_band)


def main() -> None:
    print("=== Liquidity velocity lab (synthetic launch inflows) ===")

    fast = [Flow(ts=i * 10.0, sol_in=3.0) for i in range(20)]
    slow = [Flow(ts=i * 60.0, sol_in=0.1) for i in range(20)]
    for name, events in (("Fast launch", fast), ("Slow launch", slow)):
        rate = inflow_rate(events)
        print(f"\n-- {name} --")
        print(f"inflow rate: {rate:.2f} SOL/min ({velocity_band(rate)})")
        print(f"trade rate: {trade_rate(events):.2f} trades/min")
        print(f"curve progress rate: {progress_rate(events):.2f} pts/min")
        print(f"prediction: {predict_graduation(events)}")

    print("\n-- Sparse data fails closed --")
    sparse = [Flow(ts=i * 10.0, sol_in=3.0) for i in range(3)]
    print(f"3 events -> {predict_graduation(sparse)}")

    print("\n-- Calibration vs costs (the honest part) --")
    print(f"Velocity predicts graduation with accuracy {calibration_accuracy():.2f}")
    print(f"Calibrated gross edge of following it: {calibrated_gross_edge():+.3f}")
    print(f"Round-trip cost: ~4%")
    print(f"Edge after costs: {edge_after_costs():+.3f}")
    print(f"Clears breakeven: {clears_breakeven()}")
    print("Predicting graduation is not the same as profiting from it.")


if __name__ == "__main__":
    main()
