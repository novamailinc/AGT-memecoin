"""Demo for feed-resilience lab. Fake transports/clocks only."""
from resilience import (
    SyntheticClock, FakeTransport, FloodGuard, RateBucket, DualKeyBuckets,
    backoff_delays, CircuitBreaker, encoding_headers, should_request_gzip,
    gzip_roundtrip_size,
)


def main() -> None:
    print("=== Feed resilience lab (synthetic clock, fake transport) ===")
    clock = SyntheticClock()
    transport = FakeTransport()
    guard = FloodGuard(clock=clock)
    print(f"Before 429: send -> {guard.try_send(transport, 'hello')}, calls={transport.calls}")
    guard.on_rate_limited(retry_after=30)
    print(f"After 429 (retry_after=30, buffer=5): parked={guard.is_parked()} remaining={guard.remaining():.0f}s")
    print(f"During cooldown: send -> {guard.try_send(transport, 'blocked')}, calls={transport.calls} (zero new calls)")
    clock.advance(36)
    print(f"After cooldown: parked={guard.is_parked()} send -> {guard.try_send(transport, 'again')}, calls={transport.calls}")
    main_b = RateBucket(capacity=2, refill_per_s=0.1, clock=clock)
    disc_b = RateBucket(capacity=2, refill_per_s=0.1, clock=clock)
    dual = DualKeyBuckets(main=main_b, discovery=disc_b)
    print(f"Dual keys: main x2 = {dual.acquire('main')},{dual.acquire('main')} third={dual.acquire('main')} discovery still={dual.acquire('discovery')}")
    print(f"Backoff delays (seeded): {[round(d, 2) for d in backoff_delays()]}")
    cb = CircuitBreaker(clock=clock, failure_threshold=3)
    for _ in range(3):
        cb.record_failure()
    print(f"Circuit after 3 failures: state={cb.state} allow={cb.allow()}")
    print(f"Headers (large): {encoding_headers(True)} gzip for 100kB? {should_request_gzip(100_000)}")
    raw, comp = gzip_roundtrip_size(b'x' * 10000)
    print(f"gzip roundtrip: {raw} -> {comp} bytes")


if __name__ == "__main__":
    main()
