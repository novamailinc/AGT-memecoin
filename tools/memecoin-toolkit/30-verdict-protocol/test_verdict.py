import pytest
from verdict import *

def lane(**over):
    base = dict(name="lane", closes=100, expectancy=0.0, win_rate=0.30, weeks_of_data=4.0)
    base.update(over)
    return LaneRecord(**base)

def test_required_n_pinned_534():
    assert required_n() == 534

def test_required_n_insane_range():
    assert 500 <= required_n() <= 700

def test_required_n_grows_as_gap_shrinks():
    assert required_n(p1=0.32, p0=0.30) > required_n(p1=0.40, p0=0.30)

def test_required_n_invalid():
    with pytest.raises(ValueError):
        required_n(p1=0.30, p0=0.30)
    with pytest.raises(ValueError):
        required_n(p1=0.25, p0=0.30)
    with pytest.raises(ValueError):
        required_n(p1=1.5, p0=0.30)
    with pytest.raises(ValueError):
        required_n(power=0)

def test_constants():
    assert REQUIRED_N_FULL == 534 and MIN_N_ANY_VERDICT == 20 and MIN_N_KILL == 50
    assert len(GATE_NAMES) == 4

def test_lane_validation_name():
    with pytest.raises(ValueError):
        lane(name="")

def test_lane_validation_closes():
    with pytest.raises(ValueError):
        lane(closes=-1)
    with pytest.raises(ValueError):
        lane(closes=1.5)

def test_lane_validation_win_rate():
    with pytest.raises(ValueError):
        lane(win_rate=1.5)
    with pytest.raises(ValueError):
        lane(win_rate=-0.1)

def test_lane_validation_weeks_and_flag():
    with pytest.raises(ValueError):
        lane(weeks_of_data=-1)
    with pytest.raises(ValueError):
        lane(structural_dead="yes")

def test_lane_validation_expectancy_type():
    with pytest.raises(ValueError):
        lane(expectancy="good")

def test_gates_all_green():
    assert Gates(True, True, True, True).all_green() is True
    assert Gates(True, True, True, False).all_green() is False
    assert Gates().all_green() is False

def test_gates_red_list():
    assert Gates(True, False, True, False).red_gates() == ["proven_entry_strategy", "hardened_execution_safety"]

def test_gates_dict():
    d = Gates(True, False, False, True).as_dict()
    assert d["sustained_positive_expectancy"] is True and d["top_picks_winning"] is False

def test_structural_kill_fixture():
    l, g = degen_v2_fixture()
    v = decide(l, g)
    assert v.decision == "KILL"
    assert any("structural" in r for r in v.reasons)

def test_structural_kill_beats_small_sample():
    v = decide(lane(closes=3, structural_dead=True), Gates())
    assert v.decision == "KILL"

def test_structural_kill_beats_green_gates():
    v = decide(lane(closes=900, expectancy=0.5, structural_dead=True), Gates(True, True, True, True))
    assert v.decision == "KILL"

def test_small_sample_fixture_measuring():
    l, g = small_sample_fixture()
    assert l.closes == 18
    assert decide(l, g).decision == "KEEP-MEASURING"

def test_sample_boundary_19_vs_20():
    assert decide(lane(closes=19, expectancy=0.1), Gates()).decision == "KEEP-MEASURING"
    assert decide(lane(closes=20, expectancy=0.1), Gates()).decision == "KEEP-FIXING"

def test_zero_closes_measuring():
    assert decide(lane(closes=0, expectancy=0.0), Gates()).decision == "KEEP-MEASURING"

def test_halted_394_fixture_kills():
    l, g = halted_394_fixture()
    assert l.closes == 394
    v = decide(l, g)
    assert v.decision == "KILL"
    assert any("expectancy" in r for r in v.reasons)

def test_deep_negative_at_50_kills():
    assert decide(lane(closes=50, expectancy=-0.25), Gates()).decision == "KILL"

def test_deep_negative_below_kill_sample_measures():
    # 30 closes: above any-verdict floor but below kill sample -> measuring, not kill
    assert decide(lane(closes=30, expectancy=-0.50), Gates()).decision == "KEEP-MEASURING"

def test_mild_negative_at_100_fixing():
    assert decide(lane(closes=100, expectancy=-0.05), Gates()).decision == "KEEP-FIXING"

def test_mild_negative_at_600_fixing():
    assert decide(lane(closes=600, expectancy=-0.05), Gates(True, True, True, True)).decision == "KEEP-FIXING"

def test_all_green_fixture_fund_eligible():
    l, g = all_green_fixture()
    v = decide(l, g)
    assert v.decision == "FUND-ELIGIBLE"
    assert any("gates green" in r for r in v.reasons)

def test_fund_needs_all_gates():
    for missing in range(4):
        flags = [True, True, True, True]
        flags[missing] = False
        v = decide(lane(closes=600, expectancy=0.2, win_rate=0.4), Gates(*flags))
        assert v.decision != "FUND-ELIGIBLE"

def test_fund_needs_full_sample():
    v = decide(lane(closes=533, expectancy=0.2, win_rate=0.4), Gates(True, True, True, True))
    assert v.decision == "KEEP-MEASURING"
    v2 = decide(lane(closes=534, expectancy=0.2, win_rate=0.4), Gates(True, True, True, True))
    assert v2.decision == "FUND-ELIGIBLE"

def test_fund_needs_positive_expectancy():
    v = decide(lane(closes=600, expectancy=0.0, win_rate=0.35), Gates(True, True, True, True))
    assert v.decision != "FUND-ELIGIBLE"

def test_positive_lane_red_gates_fixing():
    v = decide(lane(closes=300, expectancy=0.1), Gates(True, False, False, False))
    assert v.decision == "KEEP-FIXING"
    assert any("red gates" in r for r in v.reasons)

def test_zero_expectancy_red_gates_fixing():
    assert decide(lane(closes=100, expectancy=0.0), Gates()).decision == "KEEP-FIXING"

def test_verdict_render_contains_reasons():
    v = decide(*degen_v2_fixture())
    text = v.render()
    assert "degen_v2: KILL" in text
    assert text.count("- ") >= 2
    assert str(v) == text

def test_verdict_fields():
    v = decide(*all_green_fixture())
    assert v.decision in VERDICTS and v.lane == "proven_lane" and len(v.reasons) >= 2

def test_decide_wrong_types_raise():
    with pytest.raises(ValueError):
        decide({"name": "x"}, Gates())
    with pytest.raises(ValueError):
        decide(lane(), {"gates": 1})

def test_sample_sufficient_levels():
    assert sample_sufficient(534, "full") is True
    assert sample_sufficient(533, "full") is False
    assert sample_sufficient(20, "any") is True
    assert sample_sufficient(50, "kill") is True
    assert sample_sufficient(49, "kill") is False

def test_sample_sufficient_bad_level():
    with pytest.raises(ValueError):
        sample_sufficient(10, "huge")
    with pytest.raises(ValueError):
        sample_sufficient(-1)

def test_every_fixture_decision_in_verdicts():
    for make in (degen_v2_fixture, small_sample_fixture, halted_394_fixture, all_green_fixture):
        assert decide(*make()).decision in VERDICTS

def test_negative_expectancy_never_fund_eligible():
    v = decide(lane(closes=1000, expectancy=-0.01, win_rate=0.29), Gates(True, True, True, True))
    assert v.decision != "FUND-ELIGIBLE"

def test_weeks_do_not_substitute_for_closes():
    # long calendar time, tiny sample -> still measuring
    assert decide(lane(closes=5, weeks_of_data=52.0, expectancy=0.3), Gates(True, True, True, True)).decision == "KEEP-MEASURING"
