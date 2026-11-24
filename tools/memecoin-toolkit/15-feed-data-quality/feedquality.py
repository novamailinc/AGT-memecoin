"""DexScreener-style pair selection for one mint (synthetic lab).

Pure stdlib, no network.  A token usually has several pairs (different
DEXes, quote tokens, and dust pools nobody trades).  Taking ``pairs[0]``
is a bug: the first entry is whatever the API happened to order first,
and it is often a dust pool with a nonsense price.  This lab implements
the careful version:

  * malformed entries are skipped and counted, never trusted;
  * duplicate pair addresses are de-duplicated (richest entry wins);
  * pairs for a different base token are ignored;
  * pairs under the dust floor (default 1000 USD liquidity) are not
    candidates at all -- their prices are noise, not evidence;
  * a pair whose data timestamp is older than the staleness threshold
    (or missing) is rejected -- fail-closed, stale is not "probably ok";
  * eligible pairs are ranked by liquidity first, with quote-token
    preference (USDC, then SOL, then USDT, then WETH/ETH) breaking
    near-ties only -- preference applies inside a 10% liquidity
    near-tie band (QUOTE_TIE_BAND_USD = 1.10) and never beats a
    bigger liquidity gap;
  * if the surviving pairs disagree on price by more than the
    threshold, the price is *unverifiable* and selection returns no
    pair at all -- fail-closed instead of picking a side.

Payload shape (synthetic, DexScreener-flavoured): each entry is a dict
with ``pairAddress``, ``baseToken: {"address": ...}``,
``quoteToken: {"symbol": ...}``, ``priceUsd`` (string or number),
``liquidity: {"usd": ...}``, ``volume: {"h24": ...}`` and
``dataTimestampMs`` (when this data snapshot was produced).
"""
from __future__ import annotations

from dataclasses import dataclass

PREFERRED_QUOTES: tuple[str, ...] = ("USDC", "SOL", "USDT", "WETH", "ETH")
DEFAULT_MAX_AGE_SECONDS = 900.0
DEFAULT_MAX_DISAGREEMENT_PCT = 15.0
QUOTE_TIE_BAND_USD = 1.10  # quote preference applies within a 10% liq band
DEFAULT_MIN_LIQUIDITY_USD = 1_000.0


@dataclass(frozen=True)
class PairRecord:
    pair_address: str
    base_address: str
    quote_symbol: str
    price_usd: float
    liquidity_usd: float
    volume_h24_usd: float
    timestamp_ms: float | None


@dataclass(frozen=True)
class Selection:
    """Result of a successful, verified selection."""

    pair: PairRecord
    status: str  # always "ok" -- failures return None, see select_pair
    candidates_considered: int
    malformed_skipped: int
    stale_rejected: int
    duplicates_dropped: int
    disagreement_pct: float


def _as_float(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def parse_pair(raw) -> PairRecord | None:
    """Parse one raw entry; None means malformed (never raise on data)."""
    if not isinstance(raw, dict):
        return None
    base = raw.get("baseToken")
    quote = raw.get("quoteToken")
    liquidity = raw.get("liquidity")
    if not isinstance(base, dict) or not isinstance(quote, dict):
        return None
    if not isinstance(liquidity, dict):
        return None
    address = raw.get("pairAddress")
    base_address = base.get("address")
    quote_symbol = quote.get("symbol")
    price = _as_float(raw.get("priceUsd"))
    liq = _as_float(liquidity.get("usd"))
    if not address or not base_address or not quote_symbol:
        return None
    if price is None or price <= 0 or liq is None or liq < 0:
        return None
    volume = raw.get("volume")
    vol = _as_float(volume.get("h24")) if isinstance(volume, dict) else None
    timestamp = _as_float(raw.get("dataTimestampMs"))
    if timestamp is None:  # fall back to pair creation time as data time
        timestamp = _as_float(raw.get("pairCreatedAt"))
    return PairRecord(
        pair_address=str(address), base_address=str(base_address),
        quote_symbol=str(quote_symbol).upper(), price_usd=price,
        liquidity_usd=liq, volume_h24_usd=vol or 0.0,
        timestamp_ms=timestamp,
    )


def parse_payload(payload) -> tuple[list[PairRecord], int]:
    """(parsed pairs, malformed count).  Non-list payload -> ([], 0)? No:
    fail-closed -- a non-list payload raises ValueError."""
    if not isinstance(payload, list):
        raise ValueError(f"payload must be a list, got {type(payload).__name__}")
    parsed, malformed = [], 0
    for raw in payload:
        record = parse_pair(raw)
        if record is None:
            malformed += 1
        else:
            parsed.append(record)
    return parsed, malformed


def dedupe_pairs(pairs: list[PairRecord]) -> tuple[list[PairRecord], int]:
    """One record per pair address; the highest-liquidity copy wins."""
    best: dict[str, PairRecord] = {}
    dropped = 0
    for pair in pairs:
        existing = best.get(pair.pair_address)
        if existing is None:
            best[pair.pair_address] = pair
        else:
            dropped += 1
            if pair.liquidity_usd > existing.liquidity_usd:
                best[pair.pair_address] = pair
    return list(best.values()), dropped


def pair_age_seconds(pair: PairRecord, now_ms: float) -> float | None:
    """Age of the pair's data in seconds; None if it has no timestamp."""
    if pair.timestamp_ms is None:
        return None
    return (now_ms - pair.timestamp_ms) / 1000.0


def is_stale(pair: PairRecord, now_ms: float,
             max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS) -> bool:
    """Stale = no timestamp, future timestamp, or older than threshold."""
    age = pair_age_seconds(pair, now_ms)
    if age is None or age < 0:
        return True
    return age > max_age_seconds


def quote_rank(quote_symbol: str) -> int:
    """Lower is more preferred; unlisted quotes rank after all preferred."""
    try:
        return PREFERRED_QUOTES.index(quote_symbol.upper())
    except ValueError:
        return len(PREFERRED_QUOTES)


def rank_pairs(pairs: list[PairRecord]) -> list[PairRecord]:
    """Liquidity first; quote preference only inside a 10% liquidity band.

    Implemented as a sort by liquidity (desc) followed by a single pass
    that swaps an adjacent preferred-quote pair ahead when its liquidity
    is within the tie band of the pair in front of it.
    """
    ordered = sorted(pairs, key=lambda p: (-p.liquidity_usd, -p.volume_h24_usd,
                                           p.pair_address))
    changed = True
    while changed:
        changed = False
        for i in range(len(ordered) - 1):
            front, back = ordered[i], ordered[i + 1]
            if (quote_rank(back.quote_symbol) < quote_rank(front.quote_symbol)
                    and back.liquidity_usd * QUOTE_TIE_BAND_USD >= front.liquidity_usd
                    and back.liquidity_usd < front.liquidity_usd):
                ordered[i], ordered[i + 1] = back, front
                changed = True
    return ordered


def price_disagreement_pct(pairs: list[PairRecord]) -> float:
    """(max - min) / median price, in %.  0.0 for a single pair."""
    if not pairs:
        raise ValueError("price_disagreement_pct needs at least one pair")
    prices = sorted(p.price_usd for p in pairs)
    n = len(prices)
    median = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2.0
    if median <= 0:
        raise ValueError("median price must be positive")
    return (prices[-1] - prices[0]) / median * 100.0


def eligible_pairs(payload, mint: str, now_ms: float,
                   max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
                   min_liquidity_usd: float = DEFAULT_MIN_LIQUIDITY_USD):
    """Full filter pipeline short of the disagreement check.

    Returns (ranked_pairs, stats_dict).  Raises ValueError on a non-list
    payload or an empty mint.
    """
    if not mint:
        raise ValueError("mint must be a non-empty string")
    parsed, malformed = parse_payload(payload)
    own_all = [p for p in parsed if p.base_address == mint]
    own = [p for p in own_all if p.liquidity_usd >= min_liquidity_usd]
    deduped, dropped = dedupe_pairs(own)
    fresh, stale = [], 0
    for pair in deduped:
        if is_stale(pair, now_ms, max_age_seconds):
            stale += 1
        else:
            fresh.append(pair)
    stats = {"malformed_skipped": malformed, "duplicates_dropped": dropped,
             "stale_rejected": stale, "wrong_mint": len(parsed) - len(own_all),
             "dust_dropped": len(own_all) - len(own)}
    return rank_pairs(fresh), stats


def select_pair(payload, mint: str, now_ms: float,
                max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
                max_disagreement_pct: float = DEFAULT_MAX_DISAGREEMENT_PCT,
                min_liquidity_usd: float = DEFAULT_MIN_LIQUIDITY_USD,
                ) -> Selection | None:
    """The one pair to trust for ``mint`` -- or None, fail-closed.

    None when: no eligible fresh pair survives, or the surviving pairs
    disagree on price by more than ``max_disagreement_pct`` (the price
    is then unverifiable and picking any side would be a guess).
    """
    ranked, stats = eligible_pairs(payload, mint, now_ms, max_age_seconds,
                                   min_liquidity_usd)
    if not ranked:
        return None
    disagreement = price_disagreement_pct(ranked)
    if disagreement > max_disagreement_pct:
        return None
    top = ranked[0]
    return Selection(
        pair=top, status="ok", candidates_considered=len(ranked),
        malformed_skipped=stats["malformed_skipped"],
        stale_rejected=stats["stale_rejected"],
        duplicates_dropped=stats["duplicates_dropped"],
        disagreement_pct=disagreement,
    )


# -- synthetic fixtures ---------------------------------------------------------
MINT = "MintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NOW_MS = 1_700_000_000_000.0


def _pair(address, quote, price, liq, vol=1000.0, ts=NOW_MS - 60_000, mint=MINT):
    return {"pairAddress": address, "baseToken": {"address": mint, "symbol": "TOK"},
            "quoteToken": {"symbol": quote}, "priceUsd": str(price),
            "liquidity": {"usd": liq}, "volume": {"h24": vol},
            "dataTimestampMs": ts}


def dust_first_payload() -> list[dict]:
    """pairs[0] is a dust pool; the real pair is second.  The classic trap."""
    return [
        _pair("DustPair111", "SOL", 9.99, 40.0, vol=3.0),
        _pair("RealPair222", "SOL", 0.50, 180_000.0, vol=900_000.0),
        _pair("MidPair333", "USDC", 0.51, 60_000.0, vol=200_000.0),
    ]


def disagreement_payload() -> list[dict]:
    """Two liquid pairs that flatly disagree on price."""
    return [
        _pair("PairA", "SOL", 0.50, 100_000.0),
        _pair("PairB", "USDC", 0.90, 90_000.0),
    ]


def messy_payload() -> list[dict]:
    """Duplicates, malformed entries, another mint, and one stale pair."""
    good = _pair("GoodPair", "SOL", 0.50, 120_000.0)
    return [
        good,
        dict(good),  # exact duplicate
        _pair("GoodPair", "SOL", 0.50, 130_000.0),  # dup address, richer
        {"pairAddress": "Broken"},  # malformed
        "not-a-dict",  # malformed
        _pair("OtherMint", "SOL", 3.0, 500_000.0, mint="OtherMintZZZ"),
        _pair("StalePair", "SOL", 0.49, 400_000.0, ts=NOW_MS - 10_000_000),
    ]
