"""Intelligence signals package (Phase 11, 2026-11-22).

Signal-driven fusion engine + decision journal. SHADOW MODE ONLY:
nothing here calls a trading path or changes live state.

This package must NOT import the launch_scanner __init__ monolith
(dual-import hazard); it imports only stdlib + sibling
intelligence modules.
"""

from .decision_journal import (  # noqa: F401
    CorruptJournalError,
    DecisionJournal,
)
from .fusion import (  # noqa: F401
    DECISION_BLOCK,
    DECISION_QUALIFIED,
    DECISION_REVIEW,
    DECISION_SKIP,
    DECISION_WATCH,
    FusionEngine,
    ShadowFusionRunner,
    observations_from_events,
)
from .shadow_reader import (  # noqa: F401
    ShadowReaderError,
    close_signal_provider,
    conviction_observation,
    deployer_band_observation,
    grad_select_filters_observation,
    live_safety_observation,
    regime_observation,
    regime_restrictions,
    safety_observation,
    signals_for_close,
    veto_observation,
)
