#!/usr/bin/env python3
"""AGT packaging tests — Phase 13 skeleton (2026-11-22).

Covers deploy/agt/: manifest validity, env-only credential startup
(missing credential fails closed), scheduler-loop enumeration
completeness vs the 19 enabled bot cron jobs, dashboard serving real
state (no invented fields), no-secrets grep over deploy/, Python
environment validation, DB init + migrations, and the start.sh smoke
test.

No network. No live files are edited. Plain script — run directly
with the venv python, not via pytest. Fake credential values used
here are well-known public placeholders (RFC 4122 example UUID, nil
UUID, short fake token), never real values and never the "TEST-"
placeholder prefix — credentials.py refuses TEST- values outright
(2026-11-22), and the GitHub secret guard allowlists exactly these
UUIDs. Every test that needs a home uses a scratch temp dir, and
the dashboard/supervisor tests never run against the real
~/.forex-agent.
"""

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGT = os.path.join(REPO, "deploy", "agt")
VENV_PY = os.path.join(os.path.dirname(REPO), ".venv", "bin", "python")
if not os.path.isfile(VENV_PY):
    VENV_PY = sys.executable

sys.path.insert(0, AGT)

import credentials as CREDS          # noqa: E402
import scheduler as SCHED            # noqa: E402
import status_server as DASH         # noqa: E402
import db_init as DBINIT             # noqa: E402
import validate_env as VENV          # noqa: E402

fails = []
skips = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def skip(name, reason):
    print("SKIP " + name + " (" + reason + ")")
    skips.append(name + ": " + reason)


FAKE_ENV = {
    'HELIUS_API_KEY_1': 'fixture-helius-source-1',
    'HELIUS_API_KEY_2': 'fixture-helius-source-2',
    'HELIUS_API_KEY_3': 'fixture-helius-source-3',
    # Well-known public placeholder shapes, never real values and
    # never the "TEST-" placeholder prefix: credentials.py refuses
    # TEST- values outright (2026-11-22), and the GitHub secret
    # guard allowlists exactly these two UUIDs (RFC 4122 example,
    # nil UUID) — any other UUID-shaped fake would block the push.
    # The Telegram fake keeps the digits: prefix but is far too
    # short to match the bot-token shape the guard scans for.
    "HELIUS_API_KEY": "123e4567-e89b-12d3-a456-426614174000",
    "HELIUS_API_KEY_DISCOVERY": "00000000-0000-0000-0000-000000000000",
    "TELEGRAM_BOT_TOKEN": "123456789:fake-telegram-token",
    "ALCHEMY_API_KEY": "fake-alchemy-key",
    "CHAINSTACK_ENDPOINT": "https://example.invalid/fake-chainstack",
    "HELIUS_ALERT_CHAT_ID": "123456789",
}

# ---------------- manifest validity ----------------
with open(os.path.join(AGT, "manifest.json")) as fh:
    manifest = json.load(fh)

check("manifest: parses as JSON with required top-level fields",
      all(k in manifest for k in ("repo_url", "branch", "start_command",
                                  "port", "required_env", "optional_env",
                                  "mode", "mainnet_enabled")))
check("manifest: repo URL is the owner AGT-memecoin repo",
      manifest["repo_url"] == "https://github.com/novamailinc/AGT-memecoin")
check("manifest: branch is main",
      manifest["branch"] == "main")
check("manifest: start command is the AGT start script",
      manifest["start_command"] == "bash deploy/agt/start.sh"
      and os.path.isfile(os.path.join(REPO, "deploy", "agt",
                                     "start.sh")))
check("manifest: port is a valid TCP port",
      isinstance(manifest["port"], int)
      and 1 <= manifest["port"] <= 65535)
check("manifest: paper only, mainnet disabled",
      manifest["mode"] == "paper" and manifest["mainnet_enabled"] is False)
check("manifest: required env matches revised complete provider profile",
      {e["name"] for e in manifest["required_env"]} == set(CREDS.REQUIRED_ENVS))
check("manifest: env entries carry names only, never values",
      all("value" not in e and "secret" not in e
          for e in manifest["required_env"] + manifest["optional_env"]))
check("manifest: env list matches the credentials module inventory",
      {e["name"] for e in manifest["required_env"] + manifest["optional_env"]}
      == {c["env"] for c in CREDS.CREDENTIALS})
check("manifest: deploy kind is GitHub URL (the only git deploy AGT "
      "supports)",
      manifest.get("deploy_kind") == "github_url")

# ---------------- credentials: env-only startup ----------------
check("credentials: full fake env passes the preflight",
      set(CREDS.check_credentials(FAKE_ENV)) == set(CREDS.present(FAKE_ENV))
      and len(CREDS.check_credentials(FAKE_ENV)) == len(FAKE_ENV))
check("credentials: missing ALCHEMY_API_KEY fails closed",
      CREDS.missing_required(
          {k: v for k, v in FAKE_ENV.items()
           if k != "ALCHEMY_API_KEY"}) == ["ALCHEMY_API_KEY"])
check("credentials: missing TELEGRAM_BOT_TOKEN fails closed",
      CREDS.missing_required(
          {k: v for k, v in FAKE_ENV.items()
           if k != "TELEGRAM_BOT_TOKEN"}) == ["TELEGRAM_BOT_TOKEN"])
check("credentials: empty env names every required provider/control source",
      CREDS.missing_required({}) == list(CREDS.REQUIRED_ENVS))
try:
    CREDS.check_credentials({})
    check("credentials: check_credentials raises on empty env", False)
except CREDS.CredentialError as exc:
    check("credentials: check_credentials raises on empty env", True)
    check("credentials: error names the missing variables, no values",
          "ALCHEMY_API_KEY" in str(exc)
          and "TELEGRAM_BOT_TOKEN" in str(exc)
          and "TEST-" not in str(exc))
check("credentials: legacy source alone cannot satisfy numbered profile",
      set(CREDS.missing_required({"HELIUS_API_KEY":"fixture","ALCHEMY_API_KEY":"fixture","TELEGRAM_BOT_TOKEN":"fixture"})) ==
      {"CHAINSTACK_ENDPOINT",*(f"HELIUS_API_KEY_{i}" for i in (1,2,3))})
check("credentials: vault surrogate is unavailable without authd",
      CREDS.vault_surrogate_available() is False
      or os.path.exists("/run/hatch/auth/authd.sock"))

# File bridge: env values land in the runtime paths, 0600, names only.
with tempfile.TemporaryDirectory() as tmp:
    home = os.path.join(tmp, ".forex-agent")
    bridged = CREDS.bridge_files_from_env(home, FAKE_ENV)
    names = [b[0] for b in bridged]
    check("credentials: bridge writes the file-only readers' paths",
          "HELIUS_API_KEY" in names
          and "TELEGRAM_BOT_TOKEN" in names
          and "HELIUS_API_KEY_DISCOVERY" in names)
    import stat as _stat
    ok_modes = True
    ok_values = True
    for name, rel in bridged:
        if rel.startswith("alchemy"):
            continue  # target is one level above home; checked below
        p = os.path.join(home, rel)
        if _stat.S_IMODE(os.stat(p).st_mode) & 0o077:
            ok_modes = False
        if open(p).read().strip() != FAKE_ENV[name]:
            ok_values = False
    check("credentials: bridged files are mode 0600", ok_modes)
    check("credentials: bridged files carry the env values", ok_values)
    alch = os.path.join(tmp, "alchemy", "api_key")
    check("credentials: Alchemy bridge lands at $HOME/alchemy/api_key",
          os.path.isfile(alch)
          and open(alch).read().strip() == FAKE_ENV["ALCHEMY_API_KEY"])
    # SEED-ONLY (2026-11-22): re-bridging an already-seeded home
    # overwrites nothing, even with different values.
    bridged2 = CREDS.bridge_files_from_env(
        home, {"HELIUS_API_KEY": "x", "TELEGRAM_BOT_TOKEN": "y"})
    check("credentials: bridge never overwrites an existing file",
          bridged2 == []
          and open(os.path.join(home, "helius", "api_key")).read().strip()
          == FAKE_ENV["HELIUS_API_KEY"]
          and open(os.path.join(home, "telegram", "bot_token")
                   ).read().strip() == FAKE_ENV["TELEGRAM_BOT_TOKEN"])
    # Subset env bridges only the present vars (skips absent).
    with tempfile.TemporaryDirectory() as tmp2:
        home2 = os.path.join(tmp2, ".forex-agent")
        bridged3 = CREDS.bridge_files_from_env(
            home2, {"HELIUS_API_KEY": FAKE_ENV["HELIUS_API_KEY"],
                    "TELEGRAM_BOT_TOKEN": FAKE_ENV["TELEGRAM_BOT_TOKEN"]})
        check("credentials: bridge skips absent env vars",
              [b[0] for b in bridged3] == ["HELIUS_API_KEY",
                                          "TELEGRAM_BOT_TOKEN"])

# Placeholder guard (2026-11-22): TEST- values are refused at the
# check and never bridged, so a test run can never plant a dead key
# in a live home again.
check("credentials: TEST- placeholder values are detected",
      CREDS.placeholder_values(
          {"HELIUS_API_KEY": "TEST-HELIUS-KEY-AAAAAAAA",
           "TELEGRAM_BOT_TOKEN": "TEST-TELEGRAM-TOKEN-CCCCCCCC"})
      == ["HELIUS_API_KEY", "TELEGRAM_BOT_TOKEN"])
try:
    CREDS.check_credentials(
        {**FAKE_ENV, "HELIUS_API_KEY": "TEST-HELIUS-KEY-AAAAAAAA",
         "TELEGRAM_BOT_TOKEN": "TEST-TELEGRAM-TOKEN-CCCCCCCC",
         "ALCHEMY_API_KEY": "fake-alchemy-key"})
    check("credentials: check_credentials raises on TEST- values", False)
except CREDS.CredentialError as exc:
    check("credentials: check_credentials raises on TEST- values", True)
    check("credentials: placeholder error names the vars, no values",
          "HELIUS_API_KEY" in str(exc)
          and "TELEGRAM_BOT_TOKEN" in str(exc)
          and "TEST-" not in str(exc))
with tempfile.TemporaryDirectory() as tmp:
    home = os.path.join(tmp, ".forex-agent")
    bridged = CREDS.bridge_files_from_env(
        home, {"HELIUS_API_KEY": "TEST-HELIUS-KEY-AAAAAAAA",
               "TELEGRAM_BOT_TOKEN": "TEST-TELEGRAM-TOKEN-CCCCCCCC"})
    check("credentials: bridge never writes a TEST- value",
          bridged == []
          and not os.path.exists(os.path.join(home, "helius", "api_key"))
          and not os.path.exists(
              os.path.join(home, "telegram", "bot_token")))

# Preflight CLI never prints a value.
proc = subprocess.run(
    [sys.executable, os.path.join(AGT, "credentials.py"), "--preflight"],
    env={**os.environ, **FAKE_ENV, "FOREX_AGENT_HOME":
         tempfile.mkdtemp()}, capture_output=True, text=True)
check("credentials: --preflight exits 0 with env-only credentials",
      proc.returncode == 0)
check("credentials: --preflight output contains no credential value",
      all(v not in proc.stdout + proc.stderr for v in FAKE_ENV.values()))
proc = subprocess.run(
    [sys.executable, os.path.join(AGT, "credentials.py"), "--preflight"],
    env={k: v for k, v in os.environ.items()
         if k not in ("HELIUS_API_KEY", "TELEGRAM_BOT_TOKEN",
                      "ALCHEMY_API_KEY")},
    capture_output=True, text=True)
check("credentials: --preflight exits 1 with credentials missing",
      proc.returncode == 1
      and "ALCHEMY_API_KEY" in proc.stdout + proc.stderr)
# Home guard (2026-11-22): preflight never defaults to the live
# ~/.forex-agent home — FOREX_AGENT_HOME must be set explicitly.
env_no_home = {k: v for k, v in os.environ.items()
               if k != "FOREX_AGENT_HOME"}
env_no_home.update(FAKE_ENV)
proc = subprocess.run(
    [sys.executable, os.path.join(AGT, "credentials.py"), "--preflight"],
    env=env_no_home, capture_output=True, text=True)
check("credentials: --preflight fails closed without FOREX_AGENT_HOME",
      proc.returncode == 1
      and "FOREX_AGENT_HOME" in proc.stdout + proc.stderr)

# ---------------- scheduler enumeration ----------------
# Expected inventory verified against the live scheduler (cron.list)
# on 2026-11-22: 19 enabled bot jobs + 7 disabled bot jobs.
EXPECTED_ENABLED = {
    "bot-self-audit": ("interval", 1800),
    "bot-test-suites-daily": ("daily", "04:10"),
    "ceo-mode-verdict": ("runonce", "2026-12-20T09:00:00"),
    "ceo-weekly-report": ("weekly", ("Fri", "17:52")),
    "daily-trading-recap": ("daily", "21:52"),
    "degen-learner-nightly": ("daily", "03:00"),
    "entry-learner-watch": ("interval", 21600),
    "forex-backup-hourly": ("interval", 3600),
    "forex-watchdog": ("interval", 300),
    "github-repo-sync": ("daily", "04:52"),
    "helius-429-watch": ("interval", 300),
    "killswitch-check": ("daily", "08:52"),
    "lane-bandit-shadow": ("interval", 3600),
    "nadir-history-recorder": ("interval", 3600),
    "nadir-live-scan": ("daily", "09:52"),
    "smart-registry-rotate": ("daily", "03:52"),
    "steady-outcome-tracker": ("interval", 3600),
    "steady-scan": ("interval", 3600),
    "weekly-paper-review": ("weekly", ("Sun", "20:52")),
}
EXPECTED_DISABLED = {"degen-2m-daily", "degen-challenge-hourly",
                     "nadir-api-watch", "nightly-grader", "spark-feed",
                     "uagt-api-watch", "wallet-hunt-guard"}
EXCLUDED = {"heartbeat"} | {"feed-pulse-%02d" % i for i in range(24)} | {
    "deterministic-doctor", "profile-image",
    "agentlink-lean-watchdog-inbox", "agentlink-watch-2-2-2"}

job_ids = [j["id"] for j in SCHED.JOBS]
check("scheduler: exactly the 19 enabled bot cron jobs are enumerated",
      len(SCHED.JOBS) == 19 and set(job_ids) == set(EXPECTED_ENABLED))
check("scheduler: no system or AgentLink jobs leak into the table",
      not (set(job_ids) & EXCLUDED))
check("scheduler: every job has a source cron id + title + cadence",
      all(j.get("source") and j.get("title") and j.get("cadence")
          for j in SCHED.JOBS))
for jid, (kind, val) in EXPECTED_ENABLED.items():
    job = next(j for j in SCHED.JOBS if j["id"] == jid)
    if kind == "interval":
        ok = (job["cadence"]["kind"] == "interval"
              and job["cadence"]["every_s"] == val)
    elif kind == "daily":
        ok = (job["cadence"]["kind"] == "daily"
              and job["cadence"]["time"] == val)
    elif kind == "weekly":
        ok = (job["cadence"]["kind"] == "weekly"
              and job["cadence"]["dow"] == val[0]
              and job["cadence"]["time"] == val[1])
    else:
        ok = (job["cadence"]["kind"] == "runonce"
              and job["cadence"]["at"] == val)
    check("scheduler: %s cadence matches the VM cron (%s)" % (jid, kind),
          ok)
check("scheduler: all 7 disabled bot jobs are listed, not scheduled",
      {d["id"] for d in SCHED.DISABLED_JOBS} == EXPECTED_DISABLED)
ready = [j for j in SCHED.JOBS if j["status"] == "ready"]
blocked = [j for j in SCHED.JOBS if j["status"] == "blocked"]
agent_only = [j for j in SCHED.JOBS if j["status"] == "agent-only"]
check("scheduler: 14 ready, 2 blocked, 3 agent-only",
      len(ready) == 14 and len(blocked) == 2 and len(agent_only) == 3)
check("scheduler: blocked jobs name their blocker",
      all(j.get("blocked_reason") for j in blocked)
      and {j["id"] for j in blocked} == {"github-repo-sync",
                                        "entry-learner-watch"})
check("scheduler: agent-only jobs are the chat-composed reports",
      {j["id"] for j in agent_only} == {"daily-trading-recap",
                                        "ceo-weekly-report",
                                        "ceo-mode-verdict"})
check("scheduler: every ready job has a runnable command or steps",
      all(j.get("command") or j.get("steps") for j in ready))
check("scheduler: test-suite job runs the 9 daily suites in order",
      len(next(j for j in SCHED.JOBS
               if j["id"] == "bot-test-suites-daily")["steps"]) == 9)

# next_due / due math (Lagos wall time, fixed UTC+1).
from datetime import datetime, timedelta, timezone  # noqa: E402
LAGOS = timezone(timedelta(hours=1))
now = datetime(2026, 11, 22, 12, 0, tzinfo=LAGOS)
job_daily = next(j for j in SCHED.JOBS if j["id"] == "killswitch-check")
check("scheduler: never-run daily job is due at its next wall time, "
      "not immediately",
      SCHED.next_due(job_daily, now, None).isoformat()
      == "2026-11-23T08:52:00+01:00")
check("scheduler: daily next_due after last run is the next day",
      SCHED.next_due(
          job_daily, now,
          datetime(2026, 11, 22, 8, 52, tzinfo=LAGOS).timestamp()
      ).isoformat() == "2026-11-23T08:52:00+01:00")
job_int = next(j for j in SCHED.JOBS if j["id"] == "forex-watchdog")
check("scheduler: interval next_due is last_run + every_s",
      SCHED.next_due(job_int, now, now.timestamp() - 200).timestamp()
      == now.timestamp() + 100)
check("scheduler: never-run interval job is due immediately",
      SCHED.next_due(job_int, now, None) <= now)
check("scheduler: due_jobs returns the never-run interval jobs",
      {"forex-watchdog", "helius-429-watch"} <= {
          j["id"] for j in SCHED.due_jobs(now, {})})
job_once = next(j for j in SCHED.JOBS if j["id"] == "ceo-mode-verdict")
check("scheduler: runonce job is due at its fixed time, once",
      SCHED.next_due(job_once, now, None).isoformat()
      == "2026-12-20T09:00:00+01:00"
      and "ceo-mode-verdict" not in {
          j["id"] for j in SCHED.due_jobs(
              datetime(2026, 12, 21, tzinfo=LAGOS),
              {"ceo-mode-verdict": {"last_run": 1796000000}})})

# run_job: blocked + agent-only skip loudly, never run a command.
with tempfile.TemporaryDirectory() as tmp:
    home = os.path.join(tmp, ".forex-agent")
    rc, note = SCHED.run_job(
        next(j for j in SCHED.JOBS if j["id"] == "github-repo-sync"),
        home)
    check("scheduler: blocked job skips with rc=2 and names the blocker",
          rc == 2 and "blocked" in note and "authd" in note)
    state = SCHED.load_state(home)
    check("scheduler: skipped run is recorded in scheduler_state.json",
          state["github-repo-sync"]["last_rc"] == 2)
    rc, note = SCHED.run_job(
        next(j for j in SCHED.JOBS if j["id"] == "daily-trading-recap"),
        home)
    check("scheduler: agent-only job skips with rc=2",
          rc == 2 and "agent-only" in note)

# --list / --dry-run CLIs work.
proc = subprocess.run([sys.executable, os.path.join(AGT, "scheduler.py"),
                       "--list"], capture_output=True, text=True)
check("scheduler: --list prints valid JSON with 19 jobs",
      proc.returncode == 0
      and len(json.loads(proc.stdout)["jobs"]) == 19)
proc = subprocess.run([sys.executable, os.path.join(AGT, "scheduler.py"),
                       "--dry-run"], capture_output=True, text=True,
                      env={**os.environ,
                           "FOREX_AGENT_HOME": tempfile.mkdtemp()})
check("scheduler: --dry-run lists jobs + disabled set",
      proc.returncode == 0 and "disabled (not scheduled)" in proc.stdout)

# ---------------- dashboard serves real state ----------------
def _fixture_home(tmp):
    home = os.path.join(tmp, ".forex-agent")
    os.makedirs(os.path.join(home, "run"), exist_ok=True)
    os.makedirs(os.path.join(home, "killswitch"), exist_ok=True)
    os.makedirs(os.path.join(home, "review_queue"), exist_ok=True)
    os.makedirs(os.path.join(home, "storage"), exist_ok=True)
    now = time.time()
    ledgers = {
        "grad_select": {
            "bankroll_start": 25.0, "bankroll": 41.25,
            "positions": {"MINT1": {"entry_price": 0.5,
                                    "stake_usd": 25.0,
                                    "symbol": "TESTA"}},
            "closes": [
                {"mint": "M1", "symbol": "WIN",
                 "close_ts": now - 3600, "pnl_usd": 16.25,
                 "realized_mult": 2.0, "closed_reason": "leg_a"},
                {"mint": "M2", "symbol": "LOSS",
                 "close_ts": now - 7200, "pnl_usd": -25.0,
                 "realized_mult": 0.0, "closed_reason": "stop"}]},
        "grad_raw": {"bankroll_start": 25.0, "bankroll": 25.0,
                     "positions": {}, "closes": []},
        "tranche": {"bankroll_start": 25.0, "bankroll": 30.0,
                    "positions": {}, "closes": [
                        {"mint": "M3", "symbol": "T1",
                         "close_ts": now - 1800, "pnl_usd": 5.0,
                         "realized_mult": 1.5,
                         "closed_reason": "leg_a"}]},
        "tranche_protect": {"bankroll_start": 25.0, "bankroll": 25.0,
                            "positions": {}, "closes": []},
        "momentum": {"bankroll_start": 25.0, "bankroll": 20.0,
                     "positions": {}, "closes": [
                         {"mint": "M4", "symbol": "MO",
                          "close_ts": now - 900, "pnl_usd": -5.0,
                          "realized_mult": 0.5,
                          "closed_reason": "stop"}]},
    }
    for lane, data in ledgers.items():
        with open(os.path.join(home, "%s_ledger.json" % lane), "w") as fh:
            json.dump(data, fh)
    with open(os.path.join(home, "live10_demo_ledger.json"), "w") as fh:
        json.dump({"lanes": {
            "grad_select": {"start": 2.5, "balance": 10.0, "closes": 4,
                            "wins": 2, "net": 7.5, "funded_closes": 4},
            "tranche": {"start": 2.5, "balance": 3.0, "closes": 2,
                        "wins": 1, "net": 0.5, "funded_closes": 2},
            "tranche_protect": {"start": 2.5, "balance": 2.5,
                                "closes": 0, "wins": 0, "net": 0.0,
                                "funded_closes": 0},
            "grad_raw": {"start": 2.5, "balance": 1.0, "closes": 1,
                         "wins": 0, "net": -1.5, "funded_closes": 1}},
            "mirrored": []}, fh)
    with open(os.path.join(home, "killswitch", "state.json"), "w") as fh:
        json.dump({"lanes": {
            "grad_select": {"halted": False, "halt_reason": "",
                            "halt_ts": 0,
                            "trades": [{"ts": now - 100, "pnl": 16.25},
                                       {"ts": now - 200, "pnl": -25.0}]},
            "momentum": {"halted": True, "halt_reason": "test halt",
                         "halt_ts": now - 5000, "trades": []}},
            "events": [{"ts": now - 5000, "lane": "momentum",
                        "action": "HALT", "reason": "test halt"}]}, fh)
    with open(os.path.join(home, "supervisor_state.json"), "w") as fh:
        json.dump({"ts": now, "pid": 1234, "daemons": {
            "launch_scanner": {"state": "running", "pid": 2001,
                               "owned": True, "restarts": 0,
                               "last_exit": None, "uptime_s": 3600.0},
            "telegram": {"state": "adopted", "pid": 2002,
                         "owned": False, "restarts": 2,
                         "last_exit": now - 600, "uptime_s": 600.0}}}, fh)
    with open(os.path.join(home, "run", "heartbeat.json"), "w") as fh:
        json.dump({"checked_at": "2026-11-22T10:00:00Z",
                   "daemons": ["launch_scanner: running (pid 2001)"],
                   "actions": "none",
                   "beats": {"launch_scanner": {"ts": now, "pid": 2001,
                                                "iso": "2026-11-22T10:00:00Z"}}},
                  fh)
    with open(os.path.join(home, "helius_429_alert.json"), "w") as fh:
        json.dump({"active": False, "count_10m": 0}, fh)
    with open(os.path.join(home, "review_queue",
                           "grad_select_review.jsonl"), "w") as fh:
        fh.write('{"lane": "grad_select"}\n{"lane": "grad_select"}\n')
    import sqlite3
    conn = sqlite3.connect(os.path.join(home, "storage", "local.db"))
    conn.execute("CREATE TABLE event_journal (id INTEGER PRIMARY KEY "
                 "AUTOINCREMENT, event_id TEXT UNIQUE, event TEXT, "
                 "severity TEXT DEFAULT 'INFO', ts TEXT)")
    for i, ev in enumerate(("launch.detected", "launch.detected",
                            "momentum.alert")):
        conn.execute("INSERT INTO event_journal (event_id, event, ts) "
                     "VALUES (?, ?, ?)",
                     ("evt_%d" % i, ev,
                      datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()
    return home


def _get(port, path):
    with urllib.request.urlopen(
            "http://127.0.0.1:%d%s" % (port, path), timeout=5) as resp:
        return resp.status, resp.read().decode()


with tempfile.TemporaryDirectory() as tmp:
    home = _fixture_home(tmp)
    server = DASH.make_server(home=home, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        code, body = _get(port, "/status")
        status = json.loads(body)
        check("dashboard: /status answers 200 with valid JSON",
              code == 200 and isinstance(status, dict))
        check("dashboard: deployment is paper, mainnet disabled",
              status["deployment"]["mode"] == "paper"
              and status["deployment"]["mainnet_enabled"] is False)
        check("dashboard: balances come from the fixture Live10 ledger",
              status["balances"]["status"] == "ok"
              and abs(status["balances"]["data"]["live10_total"]
                      - 16.5) < 1e-9)
        check("dashboard: open positions count the fixture position",
              status["open_positions"]["data"]["total_open"] == 1
              and status["open_positions"]["data"]
              ["capital_at_risk_usd"] == 25.0)
        check("dashboard: recent trades are the fixture closes, newest "
              "first",
              [t["symbol"] for t in
               status["recent_trades"]["data"]["trades"]]
              == ["MO", "T1", "WIN", "LOSS"])
        check("dashboard: realized PnL sums the fixture closes",
              abs(status["pnl"]["data"]["per_lane"]["grad_select"]
                  ["realized_usd_all_time"] - (-8.75)) < 1e-9
              and abs(status["pnl"]["data"]
                      ["total_realized_usd_all_time"] - (-8.75)) < 1e-9)
        check("dashboard: unrealized PnL is null, never estimated",
              status["pnl"]["data"]["unrealized_usd"] is None)
        check("dashboard: killswitch reports the halted fixture lane",
              status["killswitch"]["data"]["halted_lanes"] == ["momentum"]
              and status["killswitch"]["data"]["lanes"]["momentum"]
              ["halt_reason"] == "test halt")
        check("dashboard: risk shows the 429 file + review queue count",
              status["risk"]["data"]["helius_429_alert"]["active"] is False
              and status["risk"]["data"]["review_queue_counts"]
              .get("grad_select") == 2)
        check("dashboard: discovery counts come from the event journal",
              status["discovery"]["data"]["signal_counts_last_24h"]
              ["launch.detected"] == 2
              and status["discovery"]["data"]["signal_counts_last_24h"]
              ["momentum.alert"] == 1)
        check("dashboard: supervisor state is served live",
              status["bot_status"]["data"]["running"] == 2
              and status["bot_status"]["data"]["daemons"]
              ["telegram"]["restarts"] == 2)
        check("dashboard: versions are unimplemented, not faked",
              status["versions"]["status"] == "unimplemented"
              and status["versions"]["data"]["strategies"] is None)
        check("dashboard: every section carries a source",
              all(isinstance(v, dict) and "source" in v
                  for v in status.values() if isinstance(v, dict)
                  and "status" in v))
        code, body = _get(port, "/health")
        health = json.loads(body)
        check("dashboard: /health answers with per-section checks",
              code == 200 and health["status"] in ("ok", "degraded")
              and "bot_status" in health["checks"])
        code, body = _get(port, "/")
        check("dashboard: / serves an HTML summary",
              code == 200 and "Paper bot" in body and "/status" in body)
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:%d/status" % port, method="POST")
            urllib.request.urlopen(req, timeout=5)
            check("dashboard: POST is rejected (read-only)", False)
        except urllib.error.HTTPError as exc:
            check("dashboard: POST is rejected (read-only)",
                  exc.code == 405)
        try:
            _get(port, "/nope")
            check("dashboard: unknown path is 404", False)
        except urllib.error.HTTPError as exc:
            check("dashboard: unknown path is 404", exc.code == 404)
    finally:
        server.shutdown()
        server.server_close()

# Missing state files -> unavailable, never invented.
with tempfile.TemporaryDirectory() as tmp:
    empty_home = os.path.join(tmp, ".forex-agent")
    os.makedirs(empty_home)
    status = DASH.collect_status(empty_home)
    check("dashboard: empty home reports supervisor unavailable",
          status["bot_status"]["status"] == "unavailable")
    check("dashboard: empty home reports balances unavailable",
          status["balances"]["status"] == "unavailable")
    check("dashboard: empty home still marks versions unimplemented",
          status["versions"]["status"] == "unimplemented")
    check("dashboard: empty home health is degraded",
          status["_health"] == "degraded")

# ---------------- no-secrets grep over deploy/ ----------------
SECRET_PATTERNS = (
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("github token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("api-key query value", re.compile(r"api-key=[^&\s\"']{8,}")),
    ("bearer token value", re.compile(r"Bearer [A-Za-z0-9._-]{16,}")),
    ("telegram bot token", re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")),
    ("alchemy key literal", re.compile(r"alch_[A-Za-z0-9]{20,}")),
    ("vault surrogate", re.compile(r"hsurr:[A-Za-z0-9_-]{8,}")),
    ("sk- secret", re.compile(r"sk-[A-Za-z0-9]{16,}")),
)
hits = []
for root, dirs, files in os.walk(AGT):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for name in files:
        path = os.path.join(root, name)
        with open(path, errors="replace") as fh:
            text = fh.read()
        for label, pat in SECRET_PATTERNS:
            if pat.search(text):
                hits.append("%s: %s" % (path, label))
check("no-secrets: no secret-shaped values anywhere under deploy/agt/",
      not hits)
if hits:
    print("  secret hits: %s" % "; ".join(hits))
check("no-secrets: no file under deploy/agt/ is mode 0600-with-secret "
      "or a credential store",
      not any(n in ("api_key", "bot_token", "secrets.env",
                    "keypair.json")
              for _, _, files in os.walk(AGT) for n in files))

# ---------------- validate_env report ----------------
report = VENV.validate()
check("validate_env: report produced for this Python",
      report["python_ok"] and report["python"].startswith("3.12"))
check("validate_env: solders is classified as a compiled dependency",
      report["modules"]["solders"]["kind"] == "compiled")
check("validate_env: stdlib modules all present",
      report["missing_stdlib"] == [])
check("validate_env: report lists a consequence per missing module",
      all("consequence_if_missing" in m
          for m in report["modules"].values()))
proc = subprocess.run([sys.executable,
                       os.path.join(AGT, "validate_env.py")],
                      capture_output=True, text=True)
check("validate_env: CLI exits 0 and prints a JSON report",
      proc.returncode == 0 and '"python_ok": true' in proc.stdout)

# ---------------- db_init + migrations ----------------
with tempfile.TemporaryDirectory() as tmp:
    home = os.path.join(tmp, ".forex-agent")
    result = DBINIT.init_db(home=home)
    check("db_init: creates local.db on first run",
          os.path.isfile(result["db"]) and result["fresh"] is True)
    check("db_init: --check passes after init",
          DBINIT.check_db(home=home))
    check("db_init: base schema tables exist",
          DBINIT.check_db(home=home))
    result2 = DBINIT.init_db(home=home)
    check("db_init: second run is a no-op (idempotent)",
          result2["fresh"] is False
          and "001_agt_meta" not in result2["applied"])
    import sqlite3
    conn = sqlite3.connect(result["db"])
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    check("db_init: expected tables present incl. agt_meta",
          set(DBINIT.EXPECTED_TABLES) <= tables
          and "agt_meta" in tables)
    proc = subprocess.run(
        [sys.executable, os.path.join(AGT, "db_init.py"),
         "--home", home, "--check"], capture_output=True, text=True)
    check("db_init: CLI --check exits 0 on an initialized DB",
          proc.returncode == 0)

# ---------------- start.sh smoke test ----------------
env = {**os.environ, **FAKE_ENV}
proc = subprocess.run(["bash", os.path.join(AGT, "start.sh"),
                       "--smoke-test"], capture_output=True, text=True,
                      env=env, timeout=120)
check("start.sh: --smoke-test passes with env-only credentials",
      proc.returncode == 0)
check("start.sh: smoke test proves dashboard /health + scheduler "
      "--dry-run",
      "smoke test OK" in proc.stdout)
scrubbed = {k: v for k, v in os.environ.items()
            if k not in ("HELIUS_API_KEY", "TELEGRAM_BOT_TOKEN",
                         "ALCHEMY_API_KEY")}
proc = subprocess.run(["bash", os.path.join(AGT, "start.sh"),
                       "--preflight-only"], capture_output=True, text=True,
                      env=scrubbed, timeout=60)
check("start.sh: --preflight-only fails closed without credentials",
      proc.returncode != 0
      and "ALCHEMY_API_KEY" in proc.stdout + proc.stderr)

# ---------------- supervisor start (isolated proof) ----------------
# A second supervisor ADOPTS live daemons by cmdline token, so the
# spawn path is proven WITHOUT touching the live fleet: a scratch
# home, a scratch /proc dir (FOREX_SUPERVISOR_PROC_DIR — the
# supervisor can adopt nothing live because it sees an empty
# process table), and a one-daemon roster
# (FOREX_SUPERVISOR_ONLY=health_monitor). Fake env creds only.
with tempfile.TemporaryDirectory() as tmp:
    home = os.path.join(tmp, ".forex-agent")
    fake_proc = os.path.join(tmp, "proc")
    os.makedirs(fake_proc)
    sup_env = {**os.environ, **FAKE_ENV, "HOME": tmp,
               "FOREX_AGENT_HOME": home,
               "FOREX_SUPERVISOR_PROC_DIR": fake_proc,
               "FOREX_SUPERVISOR_ONLY": "health_monitor"}
    proc = subprocess.Popen(
        [sys.executable, "scripts/supervisor"], cwd=REPO,
        env=sup_env, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    daemon_pid = 0
    spawned = False
    state_ok = False
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                st = json.load(open(os.path.join(
                    home, "supervisor_state.json")))
                ds = st.get("daemons") or {}
                if set(ds) == {"health_monitor"}:
                    state_ok = True
                    entry = ds["health_monitor"] or {}
                    daemon_pid = entry.get("pid") or 0
                    if (daemon_pid
                            or (entry.get("restarts") or 0) > 0
                            or os.path.isfile(os.path.join(
                                home, "log", "health_monitor.log"))):
                        spawned = True
                        break
            except Exception:
                pass
            time.sleep(1)
        check("supervisor: starts under env-only creds in a scratch "
              "home and writes state for exactly its one roster "
              "daemon",
              state_ok and spawned)
        check("supervisor: spawn evidence is a daemon log under the "
              "scratch home (the spawn path ran)",
              os.path.isfile(os.path.join(
                  home, "log", "health_monitor.log")))
        isolated = False
        if daemon_pid:
            try:
                env_text = open("/proc/%d/environ" % daemon_pid,
                                "rb").read().decode("utf-8", "replace")
                isolated = ("FOREX_AGENT_HOME=%s" % home) in env_text
            except OSError:
                isolated = False
        check("supervisor: spawned daemon runs under the scratch "
              "home, never the live home",
              isolated if daemon_pid else spawned)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if daemon_pid:
            try:
                os.kill(daemon_pid, 15)
            except OSError:
                pass
            # The daemon is a child the supervisor never kills on
            # exit; confirm it is gone so the test leaves nothing
            # behind.
            for _ in range(10):
                try:
                    os.kill(daemon_pid, 0)
                except OSError:
                    break
                time.sleep(1)

# ---------------- void list (Phase 13 finalize) ----------------
import voided_closes as VOID  # noqa: E402

entries, void_err = VOID.load_voided()
check("void: void file loads with 2 entries, no error",
      void_err is None and len(entries) == 2)
check("void: both entries void the 91GG phantom mint",
      all(e.get("mint") == "91GG7PWsUTmXwHcDtkoaEm77v1xc6NyQWoETFjrD"
                         "pump" for e in entries))
index = VOID.void_index(entries)
check("void: (lane, mint, close_ts) matches both voided closes",
      VOID.find_void(index, "grad_raw",
                     "91GG7PWsUTmXwHcDtkoaEm77v1xc6NyQWoETFjrDpump",
                     1795335524.270021) is not None
      and VOID.find_void(index, "tranche",
                         "91GG7PWsUTmXwHcDtkoaEm77v1xc6NyQWoETFjrD"
                         "pump", 1795335526.772091) is not None)
check("void: a far-off close_ts does NOT match (no stale voiding)",
      VOID.find_void(index, "tranche",
                     "91GG7PWsUTmXwHcDtkoaEm77v1xc6NyQWoETFjrDpump",
                     1795335526.772091 + 100000) is None)
check("void: wrong lane does NOT match",
      VOID.find_void(index, "grad_select",
                     "91GG7PWsUTmXwHcDtkoaEm77v1xc6NyQWoETFjrDpump",
                     1795335526.772091) is None)
check("void: funded phantom is tranche $5,241.07 x 1 close only "
      "(when the close is in the lane ledger)",
      VOID.funded_void_for_lane(
          entries, "tranche",
          [{"mint": "91GG7PWsUTmXwHcDtkoaEm77v1xc6NyQWoETFjrDpump",
            "close_ts": 1795335526.772091}]) == (5241.07, 1))
check("void: grad_raw void was unfunded (no funded amount, no "
      "funded-close count)",
      VOID.funded_void_for_lane(
          entries, "grad_raw",
          [{"mint": "91GG7PWsUTmXwHcDtkoaEm77v1xc6NyQWoETFjrDpump",
            "close_ts": 1795335524.270021}]) == (0.0, 0))
check("void: (lane, pnl, ts) matches the killswitch window "
      "trades (recorded ~1s after the close)",
      VOID.find_void_by_pnl_ts(entries, "tranche", 13102.6807,
                               1795335527.7756526) is not None
      and VOID.find_void_by_pnl_ts(entries, "grad_raw", 12724.3124,
                                   1795335525.3021965) is not None)
check("void: (lane, pnl, ts) does NOT match a different pnl or "
      "a far-off ts",
      VOID.find_void_by_pnl_ts(entries, "tranche", 13102.6807 + 1.0,
                               1795335527.7756526) is None
      and VOID.find_void_by_pnl_ts(entries, "tranche", 13102.6807,
                                   1795335527.7756526 + 60) is None)
check("void: no adjustment when the voided close is NOT in the "
      "lane ledger (void list is global, homes are not)",
      VOID.funded_void_for_lane(entries, "tranche", []) == (0.0, 0)
      and VOID.funded_void_for_lane(entries, "tranche") == (0.0, 0))
kept, voided = VOID.filter_closes(
    index, "tranche",
    [{"mint": "91GG7PWsUTmXwHcDtkoaEm77v1xc6NyQWoETFjrDpump",
      "close_ts": 1795335526.772091, "pnl_usd": 13102.6807},
     {"mint": "M3", "close_ts": time.time(), "pnl_usd": 5.0}])
check("void: filter_closes keeps the real close, voids the phantom",
      len(kept) == 1 and len(voided) == 1
      and kept[0]["mint"] == "M3")


def _void_fixture_home(tmp):
    home = os.path.join(tmp, ".forex-agent")
    os.makedirs(os.path.join(home, "run"), exist_ok=True)
    os.makedirs(os.path.join(home, "killswitch"), exist_ok=True)
    os.makedirs(os.path.join(home, "review_queue"), exist_ok=True)
    os.makedirs(os.path.join(home, "storage"), exist_ok=True)
    void_grad_raw = {
        "mint": "91GG7PWsUTmXwHcDtkoaEm77v1xc6NyQWoETFjrDpump",
        "symbol": "?", "close_ts": 1795335524.270021,
        "pnl_usd": 12724.3124, "realized_mult": 509.9725,
        "closed_reason": "time_stop_24h"}
    void_tranche = {
        "mint": "91GG7PWsUTmXwHcDtkoaEm77v1xc6NyQWoETFjrDpump",
        "symbol": "?", "close_ts": 1795335526.772091,
        "pnl_usd": 13102.6807, "realized_mult": 525.1072,
        "closed_reason": "time_stop_24h"}
    ledgers = {
        "grad_select": {"bankroll_start": 25.0, "bankroll": 41.25,
                        "positions": {}, "closes": []},
        "grad_raw": {"bankroll_start": 25.0, "bankroll": 12749.31,
                     "positions": {}, "closes": [void_grad_raw]},
        "tranche": {"bankroll_start": 25.0, "bankroll": 13132.68,
                    "positions": {},
                    "closes": [void_tranche,
                               {"mint": "M3", "symbol": "T1",
                                "close_ts": time.time() - 1800,
                                "pnl_usd": 5.0, "realized_mult": 1.5,
                                "closed_reason": "leg_a"}]},
        "tranche_protect": {"bankroll_start": 25.0, "bankroll": 25.0,
                            "positions": {}, "closes": []},
        "momentum": {"bankroll_start": 25.0, "bankroll": 20.0,
                     "positions": {}, "closes": []},
    }
    for lane, data in ledgers.items():
        with open(os.path.join(home, "%s_ledger.json" % lane), "w") as fh:
            json.dump(data, fh)
    with open(os.path.join(home, "live10_demo_ledger.json"), "w") as fh:
        json.dump({"lanes": {
            "grad_select": {"start": 2.5, "balance": 3851.0855,
                            "closes": 929, "wins": 457,
                            "net": 3880.7861, "funded_closes": 929},
            "tranche": {"start": 2.5, "balance": 9524.145,
                        "closes": 882, "wins": 469,
                        "net": 9531.8483, "funded_closes": 882},
            "tranche_protect": {"start": 2.5, "balance": 954.4713,
                                "closes": 220, "wins": 89,
                                "net": 927.9138, "funded_closes": 220},
            "grad_raw": {"start": 2.5, "balance": 7.2311,
                         "closes": 6, "wins": 0, "net": -19.6153,
                         "funded_closes": 6}},
            "mirrored": []}, fh)
    with open(os.path.join(home, "killswitch", "state.json"), "w") as fh:
        json.dump({"lanes": {
            # Window trades carry no mint — the phantom is matched
            # by (lane, pnl to the cent, ts ~1s after close_ts).
            "grad_raw": {"halted": False, "halt_reason": "",
                         "halt_ts": 0,
                         "trades": [{"ts": 1795335525.3021965,
                                     "pnl": 12724.3124},
                                    {"ts": time.time() - 100,
                                     "pnl": -25.0}]},
            "tranche": {"halted": False, "halt_reason": "",
                        "halt_ts": 0,
                        "trades": [{"ts": 1795335527.7756526,
                                    "pnl": 13102.6807},
                                   {"ts": time.time() - 100,
                                    "pnl": 5.0}]}},
            "events": []}, fh)
    with open(os.path.join(home, "supervisor_state.json"), "w") as fh:
        json.dump({"ts": time.time(), "pid": 1, "daemons": {}}, fh)
    with open(os.path.join(home, "run", "heartbeat.json"), "w") as fh:
        json.dump({"checked_at": "2026-11-22T10:00:00Z",
                   "daemons": [], "actions": "none",
                   "beats": {}}, fh)
    with open(os.path.join(home, "helius_429_alert.json"), "w") as fh:
        json.dump({"active": False, "count_10m": 0}, fh)
    return home


with tempfile.TemporaryDirectory() as tmp:
    vhome = _void_fixture_home(tmp)
    bal = DASH.read_balances(vhome)
    check("void: Live10 total is void-excluded ($9,095.86), not the "
          "raw $14,336.93",
          bal["status"] == "ok"
          and abs(bal["data"]["live10_total"] - 9095.8629) < 1e-9
          and abs(bal["data"]["live10_total_raw"]
                  - 14336.9329) < 1e-9)
    tr = bal["data"]["live10_lanes"]["tranche"]
    check("void: tranche Live10 balance/net drop by exactly the "
          "funded phantom",
          abs(tr["balance"] - 4283.075) < 1e-9
          and abs(tr["net"] - 4290.7783) < 1e-9
          and tr["balance_raw"] == 9524.145)
    check("void: tranche funded closes/wins drop by exactly one",
          tr["funded_closes"] == 881 and tr["closes"] == 881
          and tr["wins"] == 468)
    gr = bal["data"]["live10_lanes"]["grad_raw"]
    check("void: unfunded grad_raw Live10 balance is untouched",
          gr["balance"] == 7.2311 and gr["funded_closes"] == 6)
    pnl = DASH.read_pnl(vhome)
    check("void: paper PnL excludes both phantom closes",
          abs(pnl["data"]["per_lane"]["tranche"]
              ["realized_usd_all_time"] - 5.0) < 1e-9
          and abs(pnl["data"]["per_lane"]["grad_raw"]
                  ["realized_usd_all_time"] - 0.0) < 1e-9
          and abs(pnl["data"]["total_realized_usd_all_time_raw"]
                  - 25831.99) < 1e-9
          and abs(pnl["data"]["voided_usd_total"]
                  - 25826.99) < 1e-9)
    trades = DASH.read_recent_trades(vhome)
    check("void: phantom closes are out of recent trades, listed "
          "separately with their reason",
          all(t["mint"] != "91GG7PWsUTmXwHcDtkoaEm77v1xc6NyQWoETFjrD"
                            "pump"
              for t in trades["data"]["trades"])
          and len(trades["data"]["voided_closes"]) == 2
          and all("VOIDED" in (t.get("void_reason") or "")
                  for t in trades["data"]["voided_closes"]))
    vc = DASH.read_voided_closes(vhome)
    check("void: dashboard carries a voided_closes section (2 "
          "entries, sources stated)",
          vc["status"] == "ok" and vc["data"]["count"] == 2)
    ks = DASH.read_killswitch(vhome)
    check("void: killswitch window excludes the phantom (pnl+ts "
          "match), raw window kept labelled",
          ks["status"] == "ok"
          and abs(ks["data"]["lanes"]["tranche"]["window_net_usd"]
                  - 5.0) < 1e-9
          and ks["data"]["lanes"]["tranche"]["window_trades"] == 1
          and abs(ks["data"]["lanes"]["tranche"]
                  ["window_net_usd_raw"] - 13107.68) < 1e-9
          and ks["data"]["lanes"]["tranche"]["voided_trades"] == 1
          and abs(ks["data"]["lanes"]["grad_raw"]["window_net_usd"]
                  - (-25.0)) < 1e-9
          and ks["data"]["lanes"]["grad_raw"]["voided_trades"] == 1)
    status = DASH.collect_status(vhome)
    check("void: /status balances row labels the raw total beside "
          "the void-excluded total",
          status["balances"]["data"]["live10_total_raw"] == 14336.9329)

# ---------------- relocatable roster (Phase 13 finalize) ----------------
_ROSTER_PROBE = (
    "import importlib.util, os\n"
    "_spec = importlib.util.spec_from_loader('sup_probe', loader=None)\n"
    "_S = importlib.util.module_from_spec(_spec)\n"
    "_S.__dict__['__file__'] = 'scripts/supervisor'\n"
    "exec(compile(open('scripts/supervisor').read(),\n"
    "             'scripts/supervisor', 'exec'), _S.__dict__)\n"
    "_dev = [s for s in _S.ROSTER if s.name == 'devnet-demo-trader'][0]\n"
    "print('DEVNET_ARGV0=' + _dev.argv[0])\n"
    "print('VENV_PY=' + _S._venv_python())\n"
    "print('ONLY=' + ','.join(s.name for s in _S._active_roster()))\n"
)
with tempfile.TemporaryDirectory() as tmp:
    fake_bin = os.path.join(tmp, "prefix", ".venv", "bin")
    os.makedirs(fake_bin)
    fake_py = os.path.join(fake_bin, "python")
    with open(fake_py, "w") as fh:
        fh.write("#!/bin/sh\nexec python3 \"$@\"\n")
    os.chmod(fake_py, 0o755)
    env = {**os.environ, "FOREX_VENV_PYTHON": fake_py,
           "FOREX_AGENT_HOME": os.path.join(tmp, ".forex-agent")}
    proc = subprocess.run([sys.executable, "-c", _ROSTER_PROBE],
                          cwd=REPO, env=env, capture_output=True,
                          text=True)
    check("roster: FOREX_VENV_PYTHON override is used for "
          "devnet-demo-trader",
          proc.returncode == 0
          and ("DEVNET_ARGV0=%s" % fake_py) in proc.stdout)
    check("roster: no hardcoded /home/hatch path survives in the "
          "devnet argv",
          "/home/hatch" not in proc.stdout)
    # Default resolution must find the repo venv without any override.
    # Create a fake {repo}/.venv so this check is hermetic: the original
    # form only passed on the dev VM, where a sibling .venv happened to
    # exist next to the checkout.
    repo_venv_bin = os.path.join(REPO, ".venv", "bin")
    repo_venv_py = os.path.join(repo_venv_bin, "python")
    made_venv = not os.path.exists(repo_venv_py)
    if made_venv:
        os.makedirs(repo_venv_bin, exist_ok=True)
        with open(repo_venv_py, "w") as fh:
            fh.write("#!/bin/sh\nexec python3 \"$@\"\n")
        os.chmod(repo_venv_py, 0o755)
    try:
        env2 = {k: v for k, v in os.environ.items()
                if k != "FOREX_VENV_PYTHON"}
        env2["FOREX_AGENT_HOME"] = os.path.join(tmp, ".forex-agent")
        proc2 = subprocess.run([sys.executable, "-c", _ROSTER_PROBE],
                               cwd=REPO, env=env2, capture_output=True,
                               text=True)
        check("roster: default resolves to the repo venv (.venv inside "
              "the checkout)",
              proc2.returncode == 0
              and ("DEVNET_ARGV0=%s" % repo_venv_py) in proc2.stdout)
        env3 = {**env2, "FOREX_SUPERVISOR_ONLY": "health_monitor"}
        proc3 = subprocess.run([sys.executable, "-c", _ROSTER_PROBE],
                               cwd=REPO, env=env3, capture_output=True,
                               text=True)
        check("roster: FOREX_SUPERVISOR_ONLY restricts the active "
              "roster (test-only)",
              proc3.returncode == 0
              and "ONLY=health_monitor" in proc3.stdout)
    finally:
        if made_venv:
            os.remove(repo_venv_py)
            try:
                os.removedirs(repo_venv_bin)
            except OSError:
                pass

# ---------------- backup job runs backup + verify ----------------
_backup = next(j for j in SCHED.JOBS if j["id"] == "forex-backup-hourly")
check("scheduler: backup job runs backup, then backup_verify "
      "--verify-latest",
      _backup.get("steps") == [
          ["python3", "scripts/forex-backup"],
          ["python3", "deploy/agt/backup_verify.py",
           "--verify-latest"]])

# ---------------- deployment version file ----------------
with tempfile.TemporaryDirectory() as tmp:
    vhome = os.path.join(tmp, ".forex-agent")
    os.makedirs(vhome)
    with open(os.path.join(vhome, "agt_version.json"), "w") as fh:
        json.dump({"branch": "main",
                   "commit": "abc123def456",
                   "commit_source": "git rev-parse HEAD",
                   "generated_at_utc": "2026-11-22T10:00:00Z",
                   "repo": "https://github.com/novamailinc/AGT-memecoin"},
                  fh)
    ver = DASH.read_versions(vhome)
    check("versions: version file present -> commit reported, "
          "strategies/models still null (no registry)",
          ver["status"] == "ok"
          and ver["data"]["commit"] == "abc123def456"
          and ver["data"]["strategies"] is None
          and ver["data"]["models"] is None)
    check("manifest: notes record the finalize state (void list + "
          "restore drill run, not deferred)",
          any("void" in n.lower() and "restore" in n.lower()
              for n in manifest.get("agt_notes", [])))

print("\n%d failures, %d skipped" % (len(fails), len(skips)))
if skips:
    for s in skips:
        print("  skipped: %s" % s)
sys.exit(1 if fails else 0)
