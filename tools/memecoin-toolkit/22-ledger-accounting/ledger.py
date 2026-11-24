"""JSON paper-ledger with live-bot hygiene rules (synthetic lab).

Pure stdlib, no network.  A ledger is a single JSON file holding an
ordered list of entries.  Each entry has a ``ticket``, a ``kind``
(e.g. ``BUY`` / ``CLOSE``), a numeric timestamp ``ts``, and arbitrary
extra fields (``amount``, ``price``, ...).

Hygiene rules (fail-closed throughout):
  * Atomic save -- write to a temp file in the same directory, fsync,
    then ``os.replace`` onto the main path, so a crash mid-write can
    never leave a half-written main file as the only copy.
  * Backup copy -- before replacing a good main file, its previous
    contents are preserved as ``<path>.bak`` (the last good state).
  * Lock discipline -- ``locked()`` creates ``<path>.lock`` with
    ``O_EXCL``; a second holder raises :class:`LockHeldError` instead
    of interleaving writes.  The lock is always released on exit.
  * Idempotent entries -- a duplicate ``(ticket, kind)`` pair is
    rejected with :class:`DuplicateEntryError`; replaying the same
    fill can never double-count it.
  * Ordering -- a ``CLOSE`` whose timestamp is earlier than the
    matching ``BUY`` timestamp is rejected with
    :class:`OrderingError`, as is a ``CLOSE`` with no ``BUY`` at all
    and a ``BUY`` recorded after an existing ``CLOSE`` for that ticket.
  * Torn-write recovery -- loading a truncated / corrupt main file
    falls back to the last good backup; if both are corrupt a loud
    :class:`CorruptLedgerError` is raised, never a silent empty state.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path


class LedgerError(Exception):
    """Base class for all ledger errors."""


class LockHeldError(LedgerError):
    """Raised when the ledger lock file already exists."""


class DuplicateEntryError(LedgerError):
    """Raised when a (ticket, kind) pair is recorded twice."""


class OrderingError(LedgerError):
    """Raised when timestamps violate BUY-before-CLOSE ordering."""


class CorruptLedgerError(LedgerError):
    """Raised when neither the main file nor its backup can be read."""


def _backup_path(path: Path) -> Path:
    return Path(str(path) + ".bak")


def _lock_path(path: Path) -> Path:
    return Path(str(path) + ".lock")


def _tmp_path(path: Path) -> Path:
    return Path(str(path) + ".tmp")


def _parse_state(raw: bytes) -> dict:
    """Parse and structurally validate raw ledger bytes."""
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("ledger root is not an object")
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ValueError("ledger 'entries' is not a list")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("ledger entry is not an object")
        if "ticket" not in entry or "kind" not in entry or "ts" not in entry:
            raise ValueError("ledger entry missing ticket/kind/ts")
    return data


class Ledger:
    """An append-oriented JSON ledger stored at ``path``."""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self.entries: list[dict] = []
        self.recovered_from_backup = False
        self._load()

    # -- paths ------------------------------------------------------------
    @property
    def backup_path(self) -> Path:
        return _backup_path(self.path)

    @property
    def lock_path(self) -> Path:
        return _lock_path(self.path)

    # -- loading / recovery -------------------------------------------------
    def _load(self) -> None:
        main, backup = self.path, self.backup_path
        if not main.exists() and not backup.exists():
            self.entries = []
            return
        if main.exists():
            try:
                self.entries = _parse_state(main.read_bytes())["entries"]
                return
            except Exception as main_exc:  # noqa: BLE001 - recovery path
                if backup.exists():
                    try:
                        state = _parse_state(backup.read_bytes())
                    except Exception as bak_exc:  # noqa: BLE001
                        raise CorruptLedgerError(
                            f"ledger corrupt: main ({main_exc}) and "
                            f"backup ({bak_exc}) both unreadable at {main}"
                        ) from bak_exc
                    self.entries = state["entries"]
                    self.recovered_from_backup = True
                    return
                raise CorruptLedgerError(
                    f"ledger corrupt: main file at {main} unreadable "
                    f"({main_exc}) and no backup exists"
                ) from main_exc
        # Main missing but backup present: recover from backup.
        try:
            self.entries = _parse_state(backup.read_bytes())["entries"]
            self.recovered_from_backup = True
        except Exception as bak_exc:  # noqa: BLE001
            raise CorruptLedgerError(
                f"ledger corrupt: main file missing and backup at "
                f"{backup} unreadable ({bak_exc})"
            ) from bak_exc

    def reload(self) -> "Ledger":
        """Re-read state from disk (with the same recovery rules)."""
        self.recovered_from_backup = False
        self._load()
        return self

    # -- locking --------------------------------------------------------------
    @contextmanager
    def locked(self):
        """Hold the ledger lock file exclusively for the block.

        Raises :class:`LockHeldError` if another holder is active.
        The lock file is removed on exit, even on exception.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.lock_path),
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise LockHeldError(
                f"ledger lock already held: {self.lock_path}"
            ) from exc
        try:
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            yield self
        finally:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

    def is_locked(self) -> bool:
        return self.lock_path.exists()

    # -- entries ----------------------------------------------------------------
    def _find(self, ticket, kind=None):
        for entry in self.entries:
            if entry["ticket"] == ticket and (kind is None or entry["kind"] == kind):
                return entry
        return None

    def has_entry(self, ticket, kind) -> bool:
        return self._find(ticket, kind) is not None

    def get_entry(self, ticket, kind):
        entry = self._find(ticket, kind)
        return dict(entry) if entry is not None else None

    def tickets(self) -> list:
        seen: list = []
        for entry in self.entries:
            if entry["ticket"] not in seen:
                seen.append(entry["ticket"])
        return seen

    def entries_for(self, ticket) -> list[dict]:
        return [dict(e) for e in self.entries if e["ticket"] == ticket]

    def __len__(self) -> int:
        return len(self.entries)

    def add_entry(self, ticket, kind, ts, **fields) -> dict:
        """Validate and append an entry in memory (call ``save`` to persist).

        Fail-closed: empty ticket/kind, non-numeric/negative ``ts``,
        duplicate ``(ticket, kind)`` and BUY/CLOSE ordering violations
        all raise before anything is appended.
        """
        if ticket is None or str(ticket) == "":
            raise ValueError(f"ticket must be non-empty, got {ticket!r}")
        if kind is None or str(kind) == "":
            raise ValueError(f"kind must be non-empty, got {kind!r}")
        if ts is None or isinstance(ts, bool) or not isinstance(ts, (int, float)):
            raise ValueError(f"ts must be a number, got {ts!r}")
        if ts < 0:
            raise ValueError(f"ts must be >= 0, got {ts!r}")
        if self.has_entry(ticket, kind):
            raise DuplicateEntryError(
                f"duplicate entry: ticket={ticket!r} kind={kind!r} already recorded"
            )
        if kind == "CLOSE":
            buy = self._find(ticket, "BUY")
            if buy is None:
                raise OrderingError(
                    f"CLOSE for ticket {ticket!r} has no matching BUY"
                )
            if ts < buy["ts"]:
                raise OrderingError(
                    f"CLOSE ts {ts} is before BUY ts {buy['ts']} "
                    f"for ticket {ticket!r}"
                )
        if kind == "BUY":
            close = self._find(ticket, "CLOSE")
            if close is not None and close["ts"] < ts:
                raise OrderingError(
                    f"BUY ts {ts} is after existing CLOSE ts {close['ts']} "
                    f"for ticket {ticket!r} (CLOSE-before-BUY)"
                )
        entry = {"ticket": ticket, "kind": kind, "ts": ts}
        entry.update(fields)
        self.entries.append(entry)
        return dict(entry)

    # -- persistence ---------------------------------------------------------
    def save(self) -> None:
        """Atomically persist the ledger, preserving the last good copy.

        Writes ``<path>.tmp``, fsyncs, copies the current (valid) main
        file to ``<path>.bak``, then renames the temp file over main.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "entries": self.entries},
                             indent=2, sort_keys=True).encode("utf-8")
        tmp = _tmp_path(self.path)
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        # Preserve the previous good main file as the backup *before*
        # replacing it -- only if it actually parses.
        if self.path.exists():
            try:
                _parse_state(self.path.read_bytes())
            except Exception:  # noqa: BLE001 - corrupt main: keep old backup
                pass
            else:
                self.backup_path.write_bytes(self.path.read_bytes())
        os.replace(tmp, self.path)
        # Keep the backup in step once we know the new main is good, so
        # a torn main after this save still has an identical fallback.
        # (The previous-good copy above is what a mid-save crash uses;
        # after a clean save both copies hold the new state.)
        try:
            _parse_state(self.path.read_bytes())
            self.backup_path.write_bytes(self.path.read_bytes())
        except Exception:  # noqa: BLE001 - never fail a good save on backup
            pass

    def add_and_save(self, ticket, kind, ts, **fields) -> dict:
        """Add an entry and persist it under the lock."""
        with self.locked():
            entry = self.add_entry(ticket, kind, ts, **fields)
            self.save()
            return entry
