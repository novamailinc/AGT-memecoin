# Ledger Accounting — study notes

## Core ideas (teach-back)

A paper ledger is the judge of everything else. If the ledger can be silently corrupted, double-counted,
or emptied, then every win rate and expectancy computed from it is fiction — and decisions built on
fiction lose money.

This lab builds one class, `Ledger(path)`, around a single JSON file holding an ordered list of entries.
Each entry is a dict with:

- `ticket` — which position this is,
- `kind` — e.g. `BUY` or `CLOSE`,
- `ts` — a numeric timestamp,
- plus arbitrary extra fields such as `price` and `amount`.

On disk the file has the shape `{"version": 1, "entries": [...]}`.

Five hygiene rules do all the work:

1. **Atomic saves.** A crash mid-write must never leave a half-written main file as the only copy.
2. **A
  backup of the last good state.** Recovery needs something trustworthy to fall back to. 3. **Lock
  discipline.** Two writers must never interleave. 4. **Idempotent entries.** Replaying the same fill
  must never record it twice. 5. **Timestamp ordering.** Impossible history — a close before its buy —
  is rejected loudly, never sorted quietly into place.

The stance throughout is *fail-closed*: when something is wrong, raise a specific error rather than
guess, coerce, or start over empty. All four errors — `LockHeldError`, `DuplicateEntryError`,
`OrderingError`, `CorruptLedgerError` — subclass `LedgerError`, so a caller can catch the base type or
the specific failure.

Loading follows recovery rules, not optimism:

- No main file and no backup → a genuinely fresh, empty ledger.
- Main file parses → use it.
- Main file
  corrupt, backup parses → use the backup and set `recovered_from_backup = True`.
- Main file missing,
  backup present → recover from backup.
- Both unreadable, or corrupt main with no backup → raise
  `CorruptLedgerError`. Never a silent empty state.

A silently emptied ledger is the worst outcome of all, because it fakes a clean slate: a lane with a
losing history would suddenly look brand new.

Lookups are deliberately boring and safe: `has_entry(ticket, kind)`, `get_entry(ticket, kind)` (returns
a *copy*), `tickets()`, `entries_for(ticket)`, `len(ledger)`, and `reload()` to re-read from disk under
the same recovery rules.

## Key derivations & formulas

No statistics here — the derivations are state invariants and the exact order of operations that
preserves them.

### The uniqueness invariant

```text
For any two entries e1 != e2 in the ledger:
    NOT (e1.ticket == e2.ticket AND e1.kind == e2.kind)
```

`add_entry` enforces it before appending: a duplicate `(ticket, kind)` raises `DuplicateEntryError`.
Note what is *allowed*: same ticket, different kind (the normal BUY then CLOSE), and same kind,
different ticket. A failed add never appends — validation happens first.

### The ordering invariant

```text
For a ticket with a BUY at ts_b and a CLOSE at ts_c:
    ts_c >= ts_b        (equality is allowed)
```

Enforced from both directions in `add_entry`:

- Adding a `CLOSE` with no matching `BUY` → `OrderingError`.
- Adding a `CLOSE` with `ts < buy.ts` →
  `OrderingError`.
- Adding a `BUY` when a `CLOSE` already exists with `close.ts < new ts` →
  `OrderingError`.

Kinds other than BUY/CLOSE are exempt from ordering. Field validation also fails closed: empty/`None`
ticket or kind, and non-numeric, boolean, or negative `ts`, raise `ValueError`. `ts = 0` is allowed.

### The atomic save sequence

`save()` performs, in order:

1. Serialise `{"version": 1, "entries": [...]}`.
2. Write it to `<path>.tmp` — *in the same directory* as
  the main file — then `flush` and `fsync`. 3. If the current main file exists *and parses*, copy it to
  `<path>.bak` (the previous good state). If it does not parse, keep the older backup instead. 4.
  `os.replace(tmp, path)` — an atomic rename on the same filesystem, which is why the temp file must
  live in the same directory, not a global temp dir. 5. After the replace, re-parse the new main file
  and refresh the backup to match it, so a later torn main still has an identical fallback. A failure in
  this last step never fails the good save itself.

No `.tmp` file is left behind after a clean save.

### The lock protocol

`locked()` is a context manager:

```text
fd = os.open(<path>.lock, O_CREAT | O_EXCL | O_WRONLY)
```

`O_EXCL` makes "check it does not exist, then create it" a single atomic operation. Without it, two
writers could both pass the check in the gap before either creates the file. The holder writes its pid
into the lock file. A second holder — same instance or another — gets `LockHeldError` with the lock path
in the message. The lock file is removed on exit, *including* on exception. A stale lock file blocks
until removed; that is deliberate — the lab does not guess whether a stale lock is safe to steal.
`add_and_save(...)` is the normal write path: add + save, under the lock.

## Worked example

From `demo.py`, through real temporary files:

1. Fresh ledger: `0 entries`.
2. Three writes via `add_and_save`:
- `T-001 BUY ts=1000 price=0.5`
- `T-001 CLOSE ts=1600 price=1.5`
- `T-002 BUY ts=2000 price=0.25`

After reload from disk: 3 entries, in order, extra fields preserved.

3. Re-adding `T-001 BUY` at ts=1100 → `DuplicateEntryError: duplicate entry: ticket='T-001' kind='BUY' already recorded`.
4. Adding `T-002 CLOSE` at ts=1500 → `OrderingError: CLOSE ts 1500 is before BUY ts 2000 for ticket 'T-002'`.
5. Inside `with ledger.locked():` — `is_locked() == True`; a second instance's `locked()` is blocked with `LockHeldError`; after the block, `is_locked() == False`.
6. A fourth entry (`T-003 BUY ts=3000`) is saved, then the main file is truncated mid-JSON. Reloading gives `recovered_from_backup=True, entries=4`.
7. The backup is then overwritten with `not json at all`. Loading now raises `CorruptLedgerError` — loudly, with both underlying parse errors named in the message.

## Trap log

- T1. **The silent empty ledger.** Falling back to "start fresh" on corruption erases a losing history
  and fakes a clean slate for the gates that read the ledger.
- T2. **The replayed fill.** Without
  `(ticket, kind)` uniqueness, a retried or replayed BUY appends twice and every P/L figure
  double-counts it.
- T3. **Sorting impossible history into place.** A CLOSE before its BUY is
  corruption, not disorder. Sorting it away destroys the evidence that something upstream is broken.
- T4. **Check-then-create locking.** Testing for a lock file and then creating it is two steps with a
  race between them; `O_EXCL` collapses it to one atomic step.
- T5. **Temp file in the wrong
  directory.** `os.replace` is only atomic within one filesystem; a temp file on another mount turns the
  "atomic" save into a copy that can be torn.
- T6. **Backing up a corrupt main.** Copying the current
  main to `.bak` without parsing it first would overwrite the last *good* backup with garbage,
  destroying the recovery path at the moment it is needed.
- T7. **Returning live entry dicts.** If
  `get_entry` returned the stored dict itself, a caller mutating it would rewrite history in memory. It
  returns a copy.
- T8. **Booleans as timestamps.** `True` is numeric in Python; without an explicit
  exclusion, `ts=True` would be accepted as ts=1.
- T9. **A lock leaked on exception.** If the lock were
  released only on the happy path, one crashed writer would deadlock every later writer. Release is in a
  `finally`.

## Source flags

- The save sequence, lock protocol, error types, and recovery branches above are read directly from
  `ledger.py`. [S]
- Coverage claims — 45 tests passing, round-trips through real `tmp_path` files,
  torn-write variants (truncated, empty, garbage, structurally wrong, missing) — are the README's
  test-suite account. [S]
- That this mirrors the live bot's mirror/CEO paper ledgers and the live-mode
  gates depending on them is the README's mirroring claim. [M]
- The "worst outcome" ranking of a
  silently emptied ledger over a loud failure is reasoned here from the fail-closed design, and matches
  the README's own rationale. [R]

## One-paragraph verdict

This toolkit proves that the core ledger disciplines — atomic same-directory saves with fsync and rename, a parsed-before-preserved backup, `O_EXCL` lock files, `(ticket, kind)` idempotency, and BUY-before-CLOSE ordering with loud corruption errors — can be implemented in pure stdlib and verified against real files, including torn writes and the both-copies-corrupt case. It does not prove anything about the correctness of the *contents* a real bot writes, about concurrent writers across machines or filesystems, or about performance at scale; it is a hygiene model for one JSON file, and its value is the failure modes it refuses to hide.
