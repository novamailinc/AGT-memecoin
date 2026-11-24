# Storage budget — AGT deployment

Phase 13 skeleton. Sizes below were measured on the VM on 2026-11-22
and are the basis for what ships, what seeds from a backup, and what
never leaves the VM.

## Measured sizes (2026-11-22)

| Item | Size | Ships in the git deploy? |
|---|---|---|
| Repo checkout (`Forex-agent-main`) | 107 MB | **Yes** — this is the deployment. |
| `storage/local.db` (VM copy) | 1.4 GB | **No** — created fresh on AGT by `db_init.py`. |
| `local.db` WAL file (VM copy) | 8.7 GB | **No** — see WAL policy below. |
| `~/.forex-agent/backups/` (103 archives) | 25 GB | **No** — backups stay on the VM. |
| `launch_scanner/` moat (VM state) | 426 MB | **No** — seeds from a backup via the restore procedure. |
| `launch_scanner/backtest_dataset.jsonl` | **395 MB** | **No — never ships.** See below. |
| `data/` (broker paper state) | 420 KB | **No** — seeds from a backup via the restore procedure. |
| `killswitch/` state | ~70 KB | **No** — seeds from a backup via the restore procedure. |
| `live/` (live lane ledger + marker) | small | **No** — seeds from a backup via the restore procedure. |

## What ships

Only the repo checkout (107 MB), which AGT downloads as a GitHub
tarball. Everything else is state, and state is either created fresh
on first start or seeded once from a chosen VM backup through the
restore procedure (`backup_recovery.md`).

## The 395 MB backtest dataset does NOT ship

`launch_scanner/backtest_dataset.jsonl` (395 MB, inside the 426 MB
moat) is a research artifact: historical closes used for backtests
and calibration. It is not read by any live lane, any trader, or any
daemon at runtime. Shipping it would:

- nearly quintuple the deployment size for zero runtime benefit;
- burn phone storage and the AGT 1 GiB add-on/package budget headroom
  on every re-deploy;
- risk the deployment being rejected or slow to restore after a
  phone reboot (AGT restores deployments on service start).

It stays on the VM. If a future phase needs backtest data on the
phone, that phase must justify a sampled subset (e.g. the last 90
days) and a size cap — not the full file.

## `local.db` / WAL growth policy

The VM's database is 1.4 GB with an 8.7 GB WAL file. That WAL grew
because nothing ever checkpointed it. On AGT:

- `db_init.py` sets `journal_mode=WAL`,
  `wal_autocheckpoint=1000`, `synchronous=NORMAL` on creation, so the
  WAL checkpoints automatically and cannot grow without bound.
- The hourly backup loop runs `scripts/forex-backup`, which copies
  the DB through the SQLite backup API (a consistent snapshot even
  while daemons write). A backup is itself a checkpoint opportunity.
- **Alert thresholds** (checked by the daily self-audit loop and
  visible on the dashboard via `storage` — finalize pass adds the
  field): warn when `local.db` exceeds 2 GB or the WAL exceeds
  500 MB; page the operator (Telegram) when `local.db` exceeds 4 GB.
- **No automatic pruning in this skeleton.** `event_journal` is
  append-only and the verdict chain reads it. Deleting old events to
  save space would delete history the killswitch and CEO verdicts
  may need. Pruning rules (e.g. keep 90 days of events, archive the
  rest into the backup rotation) are a finalize-pass decision with
  the operator, not a silent default.

## Backup rotation on AGT

`scripts/forex-backup` keeps the newest 48 archives (rotation 48).
At the VM's observed archive size (~250 MB each — 25 GB / 103
archives), 48 archives is ~12 GB. That is the standing cap; the
finalize pass must confirm the phone has room for it plus the live
database, or lower the cap in the deployment config. Archives older
than the rotation are deleted by the backup script itself.

## What the phone needs

Rough budget for the deployment after seeding: repo 107 MB + seeded
state (~35 MB excluding the backtest dataset) + fresh `local.db`
(starts near-empty, grows with events) + 48 backup archives (up to
~12 GB at VM archive sizes — confirm against actual phone storage in
the finalize pass). If the phone is short on space, the first lever
is the backup rotation cap, never the live ledgers or the killswitch
state.
