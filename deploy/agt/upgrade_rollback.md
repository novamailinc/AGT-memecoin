# Upgrade + rollback — AGT deployment

Phase 13 finalize (2026-11-22). AGT deployments are GitHub-URL deploys: an upgrade
is a new commit on `main` plus a redeploy. There is no in-place
`apt-get` lifecycle on the phone (the AGT Debian rootfs path is dead;
the prefix is the bootstrap zip).

## Upgrade procedure

1. Land the change on GitHub `main` (the repo is public;
   `olamoreA/meme-project`). The change must already have passed its
   tests on the VM — never deploy an untested commit to the phone.
2. Take a pre-upgrade snapshot: run the hourly backup loop once by
   hand (`deploy/agt/scheduler.py --once forex-backup-hourly` from
   the deployment shell, or wait for the next hourly run) and note
   the archive name. The backup is the rollback data.
3. Redeploy in the AGT app (deployment → Redeploy, or re-create from
   the repo URL). AGT downloads the new tarball, extracts it, and
   restarts the deployment under the supervisor.
4. Watch the dashboard (`/health` then `/status`): supervisor state
   fresh within 2 minutes, heartbeat fresh, killswitch lanes as
   before the upgrade, no new halts, no 429 spike attributable to
   the new code — and `versions.commit` shows the new commit.
5. Leave the pre-upgrade backup in place for 24h. If the new code
   misbehaves, roll back (below) — do not "fix forward" on the phone
   without a snapshot.

Notes:

- **Database migrations run forward only.** `db_init.py` applies
  `storage/schemas/schema.sql` (idempotent `CREATE TABLE IF NOT
  EXISTS`) and `deploy/agt/migrations/*.sql` in order, recording
  each in `agt_meta`. A migration that cannot run stops the start
  (fail closed) — the deployment refuses to start on a half-migrated
  DB rather than running on a broken one.
- **The scheduler and dashboard are part of the deploy.** Upgrading
  the bot upgrades them too; their state files
  (`scheduler_state.json`, `supervisor_state.json`) survive a
  redeploy because they live in `FOREX_AGENT_HOME`, not in the repo
  checkout.
- **Secrets survive an upgrade untouched.** They live in the AGT
  vault, not in the checkout; the credential bridge rewrites the
  runtime files from env on every start.

## Rollback procedure

Two levels, in order:

**1. Code rollback (usual case).** Redeploy the previous good commit:
in the AGT app, redeploy from the repo URL at the previous commit
(AGT git deploys accept a branch; pinning an exact commit needs the
tarball URL for that commit — use the GitHub `codeload` tarball for
the known-good SHA and deploy it as an uploaded bundle if the app
does not accept a SHA). Then verify on the dashboard as in upgrade
step 4.

**2. State rollback (only when the new code corrupted state).**
Stop the deployment, restore the pre-upgrade backup with the
procedure in `backup_recovery.md` (steps 1–9), then redeploy the
previous good commit. Never restore state without also rolling the
code back — new code on old state (or old code on new state) is how
ledgers get silently diverged.

**What rollback never does:** it never restores a credential file,
never re-enables a halted lane (halts are human-only re-enable, per
the killswitch rules), and never edits a ledger by hand. A ledger
that is corrupt is preserved as a `.corrupt-<ts>` sidecar and the
shadow/backup is the recovery path.

## Version pinning

The deployment version is recorded, not guessed. `start.sh`
writes `$FOREX_AGENT_HOME/agt_version.json` at preflight
(`{"branch", "commit", "commit_source", "generated_at_utc",
"repo"}` — the git SHA when the checkout has one, else null with
the reason "no git checkout in this deploy (tarball); record main
HEAD by hand"). The dashboard's `versions` section reports that
commit. Before any upgrade or rollback, record the running
commit from the dashboard; after the redeploy, confirm the
dashboard shows the new (or rolled-back) commit. Strategy/model
versions still have no registry — the deployment commit is the
only version reported.

## Deployment verification by dry run (finalize)

Before any deploy to the phone is called good, the same
verification that runs on the VM must pass:

1. `bash deploy/agt/start.sh --preflight-only` with the
   deployment credentials in env — exits 0, and fails closed
   naming the missing variable when a required credential is
   absent. Set `FOREX_AGENT_HOME` to a scratch dir when
   dry-running off the phone, so the version record
   (`agt_version.json`) and the env report land there, not in
   the live home. (The preflight writes those two files into
   whatever home it runs against; `db_init` itself is a no-op
   on an initialized DB.)
2. `bash deploy/agt/start.sh --smoke-test` — preflight +
   dashboard `/health` on a scratch home + scheduler
   `--dry-run`, exits 0.
3. `python3 deploy/agt/scheduler.py --dry-run` — all 19 jobs
   listed with their next due times, 7 disabled jobs listed.
4. `python3 tests/test_agt_packaging.py` — 129 checks, 0 failed.
   `python3 tests/test_agt_restore_drill.py` — 20 checks, 0
   failed.
5. On the phone after the deploy: `bash
   deploy/agt/healthcheck.sh` exits 0 and the dashboard
   `/status` shows the new commit in `versions`.

A deploy that fails any of these is not "deployed with a small
issue" — it is not deployed. Roll back (below) and fix it on
the VM first.
