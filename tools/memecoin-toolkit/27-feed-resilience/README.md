# Feed Resilience (lab)

A from-scratch, pure-stdlib lab for surviving hostile APIs: rate limits, flood bans, flaky endpoints, and an egress proxy that drops large uncompressed responses. Everything is synthetic — an injected clock and a call-counting fake transport stand in for time and the network, so no real HTTP happens anywhere and every "zero calls during cooldown" claim is proven by a counter, not asserted.

## What it does

Module `resilience.py`:

- `SyntheticClock(start=1000.0)` with `now() -> float` and `advance(seconds) -> float`. Fail-closed: negative start or negative advance raises `ValueError` — time never runs backwards.
- `FakeTransport()` — `send(payload)` increments `calls`, appends to `sent`, returns `{"ok": True, "payload": payload}`. The test double that proves the guard's behaviour.
- `FloodGuard(clock, buffer_s=5.0)` — `on_rate_limited(retry_after) -> float` parks the guard until `now + retry_after + buffer_s`; `is_parked() -> bool`, `remaining() -> float`, `parked_until`; `try_send(transport, payload) -> dict | None` returns `None` **without touching the transport** while parked. `export_state() -> dict` / `FloodGuard.restore(clock, state)` persist and restore the cooldown across a simulated restart. Fail-closed: negative `retry_after` or `buffer_s`, and a state dict missing `parked_until`, raise `ValueError`.
- `RateBucket(capacity, refill_per_s, clock)` — a token bucket with `try_acquire(n=1) -> bool` and a `tokens` property (refill capped at capacity). `DualKeyBuckets(main, discovery)` — `acquire(key)` drains the two buckets independently; an unknown key raises `ValueError`. Fail-closed: non-positive capacity, negative refill, or non-positive `n` raise.
- `backoff_delays(base_s=1.0, factor=2.0, max_s=60.0, attempts=5, seed=7, jitter_frac=0.25) -> list[float]` — exponential delays capped at `max_s` with seeded jitter (same seed, same list); `backoff_delay(attempt, ...)` returns one entry consistently with the list. Fail-closed: non-positive base, factor < 1, `max_s < base_s`, negative attempts, or jitter outside `[0, 1]` raise.
- `CircuitBreaker(clock, failure_threshold=3, reset_timeout_s=30.0)` — states `closed` / `open` / `half_open`: `record_failure()` opens the breaker at the threshold (a failure while half-open re-opens it immediately), `record_success()` closes and resets, `allow() -> bool` is true only when closed or half-open, and `state` flips to `half_open` once the reset timeout elapses. Fail-closed: non-positive threshold or negative timeout raises.
- Content encoding (the gzip lesson): `encoding_headers(expect_large=True) -> dict` requests `Accept-Encoding: gzip` for large responses; `should_request_gzip(estimated_bytes) -> bool` at a 50,000-byte threshold; `decode_body(body, content_encoding) -> bytes` handles `None`/`identity`/`gzip` (stdlib `gzip`) and raises `ValueError` on `None` bodies or unsupported encodings; `gzip_roundtrip_size(data) -> (raw, compressed)`.

`demo.py` walks a 429 through the FloodGuard (call count frozen during cooldown), drains dual key buckets independently, prints seeded backoff delays, trips a circuit breaker, and shows the gzip headers and compression win.

## Test suite

43 tests, all passing (`pytest -q`: "43 passed"). Covered: clock advance and its fail-closed edges; FloodGuard parking, the +5s buffer arithmetic, resume at and after the exact boundary, **zero transport calls during cooldown** (call-counting fake), state export/restore surviving a simulated restart while still parked, and bad-input raises; token-bucket drain, refill over synthetic time, the capacity cap, and invalid parameters; dual keys draining independently (main exhausted, discovery untouched) and unknown-key rejection; backoff growth, the max cap, seed determinism vs seed sensitivity, zero attempts, list/single consistency, and invalid parameters; the circuit breaker closed→open at threshold, open→half-open after timeout, success reset, half-open failure re-opening, and invalid threshold; gzip header/threshold behaviour, identity and gzip decode round-trips, unsupported-encoding and `None`-body rejection, and compression shrinking repetitive payloads.

## Captured output

Verbatim output of `python demo.py`:

```
=== Feed resilience lab (synthetic clock, fake transport) ===
Before 429: send -> {'ok': True, 'payload': 'hello'}, calls=1
After 429 (retry_after=30, buffer=5): parked=True remaining=35s
During cooldown: send -> None, calls=1 (zero new calls)
After cooldown: parked=False send -> {'ok': True, 'payload': 'again'}, calls=2
Dual keys: main x2 = True,True third=False discovery still=True
Backoff delays (seeded): [0.91, 1.65, 4.3, 6.29, 16.29]
Circuit after 3 failures: state=open allow=False
Headers (large): {'Accept-Encoding': 'gzip', 'Accept': 'application/json'} gzip for 100kB? True
gzip roundtrip: 10000 -> 45 bytes
```

## What this mirrors in the live bot

This mirrors two hard-won live lessons. First, the FloodGuard in the live Telegram client: when Telegram answers 429, retrying makes the ban worse, so the live guard parks *all* mutating calls until `retry_after` plus a buffer, restores that cooldown from saved state after a restart, and lets queued alerts wait rather than storm — exactly the state machine proven here by the frozen call counter. Second, the Helius dual-key discipline: a main key and a separate research/discovery key with independent rate budgets, so a discovery sweep can never starve live trading calls (the `DualKeyBuckets` independence test). The gzip helper encodes the egress lesson: large uncompressed responses get dropped by the proxy, so large payloads are requested compressed. It is a teaching/checking model only; it shares no code with the live bot and makes no network calls.

## Self-exam

1. Why does the FloodGuard park *all* mutating calls on a single 429 instead of just retrying the failed call after `retry_after`?
2. The cooldown adds a +5s buffer on top of `retry_after`. What race does that buffer protect against, given the clock that reported `retry_after` is not your clock?
3. How does the test suite *prove* zero calls happen during cooldown, rather than merely asserting the guard reports itself parked?
4. Why must cooldown state survive a restart — what goes wrong if a daemon restarts mid-ban with a fresh, unparked guard?
5. Main and discovery keys drain independently. What live failure does that prevent, and what would a single shared bucket allow a research sweep to do?
6. Why is the jitter in `backoff_delays` seeded instead of drawn from the system RNG — and in production, why is jitter needed at all if every client uses the same base and factor?
7. A circuit breaker in `half_open` lets exactly the probe traffic through. What does a failure during half-open do here, and why is that the safe choice?
