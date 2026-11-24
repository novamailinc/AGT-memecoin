"""Tests for ruglab.py -- one trigger fixture per detector, one clean."""
from ruglab import (cant_sell, holder_crash, lp_pull, mint_and_dump,
                    run_detectors, triggered_names, wash_skew)


def snap(ts=0, price=1.0, liq=100_000, holders=500, supply=1_000_000,
         mint=False, sell_ok=True, buy=3_000, sell=2_500,
         unique=150, trades=300):
    return {"ts": ts, "price": price, "liquidity_usd": liq,
            "holders": holders, "supply": supply,
            "mint_authority_active": mint, "sell_sim_ok": sell_ok,
            "buy_vol": buy, "sell_vol": sell,
            "unique_buyers": unique, "trades": trades}


CLEAN = [
    snap(ts=0, price=1.0, liq=100_000, holders=500),
    snap(ts=1800, price=1.25, liq=115_000, holders=545,
         buy=4_000, sell=3_000, unique=180, trades=320),
    snap(ts=3600, price=1.4, liq=125_000, holders=590,
         buy=4_500, sell=3_200, unique=200, trades=340),
    snap(ts=5400, price=1.35, liq=120_000, holders=600,
         buy=4_200, sell=3_600, unique=190, trades=330),
]


def test_clean_lifecycle_triggers_nothing():
    results = run_detectors(CLEAN)
    assert set(results) == {"lp_pull", "mint_and_dump", "cant_sell",
                            "wash_skew", "holder_crash"}
    assert all(hit is False for hit, _ in results.values())
    assert triggered_names(CLEAN) == []


def test_lp_pull_triggers():
    life = [snap(ts=0, price=1.0, liq=100_000, holders=500),
            snap(ts=1200, price=0.85, liq=85_000, holders=520),
            snap(ts=2400, price=0.3, liq=15_000, holders=460)]
    hit, evidence = lp_pull(life)
    assert hit is True
    assert "liquidity" in evidence


def test_lp_pull_79_percent_drop_does_not_trigger():
    life = [snap(ts=0, price=1.0, liq=100_000),
            snap(ts=1800, price=0.3, liq=21_000)]  # exactly -79% liq
    assert lp_pull(life)[0] is False


def test_lp_pull_needs_price_collapse_too():
    life = [snap(ts=0, price=1.0, liq=100_000),
            snap(ts=1800, price=0.9, liq=10_000)]  # liq -90%, price -10%
    assert lp_pull(life)[0] is False


def test_lp_pull_outside_short_window_does_not_trigger():
    life = [snap(ts=0, price=1.0, liq=100_000),
            snap(ts=86_400, price=0.2, liq=5_000)]  # a day later
    assert lp_pull(life)[0] is False


def test_mint_and_dump_triggers():
    life = [snap(ts=0, price=1.0, supply=1_000_000, mint=True),
            snap(ts=1800, price=1.1, supply=1_300_000, mint=True),  # +30%
            snap(ts=3600, price=0.4, supply=1_300_000, mint=True)]   # -60%
    hit, evidence = mint_and_dump(life)
    assert hit is True
    assert "supply" in evidence


def test_mint_and_dump_needs_active_mint_authority():
    life = [snap(ts=0, price=1.0, supply=1_000_000, mint=False),
            snap(ts=1800, price=1.1, supply=1_300_000, mint=False),
            snap(ts=3600, price=0.4, supply=1_300_000, mint=False)]
    assert mint_and_dump(life)[0] is False


def test_mint_and_dump_20_percent_exactly_does_not_trigger():
    life = [snap(ts=0, price=1.0, supply=1_000_000, mint=True),
            snap(ts=1800, price=1.0, supply=1_200_000, mint=True),  # +20.0%
            snap(ts=3600, price=0.4, supply=1_200_000, mint=True)]
    assert mint_and_dump(life)[0] is False  # rule is strictly >20%


def test_cant_sell_triggers_on_any_failed_sim():
    life = [snap(ts=0), snap(ts=1800, sell_ok=False), snap(ts=3600)]
    hit, evidence = cant_sell(life)
    assert hit is True
    assert "snapshot 1" in evidence


def test_cant_sell_missing_field_never_triggers():
    life = [{"ts": 0, "price": 1.0}, {"ts": 10, "price": 1.1}]
    assert cant_sell(life)[0] is False


def test_wash_skew_triggers():
    life = [snap(ts=0, holders=500),
            snap(ts=1800, holders=505, buy=60_000, sell=58_000,
                 unique=40, trades=2_000)]  # ratio 0.02, holders +1%
    hit, evidence = wash_skew(life)
    assert hit is True
    assert "unique/trades" in evidence


def test_wash_skew_needs_all_three_conditions():
    high_unique = [snap(ts=0, holders=500),
                   snap(ts=1800, holders=505, buy=60_000, sell=58_000,
                        unique=400, trades=2_000)]  # ratio 0.2
    assert wash_skew(high_unique)[0] is False
    growing = [snap(ts=0, holders=500),
               snap(ts=1800, holders=700, buy=60_000, sell=58_000,
                    unique=40, trades=2_000)]  # holders +40%
    assert wash_skew(growing)[0] is False
    low_volume = [snap(ts=0, holders=500),
                  snap(ts=1800, holders=505, buy=400, sell=300,
                       unique=4, trades=2_000)]  # volume $700
    assert wash_skew(low_volume)[0] is False


def test_holder_crash_triggers_at_40_percent():
    life = [snap(ts=0, holders=500), snap(ts=1800, holders=300)]  # -40%
    hit, evidence = holder_crash(life)
    assert hit is True
    assert "holders" in evidence


def test_holder_crash_39_percent_and_slow_bleed_do_not_trigger():
    life = [snap(ts=0, holders=500), snap(ts=1800, holders=305)]  # -39%
    assert holder_crash(life)[0] is False
    bleed = [snap(ts=i * 1800, holders=h)
             for i, h in enumerate((500, 450, 400, 350, 300))]
    assert holder_crash(bleed)[0] is False  # -40% total, never in one step


def test_run_detectors_on_trigger_fixture_reports_only_that_detector():
    life = [snap(ts=0, holders=500), snap(ts=1800, holders=280)]
    assert triggered_names(life) == ["holder_crash"]


def test_empty_lifecycle_is_all_false_not_an_exception():
    results = run_detectors([])
    assert len(results) == 5
    assert all(hit is False for hit, _ in results.values())


# -- lp_pull boundaries ---------------------------------------------------------

import pytest

from ruglab import assess, risk_grade, rug_score


def test_lp_pull_exact_thresholds_fire_inclusively():
    life = [snap(ts=0, price=1.0, liq=100_000),
            snap(ts=1800, price=0.5, liq=20_000)]  # liq -80.0%, price -50.0%
    assert lp_pull(life)[0] is True


def test_lp_pull_price_just_below_threshold_does_not_fire():
    life = [snap(ts=0, price=1.0, liq=100_000),
            snap(ts=1800, price=0.501, liq=10_000)]  # liq -90%, price -49.9%
    assert lp_pull(life)[0] is False


def test_lp_pull_window_edge_3600_fires_3601_does_not():
    fires = [snap(ts=0, price=1.0, liq=100_000),
             snap(ts=3600, price=0.2, liq=5_000)]
    assert lp_pull(fires)[0] is True
    late = [snap(ts=0, price=1.0, liq=100_000),
            snap(ts=3601, price=0.2, liq=5_000)]
    assert lp_pull(late)[0] is False


def test_lp_pull_step_window_fallback_without_timestamps():
    bare = lambda price, liq: {"price": price, "liquidity_usd": liq}
    empty = {"holders": 500}  # unusable as a pair start
    within = [bare(1.0, 100_000), empty, empty, bare(0.3, 10_000)]
    assert lp_pull(within)[0] is True   # pair 0->3, gap == 3 steps
    beyond = [bare(1.0, 100_000), empty, empty, empty, bare(0.3, 10_000)]
    assert lp_pull(beyond)[0] is False  # pair 0->4, gap == 4 steps


def test_lp_pull_window_is_measured_per_pair():
    life = [snap(ts=0, price=1.0, liq=100_000),
            snap(ts=3500, price=0.95, liq=95_000),
            snap(ts=4000, price=0.35, liq=10_000)]
    # Pair 0->2 is outside the hour; pair 1->2 (500s) collapses.
    assert lp_pull(life)[0] is True


def test_lp_pull_zero_or_missing_liquidity_never_fires():
    zeroed = [snap(ts=0, price=1.0, liq=0), snap(ts=60, price=0.1, liq=0)]
    assert lp_pull(zeroed)[0] is False
    missing = [{"price": 1.0}, {"price": 0.1}]
    assert lp_pull(missing)[0] is False
    assert lp_pull([snap(ts=0)])[0] is False  # single snapshot


# -- mint_and_dump boundaries ------------------------------------------------------


def test_mint_and_dump_price_drop_exactly_50_does_not_fire():
    life = [snap(ts=0, price=1.0, supply=1_000_000, mint=True),
            snap(ts=1800, price=1.0, supply=1_300_000, mint=True),
            snap(ts=3600, price=0.5, supply=1_300_000, mint=True)]  # -50.0%
    assert mint_and_dump(life)[0] is False  # rule is strictly >50%


def test_mint_and_dump_inflation_just_over_20_fires():
    life = [snap(ts=0, price=1.0, supply=1_000_000, mint=True),
            snap(ts=1800, price=1.0, supply=1_201_000, mint=True),  # +20.1%
            snap(ts=3600, price=0.4, supply=1_201_000, mint=True)]
    assert mint_and_dump(life)[0] is True


def test_mint_and_dump_mint_active_on_either_snapshot_of_the_pair():
    only_at_start = [snap(ts=0, price=1.0, supply=1_000_000, mint=True),
                     snap(ts=1800, price=1.0, supply=1_300_000, mint=False),
                     snap(ts=3600, price=0.4, supply=1_300_000, mint=False)]
    assert mint_and_dump(only_at_start)[0] is True
    only_at_inflation = [snap(ts=0, price=1.0, supply=1_000_000, mint=False),
                         snap(ts=1800, price=1.0, supply=1_300_000, mint=True),
                         snap(ts=3600, price=0.4, supply=1_300_000, mint=False)]
    assert mint_and_dump(only_at_inflation)[0] is True


def test_mint_and_dump_price_drop_before_inflation_does_not_count():
    life = [snap(ts=0, price=1.0, supply=1_000_000, mint=True),
            snap(ts=1800, price=0.4, supply=1_000_000, mint=True),   # drop first
            snap(ts=3600, price=0.9, supply=1_300_000, mint=True)]   # then inflate
    assert mint_and_dump(life)[0] is False


def test_mint_and_dump_missing_or_zero_supply_never_fires():
    no_supply = [{"ts": 0, "price": 1.0, "mint_authority_active": True},
                 {"ts": 60, "price": 0.2, "mint_authority_active": True}]
    assert mint_and_dump(no_supply)[0] is False
    zero_supply = [snap(ts=0, price=1.0, supply=0, mint=True),
                   snap(ts=60, price=0.2, supply=1_000_000, mint=True)]
    assert mint_and_dump(zero_supply)[0] is False


# -- cant_sell strictness -------------------------------------------------------------


def test_cant_sell_only_a_real_boolean_false_fires():
    for not_false in (None, 0, "false", 1):
        life = [{"ts": 0, "sell_sim_ok": not_false}]
        assert cant_sell(life)[0] is False, not_false


def test_cant_sell_at_first_snapshot_reports_index_zero():
    hit, evidence = cant_sell([snap(ts=0, sell_ok=False), snap(ts=60)])
    assert hit is True
    assert "snapshot 0" in evidence


# -- wash_skew boundaries -----------------------------------------------------------------


def _wash_life(holders2=505, buy=60_000, sell=58_000, unique=40, trades=2_000):
    return [snap(ts=0, holders=500),
            snap(ts=1800, holders=holders2, buy=buy, sell=sell,
                 unique=unique, trades=trades)]


def test_wash_volume_exactly_10000_does_not_fire_but_10001_does():
    assert wash_skew(_wash_life(buy=6_000, sell=4_000))[0] is False
    assert wash_skew(_wash_life(buy=6_001, sell=4_000))[0] is True


def test_wash_unique_ratio_exactly_0_1_does_not_fire():
    assert wash_skew(_wash_life(unique=100, trades=1_000))[0] is False
    assert wash_skew(_wash_life(unique=99, trades=1_000))[0] is True


def test_wash_holder_change_exactly_2_percent_does_not_fire():
    assert wash_skew(_wash_life(holders2=510))[0] is False  # +2.0% exactly
    assert wash_skew(_wash_life(holders2=509))[0] is True   # +1.8%


def test_wash_holder_drop_also_counts_as_flat_via_absolute_change():
    assert wash_skew(_wash_life(holders2=495))[0] is True  # -1.0% move


def test_wash_zero_trades_and_missing_fields_are_skipped_safely():
    assert wash_skew(_wash_life(trades=0, unique=0))[0] is False
    sparse = [{"ts": 0, "holders": 500},
              {"ts": 60, "buy_vol": 90_000}]  # everything else missing
    assert wash_skew(sparse)[0] is False
    assert wash_skew([])[0] is False


# -- holder_crash edges -----------------------------------------------------------------------


def test_holder_crash_boundary_at_a_different_scale():
    assert holder_crash([snap(holders=1_000), snap(holders=600)])[0] is True
    assert holder_crash([snap(holders=1_000), snap(holders=601)])[0] is False


def test_holder_crash_skips_unusable_pairs_and_fires_on_a_later_pair():
    life = [{"holders": 500}, {"holders": None}, {"holders": 400},
            {"holders": 200}]  # 400 -> 200 is a -50% consecutive pair
    assert holder_crash(life)[0] is True
    zero_start = [snap(holders=0), snap(holders=0)]
    assert holder_crash(zero_start)[0] is False
    rising = [snap(holders=100), snap(holders=900)]
    assert holder_crash(rising)[0] is False


# -- combined scoring ----------------------------------------------------------------------------


def test_run_detectors_none_lifecycle_is_all_false():
    results = run_detectors(None)
    assert len(results) == 5
    assert all(hit is False for hit, _ in results.values())
    assert triggered_names(None) == []


def test_multiple_detectors_fire_together_sorted():
    life = [snap(ts=0, price=1.0, liq=100_000, holders=500),
            snap(ts=1800, price=0.2, liq=8_000, holders=250, sell_ok=False)]
    assert triggered_names(life) == ["cant_sell", "holder_crash", "lp_pull"]


def test_rug_score_weights_for_single_detectors():
    wash_only = _wash_life()
    assert rug_score(wash_only) == 15
    sell_only = [snap(ts=0), snap(ts=60, sell_ok=False)]
    assert rug_score(sell_only) == 25
    lp_only = [snap(ts=0, price=1.0, liq=100_000),
               snap(ts=1800, price=0.3, liq=15_000)]
    assert rug_score(lp_only) == 35
    assert rug_score(CLEAN) == 0


def _all_five_lifecycle():
    return [
        snap(ts=0, price=1.0, liq=100_000, holders=500,
             supply=1_000_000, mint=True),
        snap(ts=1800, price=1.05, liq=105_000, holders=505,
             supply=1_300_000, mint=True,
             buy=60_000, sell=58_000, unique=40, trades=2_000),
        snap(ts=3000, price=0.3, liq=8_000, holders=300,
             supply=1_300_000, mint=True, sell_ok=False),
    ]


def test_all_five_detectors_can_fire_on_one_lifecycle():
    life = _all_five_lifecycle()
    assert triggered_names(life) == ["cant_sell", "holder_crash", "lp_pull",
                                     "mint_and_dump", "wash_skew"]


def test_rug_score_caps_at_100_and_assess_agrees():
    life = _all_five_lifecycle()
    assert rug_score(life) == 100  # raw weights sum to 125
    report = assess(life)
    assert report["score"] == 100
    assert report["grade"] == "severe"
    assert report["detectors_fired"] == 5
    assert report["triggered"] == triggered_names(life)


def test_assess_clean_and_single_detector_are_consistent():
    clean = assess(CLEAN)
    assert clean == {"triggered": [], "score": 0, "grade": "clean",
                     "detectors_fired": 0}
    crash_only = assess([snap(ts=0, holders=500), snap(ts=60, holders=280)])
    assert crash_only["triggered"] == ["holder_crash"]
    assert crash_only["score"] == 20
    assert crash_only["grade"] == "watch"
    assert crash_only["detectors_fired"] == 1


def test_risk_grade_boundaries():
    assert risk_grade(0) == "clean"
    assert risk_grade(1) == "watch"
    assert risk_grade(24) == "watch"
    assert risk_grade(25) == "elevated"
    assert risk_grade(49) == "elevated"
    assert risk_grade(50) == "severe"
    assert risk_grade(100) == "severe"


def test_risk_grade_rejects_out_of_range_scores():
    for bad in (-1, 101, None, "high", float("nan")):
        with pytest.raises(ValueError):
            risk_grade(bad)
