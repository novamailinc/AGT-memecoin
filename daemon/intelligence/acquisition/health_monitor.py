"""Compatibility shim (Phases 3-4).

The implementation moved to ``provider_health.py``. This module
re-exports the whole public surface so existing imports keep
working unchanged. New code imports ``provider_health`` directly.
"""

from __future__ import annotations

from .provider_health import (
    ALERT_FILE,
    MAX_SAMPLES,
    WINDOW_S,
    HealthMonitor,
    ProviderHealth,
    _percentile,
)

__all__ = [
    "ALERT_FILE",
    "MAX_SAMPLES",
    "WINDOW_S",
    "HealthMonitor",
    "ProviderHealth",
    "_percentile",
]
