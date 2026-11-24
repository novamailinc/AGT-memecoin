# Daemon Supervision (lab)

A from-scratch, pure-stdlib lab for watchdog semantics: liveness decisions, restart backoff and budgets, the self-match trap in pattern-based process matching, and the exactly-one-instance invariant. Everything here is synthetic — no network, no live bot code, and **no real process inspection at all**: every function operates on a synthetic process table (a list of fake `Proc` records supplied by the caller), so the dangerous failure modes can be exercised without ever signalling a real process.

## What it does

Module `supervisor.py` — a frozen record type, `Proc(pid, ppid, cmdline)`, plus pure functions over lists of `Proc`:

- `Proc(pid, ppid, cmdline)` — one fake process-table row. Fail-closed: construction raises `ValueError` on a non-positive/non-int `pid`, a negative `ppid`, or an empty/non-string `cmdline`.
- `pid_alive(pid, table) -> bool` — True iff `pid` appears in the table; `None` is False rather than an error, because a missing pidfile pid is a normal input.
- `check_liveness(pidfile_pid, table, heartbeat_age_s, max_heartbeat_age_s) -> Liveness(alive, reason)` — the watchdog's decision. `reason` is `"no_pidfile"` (pid is `None`), `"no_process"` (pid absent from the table), `"stale_heartbeat"` (pid exists but the heartbeat age exceeds the limit — a wedged process is *not* alive), or `"ok"`. Fail-closed: negative ages and non-positive limits raise `ValueError`. `heartbeat_fresh(age, max_age) -> bool` is the inclusive boundary check on its own.
- `backoff_delay(attempt, base_s=1.0, factor=2.0, cap_s=60.0) -> float` — `min(cap, base * factor**attempt)` for a 0-based attempt; `backoff_schedule(n, ...) -> list[float]` returns the first `n`. Fail-closed on negative attempts, non-positive base/cap, or a factor below 1.
- `RestartPolicy(max_restarts=5, window_s=600.0, ...)` — a sliding-window restart budget. `record_restart(now)` records a restart and returns the backoff before the next one; when the budget is exhausted it raises `RuntimeError` (fail-closed: give up and flag, never a restart storm), and a failed record does not consume budget. `should_restart(now)`, `gave_up(now)`, and `restarts_in_window(now)` expose the state; restarts age out of the window so the budget recovers.
- `find_matches_naive(pattern, table) -> list[Proc]` — plain substring matching over command lines, kept deliberately as the unsafe baseline. `kill_targets_naive(pattern, table) -> list[int]` is what it would signal — including the watcher itself when its own command line contains the pattern.
- `find_matches_safe(pattern, table, self_pid, parent_pid) -> list[Proc]` / `kill_targets_safe(...)` — the fixed matcher: identical substring matching, but the watcher's own pid **and** its parent pid are always excluded, because the parent shell's command line typically contains the pattern too (a restart one-liner). Fail-closed on empty patterns and non-positive self/parent pids.
- `ensure_single_instance(pattern, table, self_pid, parent_pid) -> InstanceReport(status, matches)` — the exactly-one-instance invariant, using the safe matcher: 0 matches → `"start"`, exactly 1 → `"ok"`, 2 or more → `"duplicates"` with the offending records reported (never silently resolved by killing extras in this lab).

`demo.py` runs one fixture table through liveness checks, a backoff schedule plus an exhausting restart budget, the naive-vs-safe kill-target contrast, and all three instance-invariant outcomes.

## Test suite

38 tests, all passing (`pytest -q`: "38 passed"). Covered: `Proc` validation for bad pids/ppids/cmdlines; pid presence and absence in a table; liveness for the ok, no-process, missing-pidfile, and empty-table cases; a live pid with a stale heartbeat being judged dead, and the heartbeat at exactly the limit being fresh; bad ages and limits raising; backoff doubling, capping, the attempt-0 base value, a flat factor-1 schedule, and custom base/factor arithmetic, with bad inputs rejected; the restart policy allowing restarts within budget, refusing and raising beyond it, returning growing backoffs, recovering as the window slides, rejecting bad configuration, and not consuming budget on a failed record; the self-match regression fixture — the naive matcher's kill list provably contains the watcher's own pid and its parent pid, while the safe matcher's contains exactly the real daemon; naive and safe matchers agreeing when no self-match exists; non-matching processes being ignored; empty patterns rejected; and the instance invariant returning `"ok"`, `"start"` (including when only the watcher and its parent are present), and `"duplicates"` with both duplicate pids reported. No test touches a real process: all tables are synthetic fixtures.

## Captured output

Verbatim output of `python demo.py`:

```
=== Daemon supervision lab (synthetic process tables only) ===

-- Liveness (pidfile pid + heartbeat age) --
pid=700 heartbeat_age=   5.0s -> alive=True reason=ok
pid=700 heartbeat_age= 999.0s -> alive=False reason=stale_heartbeat
pid=4242 heartbeat_age=   5.0s -> alive=False reason=no_process
pid=None heartbeat_age=   5.0s -> alive=False reason=no_pidfile

-- Restart backoff schedule --
Delays: [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]
Restart at t=0s: next backoff 1s
Restart at t=10s: next backoff 2s
Restart at t=20s: next backoff 4s
Budget exhausted at t=30s: gave_up=True

-- The self-match trap --
Naive kill targets for 'smart-watcher': [499, 500, 700, 701] (includes watcher+parent!)
Safe kill targets: [700, 701]

-- Exactly-one-instance invariant --
Status: duplicates (matching daemon pids: [700, 701])
One daemon running: ok
No daemon running: start
```

## What this mirrors in the live bot

This mirrors `scripts/forex-watchdog` in the live bot — the supervisor that relaunches dead daemons, judges liveness from pidfiles plus heartbeat freshness, and keeps exactly one instance of each daemon running — and the hard-won lesson recorded in the workspace's operating notes: killing daemons by command-line pattern is a real failure mode, because the pattern also matches the shell running the kill when that shell's own command line contains the pattern text, so the operator's shell gets terminated by its own restart command. The fix modelled here — always exclude both your own pid and your parent pid from any pattern-based kill set — is the direct encoding of that lesson. It is a teaching/checking model of those semantics only; it shares no code with the live watchdog and never inspects a real process.

## Self-exam

1. Why is a process whose pid exists but whose heartbeat is stale judged *not alive*, and what failure does each of the two signals catch that the other misses?
2. In the self-match fixture, why is excluding only the watcher's own pid insufficient — what is special about the parent pid's command line?
3. Why does the restart policy use a sliding window rather than a lifetime counter of restarts?
4. What goes wrong operationally if a watchdog restarts a crashing daemon immediately, forever, with no backoff and no budget?
5. The exactly-one-instance check *reports* duplicates instead of killing the extras — what information would you want before deciding which duplicate should survive?
6. Why does this lab insist on synthetic process tables instead of reading the real process list, even though the real list is available on the machine?
