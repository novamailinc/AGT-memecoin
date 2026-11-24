"""Tests for wash.py -- synthetic streams, hand-checkable shares."""
import pytest

from wash import (BOT_THRESHOLD, BUMP_THRESHOLD, WASH_THRESHOLD, Trade,
                  assess, bot_like_mask, bot_share, bot_volume_share,
                  bump_mask, bump_share, distinct_wallets, flags_for,
                  is_tiny, negative_signal_score, roundtrip_volume,
                  same_slot_clusters, total_volume, toxicity_score,
                  trade_count, wallet_volumes, wash_share)


def buy(wallet, amt, slot=None, ts=0.0):
    return Trade(wallet, "buy", amt, ts=ts, slot=slot)


def sell(wallet, amt, slot=None, ts=0.0):
    return Trade(wallet, "sell", amt, ts=ts, slot=slot)


def clean_stream(n=12):
    # Distinct one-time buyers, real sizes, unique slots: fires nothing.
    return [buy(f"wallet{i}", 1.0 + 0.1 * i, slot=100 + i, ts=float(i)) for i in range(n)]


def wash_stream():
    out = []
    for i in range(6):
        out.append(buy(f"w{i}", 2.0, slot=200 + 2 * i))
        out.append(sell(f"w{i}", 2.0, slot=201 + 2 * i))
    return out


# -- validation / fail-closed --------------------------------------------
def test_trade_validation_rejects_bad_side():
    with pytest.raises(ValueError):
        Trade("w", "hold", 1.0)


def test_trade_validation_rejects_nonpositive_amount():
    with pytest.raises(ValueError):
        buy("w", 0)
    with pytest.raises(ValueError):
        buy("w", -1.0)
    with pytest.raises(ValueError):
        buy("w", None)


def test_trade_validation_rejects_empty_wallet_and_bad_slot():
    with pytest.raises(ValueError):
        buy("", 1.0)
    with pytest.raises(ValueError):
        buy("w", 1.0, slot=-1)
    with pytest.raises(ValueError):
        Trade("w", "buy", 1.0, ts=-1.0)


def test_share_functions_raise_on_empty_and_none():
    for fn in (wash_share, bot_share, bump_share, total_volume, toxicity_score):
        with pytest.raises(ValueError):
            fn([])
        with pytest.raises(ValueError):
            fn(None)


def test_is_tiny_boundary_and_validation():
    assert is_tiny(0.09) is True
    assert is_tiny(0.10) is False  # tiny is strictly below the threshold
    assert is_tiny(1.0) is False
    with pytest.raises(ValueError):
        is_tiny(0)


# -- wash ------------------------------------------------------------------
def test_total_volume_and_count():
    s = clean_stream(10)
    assert trade_count(s) == 10
    assert total_volume(s) == pytest.approx(sum(1.0 + 0.1 * i for i in range(10)))


def test_wallet_volumes_split_buys_and_sells():
    vols = wallet_volumes([buy("a", 3.0), sell("a", 1.0), buy("b", 2.0)])
    assert vols["a"] == (3.0, 1.0)
    assert vols["b"] == (2.0, 0.0)


def test_pure_buyers_have_zero_wash_share():
    assert wash_share(clean_stream()) == 0.0
    assert roundtrip_volume(clean_stream()) == 0.0


def test_full_roundtrip_stream_is_all_wash():
    assert wash_share(wash_stream()) == pytest.approx(1.0)
    assert roundtrip_volume(wash_stream()) == pytest.approx(total_volume(wash_stream()))


def test_partial_roundtrip_hand_value():
    # a buys 10 sells 4 (wash 8), b buys 10 (wash 0); total 24 -> 1/3.
    s = [buy("a", 10.0), sell("a", 4.0), buy("b", 10.0)]
    assert wash_share(s) == pytest.approx(8.0 / 24.0)


def test_wash_share_never_exceeds_one():
    s = []
    for i in range(5):
        s += [buy(f"w{i}", 1.0), sell(f"w{i}", 5.0)]
    assert 0.0 <= wash_share(s) <= 1.0


def test_sell_heavy_wallet_wash_uses_smaller_leg():
    # buys 2, sells 9 -> roundtrip is 2*2=4 of 11 total.
    s = [buy("a", 2.0), sell("a", 9.0)]
    assert roundtrip_volume(s) == pytest.approx(4.0)


# -- bot share ---------------------------------------------------------------
def test_clean_stream_has_zero_bot_share():
    assert bot_share(clean_stream()) == 0.0
    assert bot_like_mask(clean_stream()) == [False] * 12


def test_tiny_trades_are_bot_like():
    s = [buy(f"w{i}", 0.01, slot=i) for i in range(10)]
    assert bot_share(s) == pytest.approx(1.0)


def test_high_frequency_wallet_is_bot_like():
    s = [buy("freq", 1.0, slot=i) for i in range(8)] + [buy("other", 1.0, slot=99)]
    mask = bot_like_mask(s)
    assert all(mask[:8]) and mask[8] is False
    assert bot_share(s) == pytest.approx(8 / 9)


def test_seven_trades_is_below_high_frequency():
    s = [buy("freq", 1.0, slot=i) for i in range(7)]
    assert bot_share(s) == 0.0


def test_same_slot_cluster_is_bot_like():
    s = [buy(f"w{i}", 1.0, slot=42) for i in range(3)] + [buy("solo", 1.0, slot=7)]
    mask = bot_like_mask(s)
    assert mask[:3] == [True, True, True] and mask[3] is False


def test_two_in_a_slot_is_not_a_cluster():
    s = [buy("a", 1.0, slot=42), buy("b", 1.0, slot=42)]
    assert bot_share(s) == 0.0
    assert same_slot_clusters(s) == 0


def test_same_slot_clusters_counts_crowded_slots_only():
    s = [buy(f"a{i}", 1.0, slot=1) for i in range(3)]
    s += [buy(f"b{i}", 1.0, slot=2) for i in range(4)]
    s += [buy("c", 1.0, slot=3)]
    assert same_slot_clusters(s) == 2


def test_none_slots_never_cluster():
    s = [buy(f"w{i}", 1.0, slot=None) for i in range(10)]
    # 10 distinct wallets, real size, no slots -> not bot-like.
    assert bot_share(s) == 0.0


def test_bot_volume_share_differs_from_count_share():
    # One huge clean trade + nine tiny bot trades: count 90%, volume tiny.
    s = [buy("whale", 100.0, slot=1)] + [buy(f"t{i}", 0.01, slot=10 + i) for i in range(9)]
    assert bot_share(s) == pytest.approx(0.9)
    assert bot_volume_share(s) < 0.01


def test_negative_signal_score_is_never_positive():
    assert negative_signal_score(clean_stream()) == 0.0
    s = [buy(f"t{i}", 0.01, slot=i) for i in range(10)]
    assert negative_signal_score(s) == pytest.approx(-100.0)


# -- bump farm -----------------------------------------------------------------
def test_bump_requires_three_small_buys_from_one_wallet():
    s = [buy("bumper", 0.05, slot=i) for i in range(3)]
    assert bump_share(s) == pytest.approx(1.0)
    assert all(bump_mask(s))


def test_two_small_buys_do_not_make_a_bump_farm():
    s = [buy("bumper", 0.05, slot=i) for i in range(2)] + [buy("real", 2.0, slot=9)]
    assert bump_share(s) == 0.0


def test_small_sells_are_not_bump_trades():
    s = [sell("bumper", 0.05, slot=i) for i in range(4)]
    assert bump_share(s) == 0.0


def test_distinct_wallets_counts_unique():
    assert distinct_wallets(clean_stream()) == 12
    assert distinct_wallets(wash_stream()) == 6


# -- thresholds ------------------------------------------------------------------
def test_flags_at_exact_thresholds():
    assert flags_for(WASH_THRESHOLD, 0.0, 0.0) == ["WASH_SKEW"]
    assert flags_for(0.0, BOT_THRESHOLD, 0.0) == ["BOT_SWARM"]
    assert flags_for(0.0, 0.0, BUMP_THRESHOLD) == ["BUMP_FARM"]


def test_flags_just_below_thresholds_fire_nothing():
    assert flags_for(WASH_THRESHOLD - 1e-9, BOT_THRESHOLD - 1e-9, BUMP_THRESHOLD - 1e-9) == []


def test_flags_all_three_in_stable_order():
    assert flags_for(1.0, 1.0, 1.0) == ["WASH_SKEW", "BOT_SWARM", "BUMP_FARM"]


def test_flags_for_rejects_out_of_range_shares():
    with pytest.raises(ValueError):
        flags_for(1.01, 0.0, 0.0)
    with pytest.raises(ValueError):
        flags_for(0.0, -0.1, 0.0)
    with pytest.raises(ValueError):
        flags_for(None, 0.0, 0.0)


def test_toxicity_score_bounds_and_clean_zero():
    assert toxicity_score(clean_stream()) == 0.0
    assert 0.0 < toxicity_score(wash_stream()) <= 100.0


# -- assess: fail-open fingerprint semantics --------------------------------------
def test_assess_none_is_unknown_not_pass_not_veto():
    out = assess(None)
    assert out["status"] == "unknown"
    assert out["flags"] == []
    assert out["wash_share"] is None and out["bot_share"] is None


def test_assess_empty_and_thin_streams_are_unknown():
    assert assess([])["status"] == "unknown"
    assert assess(clean_stream(9))["status"] == "unknown"  # below MIN_TRADES


def test_assess_clean_stream_at_minimum_is_clean_and_fires_nothing():
    out = assess(clean_stream(10))
    assert out["status"] == "clean"
    assert out["flags"] == []
    assert out["wash_share"] == 0.0 and out["bot_share"] == 0.0 and out["bump_share"] == 0.0


def test_assess_wash_stream_flags_wash_skew():
    out = assess(wash_stream())
    assert out["status"] == "flagged"
    assert "WASH_SKEW" in out["flags"]


def test_assess_bot_stream_flags_bot_swarm():
    s = [buy(f"t{i}", 0.01, slot=i) for i in range(12)]
    out = assess(s)
    assert out["status"] == "flagged" and "BOT_SWARM" in out["flags"]


def test_assess_bump_stream_flags_bump_farm():
    s = []
    for w in range(4):
        s += [buy(f"bumper{w}", 0.05, slot=w * 10 + i) for i in range(3)]
    out = assess(s)
    assert "BUMP_FARM" in out["flags"]


def test_assess_reports_trade_count_and_shares_in_range():
    out = assess(clean_stream(15))
    assert out["trade_count"] == 15
    assert all(0.0 <= out[k] <= 1.0 for k in ("wash_share", "bot_share", "bump_share"))


def test_assess_rejects_bad_min_trades():
    with pytest.raises(ValueError):
        assess(clean_stream(), min_trades=0)
