"""Subscription bookkeeping (Phase 2).

Owns the registry of what the fleet is subscribed to, per provider:
which subscriptions exist, which are stale (no message within the
expected interval), which died with a disconnect and need
re-issuing. The WebSocketManager owns the socket; this module owns
the BOOK — so a restart or a provider switch can rebuild the exact
subscription set instead of guessing.

A subscription record:
  {"key": "<method>:<params-json>", "provider": str, "method": str,
   "params": [...], "created_ts": float, "last_msg_ts": float|None,
   "last_msg_slot": int|None, "server_id": int|None,
   "expected_interval_s": float, "messages": int, "status": str}

status: active | stale | disconnected | wanted
"""

from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger("forex_agent.daemon.intelligence.acquisition.subscription_manager")

DEFAULT_EXPECTED_INTERVAL_S = 120.0


def subscription_key(method, params):
    return "%s:%s" % (method, json.dumps(params, sort_keys=True,
                                         default=str))


class SubscriptionManager:
    def __init__(self, now=None):
        self._now = now
        self._subs = {}

    def _time(self):
        return self._now() if self._now else time.time()

    def register(self, provider, method, params,
                 expected_interval_s=DEFAULT_EXPECTED_INTERVAL_S):
        key = subscription_key(method, params)
        now = self._time()
        rec = self._subs.get(key)
        if rec is None:
            rec = {"key": key, "provider": provider, "method": method,
                   "params": params, "created_ts": now,
                   "last_msg_ts": None, "last_msg_slot": None,
                   "server_id": None,
                   "expected_interval_s": expected_interval_s,
                   "messages": 0, "status": "wanted"}
            self._subs[key] = rec
        else:
            rec["provider"] = provider
            rec["expected_interval_s"] = expected_interval_s
        return rec

    def mark_subscribed(self, method, params, server_id):
        rec = self._subs.get(subscription_key(method, params))
        if rec is not None:
            rec["server_id"] = server_id
            rec["status"] = "active"
        return rec

    def mark_message(self, method, params, slot=None):
        rec = self._subs.get(subscription_key(method, params))
        if rec is None:
            return None
        rec["last_msg_ts"] = self._time()
        if slot is not None:
            rec["last_msg_slot"] = slot
        rec["messages"] += 1
        rec["status"] = "active"
        return rec

    def mark_disconnected(self, provider=None):
        """A socket died: every active sub on that provider (or all
        providers when None) becomes 'disconnected' — it must be
        re-issued, and the gap it covered must be backfilled
        (event_backfill), never silently skipped."""
        out = []
        for rec in self._subs.values():
            if provider is None or rec["provider"] == provider:
                if rec["status"] == "active":
                    rec["status"] = "disconnected"
                    out.append(rec)
        return out

    def stale(self, now=None):
        """Subscriptions past their expected interval with no message.
        A stale subscription is a candidate gap, not proof of one —
        the caller confirms via slot comparison (event_backfill)."""
        now = now if now is not None else self._time()
        out = []
        for rec in self._subs.values():
            if rec["status"] not in ("active", "wanted"):
                continue
            last = rec["last_msg_ts"] or rec["created_ts"]
            if now - last > rec["expected_interval_s"]:
                rec["status"] = "stale"
                out.append(rec)
        return out

    def all(self):
        return [dict(r) for r in self._subs.values()]

    def by_provider(self, provider):
        return [dict(r) for r in self._subs.values()
                if r["provider"] == provider]

    def count(self):
        return len(self._subs)
