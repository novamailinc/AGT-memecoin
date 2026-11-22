import pytest
from refinery import *

def good(**over):
    r = {"schema": 1, "chain": "solana", "source": "df", "mint": "MintABC123", "ts": 1700000000, "score": 80}
    r.update(over)
    return r

def test_valid_record():
    ok, reasons = validate_record(good())
    assert ok is True and reasons == []

def test_valid_each_chain():
    for chain, src in [("base", "br"), ("ethereum", "er"), ("hyperliquid", "hl")]:
        ok, _ = validate_record(good(chain=chain, source=src))
        assert ok is True

def test_not_a_dict_rejected():
    ok, reasons = validate_record("nope")
    assert ok is False and "record_not_a_dict" in reasons

def test_missing_field_rejected():
    r = good(); del r["mint"]
    ok, reasons = validate_record(r)
    assert ok is False and "missing:mint" in reasons

def test_missing_all_fields():
    ok, reasons = validate_record({})
    assert ok is False and len(reasons) == len(REQUIRED_FIELDS)

def test_bad_schema_version():
    ok, reasons = validate_record(good(schema=2))
    assert ok is False and "bad_schema_version" in reasons

def test_unknown_chain_rejected():
    ok, reasons = validate_record(good(chain="mars"))
    assert ok is False and "unknown_chain" in reasons

def test_unknown_source_rejected():
    ok, reasons = validate_record(good(source="zz"))
    assert ok is False and "unknown_source" in reasons

def test_bad_mint_rejected():
    for bad in ("", "ab", 123, None):
        ok, reasons = validate_record(good(mint=bad))
        assert ok is False and "bad_mint" in reasons

def test_bad_ts_rejected():
    for bad in (0, -5, "now", True):
        ok, reasons = validate_record(good(ts=bad))
        assert ok is False and "bad_ts" in reasons

def test_bad_score_rejected():
    for bad in (-1, 101, "high", True):
        ok, reasons = validate_record(good(score=bad))
        assert ok is False and "bad_score" in reasons

def test_score_boundaries_ok():
    assert validate_record(good(score=0))[0] is True
    assert validate_record(good(score=100))[0] is True

def test_source_chain_mismatch():
    ok, reasons = validate_record(good(chain="solana", source="br"))
    assert ok is False and "source_chain_mismatch" in reasons

def test_source_name_roundtrip():
    for code, name in SOURCE_CODES.items():
        assert source_name(code) == name
        assert code_for_source(name) == code

def test_source_name_unknown_raises():
    with pytest.raises(ValueError):
        source_name("zz")
    with pytest.raises(ValueError):
        code_for_source("nope")

def test_win_definition_boundary():
    assert TapOutcome("base", "m1", True, 2.0).is_win is True
    assert TapOutcome("base", "m1", True, 1.99).is_win is False
    assert TapOutcome("base", "m1", True, 1.99).is_loss is True

def test_open_outcome_not_win():
    o = TapOutcome("base", "m1", False, None)
    assert o.is_win is False and o.is_loss is False

def test_outcome_validation():
    with pytest.raises(ValueError):
        TapOutcome("mars", "m1", True, 2.0)
    with pytest.raises(ValueError):
        TapOutcome("base", "m1", True, None)
    with pytest.raises(ValueError):
        TapOutcome("base", "", True, 2.0)
    with pytest.raises(ValueError):
        TapOutcome("base", "m1", True, -1)

def test_journal_win_rate():
    j = TapJournal()
    j.add(TapOutcome("base", "a", True, 3.0))
    j.add(TapOutcome("base", "b", True, 1.0))
    j.add(TapOutcome("base", "c", True, 2.0))
    j.add(TapOutcome("base", "d", False, None))
    assert j.tap_count("base") == 3
    assert j.win_rate("base") == pytest.approx(2/3)

def test_journal_empty_win_rate_none():
    assert TapJournal().win_rate("base") is None

def test_journal_unknown_chain_raises():
    with pytest.raises(ValueError):
        TapJournal().win_rate("mars")

def test_journal_add_wrong_type():
    with pytest.raises(ValueError):
        TapJournal().add({"chain": "base"})

def test_switches_start_unpaused():
    s = FeedSwitches()
    assert all(s.is_paused(c) is False for c in CHAINS)

def test_pause_and_human_resume():
    s = FeedSwitches()
    assert s.pause("base") is True and s.is_paused("base") is True
    assert s.resume("base", by="human") is True and s.is_paused("base") is False

def test_solana_never_paused():
    s = FeedSwitches()
    assert s.pause("solana") is False
    assert s.is_paused("solana") is False
    assert any("SOLANA" in f for f in s.flags)

def test_auto_resume_refused():
    s = FeedSwitches()
    s.pause("base")
    assert s.resume("base", by="auto") is False
    assert s.is_paused("base") is True
    assert any("AUTO_RESUME" in f for f in s.flags)

def test_switch_unknown_chain_raises():
    s = FeedSwitches()
    with pytest.raises(ValueError):
        s.pause("mars")
    with pytest.raises(ValueError):
        s.is_paused("mars")

def _journal(chain, wins, losses):
    j = TapJournal()
    for i in range(wins):
        j.add(TapOutcome(chain, f"w{i}", True, 2.5))
    for i in range(losses):
        j.add(TapOutcome(chain, f"l{i}", True, 1.0))
    return j

def test_auto_pause_at_threshold():
    s = FeedSwitches()
    j = _journal("base", wins=1, losses=10)  # 11 taps, WR ~9%
    assert s.evaluate_auto_pause(j) == ["base"]
    assert s.is_paused("base") is True

def test_auto_pause_needs_10_taps():
    s = FeedSwitches()
    j = _journal("base", wins=0, losses=9)  # only 9 taps
    assert s.evaluate_auto_pause(j) == []

def test_auto_pause_needs_low_wr():
    s = FeedSwitches()
    j = _journal("base", wins=3, losses=7)  # WR 30%
    assert s.evaluate_auto_pause(j) == []

def test_auto_pause_boundary_wr_20_not_paused():
    s = FeedSwitches()
    j = _journal("ethereum", wins=2, losses=8)  # exactly 20% -> NOT <20%
    assert s.evaluate_auto_pause(j) == []

def test_auto_pause_never_pauses_solana():
    s = FeedSwitches()
    j = _journal("solana", wins=0, losses=15)
    assert s.evaluate_auto_pause(j) == []
    assert s.is_paused("solana") is False

def test_auto_pause_idempotent():
    s = FeedSwitches()
    j = _journal("base", wins=0, losses=12)
    s.evaluate_auto_pause(j)
    assert s.evaluate_auto_pause(j) == []

def test_publish_allowed_happy():
    s = FeedSwitches()
    assert s.publish_allowed(good()) == (True, [])

def test_publish_blocked_when_paused():
    s = FeedSwitches()
    s.pause("base")
    ok, reasons = s.publish_allowed(good(chain="base", source="br"))
    assert ok is False and reasons == ["chain_paused"]

def test_publish_blocked_invalid_record():
    s = FeedSwitches()
    ok, reasons = s.publish_allowed(good(chain="mars"))
    assert ok is False and "unknown_chain" in reasons

def test_publish_solana_unaffected_by_other_pauses():
    s = FeedSwitches()
    s.pause("base"); s.pause("ethereum")
    assert s.publish_allowed(good())[0] is True

def test_chains_and_codes_pinned():
    assert CHAINS == ("solana", "base", "ethereum", "hyperliquid")
    assert SOURCE_CODES["df"] == "degen-feed"
    assert SOURCE_CODES["hb"] == "heartbeat"
    assert WIN_PEAK_MULT == 2.0
