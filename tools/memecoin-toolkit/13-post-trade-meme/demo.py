"""Demo: run the $1-shot analytics over the synthetic 10-close fixture."""
from posttrade import (demo_fixture, expectancy, leakage_report,
                       render_scorecard, round_trippers, rug_rate, win_rate)


def main() -> None:
    closes = demo_fixture()
    print("Post-Trade Analytics — $1 shots, win = realized >= 1.5x")
    print(f"\n{len(closes)} synthetic closes")
    print(render_scorecard(closes))
    rt = round_trippers(closes)
    print(f"\nround-trippers: {rt['count']} closes peaked >=2x but closed <1x, "
          f"avg realized {rt['mean_realized']:.3f}x")
    print(f"leakage report: {leakage_report(closes)}")
    print(f"overall: win rate {win_rate(closes):.2f}, "
          f"expectancy {expectancy(closes):+.3f} per $1, rug rate {rug_rate(closes):.2f}")
    print(f"empty ledger renders as: {render_scorecard([])!r}")


if __name__ == "__main__":
    main()
