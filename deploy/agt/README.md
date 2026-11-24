# Current qualification boundary (2026-11-23)

This is packaging under validation, not deployment or trading approval. Current
local tests do not establish Stage A historical validation, complete VM lifecycle,
or phone/reboot qualification. Revised owner allocation: Alchemy handles normal scanning; Helius is primary
for routine RPC, WebSockets and transaction reads plus selective intelligence.
Chainstack complements recent transaction/block/account reads and independent
verification within plan limits. Speed and coverage still require measurement.
Current Free-account Alchemy HTTP works but Solana WS access is denied. Startup
credential presence does not prove streaming entitlement. Do not activate the
full bot while this and the remaining acceptance gates are unresolved.

# AGT deployment package — meme-project paper bot

Phase 13 **finalize** (2026-11-22). This package is the deployable
AGT deployment for the paper bot: the skeleton's known limits that
were fixable from the VM are fixed, the restore drill has been run
against a scratch home and passes, the supervisor spawn path is
proven in isolation, and the remaining acceptance items are
on-phone procedures documented here (2h full soak, phone-reboot
"running" check, live Alchemy/Chainstack verification, live
benchmarks).

**Paper only throughout. Mainnet stays disabled.** No live trade, no
mainnet key, no private key is created or handled by anything in this
package.

## What AGT is (from the audit)

AGT is an Android app that runs a real supervised Linux prefix
(Python 3.12, bash, git, curl) as a foreground service with a
watchdog, a process supervisor, an encrypted secrets vault, and an
outbound-only control plane (Cloudflare Tunnel + Firebase +
Supabase). Deployments are created from a GitHub URL (public repo,
`main` branch), get a work dir (also their `HOME`), env vars (+ vault
secrets by name), a port, and a start command run under the
supervisor. Health is process-alive + TCP connect to the port.

Two AGT facts govern this package:

- **AGT has no cron / scheduled jobs** (verified from AGT source:
  zero "cron" in the Kotlin code). The bot's hourly backup, 30-min
  self-audit, 5-min watchdog, and 429-watch run as loops inside the
  deployment — see `scheduler.py`.
- **The AGT vault injects secrets as env vars only.** The VM's
  file-based reads and vault-surrogate bridges do not work on AGT —
  see `secrets_mapping.md`.

Visual monitoring runs through the XFCE add-on (view-only live link)
plus this package's own status dashboard over the tunnel.

## Files

| File | Purpose |
|---|---|
| `manifest.json` | Deployment manifest: repo URL, `main` branch, start command, port 8765, env list (names only). |
| `start.sh` | Start command. Credential preflight (fail closed) → version record → file bridge → env validation → DB init → supervisor + scheduler + dashboard. `--preflight-only` and `--smoke-test` modes for local validation. |
| `credentials.py` | Env-var credential resolution + fail-closed startup check + runtime file bridge for file-only readers. Never logs values. |
| `scheduler.py` | In-deployment scheduler: loop equivalents for all 19 enabled bot cron jobs; 7 disabled jobs listed, not scheduled. The hourly backup job runs `scripts/forex-backup` then `backup_verify.py --verify-latest`. `--list`, `--dry-run`, `--once <job>`, `--run`. |
| `status_server.py` | Read-only HTTP dashboard (127.0.0.1:8765): bot status, balances (void-excluded Live10 total with the raw total labelled beside it), open positions, recent trades, realized PnL, exposure, discovery activity, risk assessments, execution failures, RPC/provider health, killswitch status, scheduler state, deployment version, voided closes. Every field carries its source; fields with no source are `unimplemented`, never faked. |
| `voided_closes.py` + `voided_closes.json` | The void list. Two voided 91GG closes (grad_raw 510.0x +$12,724.31, tranche 525.1x +$13,102.68 — bad exit prices, not wins). Applied at read time; the live ledgers are never edited. |
| `backup_verify.py` | Backup verification (checksum, no credential material, ledgers parse, killswitch shadow, DB integrity) + off-phone export manifest. Run by the hourly backup job and by `healthcheck.sh`. |
| `restore_drill.py` | The restore drill, runnable against a scratch home (never the live home). Records pass/fail per step and writes a drill report. |
| `live_benchmarks.md` | Phase 10 UNMEASURED items + the exact AGT procedures to measure them. Nothing re-measured here. |
| `validate_env.py` | Python environment validation for the AGT prefix (pure-Python + vendored wheels; flags `solders` and unproven ML deps). |
| `db_init.py` + `migrations/` | Database init + migrations. Applies `storage/schemas/schema.sql` plus numbered AGT migrations, idempotently, recorded in `agt_meta`. WAL with auto-checkpoint. |
| `healthcheck.sh` | Operator health check: supervisor/heartbeat freshness, dashboard `/health`, killswitch parse, DB tables, 429 spike, backup age, backup-verify report, disk space. Read-only. |
| `secrets_mapping.md` | Every live credential: env name, purpose, file fallback, which readers are env-first vs file-only. |
| `storage_budget.md` | What ships (repo, 107 MB) vs what does not (395 MB backtest dataset, 1.4 GB DB + 8.7 GB WAL, 25 GB backups); DB/WAL growth policy. |
| `backup_recovery.md` | Hourly backup + verify, what is never backed up, the manual off-phone export (no automated off-phone copy exists yet — see below), restore procedure + restore drill (run 2026-11-22, all steps pass). |
| `upgrade_rollback.md` | Upgrade (redeploy from `main`) and rollback (code, then state) procedures, with the version record and dry-run verification. |

## Deploy (operator steps)

1. Confirm AGT is installed on the target phone and the deployment
   is created from `https://github.com/novamailinc/AGT-memecoin`,
   branch `main`, start command `bash deploy/agt/start.sh`, port
   8765.
2. For the declared three-provider profile, set `ALCHEMY_API_KEY`,
   `HELIUS_API_KEY_1`, `HELIUS_API_KEY_2`, `HELIUS_API_KEY_3`,
   `CHAINSTACK_ENDPOINT` and `TELEGRAM_BOT_TOKEN` in the AGT vault.
   All six are required at preflight. Helius owns routine RPC/WSS and
   selective intelligence, Alchemy normal scanning, and Chainstack
   complementary recent reads and independent verification.
   `HELIUS_ALERT_CHAT_ID` is optional. `HELIUS_API_KEY` is legacy
   compatibility, not a substitute for an explicit numbered slot or
   another allowance. Presence does not prove service access or qualification.
   See `secrets_mapping.md`.
3. Start the deployment. `start.sh` fails closed with a clear
   message when a required credential is missing.
4. Verify: `bash deploy/agt/healthcheck.sh` from the deployment
   shell, and open the dashboard at the deployment's public
   `<slug>-agt.novamail.store` address.

## Independence

Once started, the deployment operates on its own: the supervisor
owns the daemons, the scheduler runs the recurring jobs, the
dashboard serves live state, and the backup loop snapshots state
hourly. The development agent's computer does not need to remain
online. (Chat delivery of the daily recap / CEO weekly / weekly
review stays with the main agent — those jobs are `agent-only` in
the scheduler and their underlying figures are served by the
dashboard.)

## Known limits (finalize — what is fixed, what is on-phone)

Fixed in this pass (2026-11-22, all tested — see Tests below):

- **Voided 91GG phantom:** the dashboard no longer reports the
  raw Live10 total as the headline. `voided_closes.json` voids
  the two 91GG closes (grad_raw +$12,724.31, tranche
  +$13,102.68 — bad exit prices, not wins); the dashboard
  subtracts the funded $5,241.07 from the tranche Live10
  balance/net and shows raw vs void-excluded totals side by side,
  labelled. Live10 total is now $9,095.86 void-excluded (raw
  $14,336.93). The live ledgers were never edited.
- **Relocatable roster:** `scripts/supervisor` no longer
  hardcodes the VM venv path. `devnet-demo-trader` runs under
  `FOREX_VENV_PYTHON` when set, else `{repo}/.venv/bin/python`,
  else `{repo}/../.venv/bin/python`, else the interpreter running
  the supervisor (the AGT prefix python). Proven with a fake
  prefix in tests.
- **Supervisor spawn proven:** the spawn path is proven in a
  scratch home with a scratch proc dir and a one-daemon roster —
  no live daemon is adopted, the spawned daemon runs under the
  scratch home.
- **Restore drill run:** `restore_drill.py` ran against a scratch
  home with all 10 steps passing (see `backup_recovery.md`). The
  drill found and now handles a real layout quirk: the backup
  archive stores `local.db` at its root and `heartbeat.json` at
  its root, while the live home expects `storage/local.db` and
  `run/heartbeat.json` — a naive restore would start on a fresh
  empty database.
- **Backup verify:** the hourly backup job now runs
  `backup_verify.py --verify-latest` after every backup
  (checksum, no credential material, ledgers parse, killswitch
  shadow, DB integrity), and `healthcheck.sh` checks the verify
  report.
- **Durable applied-set (Phase 12 follow-up):** the event
  store's `applied_events` table survives processor restarts, so
  a restarted processor never re-applies an already-applied
  event (no double-counted trades).
- **Read-only shadow reader (Phase 11 follow-up, partial):**
  `daemon/intelligence/signals/shadow_reader.py` translates
  conviction, grad_select filters, deployer band, regime, and
  safety/veto RESULT dicts into fusion inputs without importing
  the launch_scanner monolith. Full `veto_check()` is NOT
  callable from the reader (relative imports resolve only inside
  the monolith; it writes veto journals; its bundle step walks
  live RPC) — the wiring plan is in the module docstring.
- **Deployment version:** `start.sh` writes
  `agt_version.json` (git SHA when the checkout has one) and the
  dashboard reports it. Strategy/model versions still have no
  registry.

Still on-phone (procedures below; nothing here is a substitute):

- **2h full soak** (supervisor + scheduler + traders running 2h
  on the phone) — procedure below. A dashboard-only 2h soak was
  started from the VM against a scratch home (no live state) as
  a stability smoke signal; it does not prove the deployment.
- **Phone-reboot "running" check** — procedure below.
- **Live Alchemy/Chainstack verification** — keys must reach a
  vault on the phone first (Phoenix route in progress, AgentLink
  msg 429bbb91, awaiting reply; bearer/query placements from the
  VM are exhausted — all 401). Until then Helius stays the live
  leg.
- **Live benchmarks (Phase 10, UNMEASURED here)** — procedures
  in `live_benchmarks.md`.
- **Off-phone backup is MANUAL:** there is still no automated
  off-phone copy on AGT (no Drive credential in the AGT vault,
  no AGT snapshot API wired). `backup_verify.py
  --export-manifest` writes the copy list; the operator copies
  the archives off the phone by hand (see
  `backup_recovery.md`). Until that copy exists, a lost or wiped
  phone loses the paper history. This remains the single biggest
  recovery gap.
- **`github-repo-sync` and `entry-learner-watch` are blocked**:
  their scripts live outside the repo and depend on VM-only
  paths. The scheduler records them as blocked, loudly, instead
  of silently skipping them.
- **Dashboard gaps** (marked `unimplemented`, never faked):
  unrealized PnL (no live mark source), strategy/model versions (no
  version registry yet), router health (no router health state file
  yet), PumpPortal stream connectivity (no stream state file yet),
  live execution failures (paper-only, no mainnet execution).
- **Five file-only credential readers** (`grad_select.py`,
  `grad_raw.py`, `raw_tight.py`, `helius_alerts.py`,
  `launch_scanner/__init__.py`, `telegram/commands.py`) are bridged
  from env at start rather than fixed in place (no live files are
  edited in this pass).
- **Agent-only jobs** (`daily-trading-recap`, `ceo-weekly-report`,
  `ceo-mode-verdict`) have no in-deployment loop: their deliverable
  is a chat message the main agent composes. The dashboard serves
  the numbers they would use.

## Phone reboot — "running" status check (on-phone procedure)

After the phone reboots, AGT restarts the deployment under its
supervisor. The operator (or an agent job on the phone) checks,
in order:

1. AGT app → deployment → status shows **running** (not
   "starting" for more than 2 minutes, not "crashed").
2. `bash deploy/agt/healthcheck.sh` from the deployment shell
   exits 0 (supervisor state fresh < 120s, heartbeat fresh <
   600s, dashboard `/health` answers, killswitch parses, DB
   tables present, no 429 spike, backup younger than 2h,
   backup-verify report passed, disk > 2 GB free).
3. Dashboard `/status`: `bot_status` ok, `_health` ok or
   degraded with a named reason; `balances.live10_total` is the
   void-excluded total; killswitch lanes match the pre-reboot
   state (no new halts).
4. If any check fails: do NOT restart blindly. Read
   `$FOREX_AGENT_HOME/log/supervisor.log` and
   `log/agt-scheduler.log` first; if the deployment crash-loops,
   stop it and follow the rollback procedure in
   `upgrade_rollback.md` (code rollback first, state rollback
   only on corruption, with the pre-reboot backup).

## 2h full soak (on-phone procedure)

What it proves: the supervisor keeps all 28 daemons alive for
2h, the scheduler fires its due jobs, the dashboard stays up,
backups run hourly and verify, and no daemon storm-holds.

1. Start (or restart) the deployment; record the start time and
   the dashboard `/status` snapshot.
2. Leave it alone for 2h. Do not trade, halt, or redeploy during
   the soak.
3. Every 30 minutes, record: `healthcheck.sh` exit code,
   `supervisor_state.json` daemon count + restarts, dashboard
   `/health` status, newest backup age + verify report.
4. Pass criteria: healthcheck exits 0 at every check; no daemon
   in storm-hold; at least two hourly backups verified; no new
   killswitch halts attributable to the deployment (halts on
   negative expectancy are the killswitch working, not a soak
   failure — record them as such).
5. If the soak fails: keep the logs, stop the deployment, and
   report the failing check with its log lines — do not "fix
   forward" on the phone without a snapshot (see
   `upgrade_rollback.md`).

## Tests

`tests/test_agt_packaging.py` (plain script, venv python — 129
checks, 0 failed, 0 skipped). Covers: manifest validity, env-only
credential startup (missing credential fails closed), scheduler
enumeration completeness vs the 19 enabled bot cron jobs, dashboard
serving real state (no invented fields) + the void list (raw vs
void-excluded totals), the relocatable roster (fake prefix),
supervisor spawn in an isolated scratch home, backup job
steps (backup + verify), deployment version file, no-secrets grep
over `deploy/`, env validation report, DB init + migrations, and the
`start.sh` smoke test.

`tests/test_agt_restore_drill.py` (20 checks, 0 failed): real
`scripts/forex-backup` against a fixture home, `restore_drill.py`
all steps passing, `backup_verify.py` pass/fail paths, drill
refusal of the live home.

`tests/test_intelligence_events_durable.py` (17 checks, 0 failed):
durable applied-set across a processor restart.
`tests/test_intelligence_shadow_reader.py` (37 checks, 0 failed):
standalone loader guard + all translators + file provider.
