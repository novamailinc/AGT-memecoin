"""Tests for feedquality.py -- synthetic DexScreener-style payloads only."""
import pytest

from feedquality import (
    MINT, NOW_MS, PairRecord, dedupe_pairs, disagreement_payload,
    dust_first_payload, eligible_pairs, is_stale, messy_payload,
    pair_age_seconds, parse_pair, parse_payload, price_disagreement_pct,
    quote_rank, rank_pairs, select_pair,
)


def raw(address="P1", quote="SOL", price="0.50", liq=50_000.0, vol=1000.0,
        ts=NOW_MS - 60_000, mint=MINT):
    return {"pairAddress": address, "baseToken": {"address": mint, "symbol": "TOK"},
            "quoteToken": {"symbol": quote}, "priceUsd": price,
            "liquidity": {"usd": liq}, "volume": {"h24": vol},
            "dataTimestampMs": ts}


def record(address="P1", quote="SOL", price=0.5, liq=50_000.0, vol=1000.0,
           ts=NOW_MS - 60_000):
    return PairRecord(address, MINT, quote, price, liq, vol, ts)


# -- the headline fixture -------------------------------------------------------
def test_pairs_zero_is_the_wrong_pair():
    payload = dust_first_payload()
    naive = payload[0]
    assert naive["pairAddress"] == "DustPair111"
    assert naive["liquidity"]["usd"] < 100  # pairs[0] is dust
    selection = select_pair(payload, MINT, NOW_MS)
    assert selection is not None
    assert selection.pair.pair_address == "RealPair222"
    assert selection.pair.pair_address != naive["pairAddress"]


def test_selector_picks_highest_liquidity():
    payload = [raw("A", liq=10_000), raw("B", liq=90_000), raw("C", liq=40_000)]
    assert select_pair(payload, MINT, NOW_MS).pair.pair_address == "B"


def test_selected_price_is_the_real_price_not_dust_price():
    selection = select_pair(dust_first_payload(), MINT, NOW_MS)
    assert selection.pair.price_usd == pytest.approx(0.50)


# -- parsing / malformed ----------------------------------------------------------
def test_parse_valid_pair_with_string_price():
    parsed = parse_pair(raw(price="1.25"))
    assert parsed.price_usd == pytest.approx(1.25)
    assert parsed.quote_symbol == "SOL"


def test_parse_numeric_price_accepted():
    assert parse_pair(raw(price=2.5)).price_usd == pytest.approx(2.5)


@pytest.mark.parametrize("bad", [
    None, "junk", 42, {}, {"pairAddress": "X"},
    {"pairAddress": "X", "baseToken": {"address": MINT},
     "quoteToken": {"symbol": "SOL"}, "priceUsd": "0.5"},  # no liquidity block
])
def test_malformed_entries_parse_to_none(bad):
    assert parse_pair(bad) is None


def test_zero_and_negative_price_are_malformed():
    assert parse_pair(raw(price="0")) is None
    assert parse_pair(raw(price="-1")) is None


def test_negative_liquidity_is_malformed():
    assert parse_pair(raw(liq=-5)) is None


def test_non_numeric_price_is_malformed():
    assert parse_pair(raw(price="soon")) is None


def test_parse_payload_counts_malformed():
    parsed, malformed = parse_payload([raw(), {"junk": 1}, raw("P2")])
    assert len(parsed) == 2 and malformed == 1


def test_non_list_payload_raises():
    with pytest.raises(ValueError):
        parse_payload({"pairs": []})
    with pytest.raises(ValueError):
        select_pair("nope", MINT, NOW_MS)


def test_missing_volume_defaults_to_zero():
    entry = raw()
    del entry["volume"]
    assert parse_pair(entry).volume_h24_usd == 0.0


def test_pair_created_at_fallback_timestamp():
    entry = raw()
    del entry["dataTimestampMs"]
    entry["pairCreatedAt"] = NOW_MS - 30_000
    assert parse_pair(entry).timestamp_ms == NOW_MS - 30_000


# -- staleness (fail-closed) ---------------------------------------------------------
def test_fresh_pair_is_not_stale():
    assert not is_stale(record(), NOW_MS)


def test_old_pair_is_stale():
    old = record(ts=NOW_MS - 901_000)
    assert is_stale(old, NOW_MS)


def test_staleness_boundary():
    assert not is_stale(record(ts=NOW_MS - 900_000), NOW_MS)
    assert is_stale(record(ts=NOW_MS - 900_001), NOW_MS)


def test_missing_timestamp_is_stale_fail_closed():
    assert is_stale(record(ts=None), NOW_MS)


def test_future_timestamp_is_stale_fail_closed():
    assert is_stale(record(ts=NOW_MS + 60_000), NOW_MS)


def test_pair_age_seconds():
    assert pair_age_seconds(record(), NOW_MS) == pytest.approx(60.0)
    assert pair_age_seconds(record(ts=None), NOW_MS) is None


def test_all_stale_payload_selects_none():
    payload = [raw("A", ts=NOW_MS - 5_000_000), raw("B", ts=None)]
    payload[1]["dataTimestampMs"] = None
    assert select_pair(payload, MINT, NOW_MS) is None


def test_stale_richest_pair_does_not_win():
    payload = [raw("Stale", liq=900_000, ts=NOW_MS - 5_000_000),
               raw("Fresh", liq=20_000)]
    assert select_pair(payload, MINT, NOW_MS).pair.pair_address == "Fresh"


def test_custom_staleness_threshold():
    payload = [raw("A", ts=NOW_MS - 120_000)]
    assert select_pair(payload, MINT, NOW_MS, max_age_seconds=60) is None
    assert select_pair(payload, MINT, NOW_MS, max_age_seconds=300) is not None


# -- ranking / quote preference ---------------------------------------------------------
def test_quote_rank_order():
    assert quote_rank("USDC") < quote_rank("SOL") < quote_rank("USDT")
    assert quote_rank("WETH") < quote_rank("SOMERANDOM")
    assert quote_rank("usdc") == quote_rank("USDC")  # case-insensitive


def test_rank_by_liquidity_descending():
    ranked = rank_pairs([record("A", liq=1_000), record("B", liq=5_000),
                         record("C", liq=3_000)])
    assert [p.pair_address for p in ranked] == ["B", "C", "A"]


def test_quote_preference_breaks_near_tie():
    # USDC pair within 10% liquidity of the SOL pair jumps ahead.
    ranked = rank_pairs([record("SolPair", quote="SOL", liq=100_000),
                         record("UsdcPair", quote="USDC", liq=95_000)])
    assert ranked[0].pair_address == "UsdcPair"


def test_quote_preference_never_beats_big_liquidity_gap():
    ranked = rank_pairs([record("SolPair", quote="SOL", liq=100_000),
                         record("UsdcPair", quote="USDC", liq=50_000)])
    assert ranked[0].pair_address == "SolPair"


def test_volume_breaks_exact_liquidity_tie_before_preference_pass():
    ranked = rank_pairs([record("Low", quote="SOL", liq=50_000, vol=10),
                         record("High", quote="SOL", liq=50_000, vol=99)])
    assert ranked[0].pair_address == "High"


# -- duplicates ---------------------------------------------------------------------------
def test_dedupe_keeps_richest_copy():
    pairs, dropped = dedupe_pairs([record("A", liq=10_000), record("A", liq=30_000),
                                   record("B", liq=5_000)])
    assert dropped == 1
    kept = {p.pair_address: p.liquidity_usd for p in pairs}
    assert kept == {"A": 30_000, "B": 5_000}


def test_dedupe_no_duplicates_drops_nothing():
    pairs, dropped = dedupe_pairs([record("A"), record("B")])
    assert dropped == 0 and len(pairs) == 2


# -- disagreement (fail-closed) --------------------------------------------------------------
def test_disagreement_pct_math():
    pairs = [record("A", price=1.0), record("B", price=1.1)]
    assert price_disagreement_pct(pairs) == pytest.approx(0.1 / 1.05 * 100)


def test_disagreement_single_pair_is_zero():
    assert price_disagreement_pct([record()]) == 0.0


def test_disagreement_empty_raises():
    with pytest.raises(ValueError):
        price_disagreement_pct([])


def test_wildly_disagreeing_pairs_are_unverifiable():
    assert select_pair(disagreement_payload(), MINT, NOW_MS) is None


def test_close_prices_verify_fine():
    payload = [raw("A", price="0.50", liq=80_000), raw("B", price="0.53", liq=70_000)]
    selection = select_pair(payload, MINT, NOW_MS)
    assert selection is not None and selection.disagreement_pct < 15.0


def test_disagreement_threshold_is_configurable():
    payload = [raw("A", price="0.50", liq=80_000), raw("B", price="0.60", liq=70_000)]
    assert select_pair(payload, MINT, NOW_MS) is None  # ~18% > 15%
    assert select_pair(payload, MINT, NOW_MS, max_disagreement_pct=25) is not None


def test_dust_price_does_not_poison_verification():
    # The dust pair in dust_first_payload quotes 9.99 vs the real 0.50;
    # dust is excluded before verification, so selection still succeeds.
    selection = select_pair(dust_first_payload(), MINT, NOW_MS)
    assert selection is not None and selection.disagreement_pct < 5.0


# -- whole pipeline on the messy fixture ---------------------------------------------------------
def test_messy_payload_end_to_end():
    selection = select_pair(messy_payload(), MINT, NOW_MS)
    assert selection is not None
    assert selection.pair.pair_address == "GoodPair"
    assert selection.pair.liquidity_usd == pytest.approx(130_000)  # richest dup kept
    assert selection.malformed_skipped == 2
    assert selection.duplicates_dropped == 2
    assert selection.stale_rejected == 1


def test_messy_payload_stats():
    ranked, stats = eligible_pairs(messy_payload(), MINT, NOW_MS)
    assert stats["wrong_mint"] == 1
    assert len(ranked) == 1


def test_empty_payload_selects_none():
    assert select_pair([], MINT, NOW_MS) is None


def test_wrong_mint_only_selects_none():
    assert select_pair([raw(mint="OtherZZZ")], MINT, NOW_MS) is None


def test_empty_mint_raises():
    with pytest.raises(ValueError):
        select_pair([raw()], "", NOW_MS)


def test_all_dust_selects_none():
    payload = [raw("A", liq=10), raw("B", liq=999)]
    assert select_pair(payload, MINT, NOW_MS) is None


def test_dust_floor_is_configurable():
    payload = [raw("A", liq=500)]
    assert select_pair(payload, MINT, NOW_MS) is None
    assert select_pair(payload, MINT, NOW_MS, min_liquidity_usd=100) is not None


def test_selection_status_and_candidate_count():
    selection = select_pair(dust_first_payload(), MINT, NOW_MS)
    assert selection.status == "ok"
    assert selection.candidates_considered == 2  # dust excluded
