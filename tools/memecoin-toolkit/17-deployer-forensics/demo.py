"""Scenario walkthrough for the deployer-forensics lab. Synthetic only."""
from deployer import (
    analyze, blacklist_candidates, mixed_history, should_veto, verdict_for,
)


def main() -> None:
    print("=== Deployer forensics lab (synthetic creator histories) ===")
    reports = analyze(mixed_history())
    for deployer in sorted(reports):
        report = reports[deployer]
        fp = report.fingerprint
        rate = "unknown" if report.rug_rate is None else f"{report.rug_rate * 100:.0f}%"
        print(f"\n{deployer}: verdict {report.verdict.value}")
        print(f"  launches {fp.launch_count}, rugs {fp.rug_count}, rug rate {rate}")
        print(f"  funding sources {fp.distinct_funding_sources}, "
              f"funding reuse {fp.funding_reuse_ratio * 100:.0f}%, "
              f"name reuse {fp.name_reuse_ratio * 100:.0f}%, "
              f"median interval {fp.median_interval_hours}h")

    print(f"\nBlacklist candidates (serial ruggers): {blacklist_candidates(mixed_history())}")
    print(f"Veto SerialSam on a new launch? {should_veto('SerialSam', mixed_history())}")
    print(f"Veto a total stranger? {should_veto('Stranger', mixed_history())} "
          f"-- but their verdict is {verdict_for('Stranger', mixed_history()).value}, "
          "not CLEAN: unknown history is never read as clean.")


if __name__ == "__main__":
    main()
