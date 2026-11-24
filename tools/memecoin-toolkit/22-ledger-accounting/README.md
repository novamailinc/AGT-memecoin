# Ledger Accounting (lab)

A from-scratch, pure-stdlib lab for a JSON paper ledger with strict hygiene rules: atomic saves, a lock-file discipline, idempotent entries, timestamp ordering, and torn-write recovery. Everything here is synthetic — no network, no live bot code, no real ledger data — and every test round-trips through real temporary files, so the on-disk behaviour (not just the in-memory logic) is what gets verified.

## What it does

Module `ledger.py` — one class, `Ledger(path)`, holding an ordered list of entries, each a dict with `ticket`, `kind`, a numeric `ts`, and arbitrary extra fields:

- `Ledger(path)` — loads state from disk. A missing main file and missing backup means a fresh empty ledger. Fail-closed on corruption: a truncated/corrupt main file falls back to the last good backup (`<path>.bak`, with `recovered_from_backup` set to `True`); if both copies are unreadable, or the main file is corrupt with no backup, construction raises `CorruptLedgerError` — never a silent empty state.
- `Ledger.locked()` — a context manager holding `<path>.lock`, created with `O_EXCL`. A second holder (same or another instance) raises `LockHeldError` with the lock path in the message; the lock is always released on exit, including on exception. `Ledger.is_locked() -> bool` reports whether the lock file exists.
- `Ledger.add_entry(ticket, kind, ts, **fields) -> dict` — validates and appends in memory. Fail-closed: empty/`None` ticket or kind and non-numeric, boolean, or negative `ts` raise `ValueError`; a duplicate `(ticket, kind)` pair raises `DuplicateEntryError`; a `CLOSE` with no matching `BUY`, a `CLOSE` timestamped before its `BUY`, or a `BUY` that would follow an existing `CLOSE` raises `OrderingError`. A failed add never appends.
- `Ledger.save() -> None` — atomic persistence: write `<path>.tmp`, fsync, preserve the previous good main file as the backup, then `os.replace` the temp file over the main path. No temp file is left behind after a clean save.
- `Ledger.add_and_save(ticket, kind, ts, **fields) -> dict` — add + save under the lock, the normal write path.
- `Ledger.reload() -> Ledger` — re-read from disk with the same recovery rules. Lookups: `has_entry(ticket, kind)`, `get_entry(ticket, kind)` (returns a copy), `tickets()`, `entries_for(ticket)`, and `len(ledger)`.

Errors `LockHeldError`, `DuplicateEntryError`, `OrderingError`, and `CorruptLedgerError` all subclass `LedgerError`, so callers can catch one base type or the specific failure.

`demo.py` records a round trip through real temp files, then exercises duplicate rejection, ordering rejection, lock contention, torn-write recovery, and the both-copies-corrupt failure.

## Test suite

45 tests, all passing (`pytest -q`: "45 passed"). Covered: fresh-ledger emptiness and no file until save; single- and multi-entry round trips through real temp files preserving order and extra fields; the saved file being valid JSON with a `version`/`entries` shape; no leftover `.tmp` file; the backup file appearing and holding the saved state; duplicate `(ticket, kind)` rejection in memory and after reload, with same-ticket/different-kind and same-kind/different-ticket allowed and failed adds not appending; CLOSE-before-BUY timestamp rejection, the exact-timestamp boundary being allowed, CLOSE-without-BUY rejection, and non-ordered kinds being exempt; `ValueError` on empty tickets/kinds and bad timestamps, with `ts=0` allowed; the lock blocking a second holder on the same instance and on a second instance, release after the block and after an exception, the error message naming the lock, and a stale lock file blocking until removed; torn-write recovery from truncated, empty, garbage, structurally wrong, and missing main files; the loud `CorruptLedgerError` when both copies are corrupt and when a corrupt main has no backup; recovery-then-save repairing the main file; lookup helpers and returned entries being copies; nested directories being created on save; and `reload()` picking up an external good write. All paths come from pytest's `tmp_path` — no fixed paths anywhere.

## Captured output

Verbatim output of `python demo.py`:

```
=== Ledger accounting lab (synthetic JSON paper ledger) ===
Fresh ledger at temp path: 0 entries

-- Recording a round trip --
Entries on disk after reload: 3
  T-001 BUY ts=1000 price=0.5
  T-001 CLOSE ts=1600 price=1.5
  T-002 BUY ts=2000 price=0.25

-- Duplicate rejection (idempotency) --
Duplicate correctly rejected: duplicate entry: ticket='T-001' kind='BUY' already recorded

-- Ordering rejection --
CLOSE-before-BUY correctly rejected: CLOSE ts 1500 is before BUY ts 2000 for ticket 'T-002'

-- Lock discipline --
Lock held inside block: is_locked=True
Second writer correctly blocked: LockHeldError
Lock released after block: is_locked=False

-- Torn-write recovery --
Main file truncated; recovered_from_backup=True, entries=4

-- Both copies corrupt: loud failure --
Both-corrupt correctly raised: ledger corrupt: main (Unterminated string starting at: line 1 column 39 ...
```

## What this mirrors in the live bot

This mirrors the mirror/CEO ledger hardening in the live bot: the paper ledgers that judge card quality (the per-chain mirror ledger and the $100 CEO account ledger) follow exactly these rules — atomic saves so a crash mid-write cannot destroy the record the live-mode gates depend on, duplicate `(ticket, kind)` rejection so a replayed fill can never double-count, CLOSE-before-BUY rejection so the ledger fails loud instead of quietly absorbing impossible history, and recovery from the last good copy with a specific error when nothing readable remains, because a silently emptied ledger would fake a clean slate. It is a teaching/checking model of that discipline only; it shares no code with the live bot.

## Self-exam

1. Why must an atomic save write the temp file in the same directory as the main file rather than in a global temp directory?
2. A duplicate `(ticket, kind)` rejection makes entry-adding idempotent — what goes wrong in the accounting if a replayed BUY fill is appended a second time instead?
3. Why is a CLOSE timestamped before its BUY treated as corruption to reject, rather than an entry to sort into place?
4. In torn-write recovery, why must "both copies corrupt" raise a loud, specific error instead of starting from an empty ledger?
5. The lock here is a lock *file* created with `O_EXCL` — why does the exclusive-create flag matter, and what race appears if the lock is implemented as "check the file exists, then create it"?
6. Why is the previous main file only preserved as the backup after it has been parsed successfully?
