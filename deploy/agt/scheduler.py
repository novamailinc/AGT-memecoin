"""In-deployment scheduler — replaces the VM cron jobs on AGT.

Phase 13 (AGT packaging skeleton). AGT has NO cron / scheduled jobs
(verified from AGT source: zero "cron" in the Kotlin code; AlarmManager
is used only for restart/watchdog alarms). Every recurring job the bot
relied on from the VM scheduler therefore runs as a loop inside this
deployment.

Job inventory — verified against the live scheduler on 2026-11-22
(cron.list). 19 enabled bot jobs get a loop equivalent here; 7 disabled
bot jobs are listed with enabled=False and are NOT scheduled; system
jobs (feed-pulse x24, deterministic-doctor, profile-image, heartbeat)
and AgentLink jobs are NOT part of this deployment and are excluded.

Status per job:
  * ready      — the repo script runs as-is inside the deployment.
  * blocked    — the job cannot run on AGT yet; the reason is recorded
                 and the loop logs it loudly instead of silently
                 skipping. Unblocking is a finalize-pass item.
  * agent-only — the job's deliverable is a chat message composed by
                 the main agent (daily recap, CEO weekly, CEO verdict).
                 No loop can write that message; the scheduler records
                 the job as agent-only and never pretends it ran.

Daily/weekly times are Africa/Lagos wall time. Lagos is UTC+1 all
year (no DST), so the scheduler uses a fixed +1h offset and never
depends on tzdata, which the AGT bootstrap prefix may not ship.

Design:
  * One process, one loop, 30s tick. Jobs run sequentially in the
    loop so two jobs never overlap the same state files; a job whose
    previous run is still going is skipped and the skip is logged.
  * Each run is a subprocess with cwd=repo root and FOREX_AGENT_HOME
    set. Stdout/stderr append to $FOREX_AGENT_HOME/log/
    agt-scheduler-<job>.log. Results land in scheduler_state.json.
  * The scheduler never edits code, ledgers, or killswitch state.
    Repairs stay with the agent under the self-repair protocol.
  * The watchdog job additionally revives the resident supervisor:
    if supervisor_state.json is older than 180s, it spawns
    `python3 scripts/supervisor` detached (the supervisor then
    restores every daemon itself).
  * The backup job runs the local backup only. The Drive upload step
    has no off-phone story on AGT yet and is skipped loudly, never
    attempted and failed silently.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
LAGOS = timezone(timedelta(hours=1))  # Africa/Lagos: UTC+1, no DST
TICK_S = 30.0
SUPERVISOR_STALE_S = 180.0

# ---------------------------------------------------------------------------
# Job table. "command" is argv with {repo} / {venv} placeholders, or a list
# of steps (each an argv) for multi-step jobs. None => no runnable command
# (agent-only or blocked; see "status" + "blocked_reason").
# Cadence: {"kind": "interval", "every_s": N} /
#          {"kind": "daily", "time": "HH:MM"} /
#          {"kind": "weekly", "dow": "Mon".."Sun", "time": "HH:MM"} /
#          {"kind": "runonce", "at": "YYYY-MM-DDTHH:MM:SS"} (Lagos wall).
# "source" is the VM cron id this loop replaces.
# ---------------------------------------------------------------------------
JOBS = (
    # --- 5-minute loops ---
    {"id": "forex-watchdog", "source": "forex-watchdog",
     "title": "Paper bot watchdog (every 5 min)",
     "cadence": {"kind": "interval", "every_s": 300},
     "command": ["python3", "scripts/forex-watchdog"],
     "timeout_s": 300, "status": "ready",
     "notes": "Also revives the resident supervisor when its state "
              "goes stale (see _ensure_supervisor)."},
    {"id": "helius-429-watch", "source": "helius-429-watch",
     "title": "Helius 429-spike watch (every 5 min)",
     "cadence": {"kind": "interval", "every_s": 300},
     "command": ["python3", "scripts/helius-429-watch"],
     "timeout_s": 300, "status": "ready",
     "notes": "Exits 1 while a spike is active; Telegram ping on "
              "transition into a spike is sent by the script itself."},
    # --- 30-minute loop ---
    {"id": "bot-self-audit", "source": "bot-self-audit",
     "title": "Bot self-audit + auto-repair (30 min)",
     "cadence": {"kind": "interval", "every_s": 1800},
     "command": ["python3", "scripts/bot-self-audit"],
     "timeout_s": 1500, "status": "ready",
     "notes": "Repairs what is safely repairable; appends one JSON "
              "line per run to self_audit.jsonl. Never edits code, "
              "killswitch state, or ledgers."},
    # --- hourly loops ---
    {"id": "forex-backup-hourly", "source": "forex-backup-hourly",
     "title": "Paper bot backup (hourly)",
     "cadence": {"kind": "interval", "every_s": 3600},
     "command": None,  # steps below (backup, then verify it)
     "steps": [
         ["python3", "scripts/forex-backup"],
         ["python3", "deploy/agt/backup_verify.py", "--verify-latest"],
     ],
     "timeout_s": 1800, "status": "ready",
     "notes": "Local backup only on AGT. The Google Drive upload step "
              "is skipped: the VM-only Drive upload has no off-phone "
              "backup story on AGT yet (finalize pass). The second "
              "step verifies the fresh archive (checksum, no "
              "credential material, ledgers parse, killswitch "
              "shadow, DB integrity) and fails the job loudly when "
              "the archive is bad — a bad backup must be loud "
              "within the hour, not discovered at restore time."},
    {"id": "lane-bandit-shadow", "source": "lane-bandit-shadow",
     "title": "Lane bandit shadow sync (hourly)",
     "cadence": {"kind": "interval", "every_s": 3600},
     "command": ["python3", "scripts/lane-bandit-shadow"],
     "timeout_s": 900, "status": "ready",
     "notes": "Shadow mode only: logs what lane it WOULD allocate to; "
              "changes nothing about entries."},
    {"id": "nadir-history-recorder", "source": "nadir-history-recorder",
     "title": "Nadir history recorder (hourly)",
     "cadence": {"kind": "interval", "every_s": 3600},
     "command": ["python3", "scripts/nadir-history-recorder"],
     "timeout_s": 900, "status": "ready",
     "notes": "Hourly price snapshots for the nadir watchlist; builds "
              "the 30-day OHLCV history."},
    {"id": "steady-outcome-tracker", "source": "steady-outcome-tracker",
     "title": "Steady outcome tracker (hourly)",
     "cadence": {"kind": "interval", "every_s": 3600},
     "command": ["python3", "scripts/steady-outcome-tracker"],
     "timeout_s": 900, "status": "ready",
     "notes": "Measures 1h/6h/24h multiples after steady signals fire."},
    {"id": "steady-scan", "source": "steady-scan",
     "title": "Steady scan (hourly)",
     "cadence": {"kind": "interval", "every_s": 3600},
     "command": ["python3", "scripts/steady-scan"],
     "timeout_s": 900, "status": "ready",
     "notes": "Mean-reversion setups on $50K-$10M tokens. Paper-only "
              "Telegram signals; killswitch + consistency gated."},
    # --- 6-hour loop ---
    {"id": "entry-learner-watch", "source": "entry-learner-watch",
     "title": "Entry-learner verdict watcher",
     "cadence": {"kind": "interval", "every_s": 21600},
     "command": None,
     "timeout_s": 300, "status": "blocked",
     "blocked_reason": "checker script lives outside the repo "
                       "(workspace/entry-learner-watch/check.py); it "
                       "must be vendored into the repo or the job "
                       "dropped in the finalize pass.",
     "notes": "One-shot purpose: fire once when the entry learner "
              "produces its first tuning proposals."},
    # --- daily jobs (Africa/Lagos wall time) ---
    {"id": "degen-learner-nightly", "source": "degen-learner-nightly",
     "title": "Degen learner — nightly auto-tune",
     "cadence": {"kind": "daily", "time": "03:00"},
     "command": ["python3", "scripts/degen-learner"],
     "timeout_s": 900, "status": "ready",
     "notes": "Adjusts degen feed score weights within guardrails; "
              "report only when it makes tuning changes."},
    {"id": "smart-registry-rotate", "source": "smart-registry-rotate",
     "title": "Smart-wallet registry rotation (14d)",
     "cadence": {"kind": "daily", "time": "03:52"},
     "command": ["python3", "scripts/smart-registry-rotate"],
     "timeout_s": 900, "status": "ready",
     "notes": "Idempotent: no-ops unless 14+ days elapsed since the "
              "last rotation."},
    {"id": "bot-test-suites-daily", "source": "bot-test-suites-daily",
     "title": "Bot core test suites (daily 04:10 WAT)",
     "cadence": {"kind": "daily", "time": "04:10"},
     "command": None,  # steps below (multi-step job)
     "steps": [
         ["{venv}", "tests/test_grad.py"],
         ["{venv}", "tests/test_grad_raw.py"],
         ["{venv}", "tests/test_killswitch.py"],
         ["{venv}", "tests/test_supervisor.py"],
         ["{venv}", "tests/test_ceo.py"],
         ["{venv}", "tests/test_mirror.py"],
         ["{venv}", "tests/test_veto_log.py"],
         ["{venv}", "tests/test_instant_safety.py"],
         ["{venv}", "tests/test_safety_gates.py"],
     ],
     "timeout_s": 1800, "status": "ready",
     "notes": "Suites run STRICTLY SEQUENTIALLY, never two at once "
              "(concurrent full-suite runs have flaked on this VM). "
              "Report only on failure; repairs happen under the "
              "self-repair protocol, never inside this job."},
    {"id": "github-repo-sync", "source": "github-repo-sync",
     "title": "GitHub repo sync (meme-project)",
     "cadence": {"kind": "daily", "time": "04:52"},
     "command": None,
     "timeout_s": 600, "status": "blocked",
     "blocked_reason": "sync script lives outside the repo "
                       "(workspace/github-deploy/push_all.sh) and "
                       "authenticates via the VM authd vault "
                       "credential, which does not exist on AGT. Needs "
                       "an AGT git credential (GITHUB_TOKEN) + vendored "
                       "push script in the finalize pass.",
     "notes": "Pushes bot code to repo root, supporting code under "
              "tools/. Secrets are never pushed (secret guard)."},
    {"id": "killswitch-check", "source": "killswitch-check",
     "title": "Kill switch check (daily)",
     "cadence": {"kind": "daily", "time": "08:52"},
     "command": ["python3", "scripts/killswitch-check"],
     "timeout_s": 900, "status": "ready",
     "notes": "Evaluates rolling expectancy per lane; halts negative "
              "lanes; Telegram alert on new halts. Silent otherwise."},
    {"id": "nadir-live-scan", "source": "nadir-live-scan",
     "title": "Nadir live scan (daily)",
     "cadence": {"kind": "daily", "time": "09:52"},
     "command": ["python3", "scripts/nadir-live-scan"],
     "timeout_s": 900, "status": "ready",
     "notes": "13 watchlist tokens, 70-90% drawdown setups. Paper-only "
              "signals via Telegram, never trades."},
    {"id": "daily-trading-recap", "source": "daily-trading-recap",
     "title": "Daily trading recap",
     "cadence": {"kind": "daily", "time": "21:52"},
     "command": None,
     "timeout_s": 900, "status": "agent-only",
     "notes": "Deliverable is a chat message composed by the main "
              "agent from the live ledgers. No in-deployment loop can "
              "write it; the numbers it would use are served by the "
              "status dashboard instead."},
    # --- weekly jobs ---
    {"id": "ceo-weekly-report", "source": "ceo-weekly-report",
     "title": "CEO weekly report (Fridays)",
     "cadence": {"kind": "weekly", "dow": "Fri", "time": "17:52"},
     "command": None,
     "timeout_s": 900, "status": "agent-only",
     "notes": "Deliverable is a chat message compiled by the main "
              "agent (CEO ledger + lane snapshot + self-audit + memory "
              "notes). Dashboard serves the underlying figures."},
    {"id": "weekly-paper-review", "source": "weekly-paper-review",
     "title": "Weekly paper-trading review (Sundays)",
     "cadence": {"kind": "weekly", "dow": "Sun", "time": "20:52"},
     "command": ["python3", "scripts/weekly-paper-review"],
     "timeout_s": 1200, "status": "ready",
     "notes": "Script output is journaled to the scheduler log; the "
              "chat delivery of the review stays with the main agent."},
    # --- one-shot (not a loop) ---
    {"id": "ceo-mode-verdict", "source": "ceo-mode-verdict",
     "title": "CEO mode final verdict — fund / fix / kill",
     "cadence": {"kind": "runonce", "at": "2026-12-20T09:00:00"},
     "command": None,
     "timeout_s": 900, "status": "agent-only",
     "notes": "One-shot calendar item, 2026-12-20 09:00 WAT: the final "
              "CEO-mode verdict (FUND / KEEP FIXING / KILL) delivered "
              "by the main agent. Not a loop; listed so the inventory "
              "is complete."},
)

# Disabled bot cron jobs (verified 2026-11-22): listed, never scheduled.
DISABLED_JOBS = (
    {"id": "degen-2m-daily", "source": "degen-2m-daily",
     "reason": "disabled in the VM scheduler (degen $2M challenge "
               "daily report)."},
    {"id": "degen-challenge-hourly", "source": "degen-challenge-hourly",
     "reason": "disabled in the VM scheduler (degen challenge hourly "
               "report)."},
    {"id": "nadir-api-watch", "source": "nadir-api-watch",
     "reason": "disabled in the VM scheduler (Nadir API watch, Phase 1 "
               "trigger)."},
    {"id": "nightly-grader", "source": "nightly-grader",
     "reason": "disabled in the VM scheduler (nightly wallet grader)."},
    {"id": "spark-feed", "source": "spark-feed",
     "reason": "disabled in the VM scheduler (spark ignition feed)."},
    {"id": "uagt-api-watch", "source": "uagt-api-watch",
     "reason": "disabled in the VM scheduler (Universal-AGT "
               "availability watch)."},
    {"id": "wallet-hunt-guard", "source": "wallet-hunt-guard",
     "reason": "disabled in the VM scheduler (wallet-hunt parser "
               "resurrection guard)."},
)

# Jobs that are NOT part of this deployment (excluded deliberately):
#   * system jobs: feed-pulse-00..23, deterministic-doctor,
#     profile-image, heartbeat (runtime/system, not the bot).
#   * agentlink-lean-watchdog-inbox, agentlink-watch-2-2-2 (AgentLink
#     service, not the trading deployment).

DOW = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5,
       "Sun": 6}


def home_dir():
    return os.environ.get("FOREX_AGENT_HOME") or os.path.join(
        os.path.expanduser("~"), ".forex-agent")


def state_path(home=None):
    return os.path.join(home or home_dir(), "scheduler_state.json")


def log_path(job_id, home=None):
    return os.path.join(home or home_dir(), "log",
                        "agt-scheduler-%s.log" % job_id)


def _parse_hhmm(text):
    hh, mm = text.split(":")
    return int(hh), int(mm)


def _lagos_day_start(now_lagos):
    return now_lagos.replace(hour=0, minute=0, second=0, microsecond=0)


def next_due(job, now=None, last_run=None):
    """Next due time (aware datetime, Lagos) for a job.

    now: aware datetime (defaults to now, Lagos). last_run: epoch
    seconds of the last completed run, or None. Interval jobs are due
    every_s after last_run (or immediately when never run). Daily /
    weekly jobs are due at their next wall-clock occurrence strictly
    after last_run (or after now when never run). Runonce jobs are due
    at their fixed time, once.
    """
    now = now or datetime.now(LAGOS)
    cad = job["cadence"]
    if cad["kind"] == "interval":
        if last_run is None:
            return now
        return datetime.fromtimestamp(
            last_run + cad["every_s"], tz=LAGOS)
    if cad["kind"] == "runonce":
        at = datetime.strptime(cad["at"], "%Y-%m-%dT%H:%M:%S")
        return at.replace(tzinfo=LAGOS)
    hh, mm = _parse_hhmm(cad["time"])
    if cad["kind"] == "daily":
        base = last_run or now.timestamp()
        base_dt = datetime.fromtimestamp(base, tz=LAGOS)
        cand = base_dt.replace(hour=hh, minute=mm, second=0,
                               microsecond=0)
        if cand.timestamp() <= base:
            cand = cand + timedelta(days=1)
        return cand
    if cad["kind"] == "weekly":
        base = last_run or now.timestamp()
        base_dt = datetime.fromtimestamp(base, tz=LAGOS)
        want = DOW[cad["dow"]]
        cand = base_dt.replace(hour=hh, minute=mm, second=0,
                               microsecond=0)
        delta = (want - cand.weekday()) % 7
        cand = cand + timedelta(days=delta)
        if cand.timestamp() <= base:
            cand = cand + timedelta(days=7)
        return cand
    raise ValueError("unknown cadence kind %r" % cad["kind"])


def due_jobs(now=None, state=None):
    """Jobs due at `now` (aware datetime, Lagos). state maps job id ->
    last-run epoch. Runonce jobs are due once and then never again
    (state records completion)."""
    now = now or datetime.now(LAGOS)
    state = state or {}
    due = []
    for job in JOBS:
        last = (state.get(job["id"]) or {}).get("last_run")
        if job["cadence"]["kind"] == "runonce" and last is not None:
            continue
        if next_due(job, now, last) <= now:
            due.append(job)
    return due


def load_state(home=None):
    try:
        with open(state_path(home)) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(state, home=None):
    path = state_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(state, fh, sort_keys=True)
    os.replace(tmp, path)


def venv_python():
    """Deployment venv python when present, else the current one."""
    cand = os.path.join(REPO, ".venv", "bin", "python")
    if os.path.isfile(cand) and os.access(cand, os.X_OK):
        return cand
    return sys.executable


def resolve_argv(argv):
    return [a.replace("{repo}", REPO).replace("{venv}", venv_python())
            for a in argv]


def run_job(job, home=None):
    """Run one job now. Returns (rc, note). rc 0 = ok; 2 = skipped
    (blocked / agent-only); 1 = command failed. Never raises for a
    failed command — the failure is recorded in state + log."""
    home = home or home_dir()
    state = load_state(home)
    entry = state.setdefault(job["id"], {})
    started = time.time()
    entry["last_started"] = started
    if job["status"] in ("blocked", "agent-only"):
        reason = job.get("blocked_reason") or (
            "agent-only job: deliverable is a chat message composed "
            "by the main agent; no in-deployment loop runs it.")
        entry.update({"last_run": started, "last_rc": 2,
                      "last_note": "skipped (%s): %s"
                                 % (job["status"], reason),
                      "runs": entry.get("runs", 0) + 1})
        save_state(state, home)
        return 2, entry["last_note"]
    steps = job.get("steps") or [job["command"]]
    env = dict(os.environ)
    env["FOREX_AGENT_HOME"] = home
    logp = log_path(job["id"], home)
    os.makedirs(os.path.dirname(logp), exist_ok=True)
    rc = 0
    notes = []
    with open(logp, "a") as logf:
        for argv in steps:
            resolved = resolve_argv(argv)
            logf.write("=== %s run %s: %s ===\n" % (
                job["id"],
                datetime.now(LAGOS).isoformat(), " ".join(resolved)))
            logf.flush()
            try:
                proc = subprocess.run(
                    resolved, cwd=REPO, env=env, stdout=logf,
                    stderr=subprocess.STDOUT,
                    timeout=job["timeout_s"])
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                rc = 124
                logf.write("TIMEOUT after %ds\n" % job["timeout_s"])
            notes.append("%s rc=%d" % (resolved[1] if len(resolved) > 1
                                       else resolved[0], rc))
            if rc != 0:
                break
    entry.update({"last_run": started,
                  "last_finished": time.time(),
                  "last_rc": rc,
                  "last_note": "; ".join(notes),
                  "runs": entry.get("runs", 0) + 1})
    save_state(state, home)
    return rc, "; ".join(notes)


def _supervisor_alive(home):
    """True when supervisor_state.json was written recently."""
    try:
        mtime = os.path.getmtime(os.path.join(home,
                                              "supervisor_state.json"))
    except OSError:
        return False
    return (time.time() - mtime) < SUPERVISOR_STALE_S


def _ensure_supervisor(home):
    """Revive the resident supervisor when its state goes stale.

    The supervisor restores every daemon itself on start. Spawned
    detached; children survive this process. Returns True when a
    (re)start was issued.
    """
    if _supervisor_alive(home):
        return False
    env = dict(os.environ)
    env["FOREX_AGENT_HOME"] = home
    logp = os.path.join(home, "log", "supervisor.log")
    os.makedirs(os.path.dirname(logp), exist_ok=True)
    with open(logp, "a") as logf:
        subprocess.Popen(
            [sys.executable, "scripts/supervisor"], cwd=REPO, env=env,
            stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True)
    return True


def tick(home=None):
    """One scheduler tick: run every due job, sequentially."""
    home = home or home_dir()
    state = load_state(home)
    now = datetime.now(LAGOS)
    ran = []
    for job in due_jobs(now, state):
        rc, note = run_job(job, home)
        ran.append((job["id"], rc, note))
        if job["id"] == "forex-watchdog" and rc == 0:
            if _ensure_supervisor(home):
                ran.append(("supervisor-revive", 0,
                            "supervisor state stale; respawned"))
        state = load_state(home)  # run_job saved its own entry
    return ran


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    home = home_dir()
    if argv == ["--list"]:
        print(json.dumps(
            {"jobs": [{k: v for k, v in j.items()}
                      for j in JOBS],
             "disabled": list(DISABLED_JOBS)}, indent=2,
            default=str))
        return 0
    if argv == ["--dry-run"]:
        now = datetime.now(LAGOS)
        state = load_state(home)
        for job in JOBS:
            last = (state.get(job["id"]) or {}).get("last_run")
            print("%-24s %-10s next_due=%s status=%s" % (
                job["id"], job["cadence"]["kind"],
                next_due(job, now, last).isoformat(),
                job["status"]))
        print("disabled (not scheduled): %s" % ", ".join(
            d["id"] for d in DISABLED_JOBS))
        return 0
    if len(argv) == 2 and argv[0] == "--once":
        job = next((j for j in JOBS if j["id"] == argv[1]), None)
        if job is None:
            print("unknown job: %s" % argv[1])
            return 2
        rc, note = run_job(job, home)
        print("%s rc=%d %s" % (job["id"], rc, note))
        return 0 if rc in (0, 2) else 1
    if argv == ["--run"]:
        print("agt-scheduler running (tick %ss, home %s)"
              % (TICK_S, home), flush=True)
        while True:
            try:
                for job_id, rc, note in tick(home):
                    print("%s rc=%d %s" % (job_id, rc, note),
                          flush=True)
            except Exception as exc:  # never die on a tick error
                print("scheduler tick error: %r" % exc, flush=True)
            time.sleep(TICK_S)
    print("usage: scheduler.py [--list | --dry-run | --once <job> | "
          "--run]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
