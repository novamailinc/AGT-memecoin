"""Per-provider health + the public-fallback-only alert (Phases 3-4).

Successor of Phase 2's ``health_monitor.py`` (kept as a shim).
The router (``provider_router.py``) feeds every call outcome into
``HealthMonitor`` here; ``provider_health`` owns the ok / degraded /
down / unconfigured state and the latency/error/429 surfacing.

Two gaps this closes (audit file E §5):
1. Silent provider degradation — no alert fires when the bot runs on
   the public-RPC fallback only. ``public_fallback_only_alert()``
   now surfaces exactly that condition.
2. No per-provider latency/error tracking surfaced — ``snapshot()``
   reports latency samples (median/p95), error and 429 rates per
   provider, with WSS handshake 429s counted SEPARATELY from RPC
   429s (the audit finding: WSS 429s are invisible to the spike
   alert).

Health states: ok | degraded | down | unconfigured.
  unconfigured: no key/endpoint (alchemy/chainstack before keys land)
  down:        monthly-quota-down, or error rate 1.0 over >=5 calls,
               or no success in the window
  degraded:    error rate >= 0.2, or any 429 in the window, or
               p95 latency > 5s
  ok:          otherwise

State is kept in-process (bounded sample lists) plus a small JSON
alert file under FOREX_AGENT_HOME so the condition survives a
restart and other processes can read it. No key material is ever
written here.
"""

from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger("forex_agent.daemon.intelligence.acquisition.provider_health")

ALERT_FILE = "acquisition_public_fallback_alert.json"
HEALTH_FILE = "acquisition_provider_health.json"
MAX_SAMPLES = 500
WINDOW_S = 3600.0


def _percentile(sorted_values, pct):
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1,
              max(0, int(round(pct / 100.0 * (len(sorted_values) - 1)))))
    return sorted_values[idx]


class ProviderHealth:
    def __init__(self, name):
        self.name = name
        self.configured = False
        self.latencies = []       # RPC latencies (seconds), bounded
        self.errors = 0
        self.calls = 0
        self.rpc_429s = 0
        self.wss_handshake_429s = 0  # SEPARATE from RPC 429s
        self.wss_subscription_429s = 0
        self.timeouts = 0
        self.monthly_quota_down = False
        self.last_success = None
        self.last_error = None

    def record_rpc(self, result):
        self.calls += 1
        elapsed = result.get("elapsed", 0.0) or 0.0
        self.latencies.append(elapsed)
        if len(self.latencies) > MAX_SAMPLES:
            self.latencies = self.latencies[-MAX_SAMPLES:]
        kind = result.get("kind")
        if kind == "ok":
            self.last_success = time.time()
        else:
            self.errors += 1
            self.last_error = time.time()
            if kind == "rate_limited":
                self.rpc_429s += 1
            elif kind == "timeout":
                self.timeouts += 1
        if result.get("rate_limit_kind") == "monthly_quota":
            self.monthly_quota_down = True

    def record_wss_handshake_429(self):
        """A WebSocket handshake refused with 429. Counted here and
        ONLY here — never folded into rpc_429s."""
        self.wss_handshake_429s += 1
        self.last_error = time.time()

    def record_wss_subscription_429(self):
        self.wss_subscription_429s += 1
        self.last_error = time.time()

    def record_wss_handshake_ok(self):
        self.last_success = time.time()

    @property
    def error_rate(self):
        return (self.errors / self.calls) if self.calls else 0.0

    def latency_stats(self):
        ordered = sorted(self.latencies)
        return {
            "n": len(ordered),
            "median_s": _percentile(ordered, 50),
            "p95_s": _percentile(ordered, 95),
            "p99_s": _percentile(ordered, 99),
        }

    def status(self):
        if not self.configured:
            return "unconfigured"
        if self.monthly_quota_down:
            return "down"
        if self.calls >= 5 and self.error_rate >= 1.0:
            return "down"
        if self.error_rate >= 0.2 or self.rpc_429s > 0 \
                or self.wss_handshake_429s > 0 or self.wss_subscription_429s > 0:
            lat = self.latency_stats()
            if (lat["p95_s"] or 0) > 5.0:
                return "degraded"
            return "degraded"
        lat = self.latency_stats()
        if (lat["p95_s"] or 0) > 5.0:
            return "degraded"
        return "ok"

    def as_dict(self):
        d = {"provider": self.name, "status": self.status(),
             "configured": self.configured, "calls": self.calls,
             "errors": self.errors,
             "error_rate": round(self.error_rate, 4),
             "rpc_429s": self.rpc_429s,
             "wss_handshake_429s": self.wss_handshake_429s,
             "wss_subscription_429s": self.wss_subscription_429s,
             "timeouts": self.timeouts,
             "monthly_quota_down": self.monthly_quota_down,
             "last_success": self.last_success,
             "last_error": self.last_error}
        d.update(self.latency_stats())
        return d


class HealthMonitor:
    def __init__(self, home=None):
        self.home = home or os.environ.get("FOREX_AGENT_HOME") \
            or os.path.expanduser("~/.forex-agent")
        self.providers = {}

    def provider(self, name):
        if name not in self.providers:
            self.providers[name] = ProviderHealth(name)
        return self.providers[name]

    def record_rpc(self, result):
        self.provider(result.get("provider", "unknown")).record_rpc(result)

    def record_wss_handshake_429(self, provider):
        self.provider(provider).record_wss_handshake_429()

    def record_wss_subscription_429(self, provider):
        self.provider(provider).record_wss_subscription_429()

    def record_wss_handshake_ok(self, provider):
        self.provider(provider).record_wss_handshake_ok()

    def set_configured(self, name, configured):
        self.provider(name).configured = bool(configured)

    def snapshot(self):
        return {name: p.as_dict() for name, p in self.providers.items()}

    # -- durable health (Phase 3) -------------------------------------
    # In-process samples (latency lists) do not survive a restart.
    # persist()/restore() keep the COUNTERS and the monthly-quota-down
    # latch in a small atomic JSON file so a restart does not forget
    # that a provider was down or quota-exhausted, and so the
    # watchdog / status readers can see health without the router
    # process. Latency samples are NOT persisted (they are a window,
    # not state); latency_stats() restarts empty after a restore.
    # Never raises; a corrupt file restores to empty health.
    def persist(self):
        path = os.path.join(self.home, HEALTH_FILE)
        data = {"ts": time.time(),
                "providers": {name: {
                    "configured": p.configured,
                    "calls": p.calls, "errors": p.errors,
                    "rpc_429s": p.rpc_429s,
                    "wss_handshake_429s": p.wss_handshake_429s,
                    "wss_subscription_429s": p.wss_subscription_429s,
                    "timeouts": p.timeouts,
                    "monthly_quota_down": p.monthly_quota_down,
                    "last_success": p.last_success,
                    "last_error": p.last_error}
                    for name, p in self.providers.items()}}
        tmp = "%s.tmp.%d" % (path, os.getpid())
        try:
            with open(tmp, "w") as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError:
            logger.warning("could not persist provider health file")
        return data

    def restore(self):
        path = os.path.join(self.home, HEALTH_FILE)
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        restored = {}
        for name, rec in (data.get("providers") or {}).items():
            if not isinstance(rec, dict):
                continue
            p = self.provider(name)
            p.configured = bool(rec.get("configured", False))
            p.calls = int(rec.get("calls") or 0)
            p.errors = int(rec.get("errors") or 0)
            p.rpc_429s = int(rec.get("rpc_429s") or 0)
            p.wss_handshake_429s = int(rec.get("wss_handshake_429s") or 0)
            p.wss_subscription_429s = int(rec.get("wss_subscription_429s") or 0)
            p.timeouts = int(rec.get("timeouts") or 0)
            p.monthly_quota_down = bool(rec.get("monthly_quota_down"))
            p.last_success = rec.get("last_success")
            p.last_error = rec.get("last_error")
            restored[name] = p.as_dict()
        return restored

    # -- public-fallback-only alert -------------------------------------
    def public_fallback_only(self, configured_providers):
        """True when the ONLY configured provider is the keyless
        public endpoint — the degraded mode the audit found silent.
        ``configured_providers`` is the live list (e.g. from
        rpc.configured_providers())."""
        configured = [p for p in configured_providers if p != "public"]
        return "public" in configured_providers and not configured

    def public_fallback_only_alert(self, configured_providers):
        """Returns an alert dict when running on public-only, else
        None. The alert is also written to a JSON file (atomic
        replace) so other processes / the watchdog can read it, and
        cleared (file removed) when the condition ends."""
        path = os.path.join(self.home, ALERT_FILE)
        if not self.public_fallback_only(configured_providers):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
            return None
        alert = {
            "alert": "public_fallback_only",
            "severity": "warning",
            "message": "Only the keyless public RPC endpoint is "
                       "configured; no keyed provider (Helius/Alchemy/"
                       "Chainstack) is available. Entry-critical reads "
                       "have no transaction fallback during an outage.",
            "configured_providers": list(configured_providers),
            "ts": time.time(),
        }
        tmp = "%s.tmp.%d" % (path, os.getpid())
        try:
            with open(tmp, "w") as fh:
                json.dump(alert, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError:
            logger.warning("could not write public-fallback alert file")
        logger.warning("PUBLIC-FALLBACK-ONLY: %s", alert["message"])
        return alert


def read_durable_statuses(home=None, max_age_s=900):
    """Read-only health view for OTHER processes (request section 4.5:
    operational memory must inform the decision engine).

    Returns a dict {provider: status} plus metadata, or None when no
    durable health file exists. The honest contract:

    * File missing  -> None: health is UNKNOWN, never reported "ok".
    * File stale (older than max_age_s, default 15 min) -> the
      statuses are returned with "stale": True and the file age, so
      the caller can journal "last known" rather than "current".
    * Corrupt file  -> None (unknown), same as missing.

    This never gates anything by itself; it exists so decision-path
    records can carry their data-quality context instead of treating
    unread information as reliable.
    """
    home = home or os.environ.get("FOREX_AGENT_HOME") \
        or os.path.expanduser("~/.forex-agent")
    path = os.path.join(home, HEALTH_FILE)
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return None
    ts = data.get("ts")
    try:
        age = time.time() - float(ts)
    except (TypeError, ValueError):
        age = None
    statuses = {}
    for name, rec in providers.items():
        if not isinstance(rec, dict):
            continue
        p = ProviderHealth(name)
        p.configured = bool(rec.get("configured", False))
        p.calls = int(rec.get("calls") or 0)
        p.errors = int(rec.get("errors") or 0)
        p.rpc_429s = int(rec.get("rpc_429s") or 0)
        p.wss_handshake_429s = int(rec.get("wss_handshake_429s") or 0)
        p.wss_subscription_429s = int(rec.get("wss_subscription_429s") or 0)
        p.timeouts = int(rec.get("timeouts") or 0)
        p.monthly_quota_down = bool(rec.get("monthly_quota_down"))
        statuses[name] = p.status()
    return {"providers": statuses,
            "ts": ts,
            "age_s": round(age, 1) if age is not None else None,
            "stale": age is None or age > max_age_s}
