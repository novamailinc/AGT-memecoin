"""Tests for deployer.py -- synthetic histories only."""
import pytest

from deployer import (
    Launch, Verdict, analyze, blacklist_candidates, clean_creator_history,
    fingerprint, has_known_outcome, is_rug, is_serial_rugger,
    launches_by_deployer, mixed_history, name_pattern, rug_count,
    rug_rate, known_count, serial_rugger_history, should_veto,
    verdict_for, verdict_for_records,
)


def launch(deployer="D", mint="M", name="Token", t=0.0, funded="F", outcome="runner"):
    return Launch(deployer=deployer, mint=mint, name=name,
                  timestamp_hours=t, funded_by=funded, outcome=outcome)


def launches_for(outcomes, deployer="D", **kwargs):
    return [launch(deployer=deployer, mint=f"M{i}", t=float(i * 10),
                   outcome=o, **kwargs) for i, o in enumerate(outcomes)]


# -- record validation -----------------------------------------------------------------
def test_valid_launch_constructs():
    record = launch()
    assert record.outcome == "runner" and not is_rug(record)


def test_rug_outcome_detected():
    assert is_rug(launch(outcome="rug"))


def test_unknown_outcome_is_not_known():
    assert not has_known_outcome(launch(outcome="unknown"))
    assert has_known_outcome(launch(outcome="flat"))


@pytest.mark.parametrize("kwargs", [
    {"deployer": ""}, {"mint": ""}, {"t": -1.0},
    {"outcome": "moon"}, {"outcome": ""},
])
def test_invalid_launch_rejected(kwargs):
    with pytest.raises(ValueError):
        launch(**kwargs)


# -- grouping / counting ------------------------------------------------------------------
def test_grouping_by_deployer_sorted_by_time():
    records = [launch(deployer="A", mint="M2", t=50.0),
               launch(deployer="B", mint="M3", t=1.0),
               launch(deployer="A", mint="M1", t=5.0)]
    grouped = launches_by_deployer(records)
    assert [r.mint for r in grouped["A"]] == ["M1", "M2"]
    assert len(grouped["B"]) == 1


def test_empty_history_groups_to_nothing():
    assert launches_by_deployer([]) == {}


def test_rug_and_known_counts():
    records = launches_for(["rug", "runner", "unknown", "rug"])
    assert rug_count(records) == 2
    assert known_count(records) == 3


def test_rug_rate_over_known_outcomes_only():
    records = launches_for(["rug", "runner", "unknown"])
    assert rug_rate(records) == pytest.approx(0.5)


def test_rug_rate_none_when_nothing_known_fail_closed():
    assert rug_rate(launches_for(["unknown", "unknown"])) is None
    assert rug_rate([]) is None


def test_rug_rate_zero_for_clean_known_history():
    assert rug_rate(launches_for(["runner", "flat"])) == 0.0


# -- serial-rugger bar -----------------------------------------------------------------------
def test_serial_bar_met():
    assert is_serial_rugger(launches_for(["rug", "rug", "flat"]))


def test_serial_bar_needs_three_launches():
    assert not is_serial_rugger(launches_for(["rug", "rug"]))


def test_serial_bar_needs_two_rugs():
    assert not is_serial_rugger(launches_for(["rug", "runner", "flat", "runner"]))


def test_serial_fixture_is_serial():
    assert is_serial_rugger(serial_rugger_history())


# -- verdicts: fail-closed semantics --------------------------------------------------------------
def test_no_history_is_unknown_not_clean():
    assert verdict_for_records([]) is Verdict.UNKNOWN
    assert verdict_for("NobodyHere", mixed_history()) is Verdict.UNKNOWN


def test_all_unknown_outcomes_is_unknown_not_clean():
    assert verdict_for_records(launches_for(["unknown", "unknown", "unknown"])) is Verdict.UNKNOWN


def test_serial_history_verdict():
    assert verdict_for_records(serial_rugger_history()) is Verdict.SERIAL_RUGGER


def test_clean_history_verdict():
    assert verdict_for_records(clean_creator_history()) is Verdict.CLEAN


def test_single_clean_launch_is_not_clean():
    # One good launch is not a track record: WATCH (insufficient evidence).
    assert verdict_for_records(launches_for(["runner"])) is Verdict.WATCH


def test_two_clean_launches_can_be_clean():
    records = [launch(mint="M1", name="Alpha", funded="F1", outcome="runner"),
               launch(mint="M2", name="Beta", t=10.0, funded="F2", outcome="flat")]
    assert verdict_for_records(records) is Verdict.CLEAN


def test_partly_unknown_history_is_not_clean():
    records = [launch(mint="M1", name="Alpha", funded="F1", outcome="runner"),
               launch(mint="M2", name="Beta", t=10.0, funded="F2", outcome="unknown"),
               launch(mint="M3", name="Gamma", t=20.0, funded="F3", outcome="runner")]
    assert verdict_for_records(records) is Verdict.WATCH


def test_one_rug_below_serial_bar_is_watch():
    assert verdict_for_records(launches_for(["runner", "rug"])) is Verdict.WATCH


def test_two_rugs_two_launches_is_watch_not_serial():
    # Brutal rate, but the serial bar also counts launches: fail-closed
    # means WATCH here, never a false SERIAL conviction on thin data.
    assert verdict_for_records(launches_for(["rug", "rug"])) is Verdict.WATCH


def test_verdict_for_rejects_empty_deployer():
    with pytest.raises(ValueError):
        verdict_for("", [])


# -- fingerprints --------------------------------------------------------------------------------------
def test_name_pattern_normalisation():
    assert name_pattern("MoonCat") == "mooncat"
    assert name_pattern("MoonCat 2!!") == "mooncat"
    assert name_pattern("  MOONCAT  ") == "mooncat"


def test_name_pattern_empty_name():
    assert name_pattern("") == "<empty>"
    assert name_pattern("!!!") == "<empty>"


def test_fingerprint_serial_fixture():
    fp = fingerprint(serial_rugger_history())
    assert fp.launch_count == 4 and fp.rug_count == 3
    assert fp.distinct_funding_sources == 1
    assert fp.funding_reuse_ratio == pytest.approx(1.0)
    assert fp.median_interval_hours == pytest.approx(6.0)


def test_fingerprint_clean_fixture():
    fp = fingerprint(clean_creator_history())
    assert fp.distinct_funding_sources == 3
    assert fp.funding_reuse_ratio == 0.0
    assert fp.name_reuse_ratio == 0.0
    assert fp.median_interval_hours == pytest.approx(130.0)


def test_fingerprint_empty_history():
    fp = fingerprint([])
    assert fp.launch_count == 0 and fp.median_interval_hours is None
    assert fp.funding_reuse_ratio == 0.0


def test_fingerprint_single_launch_has_no_interval():
    assert fingerprint(launches_for(["runner"])).median_interval_hours is None


def test_name_reuse_ratio_math():
    records = [launch(mint="M1", name="Cat"), launch(mint="M2", name="Cat 2", t=1.0),
               launch(mint="M3", name="Dog", t=2.0), launch(mint="M4", name="Bird", t=3.0)]
    assert fingerprint(records).name_reuse_ratio == pytest.approx(0.25)


def test_funding_reuse_alone_triggers_watch():
    records = [launch(mint="M1", name="Alpha", funded="SameFund", outcome="runner"),
               launch(mint="M2", name="Beta", t=10.0, funded="SameFund", outcome="runner")]
    assert verdict_for_records(records) is Verdict.WATCH


def test_name_reuse_alone_triggers_watch():
    records = [launch(mint="M1", name="Cat", funded="F1", outcome="runner"),
               launch(mint="M2", name="Cat 2", t=10.0, funded="F2", outcome="runner"),
               launch(mint="M3", name="Cat 3", t=20.0, funded="F3", outcome="flat"),
               launch(mint="M4", name="Cat 4", t=30.0, funded="F4", outcome="runner")]
    assert verdict_for_records(records) is Verdict.WATCH


def test_funding_reuse_ratio_partial():
    records = [launch(mint="M1", funded="X"), launch(mint="M2", funded="X", t=1.0),
               launch(mint="M3", funded="Y", t=2.0), launch(mint="M4", funded="Z", t=3.0)]
    assert fingerprint(records).funding_reuse_ratio == pytest.approx(0.5)


def test_cadence_median_with_even_intervals():
    records = launches_for(["runner", "runner", "runner"])  # t = 0, 10, 20
    assert fingerprint(records).median_interval_hours == pytest.approx(10.0)


def test_fingerprint_is_order_insensitive():
    records = serial_rugger_history()
    assert fingerprint(list(reversed(records))) == fingerprint(records)


# -- whole-history analysis --------------------------------------------------------------------------------
def test_analyze_mixed_history_verdicts():
    reports = analyze(mixed_history())
    assert reports["SerialSam"].verdict is Verdict.SERIAL_RUGGER
    assert reports["HonestAda"].verdict is Verdict.CLEAN
    assert reports["OneRugRita"].verdict is Verdict.WATCH
    assert reports["GhostGary"].verdict is Verdict.UNKNOWN


def test_analyze_report_carries_rug_rate():
    reports = analyze(mixed_history())
    assert reports["SerialSam"].rug_rate == pytest.approx(0.75)
    assert reports["GhostGary"].rug_rate is None


def test_blacklist_candidates_only_serial():
    assert blacklist_candidates(mixed_history()) == ["SerialSam"]


def test_blacklist_empty_when_no_serial():
    assert blacklist_candidates(clean_creator_history()) == []


def test_should_veto_only_serial():
    records = mixed_history()
    assert should_veto("SerialSam", records)
    assert not should_veto("OneRugRita", records)   # WATCH informs, not vetoes
    assert not should_veto("GhostGary", records)    # UNKNOWN informs, not vetoes
    assert not should_veto("HonestAda", records)


def test_unknown_deployer_is_not_vetoed_but_not_clean():
    records = mixed_history()
    assert not should_veto("Stranger", records)
    assert verdict_for("Stranger", records) is Verdict.UNKNOWN
