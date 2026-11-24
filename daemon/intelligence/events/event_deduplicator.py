"""Duplicate-event detection (Phase 8, 2026-11-22).

Duplicate delivery — the same event from two providers, or a
replayed observation — must produce ONE stored event. Identity is
the deterministic event ID (signature + slot); the store is the
authoritative check and this in-memory set is the fast path.

The set is bounded (default 100k IDs, FIFO eviction) so a
long-running process cannot grow without limit; eviction only
affects the fast path — the store check still catches duplicates.
"""

from __future__ import annotations

from collections import OrderedDict


class EventDeduplicator(object):
    """In-memory seen-event-ID set with a bounded size."""

    def __init__(self, capacity=100_000):
        self.capacity = capacity
        self._seen = OrderedDict()

    def seen(self, event_id):
        return event_id in self._seen

    def add(self, event_id):
        """Record an event ID. Returns False when it was already
        present (a duplicate), True when it is new."""
        if event_id in self._seen:
            self._seen.move_to_end(event_id)
            return False
        self._seen[event_id] = True
        while len(self._seen) > self.capacity:
            self._seen.popitem(last=False)
        return True

    def discard(self, event_id):
        self._seen.pop(event_id, None)

    def __len__(self):
        return len(self._seen)
