import pytest
from momentumscan import *

SAFE = lambda mint: True
UNSAFE = lambda mint: False

def test_good_pool_qualifies():
    ok, reason = qualify(good_pool(), safety_ok=SAFE, now=0)
    assert ok is True and reason is None

def test_doomsday_regression_qualifies():
    # h24 +2540% MUST qualify -- the old invented 300% ceiling blocked it.
    ok, reason = qualify(doomsday_pool(), safety_ok=SAFE, now=0)
    assert ok is True, f"Doomsday blocked with reason={reason}"

def test_no_ceiling_extreme_gain():
    ok, _ = qualify(good_pool(change_h24_pct=100_000.0), safety_ok=SAFE)
    assert ok is True

def test_gain_exactly_at_bar_qualifies():
    ok, _ = qualify(good_pool(change_h24_pct=20.0), safety_ok=SAFE)
    assert ok is True

def test_gain_just_below_bar_rejects():
    ok, reason = qualify(good_pool(change_h24_pct=19.9), safety_ok=SAFE)
    assert ok is False and reason == "gain24_low"

def test_liq_exactly_at_bar_qualifies():
    ok, _ = qualify(good_pool(liquidity_usd=50_000), safety_ok=SAFE)
    assert ok is True

def test_liq_below_rejects():
    ok, reason = qualify(good_pool(liquidity_usd=49_999), safety_ok=SAFE)
    assert ok is False and reason == "liq_low"

def test_vol_exactly_at_bar_qualifies():
    ok, _ = qualify(good_pool(volume_24h=100_000), safety_ok=SAFE)
    assert ok is True

def test_vol_below_rejects():
    ok, reason = qualify(good_pool(volume_24h=99_999), safety_ok=SAFE)
    assert ok is False and reason == "vol_low"

def test_dump_guard_rejects():
    ok, reason = qualify(good_pool(change_h1_pct=-20.0, change_m5_pct=5.0), safety_ok=SAFE)
    assert ok is False and reason == "dump_guard"

def test_dump_guard_boundary():
    # h1 exactly -15 is NOT < -15, but momentum then needs m5 > 0
    ok, _ = qualify(good_pool(change_h1_pct=-15.0, change_m5_pct=2.0), safety_ok=SAFE)
    assert ok is True
    ok2, reason2 = qualify(good_pool(change_h1_pct=-15.01, change_m5_pct=2.0), safety_ok=SAFE)
    assert ok2 is False and reason2 == "dump_guard"

def test_no_momentum_rejects():
    ok, reason = qualify(good_pool(change_h1_pct=-5.0, change_m5_pct=-1.0), safety_ok=SAFE)
    assert ok is False and reason == "no_momentum"

def test_momentum_via_m5_only():
    ok, _ = qualify(good_pool(change_h1_pct=-5.0, change_m5_pct=0.5), safety_ok=SAFE)
    assert ok is True

def test_momentum_zero_is_not_positive():
    ok, reason = qualify(good_pool(change_h1_pct=0.0, change_m5_pct=0.0), safety_ok=SAFE)
    assert ok is False and reason == "no_momentum"

def test_momentum_m5_none_uses_h1():
    ok, _ = qualify(good_pool(change_m5_pct=None), safety_ok=SAFE)
    assert ok is True

def test_safety_false_rejects():
    ok, reason = qualify(good_pool(), safety_ok=UNSAFE)
    assert ok is False and reason == "safety"

def test_safety_missing_predicate_fails_closed():
    ok, reason = qualify(good_pool(), safety_ok=None)
    assert ok is False and reason == "safety"

def test_safety_exception_fails_closed():
    def boom(mint):
        raise RuntimeError("api down")
    ok, reason = qualify(good_pool(), safety_ok=boom)
    assert ok is False and reason == "safety"

def test_bad_pool_type_rejects():
    ok, reason = qualify({"mint": "x"}, safety_ok=SAFE)
    assert ok is False and reason == "bad_pool"

def test_pool_validation():
    with pytest.raises(ValueError):
        Pool(mint="", liquidity_usd=1, volume_24h=1, change_h24_pct=1, change_h1_pct=1)
    with pytest.raises(ValueError):
        Pool(mint="x", liquidity_usd=None, volume_24h=1, change_h24_pct=1, change_h1_pct=1)
    with pytest.raises(ValueError):
        Pool(mint="x", liquidity_usd=1, volume_24h=1, change_h24_pct=True, change_h1_pct=1)

def test_cooldown_blocks_second_card():
    book = CooldownBook()
    assert qualify(good_pool(), safety_ok=SAFE, cooldown=book, now=1000)[0] is True
    ok, reason = qualify(good_pool(), safety_ok=SAFE, cooldown=book, now=2000)
    assert ok is False and reason == "cooldown"

def test_cooldown_expires_at_6h():
    book = CooldownBook()
    qualify(good_pool(), safety_ok=SAFE, cooldown=book, now=0)
    ok, _ = qualify(good_pool(), safety_ok=SAFE, cooldown=book, now=6 * 3600)
    assert ok is True

def test_cooldown_is_per_mint():
    book = CooldownBook()
    qualify(good_pool(), safety_ok=SAFE, cooldown=book, now=0)
    ok, _ = qualify(good_pool(mint="OtherMint"), safety_ok=SAFE, cooldown=book, now=10)
    assert ok is True

def test_cooldown_record_validation():
    book = CooldownBook()
    with pytest.raises(ValueError):
        book.record("", 0)
    with pytest.raises(ValueError):
        book.record("m", -1)

def test_reject_priority_liq_before_gain():
    ok, reason = qualify(good_pool(liquidity_usd=1, change_h24_pct=1), safety_ok=SAFE)
    assert reason == "liq_low"

def test_stats_count_reasons():
    pools = [good_pool(), good_pool(mint="A", liquidity_usd=1),
             good_pool(mint="B", change_h24_pct=1), good_pool(mint="C")]
    # C same default mint as first -> second C is a different mint, both qualify; use cooldown book shared via scan
    qualified, stats = scan(pools, safety_ok=SAFE, now=0)
    assert stats["scanned"] == 4
    assert stats["liq_low"] == 1 and stats["gain24_low"] == 1
    assert stats["qualified"] == 2

def test_stats_all_keys_present():
    _, stats = scan([], safety_ok=SAFE)
    for r in REJECT_REASONS:
        assert r in stats
    assert stats["qualified"] == 0 and stats["scanned"] == 0

def test_stats_filled_by_qualify():
    stats = new_stats()
    qualify(good_pool(liquidity_usd=1), safety_ok=SAFE, stats=stats)
    assert stats["liq_low"] == 1 and stats["scanned"] == 1

def test_scan_returns_qualified_only():
    pools = [good_pool(mint="Keep1"), good_pool(mint="Drop", volume_24h=1), doomsday_pool()]
    qualified, stats = scan(pools, safety_ok=SAFE, now=0)
    assert [p.mint for p in qualified] == ["Keep1", "DoomsdayMint"]

def test_constants_pinned():
    assert MIN_LIQUIDITY == 50_000 and MIN_VOLUME_24H == 100_000
    assert MIN_GAIN_24H == 20.0 and DUMP_GUARD_H1 == -15.0 and COOLDOWN_S == 21600

def test_has_positive_momentum_helper():
    assert has_positive_momentum(good_pool()) is True
    assert has_positive_momentum(good_pool(change_h1_pct=-1, change_m5_pct=None)) is False

def test_negative_gain_rejects_as_low():
    ok, reason = qualify(good_pool(change_h24_pct=-50.0), safety_ok=SAFE)
    assert ok is False and reason == "gain24_low"

def test_doomsday_shape_fields():
    p = doomsday_pool()
    assert p.change_h24_pct == 2540.0 and p.liquidity_usd == 96_000 and p.change_h1_pct > 0

def test_failed_qualify_does_not_start_cooldown():
    book = CooldownBook()
    qualify(good_pool(liquidity_usd=1), safety_ok=SAFE, cooldown=book, now=0)
    ok, _ = qualify(good_pool(), safety_ok=SAFE, cooldown=book, now=10)
    assert ok is True  # rejection never recorded a card

def test_scan_with_cooldown_dedupes():
    pools = [good_pool(), good_pool()]
    qualified, stats = scan(pools, safety_ok=SAFE, cooldown=CooldownBook(), now=0)
    assert len(qualified) == 1 and stats["cooldown"] == 1

def test_zero_liquidity_rejects():
    ok, reason = qualify(good_pool(liquidity_usd=0), safety_ok=SAFE)
    assert ok is False and reason == "liq_low"

def test_huge_liquidity_and_volume_qualify():
    ok, _ = qualify(good_pool(liquidity_usd=10_000_000, volume_24h=50_000_000), safety_ok=SAFE)
    assert ok is True

def test_new_stats_zeroed():
    s = new_stats()
    assert all(v == 0 for v in s.values())
