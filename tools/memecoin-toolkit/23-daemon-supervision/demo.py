"""Scenario walkthrough for the daemon-supervision lab. Synthetic only."""
from supervisor import (Proc, RestartPolicy, backoff_schedule,
                        check_liveness, ensure_single_instance,
                        kill_targets_naive, kill_targets_safe)

WATCHER_PID = 500
PARENT_PID = 499
PATTERN = "smart-watcher"


def fixture_table():
    # The watcher (500) was launched by a shell (499) whose own command
    # line contains the pattern; two real daemon copies also match.
    return [
        Proc(pid=499, ppid=1, cmdline="bash -c python3 scripts/smart-watcher --restart"),
        Proc(pid=500, ppid=499, cmdline="python3 supervisor.py smart-watcher"),
        Proc(pid=700, ppid=1, cmdline="python3 scripts/smart-watcher"),
        Proc(pid=701, ppid=1, cmdline="python3 scripts/smart-watcher"),
        Proc(pid=900, ppid=1, cmdline="python3 scripts/other-daemon"),
    ]


def main() -> None:
    print("=== Daemon supervision lab (synthetic process tables only) ===")
    table = fixture_table()

    print("\n-- Liveness (pidfile pid + heartbeat age) --")
    for pid, age in ((700, 5.0), (700, 999.0), (4242, 5.0), (None, 5.0)):
        result = check_liveness(pid, table, age, max_heartbeat_age_s=120.0)
        print(f"pid={pid} heartbeat_age={age:6.1f}s -> alive={result.alive} "
              f"reason={result.reason}")

    print("\n-- Restart backoff schedule --")
    print(f"Delays: {backoff_schedule(7, base_s=1.0, factor=2.0, cap_s=60.0)}")
    policy = RestartPolicy(max_restarts=3, window_s=600.0)
    for now in (0.0, 10.0, 20.0):
        print(f"Restart at t={now:.0f}s: next backoff "
              f"{policy.record_restart(now):.0f}s")
    print(f"Budget exhausted at t=30s: gave_up={policy.gave_up(30.0)}")

    print("\n-- The self-match trap --")
    print(f"Naive kill targets for {PATTERN!r}: "
          f"{kill_targets_naive(PATTERN, table)} (includes watcher+parent!)")
    print(f"Safe kill targets: "
          f"{kill_targets_safe(PATTERN, table, WATCHER_PID, PARENT_PID)}")

    print("\n-- Exactly-one-instance invariant --")
    report = ensure_single_instance(PATTERN, table, WATCHER_PID, PARENT_PID)
    print(f"Status: {report.status} "
          f"(matching daemon pids: {[p.pid for p in report.matches]})")
    single = [Proc(pid=700, ppid=1, cmdline="python3 scripts/smart-watcher")]
    print(f"One daemon running: "
          f"{ensure_single_instance(PATTERN, single, WATCHER_PID, PARENT_PID).status}")
    print(f"No daemon running: "
          f"{ensure_single_instance(PATTERN, [], WATCHER_PID, PARENT_PID).status}")


if __name__ == "__main__":
    main()
