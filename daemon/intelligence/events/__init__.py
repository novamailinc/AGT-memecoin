"""Intelligence events package (Phase 8, 2026-11-22).

Cross-provider event normalization + verification (spec §9):
one event, one logical record. See event_models.py for the schema,
event_normalizer.py for the merge + §11 verification rules,
event_store.py for persistence, event_processor.py for the
pipeline, event_recovery.py for checkpoints / retry / backfill.

This package must NOT import the launch_scanner __init__ monolith
(dual-import hazard).
"""

from .event_deduplicator import EventDeduplicator  # noqa: F401
from .event_models import LogicalEvent, event_id_for  # noqa: F401
from .event_normalizer import EventNormalizer  # noqa: F401
from .event_processor import EventProcessor, TokenStateRegistry  # noqa: F401
from .event_recovery import (  # noqa: F401
    CheckpointTracker,
    RetryQueue,
    backfill_after_disconnect,
    reprocess_failed,
)
from .event_store import CorruptStoreError, EventStore  # noqa: F401
