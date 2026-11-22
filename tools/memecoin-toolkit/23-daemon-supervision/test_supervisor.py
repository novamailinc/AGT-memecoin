"""Tests for supervisor.py -- synthetic process tables only, no real ps."""
import pytest

from supervisor import (Proc, RestartPolicy, backoff_delay, backoff_schedule,
                        check_liveness, ensure_single_instance,
                        find_matches_naive, find_matches_safe,
                        heartbeat_fresh, kill_targets_naive,
                        kill_targets_safe, pid_alive)

SELF = 500
PARENT = 499
PATTERN = "smart-watcher"


def table():
    return [
        Proc(PARENT, 1, "bash -c python3 scripts/smart-watcher --restart"),
        Proc(SELF, PARENT, "python3 supervisor.py smart-watcher"),
        Proc(700, 1, "python3 scripts/smart-watcher"),
        Proc(900, 1, "python3 scripts/other-daemon"),
    ]


def dup_table():
    return table() + [Proc(701, 1, "python3 scripts/smart-watcher")]


# -- Proc validation ----------------------------------------------------------

def test_proc_valid_record():
    proc = Proc(10, 1, "python3 daemon.py")
    assert proc.pid == 10 and proc.ppid == 1


@pytest.mark.parametrize("bad_pid", [0, -3, None, "700"])
def test_proc_bad_pid_rejected(bad_pid):
    with pytest.raises(ValueError):
        Proc(bad_pid, 1, "python3 daemon.py")


def test_proc_bad_ppid_and_cmdline_rejected():
    with pytest.raises(ValueError):
        Proc(10, -1, "python3 daemon.py")
    with pytest.raises(ValueError):
        Proc(10, 1, "")
    with pytest.raises(ValueError):
        Proc(10, 1, None)


# -- liveness -------------------------------------------------------------------

def test_pid_alive_present_and_absent():
    assert pid_alive(700, table()) is True
    assert pid_alive(4242, table()) is False
    assert pid_alive(None, table()) is False


def test_liveness_ok_when_pid_alive_and_heartbeat_fresh():
    result = check_liveness(700, table(), 5.0, 120.0)
    assert result.alive is True and result.reason == "ok"


def test_liveness_no_process():
    result = check_liveness(4242, table(), 5.0, 120.0)
    assert result.alive is False and result.reason == "no_process"


def test_liveness_stale_heartbeat_despite_live_pid():
    result = check_liveness(700, table(), 121.0, 120.0)
    assert result.alive is False and result.reason == "stale_heartbeat"


def test_liveness_heartbeat_at_exact_limit_is_fresh():
    result = check_liveness(700, table(), 120.0, 120.0)
    assert result.alive is True


def test_liveness_missing_pidfile():
    result = check_liveness(None, table(), 5.0, 120.0)
    assert result.alive is False and result.reason == "no_pidfile"


def test_liveness_empty_table_is_no_process():
    result = check_liveness(700, [], 5.0, 120.0)
    assert result.alive is False and result.reason == "no_process"


def test_heartbeat_fresh_boundary():
    assert heartbeat_fresh(0.0, 120.0) is True
    assert heartbeat_fresh(120.0, 120.0) is True
    assert heartbeat_fresh(120.1, 120.0) is False


def test_liveness_rejects_bad_ages():
    with pytest.raises(ValueError):
        check_liveness(700, table(), -1.0, 120.0)
    with pytest.raises(ValueError):
        check_liveness(700, table(), 5.0, 0)
    with pytest.raises(ValueError):
        heartbeat_fresh(5.0, -10.0)


# -- backoff ----------------------------------------------------------------------

def test_backoff_doubles_each_attempt():
    assert backoff_schedule(5, base_s=1.0, factor=2.0, cap_s=1000.0) == \
        [1.0, 2.0, 4.0, 8.0, 16.0]


def test_backoff_is_capped():
    assert backoff_delay(20, base_s=1.0, factor=2.0, cap_s=60.0) == 60.0
    assert backoff_schedule(8, cap_s=60.0)[-1] == 60.0


def test_backoff_attempt_zero_is_base():
    assert backoff_delay(0, base_s=3.0) == 3.0


def test_backoff_factor_one_is_flat():
    assert backoff_schedule(3, base_s=5.0, factor=1.0, cap_s=60.0) == [5.0] * 3


def test_backoff_custom_base_and_factor():
    assert backoff_delay(2, base_s=0.5, factor=3.0, cap_s=100.0) == \
        pytest.approx(4.5)


def test_backoff_rejects_bad_inputs():
    with pytest.raises(ValueError):
        backoff_delay(-1)
    with pytest.raises(ValueError):
        backoff_delay(0, base_s=0)
    with pytest.raises(ValueError):
        backoff_delay(0, factor=0.5)
    with pytest.raises(ValueError):
        backoff_schedule(-1)


# -- restart policy -----------------------------------------------------------------

def test_policy_allows_restarts_within_budget():
    policy = RestartPolicy(max_restarts=3, window_s=600.0)
    for now in (0.0, 10.0, 20.0):
        policy.record_restart(now)
    assert policy.restarts_in_window(30.0) == 3


def test_policy_refuses_beyond_budget():
    policy = RestartPolicy(max_restarts=2, window_s=600.0)
    policy.record_restart(0.0)
    policy.record_restart(10.0)
    assert policy.should_restart(20.0) is False
    assert policy.gave_up(20.0) is True
    with pytest.raises(RuntimeError):
        policy.record_restart(20.0)


def test_policy_window_slides_and_budget_recovers():
    policy = RestartPolicy(max_restarts=2, window_s=100.0)
    policy.record_restart(0.0)
    policy.record_restart(10.0)
    assert policy.gave_up(50.0) is True
    assert policy.should_restart(200.0) is True  # both restarts aged out
    assert policy.restarts_in_window(200.0) == 0


def test_policy_record_returns_growing_backoff():
    policy = RestartPolicy(max_restarts=5, window_s=600.0,
                           base_s=1.0, factor=2.0, cap_s=60.0)
    delays = [policy.record_restart(float(t)) for t in (0, 10, 20)]
    assert delays == [1.0, 2.0, 4.0]


def test_policy_rejects_bad_config():
    with pytest.raises(ValueError):
        RestartPolicy(max_restarts=0)
    with pytest.raises(ValueError):
        RestartPolicy(window_s=0)
    with pytest.raises(ValueError):
        RestartPolicy().record_restart(-5.0)


def test_policy_failed_record_does_not_consume_budget():
    policy = RestartPolicy(max_restarts=1, window_s=100.0)
    policy.record_restart(0.0)
    with pytest.raises(RuntimeError):
        policy.record_restart(10.0)
    assert policy.restarts_in_window(10.0) == 1


# -- the self-match trap ---------------------------------------------------------------

def test_naive_matcher_matches_watcher_itself():
    # Regression fixture: the naive matcher WOULD kill the watcher.
    targets = kill_targets_naive(PATTERN, table())
    assert SELF in targets
    assert PARENT in targets
    assert 700 in targets


def test_safe_matcher_excludes_self_and_parent():
    targets = kill_targets_safe(PATTERN, table(), SELF, PARENT)
    assert targets == [700]
    assert SELF not in targets and PARENT not in targets


def test_safe_matcher_excluding_only_self_is_not_the_contract():
    # The parent shell also carries the pattern; both must be excluded.
    matches = find_matches_safe(PATTERN, table(), SELF, PARENT)
    assert [p.pid for p in matches] == [700]


def test_naive_and_safe_agree_when_no_self_match():
    clean = [Proc(700, 1, "python3 scripts/smart-watcher"),
             Proc(900, 1, "python3 scripts/other-daemon")]
    assert kill_targets_naive(PATTERN, clean) == \
        kill_targets_safe(PATTERN, clean, SELF, PARENT) == [700]


def test_matchers_ignore_non_matching_processes():
    assert find_matches_naive("nonexistent-daemon", table()) == []
    assert find_matches_safe("nonexistent-daemon", table(), SELF, PARENT) == []


def test_matchers_reject_empty_pattern():
    with pytest.raises(ValueError):
        find_matches_naive("", table())
    with pytest.raises(ValueError):
        find_matches_safe(None, table(), SELF, PARENT)
    with pytest.raises(ValueError):
        find_matches_safe(PATTERN, table(), 0, PARENT)


# -- exactly-one-instance ------------------------------------------------------------------

def test_single_instance_ok():
    clean = [Proc(700, 1, "python3 scripts/smart-watcher")]
    report = ensure_single_instance(PATTERN, clean, SELF, PARENT)
    assert report.status == "ok"
    assert [p.pid for p in report.matches] == [700]


def test_zero_instances_means_start():
    report = ensure_single_instance(PATTERN, [], SELF, PARENT)
    assert report.status == "start"
    assert report.matches == ()


def test_zero_instances_when_only_watcher_present():
    only_watcher = [Proc(SELF, PARENT, "python3 supervisor.py smart-watcher"),
                    Proc(PARENT, 1, "bash -c python3 scripts/smart-watcher")]
    report = ensure_single_instance(PATTERN, only_watcher, SELF, PARENT)
    assert report.status == "start"  # watcher + parent don't count


def test_duplicates_are_reported_not_hidden():
    report = ensure_single_instance(PATTERN, dup_table(), SELF, PARENT)
    assert report.status == "duplicates"
    assert sorted(p.pid for p in report.matches) == [700, 701]


def test_instance_check_uses_safe_matcher():
    # With the naive matcher this table would look like 4 instances;
    # safely it is exactly 1 real daemon plus a duplicate-free report.
    report = ensure_single_instance(PATTERN, table(), SELF, PARENT)
    assert report.status == "ok"
    assert len(report.matches) == 1
