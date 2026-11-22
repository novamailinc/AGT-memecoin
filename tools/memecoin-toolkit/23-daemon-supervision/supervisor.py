"""Daemon supervision / watchdog semantics (synthetic lab).

Pure stdlib, no network, and -- by design -- NO real process
inspection: every function operates on a *synthetic process table*,
a list of fake :class:`Proc` records supplied by the caller.  Nothing
here calls ``ps``, ``pgrep``, ``os.kill`` or reads ``/proc``.

Semantics modelled:
  * Liveness -- a daemon is alive iff its pidfile pid exists in the
    process table AND its heartbeat age is within the allowed limit.
    Either failure alone means "not alive", with a distinct reason.
  * Restart policy -- exponential backoff between restarts and a
    budget of ``max_restarts`` inside a sliding window; exhausting
    the budget means give up and flag, never a restart storm.
  * The self-match trap -- a naive substring matcher over command
    lines also matches the watcher's *own* command line when the
    pattern appears in it (e.g. the watcher was started with the
    pattern as an argument).  The safe matcher excludes the watcher's
    own pid AND its parent pid; a regression fixture in the test
    suite proves the naive matcher would kill the watcher and the
    fixed one does not.
  * Exactly-one-instance invariant -- for a daemon name: 0 matching
    processes -> start one, exactly 1 -> ok, 2 or more -> duplicates
    to report (never silently kill extras in this lab).

Fail-closed: malformed records, negative ages, non-positive limits
and empty patterns raise ``ValueError`` instead of guessing.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Proc:
    """One fake process-table row."""
    pid: int
    ppid: int
    cmdline: str

    def __post_init__(self) -> None:
        if self.pid is None or not isinstance(self.pid, int) or self.pid <= 0:
            raise ValueError(f"pid must be a positive int, got {self.pid!r}")
        if self.ppid is None or not isinstance(self.ppid, int) or self.ppid < 0:
            raise ValueError(f"ppid must be a non-negative int, got {self.ppid!r}")
        if not isinstance(self.cmdline, str) or self.cmdline == "":
            raise ValueError(f"cmdline must be a non-empty str, got {self.cmdline!r}")


@dataclass(frozen=True)
class Liveness:
    alive: bool
    reason: str  # "ok" | "no_process" | "stale_heartbeat" | "no_pidfile"


@dataclass(frozen=True)
class InstanceReport:
    status: str  # "start" | "ok" | "duplicates"
    matches: tuple = ()


def _require_positive(name, value):
    if (value is None or isinstance(value, bool)
            or not isinstance(value, (int, float)) or not (value > 0)):
        raise ValueError(f"{name} must be positive, got {value!r}")
    return value


def _require_non_negative(name, value):
    if (value is None or isinstance(value, bool)
            or not isinstance(value, (int, float)) or not (value >= 0)):
        raise ValueError(f"{name} must be >= 0, got {value!r}")
    return value


def pid_alive(pid, table) -> bool:
    """True iff ``pid`` appears in the synthetic process table."""
    if pid is None:
        return False
    _require_positive("pid", pid)
    return any(proc.pid == pid for proc in table)


def check_liveness(pidfile_pid, table, heartbeat_age_s, max_heartbeat_age_s) -> Liveness:
    """Decide liveness from pid existence + heartbeat freshness.

    ``pidfile_pid=None`` models a missing pidfile.  A live pid with a
    stale heartbeat is NOT alive: the process exists but is wedged.
    """
    _require_positive("max_heartbeat_age_s", max_heartbeat_age_s)
    _require_non_negative("heartbeat_age_s", heartbeat_age_s)
    if pidfile_pid is None:
        return Liveness(False, "no_pidfile")
    if not pid_alive(pidfile_pid, table):
        return Liveness(False, "no_process")
    if heartbeat_age_s > max_heartbeat_age_s:
        return Liveness(False, "stale_heartbeat")
    return Liveness(True, "ok")


def heartbeat_fresh(heartbeat_age_s, max_age_s) -> bool:
    """True iff the heartbeat age is within the limit (inclusive)."""
    _require_positive("max_age_s", max_age_s)
    _require_non_negative("heartbeat_age_s", heartbeat_age_s)
    return heartbeat_age_s <= max_age_s


# -- restart policy ---------------------------------------------------------

def backoff_delay(attempt, base_s=1.0, factor=2.0, cap_s=60.0) -> float:
    """Exponential backoff for restart ``attempt`` (0-based), capped."""
    if attempt is None or isinstance(attempt, bool) or attempt < 0:
        raise ValueError(f"attempt must be >= 0, got {attempt!r}")
    _require_positive("base_s", base_s)
    _require_positive("cap_s", cap_s)
    if factor is None or not (factor >= 1.0):
        raise ValueError(f"factor must be >= 1.0, got {factor!r}")
    return min(cap_s, base_s * (factor ** attempt))


def backoff_schedule(n, base_s=1.0, factor=2.0, cap_s=60.0) -> list[float]:
    """The first ``n`` backoff delays."""
    if n is None or isinstance(n, bool) or n < 0:
        raise ValueError(f"n must be >= 0, got {n!r}")
    return [backoff_delay(i, base_s, factor, cap_s) for i in range(n)]


@dataclass
class RestartPolicy:
    """Restart budget: at most ``max_restarts`` per ``window_s``."""
    max_restarts: int = 5
    window_s: float = 600.0
    base_s: float = 1.0
    factor: float = 2.0
    cap_s: float = 60.0
    _history: list = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.max_restarts is None or self.max_restarts < 1:
            raise ValueError(f"max_restarts must be >= 1, got {self.max_restarts!r}")
        _require_positive("window_s", self.window_s)

    def _recent(self, now) -> list:
        cutoff = now - self.window_s
        return [t for t in self._history if t > cutoff]

    def restarts_in_window(self, now) -> int:
        _require_non_negative("now", now)
        return len(self._recent(now))

    def should_restart(self, now) -> bool:
        """True while the restart budget for the window lasts."""
        return self.restarts_in_window(now) < self.max_restarts

    def record_restart(self, now) -> float:
        """Record a restart at ``now``; return the backoff before the next.

        Raises RuntimeError (fail-closed) when the budget is already
        exhausted -- the caller must give up and flag, not restart.
        """
        _require_non_negative("now", now)
        if not self.should_restart(now):
            raise RuntimeError(
                f"restart budget exhausted: {self.max_restarts} restarts "
                f"within {self.window_s}s window -- giving up"
            )
        recent = self._recent(now)
        attempt = len(recent)
        self._history = recent + [now]
        return backoff_delay(attempt, self.base_s, self.factor, self.cap_s)

    def gave_up(self, now) -> bool:
        """True iff the budget is exhausted at ``now`` (flag, don't loop)."""
        return not self.should_restart(now)


# -- matching: the self-match trap -------------------------------------------

def _require_pattern(pattern):
    if not isinstance(pattern, str) or pattern == "":
        raise ValueError(f"pattern must be a non-empty str, got {pattern!r}")
    return pattern


def find_matches_naive(pattern, table) -> list:
    """Substring match over cmdlines -- the UNSAFE matcher.

    Kept deliberately as the regression fixture: if the watcher's own
    command line contains ``pattern``, this returns the watcher too.
    """
    _require_pattern(pattern)
    return [proc for proc in table if pattern in proc.cmdline]


def find_matches_safe(pattern, table, self_pid, parent_pid) -> list:
    """Substring match excluding the watcher's own pid AND parent pid.

    Excluding only self is not enough: the watcher's parent shell
    often carries the pattern in its command line too (e.g. a restart
    one-liner), and killing it kills the watcher mid-restart.
    """
    _require_pattern(pattern)
    _require_positive("self_pid", self_pid)
    _require_positive("parent_pid", parent_pid)
    excluded = {self_pid, parent_pid}
    return [proc for proc in table
            if pattern in proc.cmdline and proc.pid not in excluded]


def kill_targets_naive(pattern, table) -> list[int]:
    """Pids the naive matcher would signal -- includes self (the trap)."""
    return [proc.pid for proc in find_matches_naive(pattern, table)]


def kill_targets_safe(pattern, table, self_pid, parent_pid) -> list[int]:
    """Pids the fixed matcher signals -- self and parent never included."""
    return [proc.pid for proc in
            find_matches_safe(pattern, table, self_pid, parent_pid)]


def ensure_single_instance(pattern, table, self_pid, parent_pid) -> InstanceReport:
    """Apply the exactly-one-instance invariant using the SAFE matcher.

    0 matches -> "start", 1 -> "ok", >=2 -> "duplicates" (reported,
    with the offending records, never silently resolved by killing).
    """
    matches = tuple(find_matches_safe(pattern, table, self_pid, parent_pid))
    if len(matches) == 0:
        return InstanceReport("start", matches)
    if len(matches) == 1:
        return InstanceReport("ok", matches)
    return InstanceReport("duplicates", matches)
