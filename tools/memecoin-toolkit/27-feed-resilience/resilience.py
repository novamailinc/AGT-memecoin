"""Feed resilience lab (synthetic, pure stdlib, no network).

State machines for API-abuse survival with an injected synthetic clock:
FloodGuard, per-key rate buckets, backoff with seeded jitter, circuit
breaker, and a gzip content-encoding helper. Fail-closed: bad inputs raise.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field


class SyntheticClock:
    def __init__(self, start: float = 1000.0):
        if start is None or start < 0:
            raise ValueError("start must be >= 0")
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        if seconds is None or seconds < 0:
            raise ValueError("seconds must be >= 0")
        self._now += seconds
        return self._now


class FakeTransport:
    """Call-counting fake transport; no real HTTP."""

    def __init__(self):
        self.calls = 0
        self.sent = []

    def send(self, payload):
        self.calls += 1
        self.sent.append(payload)
        return {"ok": True, "payload": payload}


@dataclass
class FloodGuard:
    """On 429: park ALL mutating calls until retry_after + buffer."""

    clock: SyntheticClock
    buffer_s: float = 5.0
    _parked_until: float = 0.0

    def __post_init__(self):
        if self.buffer_s is None or self.buffer_s < 0:
            raise ValueError("buffer_s must be >= 0")

    def on_rate_limited(self, retry_after: float) -> float:
        if retry_after is None or retry_after < 0:
            raise ValueError("retry_after must be >= 0")
        self._parked_until = self.clock.now() + retry_after + self.buffer_s
        return self._parked_until

    def is_parked(self) -> bool:
        return self.clock.now() < self._parked_until

    @property
    def parked_until(self) -> float:
        return self._parked_until

    def remaining(self) -> float:
        return max(0.0, self._parked_until - self.clock.now())

    def try_send(self, transport: FakeTransport, payload) -> dict | None:
        """Returns None without touching transport while parked."""
        if self.is_parked():
            return None
        return transport.send(payload)

    def export_state(self) -> dict:
        return {"parked_until": self._parked_until, "buffer_s": self.buffer_s}

    @classmethod
    def restore(cls, clock: SyntheticClock, state: dict) -> "FloodGuard":
        if state is None or "parked_until" not in state:
            raise ValueError("state must contain parked_until")
        g = cls(clock=clock, buffer_s=state.get("buffer_s", 5.0))
        g._parked_until = float(state["parked_until"])
        return g


@dataclass
class RateBucket:
    """Token bucket for one API key."""

    capacity: int
    refill_per_s: float
    clock: SyntheticClock
    _tokens: float = field(default=None)
    _last: float = field(default=None)

    def __post_init__(self):
        if self.capacity is None or self.capacity <= 0:
            raise ValueError("capacity must be positive")
        if self.refill_per_s is None or self.refill_per_s < 0:
            raise ValueError("refill_per_s must be >= 0")
        self._tokens = float(self.capacity)
        self._last = self.clock.now()

    def _refill(self):
        now = self.clock.now()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(float(self.capacity), self._tokens + elapsed * self.refill_per_s)
            self._last = now

    def try_acquire(self, n: int = 1) -> bool:
        if n is None or n <= 0:
            raise ValueError("n must be positive")
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens


@dataclass
class DualKeyBuckets:
    """Main vs discovery keys drain independently."""

    main: RateBucket
    discovery: RateBucket

    def acquire(self, key: str, n: int = 1) -> bool:
        if key == "main":
            return self.main.try_acquire(n)
        if key == "discovery":
            return self.discovery.try_acquire(n)
        raise ValueError(f"unknown key: {key!r}")


def backoff_delays(base_s: float = 1.0, factor: float = 2.0, max_s: float = 60.0,
                   attempts: int = 5, seed: int = 7, jitter_frac: float = 0.25) -> list:
    if base_s is None or base_s <= 0:
        raise ValueError("base_s must be positive")
    if factor is None or factor < 1:
        raise ValueError("factor must be >= 1")
    if max_s is None or max_s < base_s:
        raise ValueError("max_s must be >= base_s")
    if attempts is None or attempts < 0:
        raise ValueError("attempts must be >= 0")
    if jitter_frac is None or not (0 <= jitter_frac <= 1):
        raise ValueError("jitter_frac must be in [0, 1]")
    rng = random.Random(seed)
    out = []
    for i in range(attempts):
        raw = min(max_s, base_s * (factor ** i))
        jitter = raw * jitter_frac * (rng.random() * 2 - 1)
        out.append(max(0.0, raw + jitter))
    return out


def backoff_delay(attempt: int, base_s: float = 1.0, factor: float = 2.0,
                  max_s: float = 60.0, seed: int = 7) -> float:
    if attempt is None or attempt < 0:
        raise ValueError("attempt must be >= 0")
    return backoff_delays(base_s, factor, max_s, attempt + 1, seed)[attempt]


@dataclass
class CircuitBreaker:
    clock: SyntheticClock
    failure_threshold: int = 3
    reset_timeout_s: float = 30.0
    _state: str = "closed"
    _failures: int = 0
    _opened_at: float = 0.0

    def __post_init__(self):
        if self.failure_threshold is None or self.failure_threshold <= 0:
            raise ValueError("failure_threshold must be positive")
        if self.reset_timeout_s is None or self.reset_timeout_s < 0:
            raise ValueError("reset_timeout_s must be >= 0")

    @property
    def state(self) -> str:
        if self._state == "open" and self.clock.now() - self._opened_at >= self.reset_timeout_s:
            return "half_open"
        return self._state

    def allow(self) -> bool:
        return self.state in ("closed", "half_open")

    def record_success(self) -> None:
        self._failures = 0
        self._state = "closed"

    def record_failure(self) -> None:
        if self.state == "half_open":
            self._state = "open"
            self._opened_at = self.clock.now()
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = "open"
            self._opened_at = self.clock.now()


def encoding_headers(expect_large: bool = True) -> dict:
    """Gzip lesson: large uncompressed responses get dropped by the egress proxy."""
    if expect_large:
        return {"Accept-Encoding": "gzip", "Accept": "application/json"}
    return {"Accept": "application/json"}


def should_request_gzip(estimated_bytes) -> bool:
    if estimated_bytes is None or estimated_bytes < 0:
        raise ValueError("estimated_bytes must be >= 0")
    return estimated_bytes >= 50_000


def decode_body(body: bytes, content_encoding: str | None) -> bytes:
    """Decode a body given its Content-Encoding. Uses stdlib gzip only."""
    if body is None:
        raise ValueError("body must not be None")
    if content_encoding is None or content_encoding == "identity":
        return body
    if content_encoding == "gzip":
        import gzip
        return gzip.decompress(body)
    raise ValueError(f"unsupported content_encoding: {content_encoding!r}")


def gzip_roundtrip_size(data: bytes) -> tuple:
    import gzip
    if data is None:
        raise ValueError("data must not be None")
    comp = gzip.compress(data)
    return (len(data), len(comp))
