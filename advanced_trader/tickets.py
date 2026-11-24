"""Monotonic ticket allocation, persisted with atomic writes.

The failure this exists to prevent: ticket numbers recycled across
restarts, so a new position overwrote an old one and positions were
silently lost. A ticket is issued exactly once, ever.

* The counter lives in a small JSON file: ``{"next_ticket": N}``.
* Every allocation persists *before* the ticket is returned. If the
  persist fails, the allocation fails — fail-closed, no ticket issued.
* Writes are atomic (temp file + ``os.replace``) so a crash mid-write
  can never corrupt the counter.
* A corrupt state file raises instead of silently resetting to 1:
  resetting would reuse tickets, which is exactly the bug.

Ticket allocation is the ONLY place in the hot trading path that does
file I/O, and it happens on buy only — never inside a decision
(``evaluate`` / ``manage`` are pure and I/O-free).
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class TicketAllocator:
    """Never-reused, monotonically increasing ticket numbers."""

    def __init__(self, state_file: str | Path, start: int = 1) -> None:
        self._file = Path(state_file)
        self._start = max(1, int(start))
        self._next = self._start
        self.load()

    # -- persistence ------------------------------------------------------
    def load(self) -> None:
        """Load the counter from disk. Missing file -> start value.
        Corrupt file -> raise (fail-closed, never silently reuse)."""
        if not self._file.exists():
            self._next = self._start
            return
        try:
            data = json.loads(self._file.read_text())
            nxt = int(data["next_ticket"])
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise ValueError(
                f"ticket state file {self._file} is corrupt: {exc}"
            ) from exc
        if nxt < self._start:
            raise ValueError(
                f"ticket state file {self._file} has next_ticket={nxt}, "
                f"below start={self._start}"
            )
        self._next = nxt

    def _save(self) -> None:
        tmp = self._file.with_suffix(self._file.suffix + ".tmp")
        tmp.write_text(json.dumps({"next_ticket": self._next}))
        os.replace(tmp, self._file)

    # -- allocation -------------------------------------------------------
    def next_ticket(self) -> int:
        """Allocate the next ticket. Persists first; raises on failure
        without issuing a ticket."""
        ticket = self._next
        self._next += 1
        try:
            self._save()
        except OSError:
            self._next -= 1
            raise
        return ticket

    def peek(self) -> int:
        """The ticket that will be issued next (no allocation)."""
        return self._next
