"""Acquisition layer (Phases 2-4): standardized provider adapters,
central provider router, quota-aware scheduling, usage management,
health monitoring, WebSocket management, subscription bookkeeping,
gap detection + backfill, and the shadow-comparison harness.

Phases 3-4 structure (§8):
- ``rpc_client`` — the unified per-provider call seam (successor of
  Phase 2's ``rpc.py``, kept as a compatibility shim).
- ``providers/`` — one standardized adapter per provider
  (``base`` / ``alchemy`` / ``chainstack`` / ``helius`` / ``public``);
  capability matching comes from ``provider_capabilities``.
- ``provider_router`` — THE central routing layer. All provider
  selection passes through it; no other module makes a routing
  decision.
- ``provider_manager`` — provider lifecycle + stats; delegates all
  selection to the router.
- ``request_scheduler`` — provider-specific quota-aware scheduling.
- ``provider_usage`` + ``usage_tracker`` — per-account usage store
  and the §7 usage manager (budgets, warn/stop, disablement).
- ``provider_health`` — per-provider health (``health_monitor`` is
  a compatibility shim).
- ``websocket_manager`` / ``subscription_manager`` /
  ``event_backfill`` — subscriptions, gap detection, recovery.
- ``shadow`` — the VERIFICATION harness (shadow comparison +
  benchmarks), not a cutover path.
- ``probes`` / ``benchmark`` — provider probes and benchmarks.

Shadow mode only — nothing downstream consumes a shadow answer;
Helius stays the live leg until a workload's removal criterion is
met. Live Alchemy/Chainstack verification from this VM is NOT
possible (url_path_segment surrogate swap UNPROVEN, corrected
2026-11-22 ~02:02 PST; the keys may be wrong after all) and is
DEFERRED to the AGT target; adapters are verified here with mocked
transport + recorded fixtures.
"""

from . import benchmark  # noqa: F401
from . import event_backfill  # noqa: F401
from . import health_monitor  # noqa: F401
from . import probes  # noqa: F401
from . import provider_capabilities  # noqa: F401
from . import provider_health  # noqa: F401
from . import provider_manager  # noqa: F401
from . import provider_router  # noqa: F401
from . import provider_usage  # noqa: F401
from . import providers  # noqa: F401
from . import request_scheduler  # noqa: F401
from . import rpc  # noqa: F401
from . import rpc_client  # noqa: F401
from . import shadow  # noqa: F401
from . import subscription_manager  # noqa: F401
from . import usage_tracker  # noqa: F401
from . import websocket_manager  # noqa: F401
