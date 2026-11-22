import gzip
import pytest
from resilience import *

def make_guard():
    clock = SyntheticClock()
    return clock, FakeTransport(), FloodGuard(clock=clock)

def test_clock_advances():
    c = SyntheticClock(start=100)
    assert c.now() == 100
    c.advance(5)
    assert c.now() == 105

def test_clock_negative_advance_raises():
    with pytest.raises(ValueError):
        SyntheticClock().advance(-1)

def test_clock_bad_start_raises():
    with pytest.raises(ValueError):
        SyntheticClock(start=-1)

def test_guard_not_parked_initially():
    _, _, g = make_guard()
    assert g.is_parked() is False
    assert g.remaining() == 0.0

def test_guard_parks_on_429():
    _, _, g = make_guard()
    g.on_rate_limited(30)
    assert g.is_parked() is True

def test_guard_buffer_added():
    clock, _, g = make_guard()
    until = g.on_rate_limited(30)
    assert until == clock.now() + 35  # 30 + 5 buffer

def test_guard_zero_calls_during_cooldown():
    _, t, g = make_guard()
    assert g.try_send(t, "a") is not None
    assert t.calls == 1
    g.on_rate_limited(30)
    assert g.try_send(t, "b") is None
    assert g.try_send(t, "c") is None
    assert t.calls == 1  # zero calls during cooldown

def test_guard_resumes_after_cooldown():
    clock, t, g = make_guard()
    g.on_rate_limited(10)
    clock.advance(16)
    assert g.is_parked() is False
    assert g.try_send(t, "x") is not None
    assert t.calls == 1

def test_guard_exact_boundary():
    clock, _, g = make_guard()
    g.on_rate_limited(10)  # parked until +15
    clock.advance(15)
    assert g.is_parked() is False

def test_guard_negative_retry_raises():
    _, _, g = make_guard()
    with pytest.raises(ValueError):
        g.on_rate_limited(-1)

def test_guard_bad_buffer_raises():
    with pytest.raises(ValueError):
        FloodGuard(clock=SyntheticClock(), buffer_s=-1)

def test_guard_state_persists_across_restart():
    clock, t, g = make_guard()
    g.on_rate_limited(60)
    state = g.export_state()
    clock2 = SyntheticClock(start=clock.now())
    g2 = FloodGuard.restore(clock2, state)
    assert g2.is_parked() is True
    assert g2.try_send(t, "x") is None
    clock2.advance(66)
    assert g2.is_parked() is False

def test_guard_restore_bad_state_raises():
    with pytest.raises(ValueError):
        FloodGuard.restore(SyntheticClock(), {})

def test_bucket_drains():
    clock = SyntheticClock()
    b = RateBucket(capacity=3, refill_per_s=0, clock=clock)
    assert [b.try_acquire() for _ in range(4)] == [True, True, True, False]

def test_bucket_refills():
    clock = SyntheticClock()
    b = RateBucket(capacity=2, refill_per_s=1.0, clock=clock)
    b.try_acquire(); b.try_acquire()
    assert b.try_acquire() is False
    clock.advance(2)
    assert b.try_acquire() is True

def test_bucket_caps_at_capacity():
    clock = SyntheticClock()
    b = RateBucket(capacity=2, refill_per_s=10, clock=clock)
    clock.advance(100)
    assert b.tokens == pytest.approx(2.0)

def test_bucket_invalid_params():
    with pytest.raises(ValueError):
        RateBucket(capacity=0, refill_per_s=1, clock=SyntheticClock())
    with pytest.raises(ValueError):
        RateBucket(capacity=1, refill_per_s=-1, clock=SyntheticClock())

def test_bucket_acquire_bad_n():
    b = RateBucket(capacity=5, refill_per_s=0, clock=SyntheticClock())
    with pytest.raises(ValueError):
        b.try_acquire(0)

def test_dual_keys_independent():
    clock = SyntheticClock()
    dual = DualKeyBuckets(main=RateBucket(1, 0, clock), discovery=RateBucket(1, 0, clock))
    assert dual.acquire("main") is True
    assert dual.acquire("main") is False
    assert dual.acquire("discovery") is True  # unaffected by main drain

def test_dual_unknown_key_raises():
    clock = SyntheticClock()
    dual = DualKeyBuckets(main=RateBucket(1, 0, clock), discovery=RateBucket(1, 0, clock))
    with pytest.raises(ValueError):
        dual.acquire("third")

def test_backoff_grows():
    ds = backoff_delays(base_s=1, factor=2, jitter_frac=0, attempts=4)
    assert ds == [1, 2, 4, 8]

def test_backoff_caps_at_max():
    ds = backoff_delays(base_s=10, factor=10, max_s=25, jitter_frac=0, attempts=3)
    assert ds == [10, 25, 25]

def test_backoff_deterministic_seed():
    assert backoff_delays(seed=7) == backoff_delays(seed=7)
    assert backoff_delays(seed=7) != backoff_delays(seed=8)

def test_backoff_zero_attempts():
    assert backoff_delays(attempts=0) == []

def test_backoff_invalid():
    with pytest.raises(ValueError):
        backoff_delays(base_s=0)
    with pytest.raises(ValueError):
        backoff_delays(factor=0.5)
    with pytest.raises(ValueError):
        backoff_delays(jitter_frac=2)

def test_backoff_single_delay_matches_list():
    assert backoff_delay(2, seed=7) == backoff_delays(seed=7)[2]

def test_backoff_negative_attempt_raises():
    with pytest.raises(ValueError):
        backoff_delay(-1)

def test_breaker_starts_closed():
    cb = CircuitBreaker(clock=SyntheticClock())
    assert cb.state == "closed" and cb.allow() is True

def test_breaker_opens_at_threshold():
    cb = CircuitBreaker(clock=SyntheticClock(), failure_threshold=3)
    cb.record_failure(); cb.record_failure()
    assert cb.state == "closed"
    cb.record_failure()
    assert cb.state == "open" and cb.allow() is False

def test_breaker_half_open_after_timeout():
    clock = SyntheticClock()
    cb = CircuitBreaker(clock=clock, failure_threshold=1, reset_timeout_s=30)
    cb.record_failure()
    assert cb.state == "open"
    clock.advance(31)
    assert cb.state == "half_open" and cb.allow() is True

def test_breaker_success_resets():
    cb = CircuitBreaker(clock=SyntheticClock(), failure_threshold=2)
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    assert cb.state == "closed"

def test_breaker_half_open_failure_reopens():
    clock = SyntheticClock()
    cb = CircuitBreaker(clock=clock, failure_threshold=1, reset_timeout_s=10)
    cb.record_failure(); clock.advance(11)
    assert cb.state == "half_open"
    cb.record_failure()
    assert cb.state == "open"

def test_breaker_invalid_threshold():
    with pytest.raises(ValueError):
        CircuitBreaker(clock=SyntheticClock(), failure_threshold=0)

def test_encoding_headers_large_requests_gzip():
    assert encoding_headers(True)["Accept-Encoding"] == "gzip"

def test_encoding_headers_small_no_gzip_requirement():
    assert "Accept-Encoding" not in encoding_headers(False)

def test_should_request_gzip_threshold():
    assert should_request_gzip(100_000) is True
    assert should_request_gzip(100) is False
    assert should_request_gzip(50_000) is True

def test_should_request_gzip_negative_raises():
    with pytest.raises(ValueError):
        should_request_gzip(-1)

def test_decode_identity():
    assert decode_body(b"abc", None) == b"abc"
    assert decode_body(b"abc", "identity") == b"abc"

def test_decode_gzip():
    raw = b"hello world" * 100
    assert decode_body(gzip.compress(raw), "gzip") == raw

def test_decode_unsupported_raises():
    with pytest.raises(ValueError):
        decode_body(b"x", "br")

def test_decode_none_body_raises():
    with pytest.raises(ValueError):
        decode_body(None, None)

def test_gzip_roundtrip_shrinks_repetitive():
    raw_len, comp_len = gzip_roundtrip_size(b"a" * 5000)
    assert comp_len < raw_len

def test_fake_transport_counts():
    t = FakeTransport()
    t.send("a"); t.send("b")
    assert t.calls == 2 and t.sent == ["a", "b"]
