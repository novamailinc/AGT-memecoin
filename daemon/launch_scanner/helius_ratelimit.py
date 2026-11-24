"""Shared Helius rate-limit guard for the launch scanner.

Both ``smartmoney`` and ``deployers`` hit the same Helius RPC endpoint with
the same API key, so they share one rate-limit bucket — and one cooldown.
Without coordination, each module retries blindly on HTTP 429 and the
combined hammering keeps the bucket empty (2026-10-07: 42k+ 429s in the
log, smart-money signal degraded to a limp).

Protocol (fail-soft, never raises):
- Before any Helius call: ``cooldown_remaining()`` — >0 means skip the
  call and return None, exactly like a failed request today.
- After a call: ``report_ratelimited()`` on HTTP 429, ``report_success()``
  on any successful response. Anything else (timeouts, 5xx, no key)
  leaves the cooldown untouched — only 429 engages the brake.

Backoff: 60s, doubling to a 600s cap, reset on first success. All state is
module-level (the daemons are single-process); stdlib only so tests can
import it freely.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import random
import time
from typing import Any, Dict, Optional

_BASE_BACKOFF_S = 60.0
_MAX_BACKOFF_S = 600.0


class RateGuard:
    """One rate-limit bucket: one guard per API key.

    The original module-level functions all shared a single bucket
    because ``smartmoney`` and ``deployers`` used one key. A second
    (discovery) key gets its own guard so research traffic never
    contends with the live signals' budget.
    """

    def __init__(self) -> None:
        self._cooldown_until = 0.0
        self._backoff_s = _BASE_BACKOFF_S

    def cooldown_remaining(self, now: float | None = None) -> float:
        """Seconds left on this guard's cooldown, 0 when clear."""
        t = time.time() if now is None else now
        return max(0.0, self._cooldown_until - t)

    def report_ratelimited(self, now: float | None = None) -> float:
        """Engage/extend the cooldown. Returns the seconds set."""
        t = time.time() if now is None else now
        self._cooldown_until = t + self._backoff_s
        set_for = self._backoff_s
        self._backoff_s = min(_MAX_BACKOFF_S, self._backoff_s * 2.0)
        return set_for

    def report_success(self) -> None:
        """A call got through — reset the backoff ladder."""
        self._backoff_s = _BASE_BACKOFF_S

    def reset(self) -> None:
        """Clear cooldown and backoff state."""
        self._cooldown_until = 0.0
        self._backoff_s = _BASE_BACKOFF_S


# The original shared bucket: smartmoney + deployers on the main key.
# Kept as module-level functions for backward compatibility.
_SHARED = RateGuard()


def cooldown_remaining(now: Optional[float] = None) -> float:
    """Seconds left on the shared cooldown, 0 when clear."""
    return _SHARED.cooldown_remaining(now)


def report_ratelimited(now: Optional[float] = None) -> float:
    """Engage/extend the cooldown. Returns the cooldown seconds set."""
    return _SHARED.report_ratelimited(now)


def report_success() -> None:
    """A call got through — reset the backoff ladder."""
    _SHARED.report_success()


def reset_for_tests() -> None:
    """Test hook: clear cooldown and backoff state."""
    _SHARED.reset()


def new_guard() -> RateGuard:
    """A fresh, independent rate-limit bucket (e.g. a second API key)."""
    return RateGuard()


# ------------------------------------------------- cross-process sharing
# 2026-11-16 (RPC-burn fix, Lever 2): the guard above is in-process
# only, so four trader processes plus the scanner each hammer the same
# two Helius keys with their own private cooldown — the fleet 429s
# itself. The shared guard below propagates one cooldown per KEY
# across processes via a small JSON state file, flock-protected for
# writers, atomically replaced (temp + os.replace, same pattern as
# killswitch.py). Readers need no lock: they always see a complete
# file. The file carries ONLY sha256 bucket ids + timestamps — never
# key material. Every operation is fail-soft: on any file/lock error
# the caller falls back to its in-process guard, never raises, never
# blocks (lock attempts are non-blocking with a bounded retry).

def bucket_id_for_key(key: str) -> str:
    """Stable, non-reversible bucket id for one Helius API key.

    sha256 hexdigest prefix — the key itself never enters the state
    file, a log line, or a test fixture."""
    try:
        return hashlib.sha256((key or "").encode("utf-8")).hexdigest()[:16]
    except Exception:
        return "unkeyed"


def _guard_home(home: Optional[str] = None) -> str:
    if home:
        return home
    return os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")


# 2026-11-22 (herd fix): lock contention is the NORMAL case when the
# fleet wakes at once (quiet clear, cooldown expiry, mass restart) —
# the old 50ms give-up (5 x 10ms) made every contender fall back to
# a private bucket and fire at once (6 handshake 429s in one second
# after the 15:03 quiet clear). The lock wait is therefore generous
# relative to a hold (0.5s vs a ~1-2ms read+write: a herd of 22
# contenders serializes in ~50ms), but bounded well under the 2s
# the stuck-lock tests allow, so a genuinely stuck lock still fails
# fast and soft. flock is released when a holder dies, so the wait
# cannot stick on a dead process. Callers that time out on the lock
# retry the transact inside their own wait budget (pacer acquire)
# or degrade to the in-process guard (guard report) — never to an
# unguarded call.
_LOCK_WAIT_BUDGET_S = 0.5
_LOCK_WAIT_POLL_S = 0.005


def _flock_exclusive(lock_fh) -> bool:
    """Take an exclusive flock, polling up to the budget. True when
    held (caller must unlock), False on timeout. Never raises."""
    deadline = time.monotonic() + _LOCK_WAIT_BUDGET_S
    while True:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except Exception:
            if time.monotonic() >= deadline:
                return False
            time.sleep(_LOCK_WAIT_POLL_S)


class SharedGuard:
    """One rate-limit bucket per Helius key, shared across processes.

    Each process keeps an in-process RateGuard per bucket (fast path
    and fallback). The shared file propagates cooldowns fleet-wide:
    a 429 seen by ANY process engages the cooldown for EVERY process
    using that key, and a success clears it. The backoff ladder is
    adopted upward from the file; the cooldown itself is re-read from
    the file on every check (never copied into the local guard, so a
    fleet clear unblocks at once instead of sticking). Monotonic merge:
    a write never lowers another process's cooldown (except an
    explicit success-clear, which is the original "reset on first
    success" semantic).
    """

    def __init__(self, home: Optional[str] = None) -> None:
        self._home = _guard_home(home)
        self._state_file = os.path.join(self._home, "helius_guard.json")
        self._lock_file = os.path.join(self._home, "helius_guard.lock")
        self._local: Dict[str, RateGuard] = {}

    # -- file layer (fail-soft) -------------------------------------
    def _read_shared(self) -> Dict[str, Any]:
        try:
            with open(self._state_file, "r") as fh:
                data = json.load(fh)
            buckets = (data or {}).get("buckets") or {}
            return buckets if isinstance(buckets, dict) else {}
        except Exception:
            return {}

    def _write_shared(self, updates: Dict[str, Dict[str, float]],
                      clear: bool = False) -> bool:
        """Merge `updates` into the shared file (or replace on clear).

        Writers serialize on the lock file (non-blocking, bounded
        retries); the file itself is replaced atomically. Returns
        False when the shared write was skipped — the caller's local
        guard is still updated, so protection degrades to in-process
        only, never to nothing. Never raises.
        """
        try:
            os.makedirs(self._home, exist_ok=True)
            lock_fh = open(self._lock_file, "a+")
        except Exception:
            return False
        try:
            if not _flock_exclusive(lock_fh):
                return False
            try:
                current = self._read_shared()
                for bucket, vals in updates.items():
                    if clear:
                        current[bucket] = dict(vals)
                    else:
                        prev = current.get(bucket) or {}
                        current[bucket] = {
                            "until": max(float(prev.get("until", 0.0)),
                                         float(vals.get("until", 0.0))),
                            "backoff": max(float(prev.get("backoff", 0.0)),
                                           float(vals.get("backoff", 0.0))),
                        }
                tmp = "%s.tmp.%d" % (self._state_file, os.getpid())
                with open(tmp, "w") as fh:
                    json.dump({"buckets": current}, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self._state_file)
                return True
            finally:
                try:
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
                except Exception:
                    pass
        except Exception:
            return False
        finally:
            try:
                lock_fh.close()
            except Exception:
                pass

    # -- per-bucket API ----------------------------------------------
    def _local_guard(self, bucket: str) -> RateGuard:
        g = self._local.get(bucket)
        if g is None:
            g = RateGuard()
            self._local[bucket] = g
        return g

    def _adopt_shared(self, bucket: str) -> float:
        """Shared `until` for this bucket (0 when clear/absent).

        Also adopts the shared backoff upward into the local ladder so
        a process that reports without checking first still escalates
        correctly. Never raises; the local guard is the fallback."""
        try:
            shared = self._read_shared().get(bucket) or {}
            until = float(shared.get("until", 0.0) or 0.0)
            backoff = float(shared.get("backoff", 0.0) or 0.0)
            if backoff > 0:
                local = self._local_guard(bucket)
                local._backoff_s = max(local._backoff_s, backoff)
            return until
        except Exception:
            return 0.0

    def cooldown_remaining(self, bucket: str,
                           now: Optional[float] = None) -> float:
        """Seconds left on this key's fleet-wide cooldown, 0 when clear."""
        if not bucket:
            return 0.0
        t = time.time() if now is None else now
        local = self._local_guard(bucket)
        remaining = local.cooldown_remaining(t)
        # The shared file is the fleet truth and is re-read on every
        # check: a clear (until 0) unblocks at once. The local guard
        # is NEVER mutated here — adopting the shared `until` into it
        # made clears sticky (the local value survived the fleet
        # clear). Only the backoff ladder is adopted upward, which is
        # safe: it affects future report durations, never whether a
        # call may proceed.
        remaining = max(remaining, self._adopt_shared(bucket) - t)
        return max(0.0, remaining)

    def report_ratelimited(self, bucket: str,
                           now: Optional[float] = None) -> float:
        """Engage/extend this key's fleet-wide cooldown. Returns seconds."""
        if not bucket:
            return 0.0
        t = time.time() if now is None else now
        self._adopt_shared(bucket)
        local = self._local_guard(bucket)
        set_for = local.report_ratelimited(t)
        self._write_shared({bucket: {
            "until": t + set_for, "backoff": local._backoff_s}})
        return set_for

    def report_success(self, bucket: str) -> None:
        """A call got through — reset this key's ladder, fleet-wide.

        Deliberate divergence from the legacy in-process guard (whose
        report_success resets only the backoff ladder): a demonstrated
        success also clears the fleet-wide cooldown, so entries resume
        at once instead of waiting out the window. Self-correcting —
        if the bucket is still exhausted the next call 429s and the
        cooldown re-engages."""
        if not bucket:
            return
        local = self._local_guard(bucket)
        local.report_success()
        local._cooldown_until = 0.0
        self._write_shared({bucket: {"until": 0.0,
                                     "backoff": _BASE_BACKOFF_S}},
                           clear=True)

    def reset(self) -> None:
        """Clear local + shared state (tests only). Best-effort."""
        self._local = {}
        try:
            os.remove(self._state_file)
        except Exception:
            pass


_SHARED_GUARD: Optional["SharedGuard"] = None


def shared_guard() -> "SharedGuard":
    """The fleet-wide guard singleton (lazy; home from the env)."""
    global _SHARED_GUARD
    if _SHARED_GUARD is None:
        _SHARED_GUARD = SharedGuard()
    return _SHARED_GUARD


def cooldown_remaining_for(bucket: str,
                           now: Optional[float] = None) -> float:
    """Fleet-wide cooldown seconds left for one key bucket, 0 when clear."""
    return shared_guard().cooldown_remaining(bucket, now)


def report_ratelimited_for(bucket: str,
                           now: Optional[float] = None) -> float:
    """Engage/extend one key bucket's fleet-wide cooldown. Returns seconds."""
    return shared_guard().report_ratelimited(bucket, now)


def report_success_for(bucket: str) -> None:
    """A call on this key got through — reset its fleet-wide ladder."""
    shared_guard().report_success(bucket)


# ------------------------------------------------- Helius quiet period
# 2026-11-20: the Helius 429 penalty box is 2+ days old with both
# keys at max backoff, despite pacing. The remaining lever is to
# stop calling Helius entirely for a window and let the flag cool:
# while quiet is active the pacer issues NO token anywhere in the
# fleet, so every caller falls through to the public-RPC fallback
# (reserves) or skips (background) — zero Helius attempts of any
# kind, including the armed-watch WSS handshake. After `until`,
# exactly one caller claims the single probe: a success clears
# quiet, a 429 re-enters quiet for 12h. The probe is the caller's
# normal Helius attempt (e.g. a curve read) — on failure it falls
# back to public, so no entry is lost.
#
# State lives in helius_quiet.json (flock + atomic replace, same
# pattern as the pacer/guard files). Fields: active (bool), until
# (epoch), reason (str), set_ts (epoch), probe_result (null or
# {ts, ok}), probe_claimed_until (epoch), probe_claimed_by (pid).
# Fail-open: an unreadable state file means "not quiet" — a broken
# file never silences the fleet. All helpers never raise.
#
# One documented edge: a probe attempt that fails WITHOUT a verdict
# (transport failure — reserves does not report non-429 failures
# to the pacer) leaves the claim unresolved; the claim lapses
# after the deadline and the next caller claims a fresh probe.
# That is bounded (one attempt per deadline window) and harmless —
# a timeout does not trip the flag — and the success/429 paths,
# which are the ones that matter, resolve exactly once.

_QUIET_FILE = "helius_quiet.json"
_QUIET_LOCK = "helius_quiet.lock"
_QUIET_REENTRY_HOURS = 12.0
_QUIET_PROBE_DEADLINE_S = 60.0


def _quiet_default():
    return {"active": False, "until": 0.0, "reason": "",
            "set_ts": 0.0, "probe_result": None,
            "probe_claimed_until": 0.0, "probe_claimed_by": 0}


def _quiet_read(home=None):
    """Quiet state dict (defaults when absent/unreadable). Never raises."""
    try:
        with open(os.path.join(_guard_home(home), _QUIET_FILE),
                  "r") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return _quiet_default()
        st = _quiet_default()
        try:
            st["active"] = bool(data.get("active"))
            st["until"] = float(data.get("until", 0.0) or 0.0)
            st["reason"] = str(data.get("reason") or "")
            st["set_ts"] = float(data.get("set_ts", 0.0) or 0.0)
            pr = data.get("probe_result")
            if isinstance(pr, dict) and "ok" in pr:
                st["probe_result"] = {
                    "ts": float(pr.get("ts", 0.0) or 0.0),
                    "ok": bool(pr.get("ok"))}
            st["probe_claimed_until"] = float(
                data.get("probe_claimed_until", 0.0) or 0.0)
            st["probe_claimed_by"] = int(
                data.get("probe_claimed_by", 0) or 0)
        except Exception:
            pass
        return st
    except Exception:
        return _quiet_default()


def _quiet_transact(fn, home=None, now=None):
    """Atomic read-modify-write of the quiet file under flock.
    fn(state, now) -> (result, new_state_or_None). Returns fn's
    result, or None when the file layer failed. Never raises."""
    try:
        h = _guard_home(home)
        os.makedirs(h, exist_ok=True)
        lock_fh = open(os.path.join(h, _QUIET_LOCK), "a+")
    except Exception:
        return None
    try:
        locked = False
        for _ in range(5):
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except Exception:
                time.sleep(0.01)
        if not locked:
            return None
        try:
            t = time.time() if now is None else now
            state = _quiet_read(home)
            result, new_state = fn(state, t)
            if new_state is not None:
                tmp = "%s.tmp.%d" % (
                    os.path.join(h, _QUIET_FILE), os.getpid())
                with open(tmp, "w") as fh:
                    json.dump(new_state, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, os.path.join(h, _QUIET_FILE))
            return result
        finally:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            except Exception:
                pass
    except Exception:
        return None
    finally:
        try:
            lock_fh.close()
        except Exception:
            pass


def quiet_status(now=None, home=None):
    """Full quiet state dict (defaults when absent). Never raises."""
    try:
        return _quiet_read(home)
    except Exception:
        return _quiet_default()


def quiet_active(now=None, home=None):
    """True while a quiet period is in force (active and now < until).
    Fail-open False on any read error. Never raises."""
    try:
        t = time.time() if now is None else now
        st = _quiet_read(home)
        return bool(st.get("active")) \
            and float(st.get("until", 0.0) or 0.0) > t
    except Exception:
        return False


def quiet_probe_pending(now=None, home=None):
    """True when a quiet period has expired and the single probe
    has not been claimed yet. Never raises."""
    try:
        t = time.time() if now is None else now
        st = _quiet_read(home)
        if not st.get("active"):
            return False
        if float(st.get("until", 0.0) or 0.0) > t:
            return False
        return float(st.get("probe_claimed_until", 0.0) or 0.0) <= t
    except Exception:
        return False


def quiet_set(hours, reason="", now=None, home=None):
    """Start a quiet period of `hours`. Returns the state dict, or
    None when the write failed. Clears any probe claim. Never raises."""
    t = time.time() if now is None else now
    try:
        hours_f = float(hours)
    except Exception:
        return None
    if hours_f <= 0:
        return None

    def _set(st, tt):
        new = _quiet_default()
        new.update({"active": True,
                    "until": tt + hours_f * 3600.0,
                    "reason": str(reason or ""),
                    "set_ts": tt,
                    "probe_result": None,
                    "probe_claimed_until": 0.0,
                    "probe_claimed_by": 0})
        return dict(new), new

    try:
        return _quiet_transact(_set, home=home, now=t)
    except Exception:
        return None


def quiet_clear(reason="", now=None, home=None):
    """End quiet (operator clear). Returns the state dict, or None
    when the write failed. Never raises."""
    t = time.time() if now is None else now

    def _clear(st, tt):
        st["active"] = False
        st["reason"] = str(reason or "cleared")
        st["probe_claimed_until"] = 0.0
        st["probe_claimed_by"] = 0
        return dict(st), st

    try:
        return _quiet_transact(_clear, home=home, now=t)
    except Exception:
        return None


def quiet_admit(now=None, deadline_s=_QUIET_PROBE_DEADLINE_S,
                home=None):
    """One atomic Helius admission decision. Returns "open" (no
    quiet), "refuse" (quiet active, or the single probe is already
    claimed by another caller), or "probe" (this caller claimed the
    single post-quiet probe — the attempt it makes is the probe, and
    quiet_report_probe() resolves it with the outcome). Fail-open
    "open" on any file error. Never raises."""
    t = time.time() if now is None else now
    # Fast path (unlocked read): no quiet at all is the common case.
    try:
        st = _quiet_read(home)
        if not st.get("active"):
            return "open"
        if float(st.get("until", 0.0) or 0.0) > t:
            return "refuse"
    except Exception:
        return "open"

    def _claim(s, tt):
        if not s.get("active"):
            return "open", None
        if float(s.get("until", 0.0) or 0.0) > tt:
            return "refuse", None
        if float(s.get("probe_claimed_until", 0.0) or 0.0) > tt:
            return "refuse", None
        s["probe_claimed_until"] = tt + float(deadline_s)
        s["probe_claimed_by"] = os.getpid()
        return "probe", s

    try:
        res = _quiet_transact(_claim, home=home, now=t)
    except Exception:
        return "open"
    return res if res is not None else "open"


def quiet_report_probe(ok, now=None,
                       reentry_hours=_QUIET_REENTRY_HOURS, home=None):
    """Resolve a probe claim held by THIS process (pid match).
    ok=True: clear quiet. ok=False: re-enter quiet for
    `reentry_hours`. Returns the new state, or None when this
    process holds no live claim (or the write failed). Never raises."""
    t = time.time() if now is None else now
    pid = os.getpid()
    try:
        hours_f = float(reentry_hours)
    except Exception:
        hours_f = _QUIET_REENTRY_HOURS

    def _resolve(st, tt):
        if not st.get("active"):
            return None, None
        if int(st.get("probe_claimed_by", 0) or 0) != pid:
            return None, None
        if float(st.get("probe_claimed_until", 0.0) or 0.0) <= tt:
            return None, None
        if ok:
            st["active"] = False
            st["reason"] = "probe ok - Helius answered, quiet cleared"
            st["probe_result"] = {"ts": tt, "ok": True}
        else:
            st["active"] = True
            st["until"] = tt + hours_f * 3600.0
            st["set_ts"] = tt
            st["reason"] = ("probe 429 - quiet re-entered for %.0fh"
                            % hours_f)
            st["probe_result"] = {"ts": tt, "ok": False}
        st["probe_claimed_until"] = 0.0
        st["probe_claimed_by"] = 0
        return dict(st), st

    try:
        return _quiet_transact(_resolve, home=home, now=t)
    except Exception:
        return None


# ------------------------------------------------- cross-process pacer
# 2026-11-17 (pacing fix): the Helius block is HTTP 429 rate limiting
# that FLAPS, not a spent monthly cap. Measured 2026-11-17 ~17:17-17:23
# UTC with timed sequential getSlot probes (direct curl, both keys,
# guard-hot window so the bot's own reserves.rpc traffic was silent):
#   discovery: 1 req/5s -> 2/6 ok; 1/2s -> 2/10; 1/1s -> 0/10;
#              2/s -> 0/10; 4/s -> 0/10
#   primary:   1 req/5s -> 0/6; 1/2s -> 1/10; 1/1s -> 1/10; 2/s, 4/s -> 0/10
#   slow probe 1 req/15s (discovery, forced-quiet window): 1/8 ok.
#   sparse probe 1 req/30s/key during a 10-min forced-quiet window
#   (reserves silenced fleet-wide): 0/40 ok on both keys. The window
#   was never fully quiet — smartmoney's wallet poll (~11 calls per
#   ~132s burst) and the logstream mint resolutions bypass the shared
#   guard on their own in-process guards — which is exactly the hole
#   the pacer closes: after this change every caller is serialized
#   through one token bucket per key.
# A spent monthly allowance never answers; these keys answer ~5-10%
# of gentle probes and 0% of anything faster. The bot's bursts (4
# evaluation workers x 2-3 calls each, across ~6 trader processes)
# trip the limit, and the old SharedGuard then locked the fleet out
# for up to 600s with no way to test recovery — while Helius was
# actually answering single probes. A fresh key would NOT fix it:
# the same hammering trips the new key too.
#
# The pacer below is the ONE admission path for every Helius call
# (reserves.rpc, deployers, logstream, instant_safety,
# funding_clusters, smartmoney, mimic_curve, telegram key verify):
# a cross-process token bucket (leaky bucket) per key, stored in
# helius_pacer.json with the same flock + atomic-replace pattern as
# the guard, plus progressive backoff with a SINGLE probe call to
# test recovery. One token per _PACER_REFILL_S per key, capacity
# _PACER_CAPACITY (so at most 2 calls back-to-back per key, ever).
# Tokens refill during cooldowns too, so a recovered key has a
# token waiting. Priority (exit pricing) may borrow one token of
# debt and waits longer — it never bypasses the pacer.
# The pacer mirrors every outcome into the SharedGuard file, so
# armed_watch's WSS gate and instant_safety's hot-bucket skip keep
# working off the same cooldowns. The pacer is the only writer to
# the guard file after this change; nothing else calls
# SharedGuard.report_ratelimited anymore.
#
# Starting values are deliberately conservative (slower than the
# measured 1/5s knee) and are tuned by scripts/helius-load-test:
# the load test finds the fastest refill with 0 429s over 10 min.

_PACER_REFILL_S = 20.0        # one token per 20s per key (3/min/key)
_PACER_CAPACITY = 2.0         # burst allowance per key
_PACER_MAX_DEBT = 1.0         # priority may borrow one token
_PACER_WAIT_S = 5.0           # normal acquire waits at most this long
_PACER_PRIORITY_WAIT_S = 20.0  # priority (exit pricing) waits longer
_PACER_BACKOFF_BASE_S = 30.0
_PACER_BACKOFF_MAX_S = 300.0
_PACER_PROBE_DEADLINE_S = 15.0  # a claimed probe must report or lapse


def _pacer_default_bucket(now: float) -> Dict[str, Any]:
    return {"tokens": _PACER_CAPACITY, "updated_ts": now,
            "cooldown_until": 0.0, "backoff_s": _PACER_BACKOFF_BASE_S,
            "probe_claimed_until": 0.0,
            # Timestamped 429 events (2026-11-18, resilience build):
            # the spike monitor counts these; a success does NOT
            # clear them (a flap is 429-ok-429-ok) — the window
            # query prunes by time instead.
            "recent_429": []}


class SharedPacer:
    """Cross-process token bucket + progressive backoff, one per key.

    State lives in helius_pacer.json (bucket ids + floats only, never
    key material). Every mutation is a flock-protected atomic
    read-modify-write. Never raises. On a file/lock error the caller
    degrades to a per-process in-memory bucket — EXCEPT in acquire(),
    where shared state exists but is contended: there the caller
    waits and retries the shared transact instead, because a
    per-process bucket under contention hands every contender its
    own full tokens and the herd fires at once (2026-11-22). The
    local bucket is used only when no shared file exists yet
    (first run)."""

    def __init__(self, home: Optional[str] = None) -> None:
        self._home = _guard_home(home)
        self._state_file = os.path.join(self._home, "helius_pacer.json")
        self._lock_file = os.path.join(self._home, "helius_pacer.lock")
        self._local: Dict[str, Dict[str, float]] = {}

    # -- file layer (fail-soft) -------------------------------------
    def _read_state(self) -> Dict[str, Any]:
        try:
            with open(self._state_file, "r") as fh:
                data = json.load(fh)
            buckets = (data or {}).get("buckets") or {}
            return buckets if isinstance(buckets, dict) else {}
        except Exception:
            return {}

    def _write_state(self, buckets: Dict[str, Any]) -> bool:
        try:
            os.makedirs(self._home, exist_ok=True)
            lock_fh = open(self._lock_file, "a+")
        except Exception:
            return False
        try:
            if not _flock_exclusive(lock_fh):
                return False
            try:
                tmp = "%s.tmp.%d" % (self._state_file, os.getpid())
                with open(tmp, "w") as fh:
                    json.dump({"buckets": buckets}, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self._state_file)
                return True
            finally:
                try:
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
                except Exception:
                    pass
        except Exception:
            return False
        finally:
            try:
                lock_fh.close()
            except Exception:
                pass

    def _transact(self, bucket: str, fn) -> Any:
        """Atomic read-modify-write for one bucket.

        fn(state_dict, now) -> (result, new_state_dict_or_None).
        The write is skipped when fn returns None as the new state.
        Returns fn's result, or None when the file layer failed
        (caller falls back to the local bucket)."""
        try:
            os.makedirs(self._home, exist_ok=True)
            lock_fh = open(self._lock_file, "a+")
        except Exception:
            return None
        try:
            if not _flock_exclusive(lock_fh):
                return None
            try:
                now = time.time()
                buckets = self._read_state()
                raw = buckets.get(bucket) or {}
                state = dict(_pacer_default_bucket(now))
                try:
                    for k, v in (raw or {}).items():
                        if k in ("tokens", "updated_ts",
                                 "cooldown_until", "backoff_s",
                                 "probe_claimed_until"):
                            state[k] = float(v)
                        elif k == "recent_429" \
                                and isinstance(v, list):
                            # Timestamped 429 events (floats only).
                            state[k] = [float(ts) for ts in v
                                        if isinstance(ts, (int, float))]
                except Exception:
                    pass
                result, new_state = fn(state, now)
                if new_state is not None:
                    buckets[bucket] = new_state
                    tmp = "%s.tmp.%d" % (self._state_file, os.getpid())
                    with open(tmp, "w") as fh:
                        json.dump({"buckets": buckets}, fh)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp, self._state_file)
                return result
            finally:
                try:
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
                except Exception:
                    pass
        except Exception:
            return None
        finally:
            try:
                lock_fh.close()
            except Exception:
                pass

    # -- local fallback (same constants, in-process only) ------------
    def _local_acquire(self, bucket: str, priority: bool) -> bool:
        now = time.time()
        st = self._local.get(bucket)
        if st is None:
            st = dict(_pacer_default_bucket(now))
            self._local[bucket] = st
        st["tokens"] = min(_PACER_CAPACITY,
                           st["tokens"] + (now - st["updated_ts"])
                           / _PACER_REFILL_S)
        st["updated_ts"] = now
        if now < st["cooldown_until"]:
            return False
        if st["tokens"] >= 1.0 or (priority and
                                   st["tokens"] - 1.0
                                   >= -_PACER_MAX_DEBT):
            st["tokens"] -= 1.0
            return True
        return False

    def _local_report(self, bucket: str, ok: bool) -> None:
        now = time.time()
        st = self._local.get(bucket)
        if st is None:
            st = dict(_pacer_default_bucket(now))
            self._local[bucket] = st
        if ok:
            st["cooldown_until"] = 0.0
            st["backoff_s"] = _PACER_BACKOFF_BASE_S
            st["probe_claimed_until"] = 0.0
        else:
            nb = _PACER_BACKOFF_BASE_S if st["cooldown_until"] <= 0 \
                else min(st["backoff_s"] * 2.0, _PACER_BACKOFF_MAX_S)
            st["cooldown_until"] = now + nb
            st["backoff_s"] = nb
            st["probe_claimed_until"] = 0.0
            _record_429_event(st, now)

    # -- public API ----------------------------------------------------
    def acquire(self, bucket: str, priority: bool = False,
                wait_s: Optional[float] = None) -> bool:
        """Take one token for a Helius call. False = do not call.

        Waits (with jitter, so processes do not wake in lockstep)
        until `wait_s` for a token or for a probe claim after a
        cooldown. A cooldown that is still active returns False
        at once — there is nothing to wait for until it expires,
        and the probe claim at expiry is granted to exactly one
        caller. Never raises; never blocks past the wait budget."""
        if not bucket:
            return False
        # Helius quiet period (2026-11-20): while quiet is active,
        # no token is issued anywhere in the fleet — callers fall
        # through to the public-RPC fallback (reserves) or skip
        # (background). After `until`, exactly one caller claims
        # the single probe (quiet_admit); everyone else is refused
        # until its verdict, which report() resolves. A broken
        # quiet file fails open (quiet_admit returns "open").
        # quiet_admit() is called ONCE per acquire (2026-11-22):
        # it is the claim — calling it again from the same process
        # would read as a second caller and refuse, and in a
        # multi-threaded daemon two threads would both read as the
        # probe holder. The wait loop below re-checks quiet with
        # the READ-ONLY quiet_active() instead, which is safe to
        # repeat and is what catches quiet engaging mid-wait.
        decision = quiet_admit()
        if decision == "refuse":
            return False
        # "probe": this caller holds the single probe claim; the
        # attempt it makes is the probe. "open": no quiet.
        if wait_s is None:
            wait_s = _PACER_PRIORITY_WAIT_S if priority \
                else _PACER_WAIT_S
        deadline = time.time() + max(0.0, wait_s)
        while True:
            # Re-check quiet on EVERY loop iteration (2026-11-22
            # fix): quiet may have engaged while this caller
            # waited for a token, and a single check at entry
            # could otherwise let a caller take a token and call
            # into an active quiet period. This is read-only —
            # quiet cannot engage AND expire inside one acquire's
            # wait budget (quiet lasts 1h/12h, waits last at most
            # 20s), so the admission decision from the single
            # quiet_admit() above stays valid throughout.
            if quiet_active():
                return False
            now = time.time()
            outcome = self._transact(bucket,
                                     lambda st, t: self._try_take(
                                         st, t, priority))
            if outcome is None:
                # Shared state unreachable. When the state file
                # exists this is lock contention, not a missing
                # file — and contention is exactly the herd case:
                # falling back to a per-process bucket hands every
                # contender its own full 2 tokens, so they all fire
                # at once (2026-11-22: 6 handshake 429s in one
                # second after the quiet clear, while the shared
                # bucket held ~2 tokens). Fail closed instead:
                # wait a little and retry the shared transact
                # until the wait budget is gone. Only when there is
                # no shared file at all (first run, nothing to
                # serialize against) is the local bucket used.
                if not os.path.exists(self._state_file):
                    return self._local_acquire(bucket, priority)
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                time.sleep(min(0.25, remaining)
                           + random.random() * 0.05)
                continue
            if outcome == "ok":
                return True
            if outcome == "cooldown":
                return False
            # outcome is seconds until a token refills (or a probe
            # claim lapses): sleep that long (jittered), unless the
            # wait budget is gone.
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            sleep_s = min(float(outcome), remaining)
            time.sleep(sleep_s + random.random() * 0.25)

    def _try_take(self, st: Dict[str, float], now: float,
                  priority: bool):
        """One atomic take attempt. Returns (outcome, new_state).

        outcome: "ok" (token taken), "cooldown" (do not call),
        or float seconds to sleep before retrying. The refill
        keeps running during cooldowns, so a recovered key has a
        token waiting; the probe claim after a cooldown is granted
        to exactly one caller."""
        st["tokens"] = min(
            _PACER_CAPACITY,
            st["tokens"] + (now - st["updated_ts"]) / _PACER_REFILL_S)
        st["updated_ts"] = now
        if now < st["cooldown_until"]:
            return "cooldown", st
        if st["cooldown_until"] > 0.0:
            # Cooldown just expired: single-probe rule. The first
            # caller claims the probe; everyone else waits for its
            # verdict (or the claim lapsing).
            if st["probe_claimed_until"] > now:
                return (max(0.05, st["probe_claimed_until"] - now), st)
            st["probe_claimed_until"] = now + _PACER_PROBE_DEADLINE_S
            if st["tokens"] >= 1.0:
                st["tokens"] -= 1.0
            elif st["tokens"] - 1.0 >= -_PACER_MAX_DEBT:
                st["tokens"] -= 1.0
            return "ok", st
        if st["tokens"] >= 1.0:
            st["tokens"] -= 1.0
            return "ok", st
        if priority and st["tokens"] - 1.0 >= -_PACER_MAX_DEBT:
            st["tokens"] -= 1.0
            return "ok", st
        # No token: sleep until the next refill lands.
        deficit = 1.0 - st["tokens"]
        return deficit * _PACER_REFILL_S, st

    def report(self, bucket: str, ok: bool) -> None:
        """Record a Helius outcome. A success clears the cooldown
        fleet-wide (and resets the backoff ladder); a 429 engages
        progressive backoff (30s, doubling to 300s) with the probe
        claim as the only way back in. Mirrors the outcome into
        the SharedGuard file so the WSS gate and the hot-bucket
        skip keep working off the same cooldowns. Never raises."""
        if not bucket:
            return

        def _apply(st: Dict[str, Any], t: float):
            st["tokens"] = min(
                _PACER_CAPACITY,
                st["tokens"] + (t - st["updated_ts"])
                / _PACER_REFILL_S)
            st["updated_ts"] = t
            if ok:
                st["cooldown_until"] = 0.0
                st["backoff_s"] = _PACER_BACKOFF_BASE_S
                st["probe_claimed_until"] = 0.0
            else:
                nb = _PACER_BACKOFF_BASE_S \
                    if st["cooldown_until"] <= 0 \
                    else min(st["backoff_s"] * 2.0,
                             _PACER_BACKOFF_MAX_S)
                st["cooldown_until"] = t + nb
                st["backoff_s"] = nb
                st["probe_claimed_until"] = 0.0
                _record_429_event(st, t)
            return (st["cooldown_until"], st["backoff_s"]), st

        done = self._transact(bucket, _apply)
        if done is None:
            self._local_report(bucket, ok)
        else:
            try:
                guard = shared_guard()
                if ok:
                    guard.report_success(bucket)
                else:
                    guard.set_cooldown(bucket, done[0], done[1])
            except Exception:
                pass
        # A held quiet probe claim is resolved by this outcome
        # (quiet_report_probe is a no-op unless this process
        # holds the claim: success clears quiet, a 429 re-enters
        # it for 12h).
        try:
            quiet_report_probe(ok)
        except Exception:
            pass

    def reset(self) -> None:
        """Clear local + shared pacer state (tests only)."""
        self._local = {}
        try:
            os.remove(self._state_file)
        except Exception:
            pass

    # -- 429-event history (2026-11-18, resilience build) ---------------
    def recent_429_events(self, bucket: Optional[str] = None,
                          window_s: float = 600.0,
                          now: Optional[float] = None):
        """Timestamps of 429s in the window (all buckets, or one).

        Read from the shared file (the fleet truth — every process
        records into it). Never raises; [] when unreadable."""
        try:
            t = time.time() if now is None else now
            buckets = self._read_state()
            names = [bucket] if bucket else list(buckets)
            events = []
            for name in names:
                evs = (buckets.get(name) or {}).get("recent_429") or []
                events.extend(float(ts) for ts in evs
                              if isinstance(ts, (int, float))
                              and t - float(ts) <= window_s)
            return sorted(events)
        except Exception:
            return []

    def recent_429_count(self, bucket: Optional[str] = None,
                         window_s: float = 600.0,
                         now: Optional[float] = None) -> int:
        """Number of 429s in the window (all buckets, or one)."""
        return len(self.recent_429_events(bucket, window_s, now))


def _record_429_event(st: Dict[str, Any], t: float) -> None:
    """Append one 429 timestamp to a bucket's state dict (in place).

    Prunes events older than 24h and caps at the newest 100, so
    the state file stays small. Never raises."""
    try:
        evs = [ts for ts in (st.get("recent_429") or [])
               if isinstance(ts, (int, float))
               and t - float(ts) <= 86400.0]
        evs.append(float(t))
        st["recent_429"] = evs[-100:]
    except Exception:
        pass


# ---- 429-spike alert + background shed (2026-11-18) -------------------
_ALERT_THRESHOLD = 5       # more than 5 429s ...
_ALERT_WINDOW_S = 600.0    # ... in 10 minutes = a spike
_SHED_LOGGED: Dict[str, float] = {}  # bucket -> cooldown `until` logged


def recent_429_events(bucket: Optional[str] = None,
                      window_s: float = _ALERT_WINDOW_S,
                      now: Optional[float] = None):
    """Timestamps of 429s in the window (all buckets, or one)."""
    return pacer().recent_429_events(bucket, window_s, now)


def recent_429_count(bucket: Optional[str] = None,
                     window_s: float = _ALERT_WINDOW_S,
                     now: Optional[float] = None) -> int:
    """Number of 429s in the window (all buckets, or one)."""
    return pacer().recent_429_count(bucket, window_s, now)


def pacer_429_spike(threshold: int = _ALERT_THRESHOLD,
                    window_s: float = _ALERT_WINDOW_S,
                    now: Optional[float] = None) -> bool:
    """True when more than `threshold` 429s landed in the window.

    The monitor (scripts/helius-429-watch) and the self-audit read
    this. Never raises; False when the state is unreadable."""
    try:
        return recent_429_count(None, window_s, now) > threshold
    except Exception:
        return False


def background_call_allowed(bucket: str) -> bool:
    """False when the bucket is in a fleet backoff — background
    Helius consumers (deployer scans, smartmoney background
    checks, the nightly grader) must not attempt. Entry-critical
    callers do NOT use this; they take pacer tokens (priority
    when exempt). Fail-open to True on any read error, so a
    broken state file never silences the fleet. Never raises."""
    if not bucket:
        return True
    try:
        if quiet_active():
            return False
    except Exception:
        pass
    try:
        if shared_guard().cooldown_remaining(bucket) > 0:
            return False
    except Exception:
        pass
    try:
        st = pacer()._read_state().get(bucket) or {}
        if float(st.get("cooldown_until", 0.0) or 0.0) > time.time():
            return False
    except Exception:
        pass
    return True


def log_backoff_episode_once(bucket: str, caller: str, logger) -> None:
    """Log a background shed once per backoff episode (not per call).

    An episode is identified by the cooldown's `until` value: a
    new (or extended) `until` means a new episode and logs again;
    repeat calls inside the same episode stay silent. Callers
    invoke this when background_call_allowed() is False. Never
    raises."""
    if not bucket:
        return
    try:
        shared = shared_guard()._read_shared().get(bucket) or {}
        until = float(shared.get("until", 0.0) or 0.0)
    except Exception:
        until = 0.0
    if until <= time.time():
        # No active backoff (cleared): forget the episode so the
        # next one logs again, and stay silent now.
        _SHED_LOGGED.pop(bucket, None)
        return
    if _SHED_LOGGED.get(bucket) == until:
        return
    _SHED_LOGGED[bucket] = until
    try:
        logger.warning(
            "background shed: %s paused, Helius bucket %s in backoff "
            "until %.0f (auto-shed; entry-critical calls keep "
            "priority tokens)", caller, bucket[:8], until)
    except Exception:
        pass


def reset_shed_log_for_tests() -> None:
    """Test hook: clear the once-per-episode shed log."""
    _SHED_LOGGED.clear()


def _set_shared_cooldown(self, bucket: str, until: float,
                         backoff: float) -> bool:
    """Overwrite one bucket's cooldown with exact values (the
    pacer's mirror). Unlike _write_shared's max-merge, this
    REPLACES the entry, so a shorter pacer cooldown is never
    stuck behind a stale 600s guard value. Returns False on
    failure; never raises."""
    if not bucket:
        return False
    return self._write_shared(
        {bucket: {"until": float(until), "backoff": float(backoff)}},
        clear=True)


SharedGuard.set_cooldown = _set_shared_cooldown


_PACER: Optional["SharedPacer"] = None


def pacer() -> "SharedPacer":
    """The fleet-wide pacer singleton (lazy; home from the env)."""
    global _PACER
    if _PACER is None:
        _PACER = SharedPacer()
    return _PACER


def pacer_acquire(bucket: str, priority: bool = False,
                  wait_s: Optional[float] = None) -> bool:
    """Take one pacer token for a Helius call on `bucket`."""
    return pacer().acquire(bucket, priority, wait_s)


def pacer_report(bucket: str, ok: bool) -> None:
    """Record a Helius outcome (True = success, False = 429)."""
    pacer().report(bucket, ok)


def reset_pacer_for_tests() -> None:
    """Test hook: clear pacer state (local + shared file)."""
    global _PACER
    if _PACER is not None:
        _PACER.reset()
    _PACER = None
