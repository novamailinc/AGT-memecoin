# Backup and recovery — AGT deployment

Phase 13 finalize (2026-11-22). Backup runs hourly inside the
deployment (`forex-backup-hourly` loop in `deploy/agt/scheduler.py`),
and every backup is now verified right after it is written
(`backup_verify.py --verify-latest`: checksum, no credential
material, ledgers parse, killswitch shadow, DB integrity). The
restore drill has been RUN (see below) — it is no longer
documented-only.

**The restore drill is run against a SCRATCH home, never the live
home.** `deploy/agt/restore_drill.py --archive <archive> --home
<scratch dir>` runs it mechanically and refuses the live home.

## What the backup contains

`scripts/forex-backup` snapshots, via the SQLite backup API (a
consistent copy even while daemons write) plus plain file copies:

- `storage/local.db` — the event journal + safety state. **Note
  the archive layout: the archive stores this file as `local.db`
  at its ROOT, not at `storage/local.db`.** The restore drill
  moves it to its live path (see "Restore drill run" below); a
  naive restore that just moves the extracted copy into place
  would leave the deployment starting on a FRESH EMPTY database.
- `data/` — broker paper state (virtual balances, fills).
- `launch_scanner/` — the private moat (wallet registry + grades,
  deployer history/fingerprints, proposals). **This is the 426 MB
  moat, including the 395 MB backtest dataset** — the backup keeps it
  because it is state, but the dataset itself is never shipped in
  the deploy (see `storage_budget.md`).
- `killswitch/` — killswitch state + shadow.
- `live/` — live lane ledger + deployment marker.
- `agent.env` (non-secret config) + `install-result.json` /
  `install-state.json` / `run/heartbeat.json` (the archive stores
  the heartbeat as `heartbeat.json` at its root; the drill moves
  it to `run/heartbeat.json`).

Layout: `$FOREX_AGENT_HOME/backups/forex-backup-<UTC timestamp>.tar.gz`
plus a sha256 sidecar and a `manifest.jsonl` entry. Rotation keeps
the newest 48.

## What the backup NEVER contains

- `secrets.env` (private keys / tokens).
- `telegram/` (bot token).
- `helius/` (API keys).
- `live/keypair.json` (the live mainnet wallet key — it lives only on
  the deployment machine and is deleted from the staged copy even if
  present).
- The AGT-vault-injected env vars themselves (they are not files).

The credential bridge files written at start (`helius/api_key`,
`helius/api_key_discovery`, `telegram/bot_token`,
`chainstack/endpoint`) live under `FOREX_AGENT_HOME` and **must be
excluded from any backup that copies the home wholesale**. The
`forex-backup` script stages an explicit file list and does not copy
them; `backup_verify.py` asserts no credential material is in any
archive it checks, so a staging-list regression fails the hourly
job loudly instead of silently shipping a key.

## Off-phone backup story (manual export — still the top gap)

On the VM, `scripts/forex-backup-upload` pushes new archives to the
private Google Drive folder "Paper trading bot backups" (hourly cron,
non-fatal — a failed upload never breaks the local backup). **That
upload has no automated story on AGT yet**: the upload script and
its Drive credential live on the VM, and there is no Drive
credential in the AGT vault and no AGT snapshot API wired. On AGT
the hourly loop runs the local backup + verify only.

What exists now (finalize pass) is the documented manual export:

1. On the phone, run `python3 deploy/agt/backup_verify.py
   --export-manifest` from the deployment shell. It writes
   `backups/offphone-export-manifest.json`: every archive with
   its sha256 + size.
2. Copy the archives off the phone (AGT file download, `adb
   pull`, or USB — whichever the AGT app exposes for the
   deployment work dir). Copy the manifest with them.
3. After the copy, verify each archive on the receiving machine:
   `sha256sum -c <archive>.sha256` (or re-run
   `backup_verify.py --verify <archive>` — it checks the
   checksum, the credential exclusion, the ledgers, the
   killswitch shadow, and DB integrity).
4. Keep at least the two newest verified archives off the phone
   at all times; rotate the older ones out.

Consequence, stated plainly: until an automated off-phone copy
exists (Drive via an AGT vault credential, or an AGT snapshot
mechanism), **a lost or wiped phone loses the paper history**.
The local rotation is the only automatic copy; the manual export
above is the bridge, and it only works if someone actually runs
it. This is the single biggest recovery gap in the deployment.

## Restore procedure

Preconditions: the deployment is STOPPED (AGT app → deployment →
Stop), so no daemon writes while state is replaced. Work on a
scratch copy first; never restore over the live home in place.

1. Pick the archive: the newest `forex-backup-*.tar.gz` in
   `$FOREX_AGENT_HOME/backups/`, or the newest archive copied off
   the phone (once the manual export exists). Note its timestamp.
2. Verify integrity: `sha256sum -c <archive>.sha256`. A failed
   checksum stops the restore — pick the previous archive. (Or
   run `backup_verify.py --verify <archive>` for the full check.)
3. Extract to a scratch dir: `mkdir /tmp/restore && tar -xzf
   <archive> -C /tmp/restore`.
4. **Normalize the layout (drill finding, 2026-11-22):** move
   `local.db` → `storage/local.db` and `heartbeat.json` →
   `run/heartbeat.json` inside the scratch copy. The archive
   stores both at its root; the live home expects them at those
   paths. Skip this step and the deployment starts on a fresh
   empty database. (`restore_drill.py` does this mechanically
   and records each move.)
5. Verify the ledgers parse: every `*_ledger.json` and
   `killswitch/state.json` in the scratch copy must load as JSON
   (the restore drill script does this mechanically).
6. Verify balances match the pre-stop live files: compare
   `bankroll` in each lane ledger and the Live10 lane balances in
   `live10_demo_ledger.json` against the values recorded before
   the stop (dashboard screenshot or the last daily recap). A mismatch
   stops the restore — investigate before proceeding. Compare the
   VOID-EXCLUDED Live10 total (dashboard `live10_total`), not the
   raw ledger total — the raw total still carries the voided 91GG
   phantom (see `voided_closes.json`).
7. Swap in: move the live `$FOREX_AGENT_HOME` aside
   (`mv .forex-agent .forex-agent.pre-restore-<ts>`) and move the
   scratch copy into place. Keep the aside copy until the drill
   passes.
8. Check the killswitch shadow: `killswitch/state.json.bak` must
   exist next to `state.json`. If the primary is corrupt, the shadow
   is restore-only (never auto-used) — restore it by hand and say so
   in the drill log.
9. Start the deployment (AGT app → deployment → Start). Watch the
   dashboard: supervisor state fresh, heartbeat fresh, killswitch
   lanes as expected, no new halts.
10. Keep the aside copy for 24h. Delete it only after a full day of
    normal closes with matching balances.

## Restore drill (RUN 2026-11-22 — all steps pass)

The drill is steps 1–10 above against a scratch copy, run
mechanically by `deploy/agt/restore_drill.py`:

```
python3 deploy/agt/restore_drill.py --archive <archive> \
    --home <scratch dir> [--expected <balances JSON>]
```

Run 2026-11-22 (in `tests/test_agt_restore_drill.py`, 20 checks,
0 failed, plus a standalone run recorded in
`~/workspace/reports/phase13-finalize-2026-11-22.md`): a real
`scripts/forex-backup` run against a small fixture home produced
one archive; the drill verified it end to end — checksum,
extraction, no credential material, layout normalization
(`local.db` → `storage/local.db`, `heartbeat.json` →
`run/heartbeat.json`), ledgers parse, killswitch shadow present,
balances match the pre-drill snapshot, `db_init --check` passes,
and the deployment starts from the restored copy (credentials
preflight + dashboard `/health` against the restored home). The
dashboard on the restored copy reports the void-excluded Live10
total ($9,095.86, raw $14,336.93 labelled beside it) and excludes
both voided closes from PnL.

Two drill findings, both handled:

- **Layout quirk (above):** the archive stores `local.db` and
  `heartbeat.json` at its root. The drill normalizes both and
  the restore procedure now says so in step 4. `forex-backup`
  itself was NOT re-laid-out: existing archives (including the
  ones already on Drive) have the root layout, and the drill
  accepts both.
- **New closes are NOT proven by this drill:** the drill proves
  the restored copy boots (preflight + dashboard). New closes
  from the restored copy need the traders running — an on-phone
  drill step: start the deployment from the restored copy and
  watch for closes before deleting the aside copy.

The drill is recorded in `<restore home>/restore-drill-<date>.md`
(+ `.json`) with the archive used, the balance comparison, and
the pass/fail per step. Re-run it after any change to the backup
or restore path — never assume the procedure still works.
## If there is no backup at all

A fresh deployment with no backup to restore from starts empty:
`db_init.py` creates a fresh `local.db`, ledgers start at their
configured bankrolls, and the moat starts empty (smart-wallet
registry, deployer history, and grades rebuild from live traffic).
That is a data loss, not a crash — the bot still runs, but every
verdict that rested on history is gone. This is why the off-phone
backup gap above is the top recovery gap.
