"""Tests for veto.py -- one test per rule, boundaries, score math."""
import pytest

from veto import (TokenReport, evaluate, evaluate_many, explain,
                  meets_score_floor, summarize)

WALLET = "SyntheticWallet1111111111111111111111111111"


def clean(**overrides):
    base = dict(mint_authority=None, freeze_authority=None,
                extensions=[], transfer_fee_bps=0)
    base.update(overrides)
    return TokenReport(**base)


def test_clean_token_passes_score_100():
    v = evaluate(clean())
    assert v.passed is True and v.status == "PASS"
    assert v.fails == [] and v.flags == []
    assert v.score == 100


def test_revoked_strings_count_as_clean():
    v = evaluate(clean(mint_authority="revoked", freeze_authority="revoked"))
    assert v.passed is True and v.score == 100


def test_benign_flag_passes_at_95():
    # Pinned rule: a flag never blocks, it only costs 5 points.
    v = evaluate(clean(extensions=["mintCloseAuthority"]))
    assert v.passed is True
    assert v.flags == ["mint_close_authority"]
    assert v.score == 95


@pytest.mark.parametrize("overrides,fail", [
    (dict(mint_authority=WALLET), "mint_authority_held"),
    (dict(freeze_authority=WALLET), "freeze_authority_held"),
    (dict(extensions=["permanentDelegate"]), "permanent_delegate"),
    (dict(extensions=["transferHook"]), "transfer_hook"),
    (dict(extensions=["nonTransferable"]), "non_transferable"),
    (dict(default_account_state="frozen"), "default_account_state_frozen"),
    (dict(extensions=["defaultAccountState:frozen"]),
     "default_account_state_frozen"),
    (dict(extensions=[{"type": "transferHook"}]), "transfer_hook"),
    (dict(transfer_fee_bps=5000), "transfer_fee_excessive"),
])
def test_each_hard_fail_individually_blocks(overrides, fail):
    v = evaluate(clean(**overrides))
    assert v.passed is False and v.status == "FAIL"
    assert fail in v.fails
    assert v.score == 75  # exactly one fail, no flags: 100 - 25


def test_default_account_state_initialized_is_clean():
    v = evaluate(clean(default_account_state="initialized",
                       extensions=["defaultAccountState:initialized"]))
    assert v.passed is True and v.score == 100


@pytest.mark.parametrize("fee,passed,score,fails,flags", [
    (0, True, 100, [], []),
    (1, True, 95, [], ["transfer_fee_present"]),
    (1000, True, 95, [], ["transfer_fee_present"]),   # 10% exactly: flag
    (1001, False, 75, ["transfer_fee_excessive"], []),  # 10.01%: hard fail
])
def test_transfer_fee_boundary(fee, passed, score, fails, flags):
    v = evaluate(clean(transfer_fee_bps=fee))
    assert v.passed is passed and v.score == score
    assert v.fails == fails and v.flags == flags


def test_flag_only_extensions():
    v = evaluate(clean(extensions=["confidentialTransferMint",
                                   "mintCloseAuthority"]))
    assert v.passed is True
    assert sorted(v.flags) == ["confidential_transfer_mint",
                               "mint_close_authority"]
    assert v.score == 90  # two flags: 100 - 2*5


def test_unknown_report_fails_closed():
    for report in (None,
                   clean(extensions=None),
                   clean(transfer_fee_bps=None),
                   clean(mint_authority="unknown"),
                   {"mint_authority": None}):  # dict: rest never fetched
        v = evaluate(report)
        assert v.passed is False and v.status == "UNKNOWN"
        assert v.score == 0 and v.fails  # reasons are always named


def test_score_arithmetic_and_floor():
    # 2 fails + 1 flag = 100 - 50 - 5 = 45.
    v = evaluate(clean(mint_authority=WALLET, freeze_authority=WALLET,
                       transfer_fee_bps=500))
    assert v.score == 45 and v.passed is False
    # Many fails floor at 0, never negative.
    v = evaluate(clean(mint_authority=WALLET, freeze_authority=WALLET,
                       extensions=["permanentDelegate", "transferHook",
                                   "nonTransferable"],
                       transfer_fee_bps=9999))
    assert v.score == 0


def test_score_never_outranks_a_fail():
    # A flagged-but-clean token (95) must not "beat" the rule that a
    # single-fail token (75) is blocked: passed is decided by fails only.
    flagged = evaluate(clean(transfer_fee_bps=100))
    failed = evaluate(clean(mint_authority=WALLET))
    assert flagged.passed is True and failed.passed is False
    assert failed.score < flagged.score  # score describes, never decides


def test_source_is_carried_through():
    v = evaluate(clean(source="synthetic-rugcheck-style"))
    assert v.source == "synthetic-rugcheck-style"
    v = evaluate(None)
    assert v.source == "unknown"


# -- authority value spellings ---------------------------------------------------


def test_revoked_synonyms_all_count_as_clean():
    # Every spelling of "authority is gone" must pass: None, the empty
    # string, and case/whitespace variants of the revoked synonyms.
    for value in (None, "revoked", "none", "burned", "", "REVOKED",
                  " Revoked "):
        v = evaluate(clean(mint_authority=value, freeze_authority=value))
        assert v.passed is True and v.score == 100, value


def test_unknown_authority_spellings_fail_closed():
    for value in ("UNKNOWN", "Unknown", " unknown "):
        v = evaluate(clean(mint_authority=value))
        assert v.status == "UNKNOWN" and v.passed is False
        assert v.fails == ["mint_authority_unknown"]
    v = evaluate(clean(freeze_authority="unknown"))
    assert v.status == "UNKNOWN"
    assert v.fails == ["freeze_authority_unknown"]


def test_default_account_state_unknown_fails_closed_and_keeps_source():
    v = evaluate(clean(default_account_state="unknown", source="src-x"))
    assert v.status == "UNKNOWN" and v.passed is False and v.score == 0
    assert v.fails == ["default_account_state_unknown"]
    assert v.source == "src-x"


def test_creator_field_never_gates():
    # The creator is informational only: a report naming its creator
    # wallet is not an authority finding.
    v = evaluate(clean(creator=WALLET))
    assert v.passed is True and v.score == 100


# -- extension entry shapes and permutations ----------------------------------------


def test_extension_name_spelling_variants_all_match():
    for entry in ("TransferHook", "transfer_hook", "transfer-hook",
                  "TRANSFERHOOK"):
        v = evaluate(clean(extensions=[entry]))
        assert v.passed is False and v.fails == ["transfer_hook"], entry


def test_duplicate_extensions_counted_once():
    v = evaluate(clean(extensions=["transferHook", "transferHook",
                                   {"type": "transferHook"}]))
    assert v.fails == ["transfer_hook"]  # a set, not three findings
    assert v.score == 75


def test_unrecognized_extensions_are_ignored():
    v = evaluate(clean(extensions=["metadataPointer", "transferFee"]))
    assert v.passed is True and v.score == 100
    assert v.fails == [] and v.flags == []


def test_dict_extension_via_name_key():
    v = evaluate(clean(extensions=[{"name": "confidentialTransferMint"}]))
    assert v.passed is True
    assert v.flags == ["confidential_transfer_mint"] and v.score == 95


def test_dict_form_default_account_state():
    v = evaluate(clean(extensions=[{"type": "defaultAccountState",
                                    "state": "frozen"}]))
    assert v.passed is False
    assert v.fails == ["default_account_state_frozen"]
    v = evaluate(clean(extensions=[{"type": "defaultAccountState",
                                    "state": "initialized"}]))
    assert v.passed is True and v.score == 100


def test_report_default_state_takes_precedence_over_extension():
    # The report's own field wins over the extension entry, both ways.
    v = evaluate(clean(default_account_state="initialized",
                       extensions=["defaultAccountState:frozen"]))
    assert v.passed is True
    v = evaluate(clean(default_account_state="frozen",
                       extensions=["defaultAccountState:initialized"]))
    assert v.passed is False
    assert v.fails == ["default_account_state_frozen"]


def test_everything_at_once_scores_zero_with_all_findings():
    v = evaluate(clean(
        mint_authority=WALLET, freeze_authority=WALLET,
        extensions=["permanentDelegate", "transferHook", "nonTransferable",
                    "mintCloseAuthority", "confidentialTransferMint"],
        default_account_state="frozen", transfer_fee_bps=9999))
    assert v.passed is False and v.status == "FAIL"
    assert v.fails == ["mint_authority_held", "freeze_authority_held",
                       "permanent_delegate", "transfer_hook",
                       "non_transferable", "default_account_state_frozen",
                       "transfer_fee_excessive"]
    assert v.flags == ["mint_close_authority", "confidential_transfer_mint"]
    assert v.score == 0  # 100 - 7*25 - 2*5, floored


def test_flag_and_fail_combine_in_score():
    # Boundary fee (flag) plus one hard fail: 100 - 25 - 5 = 70.
    v = evaluate(clean(mint_authority=WALLET, transfer_fee_bps=1000))
    assert v.passed is False
    assert v.fails == ["mint_authority_held"]
    assert v.flags == ["transfer_fee_present"]
    assert v.score == 70


# -- malformed inputs fail closed ------------------------------------------------------


def test_invalid_fee_shapes_are_unknown():
    # Negative, fractional, string, and bool fees are not measurements;
    # each is missing data, which fails closed as UNKNOWN (never raises).
    for bad_fee in (-1, 100.5, "100", True):
        v = evaluate(clean(transfer_fee_bps=bad_fee))
        assert v.status == "UNKNOWN" and v.passed is False, bad_fee
        assert v.fails == ["transfer_fee_invalid"] and v.score == 0


def test_wrong_type_reports_are_unknown():
    for bad in (42, "nope", ["transferHook"]):
        v = evaluate(bad)
        assert v.status == "UNKNOWN" and v.passed is False
        assert v.fails == ["unknown_report_type"] and v.score == 0


def test_from_dict_defaults_and_errors():
    report = TokenReport.from_dict({})
    assert report.source == "synthetic-dict"
    v = evaluate(report)
    assert v.status == "UNKNOWN"
    # Every never-fetched piece is named, in field order.
    assert v.fails == ["mint_authority_unknown", "freeze_authority_unknown",
                       "extensions_unknown", "transfer_fee_unknown"]
    with pytest.raises(ValueError):
        TokenReport.from_dict("not-a-dict")


def test_evaluate_accepts_a_clean_dict_directly():
    v = evaluate({"mint_authority": None, "freeze_authority": None,
                  "extensions": [], "transfer_fee_bps": 0,
                  "source": "dict-src"})
    assert v.passed is True and v.status == "PASS" and v.score == 100
    assert v.source == "dict-src"


# -- score-floor semantics + batch helpers (new) -----------------------------------------


def test_meets_score_floor_semantics():
    clean_v = evaluate(clean())
    assert meets_score_floor(clean_v, 100) is True
    assert meets_score_floor(clean_v, 0) is True
    flagged = evaluate(clean(transfer_fee_bps=100))  # score 95, passes
    assert meets_score_floor(flagged, 95) is True
    assert meets_score_floor(flagged, 96) is False
    # A failed verdict meets no floor, not even 0: the floor only ever
    # tightens a PASS, it can never loosen a FAIL into a pass.
    failed = evaluate(clean(mint_authority=WALLET))
    assert meets_score_floor(failed, 0) is False
    assert meets_score_floor(evaluate(None), 0) is False
    for bad_floor in (-1, 101, "50", True):
        with pytest.raises(ValueError):
            meets_score_floor(clean_v, bad_floor)


def test_evaluate_many_and_summarize():
    verdicts = evaluate_many([clean(), clean(mint_authority=WALLET), None,
                              clean(transfer_fee_bps=100)])
    assert [v.status for v in verdicts] == ["PASS", "FAIL", "UNKNOWN",
                                            "PASS"]
    assert summarize(verdicts) == {"total": 4, "passed": 2, "pass": 2,
                                   "fail": 1, "unknown": 1}
    assert summarize([]) == {"total": 0, "passed": 0, "pass": 0,
                             "fail": 0, "unknown": 0}
    with pytest.raises(ValueError):
        evaluate_many(None)


def test_explain_renders_the_verdict():
    text = explain(evaluate(clean(mint_authority=WALLET)))
    assert text.splitlines()[0] == "FAIL score=75 source=synthetic-fixture"
    assert "fail: mint_authority_held" in text
    clean_text = explain(evaluate(clean()))
    assert clean_text.splitlines()[0].startswith("PASS score=100")
    assert "no fails or flags" in clean_text
