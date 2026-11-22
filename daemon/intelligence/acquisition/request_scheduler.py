"""Provider-specific quota-aware scheduling (Phase 2).

Replaces the ~20s-per-key Helius pacer as the live default — the old
pacer is Helius-shaped, not provider-shaped: it knows nothing about
per-method cost, per-provider quotas, or call priority beyond the
single exempt lane.

Rates (per the replacement task; deliberately conservative — the
Phase 2 load-test question is "what refill rate holds 0 429s over a
full day at real burst shapes", run against the ACTUAL keys):
  alchemy    5 calls/s, burst 10   (provider: 300 CU/s, ~15 req/s Free)
  chainstack 2 calls/s, burst 5    (provider: 5 RPS on Solana mainnet)
  public     1 call/2s fleet-wide  (existing budget, unchanged)
  helius     1 token/20s/key       (NOT re-implemented here — the live
                                   helius_ratelimit.SharedPacer owns it;
                                   this scheduler never paces Helius)

Priority classes, enforced by the scheduler (entry/exit-critical
first; background callers shed first — the existing ``exempt=True``
priority lane becomes one class among four, not a special case):
  CRITICAL   entry/exit-critical (curve reads, canonical pool, exit pricing)
  CARD       instant-safety card lane
  DEPLOYER   deployer first-resolution
  BACKGROUND research (deployer batch, smart money, funding, dossiers)

Cross-process shared quota per provider (flock + atomic state files,
same pattern as the existing pacer): N processes must never jointly
exhaust one provider quota. One state file per provider under
FOREX_AGENT_HOME (``acquisition_quota_<provider>.json``) holding the
token bucket + 429 backoff + CU/RU accounting.

429 handling: a 429 engages progressive backoff (30s base, doubling
to 300s max, single-probe recovery — same shape as the Helius pacer).
Background callers auto-shed while a bucket is in backoff (they stop
attempting entirely, never compete for tokens entry-critical calls
need). A monthly-quota 429 marks the provider DOWN for the workload
until the allowance renews — it is never retried into the same
provider.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time

logger = logging.getLogger("forex_agent.daemon.intelligence.acquisition.scheduler")

PRIORITY_CRITICAL = "critical"
PRIORITY_CARD = "card"
PRIORITY_DEPLOYER = "deployer"
PRIORITY_BACKGROUND = "background"
PRIORITIES = (PRIORITY_CRITICAL, PRIORITY_CARD, PRIORITY_DEPLOYER,
              PRIORITY_BACKGROUND)

# (refill calls/sec, burst capacity tokens)
RATES = {
    "alchemy": (5.0, 10.0),
    "chainstack": (2.0, 5.0),
    "public": (0.5, 1.0),
    # helius intentionally absent: paced by helius_ratelimit.SharedPacer.
}
MONTHLY_QUOTA_CU = {"alchemy": 30_000_000}
MONTHLY_QUOTA_RU = {"chainstack": 3_000_000}

BACKOFF_BASE_S = 30.0
BACKOFF_MAX_S = 300.0
# Max time a CRITICAL caller will wait for a token before failing fast.
CRITICAL_MAX_WAIT_S = 25.0
# Non-critical callers wait at most this long (CARD/DEPLOYER queue
# briefly; BACKGROUND never queues — see acquire()).
NONCRITICAL_MAX_WAIT_S = 5.0


def _state_path(provider, home):
    return os.path.join(home, "acquisition_quota_%s.json" % provider)


def _lock_path(provider, home):
    return os.path.join(home, "acquisition_quota_%s.lock" % provider)


def _atomic_write(path, data):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class SharedQuota:
    """Cross-process token bucket for ONE provider.

    State file (flock + atomic replace):
      {"tokens": float, "updated": epoch, "backoff_until": epoch,
       "backoff_s": float, "monthly_quota_down": bool,
       "cu_spent": int, "ru_spent": int, "calls": int,
       "429s": int, "shed": int}
    """

    def __init__(self, provider, home=None, now=None):
        if provider not in RATES:
            raise ValueError("no scheduler bucket for provider %r "
                             "(helius is paced by helius_ratelimit)" % provider)
        self.provider = provider
        self.home = home or os.environ.get("FOREX_AGENT_HOME") \
            or os.path.expanduser("~/.forex-agent")
        self._now = now  # injectable clock for tests
        self.refill, self.capacity = RATES[provider]

    def _time(self):
        return self._now() if self._now else time.time()

    def _default_state(self, now):
        return {"tokens": self.capacity, "updated": now,
                "backoff_until": 0.0, "backoff_s": BACKOFF_BASE_S,
                "monthly_quota_down": False, "cu_spent": 0, "ru_spent": 0,
                "calls": 0, "429s": 0, "shed": 0}

    def _read(self, now):
        path = _state_path(self.provider, self.home)
        try:
            with open(path) as fh:
                state = json.load(fh)
            state.setdefault("tokens", self.capacity)
            return state
        except (OSError, ValueError):
            return self._default_state(now)

    def _transact(self, fn):
        """Read-modify-write under an exclusive flock. fn(state, now)
        returns the value to return to the caller. Degrades to an
        in-process decision (fail-closed: no token) on lock failure."""
        path = _state_path(self.provider, self.home)
        lock_path = _lock_path(self.provider, self.home)
        now = self._time()
        try:
            lock_fh = open(lock_path, "w")
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                state = self._read(now)
                result = fn(state, now)
                _atomic_write(path, state)
                return result
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
                lock_fh.close()
        except OSError:
            logger.warning("quota lock unavailable for %s; failing closed",
                           self.provider)
            return (False, 0.0, "lock_error")

    def _refill(self, state, now):
        elapsed = max(0.0, now - state.get("updated", now))
        state["tokens"] = min(self.capacity,
                              state.get("tokens", self.capacity)
                              + elapsed * self.refill)
        state["updated"] = now

    def acquire(self, priority=PRIORITY_CARD, cost=1.0):
        """Take one token. Returns (granted, wait_s, reason).

        - BACKGROUND never queues: if no token is free right now it
          sheds immediately (reason "shed") and counts the shed.
        - CRITICAL/CARD/DEPLOYER report the wait instead of sleeping;
          the caller decides whether to wait (max waits above).
        - While the bucket is in 429 backoff, background sheds and
          everyone else reports the backoff wait.
        - Monthly-quota-down refuses everything (reason "quota_down").
        """
        def _do(state, now):
            if state.get("monthly_quota_down"):
                return (False, 0.0, "quota_down")
            self._refill(state, now)
            if now < state.get("backoff_until", 0.0):
                wait = state["backoff_until"] - now
                if priority == PRIORITY_BACKGROUND:
                    state["shed"] = state.get("shed", 0) + 1
                    return (False, wait, "shed_backoff")
                return (False, wait, "backoff")
            if state["tokens"] >= cost:
                state["tokens"] -= cost
                state["calls"] = state.get("calls", 0) + 1
                return (True, 0.0, "ok")
            deficit = cost - state["tokens"]
            wait = deficit / self.refill
            if priority == PRIORITY_BACKGROUND:
                state["shed"] = state.get("shed", 0) + 1
                return (False, wait, "shed")
            return (False, wait, "wait")
        return self._transact(_do)

    def acquire_wait(self, priority=PRIORITY_CARD, cost=1.0,
                     max_wait=None, sleep=time.sleep):
        """acquire(), sleeping up to max_wait for a token. Returns
        (granted, waited_s, reason). BACKGROUND never waits."""
        if max_wait is None:
            max_wait = (CRITICAL_MAX_WAIT_S if priority == PRIORITY_CRITICAL
                        else NONCRITICAL_MAX_WAIT_S)
        if priority == PRIORITY_BACKGROUND:
            max_wait = 0.0
        waited = 0.0
        while True:
            granted, wait, reason = self.acquire(priority, cost)
            if granted:
                return True, waited, "ok"
            if reason in ("quota_down", "shed", "shed_backoff",
                          "lock_error"):
                return False, waited, reason
            if waited + wait > max_wait:
                return False, waited, "max_wait"
            step = min(wait, max_wait - waited, 1.0)
            sleep(step)
            waited += step

    def report_429(self, rate_limit_kind=None, wait_hint_s=None):
        """Record a 429: engage (or double) backoff. A monthly-quota
        429 marks the provider down until the allowance renews."""
        def _do(state, now):
            state["429s"] = state.get("429s", 0) + 1
            if rate_limit_kind == "monthly_quota":
                state["monthly_quota_down"] = True
                state["backoff_until"] = now + BACKOFF_MAX_S
                return "quota_down"
            hint = wait_hint_s or 0.0
            backoff = max(BACKOFF_BASE_S, hint)
            if now < state.get("backoff_until", 0.0):
                backoff = min(BACKOFF_MAX_S,
                              state.get("backoff_s", BACKOFF_BASE_S) * 2)
            else:
                backoff = min(BACKOFF_MAX_S, backoff)
            state["backoff_s"] = backoff
            state["backoff_until"] = now + backoff
            return "backoff"
        return self._transact(_do)

    def report_success(self):
        """A success clears the backoff fleet-wide at once (single-
        probe recovery shape: one success ends the episode)."""
        def _do(state, now):
            state["backoff_until"] = 0.0
            state["backoff_s"] = BACKOFF_BASE_S
            return "ok"
        return self._transact(_do)

    def record_cost(self, cu=0, ru=0):
        def _do(state, now):
            state["cu_spent"] = state.get("cu_spent", 0) + cu
            state["ru_spent"] = state.get("ru_spent", 0) + ru
            return "ok"
        return self._transact(_do)

    def reset_quota_flag(self):
        """Operator action: clear a monthly-quota-down flag after the
        allowance renews (or a new key is fitted)."""
        def _do(state, now):
            state["monthly_quota_down"] = False
            state["backoff_until"] = 0.0
            state["backoff_s"] = BACKOFF_BASE_S
            return "ok"
        return self._transact(_do)

    def status(self):
        now = self._time()
        try:
            with open(_state_path(self.provider, self.home)) as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            state = self._default_state(now)
        self._refill(state, now)
        state["backoff_remaining_s"] = max(
            0.0, state.get("backoff_until", 0.0) - now)
        return state


def acquire(provider, priority=PRIORITY_CARD, cost=1.0, home=None):
    """Module-level convenience: one token from a provider's shared
    bucket. Helius raises (it is paced by helius_ratelimit, never here)."""
    return SharedQuota(provider, home=home).acquire(priority, cost)
