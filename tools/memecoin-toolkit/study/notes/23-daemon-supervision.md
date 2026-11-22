# Daemon Supervision — study notes

## Core ideas (teach-back)

A watchdog has one job: keep the right daemons running, exactly one copy each, and notice honestly when
it cannot. This lab models that job's decision logic — and nothing else. By design there is **no real
process inspection at all**: every function operates on a synthetic process table, a list of fake
`Proc(pid, ppid, cmdline)` records supplied by the caller. Nothing calls `ps`, `pgrep`, `os.kill`, or
reads `/proc`, so the dangerous failure modes can be exercised without ever signalling a real process.

Four semantics are modelled.

**1. Liveness is two signals, not one.** A daemon is alive only if *both*:

- its pidfile pid exists in the process table, **and**
- its heartbeat age is within the allowed limit.

Each failure has its own reason string:

- pid is `None` → `"no_pidfile"`
- pid absent from the table → `"no_process"`
- pid present but
  heartbeat too old → `"stale_heartbeat"`
- both fine → `"ok"`

A process that exists but has stopped heartbeating is *wedged*, and a wedged process is judged not
alive. The two signals catch different failures: pid existence catches a crash; heartbeat freshness
catches a hang. `pid_alive(None, table)` is `False`, not an error — a missing pidfile pid is a normal
input.

**2. Restarts need backoff and a budget.** Restarting a crashing daemon immediately, forever, is a
restart storm: it burns resources, spams logs, and can hammer whatever the daemon connects to. So
retries wait longer each time (exponential backoff), and only a limited number of restarts are allowed
inside a sliding time window. Exhausting the budget means give up and flag — never keep looping.

**3. Pattern matching can kill the killer.** The naive way to find a daemon is substring matching over
command lines: "contains `smart-watcher`". The trap: the watcher's *own* command line may contain that
pattern, and so may its *parent shell's* — a restart one-liner like `bash -c python3
scripts/smart-watcher --restart` contains it verbatim. A naive kill list therefore includes the
operator's own shell, and running the restart command terminates the shell running it. The fix: the safe
matcher always excludes both the watcher's own pid **and** its parent pid.

**4. Exactly one instance.** For a given daemon pattern, using the safe matcher:

- 0 matches → `"start"` (start one)
- exactly 1 → `"ok"`
- 2 or more → `"duplicates"` (reported, with
  the records)

Duplicates are *reported*, never silently resolved by killing extras in this lab — deciding which copy
should survive needs information the matcher does not have.

## Key derivations & formulas

### Liveness decision

```text
check_liveness(pidfile_pid, table, age, max_age):
    if pidfile_pid is None:            -> Liveness(False, "no_pidfile")
    elif pidfile_pid not in table:     -> Liveness(False, "no_process")
    elif age > max_age:                -> Liveness(False, "stale_heartbeat")
    else:                              -> Liveness(True,  "ok")
```

`heartbeat_fresh(age, max_age)` is `age <= max_age` — the boundary is *inclusive*: a heartbeat at
exactly the limit is still fresh. Validation is fail-closed: negative ages and non-positive limits raise
`ValueError`, and `Proc` construction itself raises on a non-positive or non-int `pid`, a negative
`ppid`, or an empty or non-string `cmdline`. Note `bool` is rejected where a number is required, via the
shared helpers.

### Backoff

For a 0-based attempt number:

```text
backoff_delay(attempt) = min(cap_s, base_s * factor ** attempt)
```

Defaults: `base_s = 1.0`, `factor = 2.0`, `cap_s = 60.0`. So the default schedule is:

```text
attempt:   0    1    2    3     4     5     6
delay:   1.0  2.0  4.0  8.0  16.0  32.0  60.0   (capped)
```

`backoff_schedule(n, ...)` returns the first `n` delays. A factor of exactly 1 gives a flat schedule and
is allowed; a factor below 1, a negative attempt, or non-positive base/cap raises `ValueError`.

### Restart budget (state machine)

`RestartPolicy(max_restarts=5, window_s=600.0, ...)` keeps a history of restart timestamps.

```text
recent(now)         = [t in history if t > now - window_s]
should_restart(now) = len(recent(now)) < max_restarts
gave_up(now)        = NOT should_restart(now)
```

`record_restart(now)`:

- if the budget is exhausted, raise `RuntimeError` (fail-closed: give up and flag);
- a *failed* record
  does not consume budget;
- otherwise it stores the pruned history plus `now`, and returns
  `backoff_delay(attempt)` where `attempt` = number of restarts already in the window.

Because the window *slides*, restarts age out and the budget recovers with time. A lifetime counter
would eventually forbid restarting a daemon that crashes once a month; the sliding window only forbids
crashing *repeatedly, fast*.

### The safe matcher

```text
find_matches_naive(pattern, table)
    = [p in table if pattern in p.cmdline]

find_matches_safe(pattern, table, self_pid, parent_pid)
    = [p in find_matches_naive(pattern, table)
       if p.pid not in {self_pid, parent_pid}]
```

Same substring semantics; only the exclusion set differs. Empty patterns and non-positive self/parent
pids raise.

## Worked example

From `demo.py`. The fixture table:

| pid | ppid | cmdline |
|-----|------|---------|
| 499 | 1 | `bash -c python3 scripts/smart-watcher --restart` |
| 500 | 499 | `python3 supervisor.py smart-watcher` |
| 700 | 1 | `python3 scripts/smart-watcher` |
| 701 | 1 | `python3 scripts/smart-watcher` |
| 900 | 1 | `python3 scripts/other-daemon` |

Watcher pid = 500, parent pid = 499, pattern = `smart-watcher`, heartbeat limit = 120s.

Liveness:

- pid 700, age 5.0s → `alive=True, reason=ok`
- pid 700, age 999.0s → `alive=False,
  reason=stale_heartbeat`
- pid 4242, age 5.0s → `alive=False, reason=no_process`
- pid None, age 5.0s →
  `alive=False, reason=no_pidfile`

Backoff schedule (base 1, factor 2, cap 60): `[1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]`. A policy with
`max_restarts=3, window_s=600` records restarts at t=0, 10, 20 returning backoffs 1s, 2s, 4s — and at
t=30, `gave_up=True`.

The self-match trap, in numbers:

- Naive kill targets: `[499, 500, 700, 701]` — parent shell, watcher itself, and the two real daemons.
- Safe kill targets: `[700, 701]` — only the real daemons.

Exactly-one-instance on the same table: `"duplicates"`, matching pids `[700, 701]`. Reduced to a single
daemon the status is `"ok"`; with none, `"start"`.

## Trap log

- T1. **"The pid exists, so it's alive."** A hung process keeps its pid forever. Without the heartbeat
  check, the watchdog watches a corpse that still has a number.
- T2. **Excluding only your own pid.**
  The parent shell's command line typically contains the pattern too — it is the line that launched the
  restart. Exclude self *and* parent, always.
- T3. **The grep-trick false comfort.** Hiding the search
  process from its own results does not hide the shell whose command line literally contains the pattern
  text.
- T4. **Restart storms.** Immediate, unlimited restarts convert one bug into continuous load and
  log spam, and mask the original crash under thousands of retries.
- T5. **Lifetime restart counters.**
  A budget that never refills punishes rare, unrelated crashes months apart the same as a crash loop.
  The window must slide.
- T6. **Silently killing duplicate extras.** Two copies might be an old wedged
  one and a healthy new one, or vice versa. Report duplicates; choosing a survivor is a separate
  decision.
- T7. **Practising on real processes.** Kill-list bugs are exactly the bugs you cannot
  afford to discover live. Synthetic tables let the regression fixture prove the naive list contains the
  watcher's own pid — safely.
- T8. **Treating a missing pidfile as an error.** It is a normal state
  (daemon never started, or cleaned up). It maps to a liveness *reason*, not an exception.

## Source flags

- All constants, reason strings, and the matcher/policy semantics above are read from `supervisor.py`.
  [S]
- The test claims — 38 tests passing, including the regression fixture proving the naive kill list
  contains the watcher and parent pids while the safe list contains exactly the real daemon — are from
  the README's test-suite section. [S]
- The mirroring of the live `scripts/forex-watchdog` and the
  operating-notes lesson about a shell being terminated by its own restart command are the README's
  live-bot claims. [M]
- The explanation of *why* a sliding window beats a lifetime counter
  operationally is reasoned here from the `_recent` implementation. [R]

## One-paragraph verdict

This toolkit proves the watchdog's decision logic in isolation: two-signal liveness with distinct failure reasons, capped exponential backoff, a sliding-window restart budget that gives up loudly instead of storming, a safe matcher that provably excludes self and parent where the naive one provably does not, and an exactly-one-instance invariant that reports duplicates instead of guessing. It does not prove anything about real process management — signals, pid reuse, pidfile races, or actual daemon behaviour are all outside the lab by design, because the whole point is that these semantics can be pinned down on synthetic tables before they are trusted near a real process list.
