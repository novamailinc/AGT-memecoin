"""Scenario walkthrough for the token-lifecycle lab. Synthetic only."""
from lifecycle import (
    Phase, Snapshot, classify, classify_timeline, dac_timeline,
    fast_rug_timeline, slow_steady_runner_timeline, time_in_phase,
    transition_flag, validate_timeline_phases, zombie_timeline,
)


def show(name, points, end):
    phases = classify_timeline(points)
    print(f"\n-- {name} --")
    for point, phase in zip(points, phases):
        print(f"t+{point.timestamp_minutes:6.0f} min  {phase.value}")
    totals = time_in_phase(points, end_minutes=end)
    summary = ", ".join(f"{p.value} {m:.0f}m" for p, m in totals.items())
    print(f"Time in phase: {summary}")
    flagged = validate_timeline_phases(phases)
    print(f"Flagged transitions: {flagged if flagged else 'none'}")


def main() -> None:
    print("=== Token lifecycle lab (synthetic timelines) ===")
    show("Fast rug: birth to dead in minutes", fast_rug_timeline(), 9.0)
    show("DACITY-shape: bonding -> graduated -> runner -> dead in <2h",
         dac_timeline(), 105.0)
    show("Slow steady runner", slow_steady_runner_timeline(), 360.0)
    show("Zombie: fading forever", zombie_timeline(), 2880.0)

    print("\n-- Revival --")
    print(f"DEAD -> RUNNER flag: {transition_flag(Phase.DEAD, Phase.RUNNER)} "
          "(legal, but a dormant revival deserves a second look)")
    print(f"RUNNER -> BONDING flag: {transition_flag(Phase.RUNNER, Phase.BONDING)}")

    print("\n-- Fail-closed --")
    snap = Snapshot(curve_completion_pct=100, age_minutes=30)
    print(f"Snapshot with missing metrics classifies as: {classify(snap).value}")
    print(f"Full snapshot classifies as: "
          f"{classify(Snapshot(100, 30, 60000, 200000, 40, 12)).value}")


if __name__ == "__main__":
    main()
