"""Tests for rigor.py — freeze, embargo, plateau, deflation, look-ahead."""
import math
from types import MappingProxyType

import pytest

from rigor import (PreRegistration, bonferroni_alpha, check_no_leakage,
                   chronological_split, deflated_expectancy, evaluate_gate,
                   is_robust_plateau, lookahead_bias, lookahead_experiment,
                   multiple_testing_penalty, parameter_plateau, plateau_region,
                   registration_fingerprint, walk_forward)


def _reg() -> PreRegistration:
    return PreRegistration(gate="WR >= 35% over 200 closes",
                           thresholds={"min_wr": 0.35, "min_closes": 200},
                           registered_at="2026-10-20")


def test_freeze_raises_on_mutation():
    reg = _reg()
    assert reg.frozen() is False
    reg.set_threshold("min_wr", 0.40)  # allowed before the freeze
    assert reg.thresholds["min_wr"] == 0.40
    reg.freeze()
    assert reg.frozen() is True
    with pytest.raises(RuntimeError):
        reg.set_threshold("min_wr", 0.20)
    with pytest.raises(RuntimeError):
        reg.thresholds = {"min_wr": 0.0}
    with pytest.raises(TypeError):
        reg.thresholds["min_wr"] = 0.0  # mapping proxy after freeze
    assert reg.certify() is True


def test_unfrozen_registration_cannot_certify():
    with pytest.raises(RuntimeError):
        _reg().certify()  # fail-closed


def test_walk_forward_embargo_drops_samples():
    data = list(range(120))
    folds = list(walk_forward(data, n_folds=3, embargo=5))
    assert len(folds) == 3
    for train, test in folds:
        # exactly 5 samples sit between train end and test start, in neither
        assert test[0] - train[-1] - 1 == 5
        assert not set(train) & set(test)
        assert max(train) < min(test)  # time order preserved, no leakage
    no_gap = list(walk_forward(data, n_folds=3, embargo=0))
    assert no_gap[0][1][0] - no_gap[0][0][-1] - 1 == 0
    with pytest.raises(ValueError):
        list(walk_forward(data, n_folds=3, embargo=-1))


def test_parameter_plateau_widths():
    plateau = {10: 0.02, 20: 0.10, 30: 0.14, 40: 0.15, 50: 0.13, 60: 0.09, 70: 0.01}
    # best 0.15 at 40; 80% bar = 0.12 -> {30, 40, 50} qualify -> width 3
    assert parameter_plateau(plateau) == 3
    spike = {10: 0.01, 20: 0.02, 30: 0.15, 40: 0.02, 50: 0.01}
    assert parameter_plateau(spike) == 1  # a spike is fragile
    assert parameter_plateau({10: -0.1, 20: -0.2}) == 0  # no good region
    with pytest.raises(ValueError):
        parameter_plateau({})


def test_deflated_expectancy_haircut():
    assert deflated_expectancy(0.30, 1, 100) == pytest.approx(0.30)  # 1 trial: no penalty
    d10 = deflated_expectancy(0.30, 10, 100)
    d1000 = deflated_expectancy(0.30, 1000, 100)
    assert d10 < 0.30 and d1000 < d10  # more trials -> bigger haircut
    assert deflated_expectancy(0.30, 100, 25) < deflated_expectancy(0.30, 100, 400)
    with pytest.raises(ValueError):
        deflated_expectancy(0.30, 10, 0)


def test_lookahead_inflates_expectancy():
    cheat, honest = lookahead_experiment(seed=42)
    assert cheat - honest > 0.2  # fabricated edge, in expectancy points
    assert honest == pytest.approx(0.0, abs=0.05)  # honest signal finds nothing
    assert lookahead_experiment(seed=3) == lookahead_experiment(seed=3)


# --- Freeze hardening: every mutation route we can construct ---

def _frozen_reg() -> PreRegistration:
    return PreRegistration(gate="WR >= 35% over 200 closes",
                           thresholds={"min_wr": 0.35, "min_closes": 200},
                           registered_at="2026-10-20").freeze()


def test_freeze_blocks_unfreezing_itself():
    reg = _frozen_reg()
    with pytest.raises(RuntimeError):
        reg._frozen = False  # the obvious jailbreak: flip the flag back
    assert reg.frozen() is True
    assert reg.certify() is True
    with pytest.raises(RuntimeError):
        reg.gate = "lowered after the fact"
    assert reg.gate == "WR >= 35% over 200 closes"


def test_freeze_blocks_setattr_and_new_attributes():
    reg = _frozen_reg()
    for name, value in (("gate", "x"), ("registered_at", "1999-01-01"),
                        ("thresholds", {}), ("brand_new_field", 1)):
        with pytest.raises(RuntimeError):
            setattr(reg, name, value)
    assert not hasattr(reg, "brand_new_field")
    assert reg.registered_at == "2026-10-20"


def test_freeze_blocks_deletion_but_unfrozen_deletion_works():
    reg = _frozen_reg()
    for name in ("gate", "thresholds", "registered_at", "_frozen"):
        with pytest.raises(RuntimeError):
            delattr(reg, name)
    assert reg.gate and reg.thresholds and reg.frozen() is True
    loose = _reg()
    del loose.registered_at  # before the freeze, deletion is ordinary Python
    assert not hasattr(loose, "registered_at")


def test_frozen_thresholds_expose_no_mutation_route():
    reg = _frozen_reg()
    assert isinstance(reg.thresholds, MappingProxyType)

    def assign_item():
        reg.thresholds["min_wr"] = 0.0

    def delete_item():
        del reg.thresholds["min_wr"]

    with pytest.raises(TypeError):
        assign_item()
    with pytest.raises(TypeError):
        delete_item()
    for mutator in ("update", "pop", "clear", "setdefault", "popitem"):
        with pytest.raises(AttributeError):
            getattr(reg.thresholds, mutator)
    assert reg.thresholds["min_wr"] == 0.35  # untouched by every attempt


def test_caller_dict_alias_cannot_mutate_the_registration():
    src = {"min_wr": 0.35, "min_closes": 200}
    reg = PreRegistration(gate="g", thresholds=src, registered_at="2026-10-20")
    assert reg.thresholds is not src
    src["min_wr"] = 0.0       # mutate the caller's dict before the freeze
    src["extra"] = 1
    del src["min_closes"]
    assert dict(reg.thresholds) == {"min_wr": 0.35, "min_closes": 200}
    reg.freeze()
    src["min_wr"] = 99.0      # ... and after it: still no effect
    assert reg.thresholds["min_wr"] == 0.35


def test_no_dict_backdoor_on_a_frozen_registration():
    reg = _frozen_reg()
    with pytest.raises(AttributeError):
        reg.__dict__          # slots: there is no instance dict to rewrite
    with pytest.raises(TypeError):
        vars(reg)


def test_freeze_is_idempotent_and_returns_self():
    reg = _reg()
    before = registration_fingerprint(reg)
    assert reg.freeze() is reg
    assert reg.freeze() is reg  # second freeze is a no-op, not an error
    assert reg.frozen() is True
    assert registration_fingerprint(reg) == before  # contents never changed


def test_failed_tampering_leaves_the_fingerprint_unchanged():
    reg = _frozen_reg()
    fp = registration_fingerprint(reg)
    for attempt in (lambda: setattr(reg, "gate", "x"),
                    lambda: reg.set_threshold("min_wr", 0.0),
                    lambda: setattr(reg, "_frozen", False),
                    lambda: delattr(reg, "thresholds")):
        with pytest.raises((RuntimeError, TypeError, AttributeError)):
            attempt()
    assert registration_fingerprint(reg) == fp
    assert dict(reg.thresholds) == {"min_wr": 0.35, "min_closes": 200}


def test_construction_rejects_invalid_registrations():
    bad = [
        {"gate": "", "thresholds": {"a": 1}, "registered_at": "t"},
        {"gate": "g", "thresholds": {"a": 1}, "registered_at": ""},
        {"gate": "g", "thresholds": [("a", 1)], "registered_at": "t"},
        {"gate": "g", "thresholds": {}, "registered_at": "t"},
        {"gate": "g", "thresholds": {"a": math.nan}, "registered_at": "t"},
        {"gate": "g", "thresholds": {"a": math.inf}, "registered_at": "t"},
        {"gate": "g", "thresholds": {"a": True}, "registered_at": "t"},
        {"gate": "g", "thresholds": {"": 1}, "registered_at": "t"},
        {"gate": "g", "thresholds": {"a": "high"}, "registered_at": "t"},
    ]
    for kwargs in bad:
        with pytest.raises(ValueError):
            PreRegistration(**kwargs)


def test_set_threshold_validation_before_the_freeze():
    reg = _reg()
    reg.set_threshold("min_closes", 250)  # ordinary int is fine
    assert reg.thresholds["min_closes"] == 250
    for bad_value in (math.nan, math.inf, True, "0.5"):
        with pytest.raises(ValueError):
            reg.set_threshold("min_wr", bad_value)
    assert reg.thresholds["min_wr"] == 0.35


def test_description_property_tracks_the_gate():
    reg = _reg()
    assert reg.description == reg.gate
    reg.freeze()
    assert reg.description == "WR >= 35% over 200 closes"


# --- Fingerprint and gate evaluation ---

def test_fingerprint_determinism_order_independence_and_format():
    a = PreRegistration(gate="g", thresholds={"x": 1, "y": 2.5}, registered_at="t")
    b = PreRegistration(gate="g", thresholds={"y": 2.5, "x": 1}, registered_at="t")
    fp = registration_fingerprint(a)
    assert fp == registration_fingerprint(b)  # dict order does not matter
    assert len(fp) == 64 and all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_changes_with_any_content_change():
    base = registration_fingerprint(_reg())
    variants = [
        PreRegistration(gate="different gate", thresholds={"min_wr": 0.35, "min_closes": 200},
                        registered_at="2026-10-20"),
        PreRegistration(gate="WR >= 35% over 200 closes", thresholds={"min_wr": 0.36, "min_closes": 200},
                        registered_at="2026-10-20"),
        PreRegistration(gate="WR >= 35% over 200 closes", thresholds={"min_wr": 0.35, "min_closes": 200},
                        registered_at="2026-10-21"),
        PreRegistration(gate="WR >= 35% over 200 closes",
                        thresholds={"min_wr": 0.35, "min_closes": 200, "extra": 1},
                        registered_at="2026-10-20"),
    ]
    for variant in variants:
        assert registration_fingerprint(variant) != base


def test_evaluate_gate_pass_fail_and_inclusive_boundary():
    reg = _frozen_reg()
    assert evaluate_gate(reg, {"min_wr": 0.35, "min_closes": 200}) is True  # at = pass
    assert evaluate_gate(reg, {"min_wr": 0.50, "min_closes": 900, "unused": 1}) is True
    assert evaluate_gate(reg, {"min_wr": 0.349, "min_closes": 200}) is False
    assert evaluate_gate(reg, {"min_wr": 0.90, "min_closes": 199}) is False


def test_evaluate_gate_fail_closed():
    reg = _frozen_reg()
    with pytest.raises(RuntimeError):
        evaluate_gate(_reg(), {"min_wr": 0.9, "min_closes": 900})  # unfrozen
    with pytest.raises(ValueError):
        evaluate_gate(reg, {"min_wr": 0.9})  # a missing metric is not a pass
    with pytest.raises(ValueError):
        evaluate_gate(reg, {"min_wr": math.nan, "min_closes": 200})
    with pytest.raises(ValueError):
        evaluate_gate(reg, [("min_wr", 0.9)])


# --- Walk-forward, single split, leakage ---

def test_walk_forward_structure_on_100_bars():
    folds = list(walk_forward(list(range(100)), n_folds=3, embargo=0))
    assert [(len(tr), len(te)) for tr, te in folds] == [(25, 25), (50, 25), (75, 25)]
    assert folds[0][0][0] == 0 and folds[-1][1][-1] == 99
    tested = [x for _, te in folds for x in te]
    assert tested == list(range(25, 100))  # test segments tile the last 75 bars


def test_walk_forward_trailing_remainder_is_never_used():
    folds = list(walk_forward(list(range(10)), n_folds=2, embargo=0))
    used = {x for tr, te in folds for x in tr + te}
    assert used == set(range(9))  # segment is 3; bar 9 falls off the end


def test_walk_forward_embargo_shrinks_tests_and_widens_the_gap():
    folds = list(walk_forward(list(range(120)), n_folds=3, embargo=10))
    assert [(len(tr), len(te)) for tr, te in folds] == [(30, 20), (60, 20), (90, 20)]
    for train, test in folds:
        assert test[0] - train[-1] - 1 == 10


def test_walk_forward_validation_extended():
    with pytest.raises(ValueError):
        list(walk_forward(list(range(50)), n_folds=0))
    with pytest.raises(ValueError):
        list(walk_forward(list(range(50)), n_folds=-2))
    with pytest.raises(ValueError):
        list(walk_forward(list(range(20)), n_folds=3, embargo=5))  # segment 5 <= embargo
    with pytest.raises(ValueError):
        list(walk_forward([], n_folds=3))


def test_chronological_split_exact_contents_with_embargo():
    train, test = chronological_split(list(range(100)), train_fraction=0.7, embargo=5)
    assert train == list(range(70))
    assert test == list(range(75, 100))
    assert not set(train) & set(test)
    assert [x for x in range(100) if x not in train and x not in test] == list(range(70, 75))


def test_chronological_split_fail_closed():
    data = list(range(100))
    for fraction in (0.0, 1.0, 1.5, -0.2):
        with pytest.raises(ValueError):
            chronological_split(data, train_fraction=fraction)
    with pytest.raises(ValueError):
        chronological_split(data, train_fraction=0.7, embargo=-1)
    with pytest.raises(ValueError):
        chronological_split([0, 1, 2], train_fraction=0.5, embargo=5)  # empty test
    with pytest.raises(ValueError):
        chronological_split([0, 1], train_fraction=0.4)  # empty train
    with pytest.raises(ValueError):
        chronological_split([], train_fraction=0.5)


def test_check_no_leakage_gap_values_match_walk_forward():
    data = list(range(120))
    for train, test in walk_forward(data, n_folds=3, embargo=5):
        assert check_no_leakage(train, test) == 5
    assert check_no_leakage([0, 1, 2], [3, 4]) == 0
    assert check_no_leakage([0, 1, 2], [5, 6, 9]) == 2


def test_check_no_leakage_rejects_overlap_disorder_and_empty():
    for train, test in (([0, 1, 2], [2, 3]),      # overlap
                        ([5, 6], [0, 1]),         # test before train
                        ([0, 3], [2, 4]),         # interleaved
                        ([], [1, 2]), ([0, 1], [])):
        with pytest.raises(ValueError):
            check_no_leakage(train, test)


# --- Plateau, extended ---

PLATEAU = {10: 0.02, 20: 0.10, 30: 0.14, 40: 0.15, 50: 0.13, 60: 0.09, 70: 0.01}
SPIKE = {10: 0.01, 20: 0.02, 30: 0.15, 40: 0.02, 50: 0.01}


def test_plateau_region_returns_the_actual_parameters():
    assert plateau_region(PLATEAU) == [30, 40, 50]
    assert plateau_region(SPIKE) == [30]
    assert plateau_region({10: -0.1, 20: -0.2}) == []
    assert plateau_region({10: 0.0}) == []  # a zero best is not a good region


def test_plateau_at_the_edges_of_the_sweep():
    assert plateau_region({10: 0.15, 20: 0.13, 30: 0.02}) == [10, 20]  # best first
    assert plateau_region({10: 0.02, 20: 0.13, 30: 0.15}) == [20, 30]  # best last


def test_plateau_tie_and_the_exact_80_percent_boundary():
    # Tied best: expansion starts at the first (lowest) argmax and crosses the tie.
    assert plateau_region({10: 0.15, 20: 0.15, 30: 0.10}) == [10, 20]
    # Threshold inclusivity, with a best of 1.0 so 0.8 * best is exactly
    # float(0.8): a neighbour AT 0.8 qualifies, one at 0.7999 does not.
    # (With best=0.10 the float threshold is 0.08000000000000002 > 0.08,
    # so a literal-0.08 neighbour would fail — decimal intuition misleads.)
    assert plateau_region({10: 0.7999, 20: 1.0, 30: 0.8}) == [20, 30]


def test_is_robust_plateau_verdicts():
    assert is_robust_plateau(PLATEAU) is True            # width 3 >= 2
    assert is_robust_plateau(PLATEAU, min_width=3) is True
    assert is_robust_plateau(PLATEAU, min_width=4) is False
    assert is_robust_plateau(SPIKE) is False             # width 1 is fragile
    assert is_robust_plateau(SPIKE, min_width=1) is True
    assert is_robust_plateau({10: -0.1}) is False
    with pytest.raises(ValueError):
        is_robust_plateau(PLATEAU, min_width=0)


def test_plateau_rejects_non_finite_expectancies():
    for bad in ({10: math.nan}, {10: 0.1, 20: math.inf}, {10: -math.inf}):
        with pytest.raises(ValueError):
            parameter_plateau(bad)
        with pytest.raises(ValueError):
            plateau_region(bad)


# --- Deflation, extended ---

def test_multiple_testing_penalty_exact_values():
    assert multiple_testing_penalty(1, 100) == 0.0
    assert multiple_testing_penalty(0, 100) == 0.0  # clamped to one trial
    assert multiple_testing_penalty(10, 100) == pytest.approx(
        math.sqrt(2 * math.log(10)) / 10)
    assert multiple_testing_penalty(100, 100) == pytest.approx(0.3035, abs=0.001)
    # The penalty is exactly what deflated_expectancy subtracts.
    for trials, samples in ((1, 50), (25, 200), (500, 30)):
        assert deflated_expectancy(0.42, trials, samples) == pytest.approx(
            0.42 - multiple_testing_penalty(trials, samples))


def test_bonferroni_alpha_exact_and_invalid():
    assert bonferroni_alpha(0.05, 1) == pytest.approx(0.05)
    assert bonferroni_alpha(0.05, 10) == pytest.approx(0.005)
    assert bonferroni_alpha(0.05, 1000) == pytest.approx(0.00005)
    assert bonferroni_alpha(0.10, 4) == pytest.approx(0.025)
    for alpha, trials in ((0.0, 10), (1.0, 10), (0.05, 0), (0.05, -3)):
        with pytest.raises(ValueError):
            bonferroni_alpha(alpha, trials)


def test_deflated_expectancy_rejects_bad_inputs():
    for bad_observed in (math.nan, math.inf, None):
        with pytest.raises(ValueError):
            deflated_expectancy(bad_observed, 10, 100)
    with pytest.raises(ValueError):
        deflated_expectancy(0.30, -1, 100)
    with pytest.raises(ValueError):
        deflated_expectancy(0.30, 10, -5)


# --- Look-ahead injector, extended ---

def test_lookahead_bias_scales_linearly_with_sigma():
    b25 = lookahead_bias(seed=42, sigma=0.25)
    b50 = lookahead_bias(seed=42, sigma=0.5)
    b100 = lookahead_bias(seed=42, sigma=1.0)
    assert b100 == pytest.approx(2 * b50, rel=1e-6)   # same draws, doubled scale
    assert b50 == pytest.approx(2 * b25, rel=1e-6)
    assert b50 == pytest.approx(0.5 * math.sqrt(2 / math.pi), abs=0.02)  # theory


def test_lookahead_honest_signal_finds_nothing_for_other_seeds():
    for seed in (1, 7, 123):
        cheat, honest = lookahead_experiment(seed=seed, n_bars=2000)
        assert cheat > 0.25
        assert honest == pytest.approx(0.0, abs=0.06)
        assert cheat - honest > 0.2


def test_lookahead_rejects_bad_sigma_and_bar_counts():
    for bad_sigma in (0.0, -0.5, math.nan, math.inf):
        with pytest.raises(ValueError):
            lookahead_experiment(seed=42, sigma=bad_sigma)
    with pytest.raises(ValueError):
        lookahead_experiment(seed=42, n_bars=99)
    cheat, honest = lookahead_experiment(seed=42, n_bars=100)  # boundary: allowed
    assert cheat > 0  # the cheater only ever books positive next bars, by construction
