# Feed Resilience — study notes

## Core ideas (teach-back)

APIs under load do not fail politely. They rate-limit you, ban you briefly, drop large responses, and go
flaky — and the natural instinct, "just retry", often makes every one of those worse. This lab builds
the small state machines that survive hostile APIs, and proves their behaviour with an injected clock
and a call-counting fake transport. No real HTTP happens anywhere.

The core components:

**A synthetic clock.** `SyntheticClock(start=1000.0)` with `now()` and `advance(seconds)`. Time never
runs backwards: a negative start or a negative advance raises `ValueError`. Because time is injected,
tests can step across a 35-second cooldown instantly and exactly.

**A fake transport.** `FakeTransport.send(payload)` increments `calls`, appends to `sent`, and returns
`{"ok": True, "payload": payload}`. This is what turns "we made zero calls during cooldown" from an
assertion into a measurement: read the counter.

**The FloodGuard.** When an API answers 429 (rate limited), the failed call is not the problem — *every
subsequent call during the ban window* deepens the punishment. So on `on_rate_limited(retry_after)`, the
guard parks **all** mutating calls until `now + retry_after + buffer_s`, with a default buffer of 5.0s.
While parked, `try_send` returns `None` *without touching the transport at all*. Queued work waits; it
does not storm.

The buffer exists because the clock that computed `retry_after` is the server's, not yours. Arriving
exactly at the nominal expiry risks landing a hair early by the server's reckoning and earning a fresh
ban.

Cooldown state must survive a restart: `export_state()` / `FloodGuard.restore(clock, state)` persist
`parked_until` (and the buffer). A daemon that restarts mid-ban with a fresh, unparked guard would
immediately resume hammering the API that just banned it — the restart would re-commit the offence. A
state dict missing `parked_until` raises `ValueError`.

**Token buckets, per key.** `RateBucket(capacity, refill_per_s, clock)` holds tokens that refill with
elapsed synthetic time, capped at capacity; `try_acquire(n=1)` spends them or returns `False`.
`DualKeyBuckets(main, discovery)` keeps two buckets independent: exhausting the discovery key leaves the
main key untouched, so a research sweep can never starve live calls. An unknown key name raises.

**Backoff with jitter.** `backoff_delays(...)` gives exponential delays capped at `max_s`, each
perturbed by seeded jitter. Jitter exists because many clients sharing the same base and factor would
otherwise retry in lock-step, re-creating the spike that caused the failure. Here the jitter is *seeded*
so tests are reproducible; `backoff_delay(attempt, ...)` returns an entry consistent with the list form.

**A circuit breaker.** `CircuitBreaker(clock, failure_threshold=3, reset_timeout_s=30.0)` cycles `closed
→ open → half_open`: failures at the threshold open it (`allow()` false); after the reset timeout it
reports `half_open` and lets probe traffic through; a success closes and resets it; a failure while
half-open re-opens it immediately.

**The gzip lesson.** Large uncompressed responses get dropped by the egress proxy, so:
`encoding_headers(expect_large=True)` requests `Accept-Encoding: gzip`, `should_request_gzip` flips at
50,000 estimated bytes, and `decode_body` handles `None`/`identity`/`gzip` via stdlib `gzip`, raising on
`None` bodies or unsupported encodings.

## Key derivations & formulas

### FloodGuard state machine

```text
state: parked_until (a timestamp), buffer_s = 5.0

on_rate_limited(retry_after):
    parked_until = clock.now() + retry_after + buffer_s

is_parked()  <=> clock.now() < parked_until
remaining()   =  max(0, parked_until − clock.now())

try_send(transport, payload):
    if is_parked(): return None        # transport untouched
    else:           return transport.send(payload)
```

The boundary is exact: at `now == parked_until` the guard is no longer parked and sending resumes.
Negative `retry_after` or `buffer_s` raises.

### Token bucket refill

On each access, refill before deciding:

```text
elapsed = now − last_refill_time
tokens  = min(capacity, tokens + elapsed * refill_per_s)
try_acquire(n):  if tokens >= n: tokens −= n, return True
                 else: return False
```

The `min(capacity, ...)` cap means idle time banks at most one bucket's worth of burst — never more.
Fail-closed inputs: non-positive capacity, negative refill, or non-positive `n` raise `ValueError`.

### Backoff with seeded jitter

For attempt index `i` (0-based):

```text
raw_i    = min(max_s, base_s * factor ** i)
jitter_i = raw_i * jitter_frac * (rng.random()*2 − 1)   # in [−jitter_frac, +jitter_frac] of raw
delay_i  = max(0, raw_i + jitter_i)
```

Defaults: `base_s=1.0, factor=2.0, max_s=60.0, attempts=5, seed=7, jitter_frac=0.25`. Validation:
non-positive base, `factor < 1`, `max_s < base_s`, negative attempts, or jitter outside `[0, 1]` all
raise.

### Circuit breaker transitions

```text
closed:     record_failure × threshold  -> open (opened_at = now)
            record_success              -> closed, failures = 0
open:       allow() = False
            now − opened_at >= reset_timeout_s -> state reports half_open
half_open:  allow() = True (probe traffic)
            record_failure -> open immediately (opened_at = now)
            record_success -> closed, failures = 0
```

Note the implementation shape: `state` is a property — an internally-`open` breaker *reports*
`half_open` once the timeout has elapsed, without a background timer. A failure during half-open is the
safe outcome: one probe failed, so the endpoint is presumed still down and the full timeout restarts.

### Gzip threshold

```text
should_request_gzip(estimated_bytes) <=> estimated_bytes >= 50_000
gzip_roundtrip_size(data) = (len(data), len(gzip.compress(data)))
```

## Worked example

From `demo.py`, one continuous walk-through:

1. Before any 429: `try_send('hello')` succeeds, transport `calls=1`. 2.
  `on_rate_limited(retry_after=30)` with buffer 5: `parked=True, remaining=35s` — the 30 + 5 arithmetic
  is the whole parking rule in one line. 3. During cooldown: `try_send('blocked')` returns `None` and
  `calls` is **still 1**. Zero new calls — proven by the counter, not by the guard's self-report. 4.
  `clock.advance(36)`: `parked=False`, the next send succeeds, `calls=2`.

Then the other machines:

- Dual keys, each bucket capacity 2: main acquires `True, True`, third main acquire `False`, discovery
  still `True` — independence in one line.
- Seeded backoff delays (defaults, seed 7): `[0.91, 1.65,
  4.3, 6.29, 16.29]` — growing, capped-ward, and identical on every run.
- Circuit breaker after 3
  failures: `state=open, allow=False`.
- Headers for a large response: `{'Accept-Encoding': 'gzip',
  'Accept': 'application/json'}`, and `should_request_gzip(100_000)` is `True`.
- Gzip round-trip:
  10,000 bytes of repetitive payload compress to **45 bytes**.

## Trap log

- T1. **Retrying the 429'd call.** A rate-limit response is an instruction to stop, not a transient
  error. Retrying inside the ban window extends the ban; parking everything is the compliant move.
- T2. **Parking only the failed endpoint.** The ban is typically per client/key, not per call type. Letting
  "other" mutating calls continue keeps the pressure on and re-triggers the limit.
- T3. **Trusting
  `retry_after` to the second.** It was computed on the server's clock and rounded by the server's
  rules. The +5s buffer absorbs clock skew; resuming at the exact nominal instant races the expiry.
- T4. **Cooldown that dies with the process.** A restart that clears the park state converts a
  deployment hiccup into a fresh offence against an active ban. Persist `parked_until`.
- T5. **Proving
  politeness by self-report.** "The guard says it didn't call" is worthless; the transport's call
  counter is the evidence. Test doubles should count, not cooperate.
- T6. **One shared bucket for live
  and research keys.** A discovery sweep that drains the shared budget starves live calls of rate
  exactly when the sweep is busiest. Separate buckets fail independently.
- T7. **Synchronised
  retries.** Identical backoff schedules across clients re-spike the recovering service in waves. Jitter
  decorrelates them; seeding keeps the jitter testable.
- T8. **Half-open means healthy.** Half-open is
  a *probe*, not a recovery. Treating the first post-timeout traffic as normal load re-breaks a
  still-down endpoint; a probe failure must re-open instantly.
- T9. **Forgetting the encoding on big
  payloads.** If the proxy drops large uncompressed responses, a "successful" request can still lose its
  body in transit. Request gzip above the size threshold and decode what you asked for.

## Source flags

- Every component, default, and threshold above (buffer 5.0s, gzip threshold 50,000 bytes, breaker
  threshold 3 / timeout 30.0s, backoff defaults and seed 7) is from `resilience.py`. [S]
- Test coverage
  — 43 tests passing, including zero-calls-during-cooldown by counter, cooldown restore across a
  simulated restart, and dual-key independence — is the README's test-suite account. [S]
- The demo
  figures (remaining 35s, calls frozen at 1, backoff list, 10,000 → 45 bytes) are the README's captured
  output. [S]
- The live-bot parallels — Telegram FloodGuard parking all mutating calls with state
  restored after restart, Helius dual keys, and the dropped-large-response egress lesson — are the
  README's mirroring claims. [M]
- The clock-skew rationale for the +5s buffer is reasoned here and
  matches the README's self-exam framing. [R]

## One-paragraph verdict

This toolkit proves the survival state machines in isolation, with the strongest kind of evidence available in a lab: a call-counting transport shows zero calls during a FloodGuard park (30s retry-after + 5s buffer = 35s), a synthetic clock makes the resume boundary exact, exported state keeps the park alive across a simulated restart, dual buckets drain independently, seeded backoff is reproducible, and the breaker's closed/open/half-open transitions behave as specified. It does not prove that any real API's ban behaviour matches the 429 model, that 5s is the right buffer in production, or that the live transports behave like the fake; it proves the *logic* a client needs before it is allowed near a real, hostile API.
