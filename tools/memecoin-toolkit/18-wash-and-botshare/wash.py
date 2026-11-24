"""Wash-trading and bot-share detectors over synthetic trade streams.

Pure stdlib, no network.  Every fixture is synthetic.

Definitions (documented so tests can hand-check them):
  * Wash volume for one wallet = ``2 * min(buy_volume, sell_volume)`` --
    the round-tripped part of that wallet's flow, counting both legs.
    ``wash_share`` is total wash volume / total volume, in [0, 1].
  * A trade is *bot-like* when any bot pattern holds for it: its size
    is tiny (< 0.10 SOL), its wallet is high-frequency (>= 8 trades in
    the stream), or its slot is crowded (>= 3 trades share the slot).
    ``bot_share`` is the fraction of *transactions* (count, not
    volume) that are bot-like.  Bot share is a NEGATIVE signal: it only
    ever subtracts from quality, it never adds.
  * A trade is a *bump* trade when it is a small buy (< 0.10 SOL) from
    a wallet with >= 3 such small buys -- the classic bump-farm shape
    of repeated dust buys meant to push a token up a trending list.
    ``bump_share`` is the count share of bump trades.

Fingerprint semantics are FAIL-OPEN in the specific sense the live bot
uses: ``assess`` on missing or too-thin data returns status
``"unknown"`` with ``None`` shares -- never ``"clean"`` (a pass) and
never a flag (a veto).  Only a sufficiently large stream can earn
either verdict.  The low-level share functions fail closed instead:
they raise ``ValueError`` on empty/invalid input rather than
returning a plausible-looking zero.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

WASH_THRESHOLD = 0.40
BOT_THRESHOLD = 0.50
BUMP_THRESHOLD = 0.50
TINY_SOL = 0.10
HIGH_FREQ_TRADES = 8
CROWDED_SLOT = 3
BUMP_MIN_REPEATS = 3
MIN_TRADES_FOR_VERDICT = 10


@dataclass(frozen=True)
class Trade:
    wallet: str
    side: str  # "buy" or "sell"
    amount_sol: float
    ts: float = 0.0
    slot: int | None = None

    def __post_init__(self) -> None:
        if not self.wallet or not isinstance(self.wallet, str):
            raise ValueError(f"wallet must be a non-empty string, got {self.wallet!r}")
        if self.side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {self.side!r}")
        if self.amount_sol is None or not (self.amount_sol > 0):
            raise ValueError(f"amount_sol must be positive, got {self.amount_sol!r}")
        if self.ts is None or self.ts < 0:
            raise ValueError(f"ts must be >= 0, got {self.ts!r}")
        if self.slot is not None and self.slot < 0:
            raise ValueError(f"slot must be >= 0 or None, got {self.slot!r}")


def _require_trades(trades) -> list:
    if trades is None:
        raise ValueError("trades must not be None")
    trades = list(trades)
    if not trades:
        raise ValueError("trades must not be empty")
    return trades


def total_volume(trades) -> float:
    return sum(t.amount_sol for t in _require_trades(trades))


def trade_count(trades) -> int:
    return len(_require_trades(trades))


def wallet_volumes(trades) -> dict:
    """{wallet: (buy_volume, sell_volume)} for the stream."""
    out: dict[str, list] = defaultdict(lambda: [0.0, 0.0])
    for t in _require_trades(trades):
        out[t.wallet][0 if t.side == "buy" else 1] += t.amount_sol
    return {w: (v[0], v[1]) for w, v in out.items()}


def roundtrip_volume(trades) -> float:
    """Volume that is part of a same-wallet buy+sell round trip."""
    return sum(2.0 * min(b, s) for b, s in wallet_volumes(trades).values())


def wash_share(trades) -> float:
    trades = _require_trades(trades)
    return roundtrip_volume(trades) / total_volume(trades)


def is_tiny(amount_sol: float) -> bool:
    if amount_sol is None or not (amount_sol > 0):
        raise ValueError(f"amount_sol must be positive, got {amount_sol!r}")
    return amount_sol < TINY_SOL


def bot_like_mask(trades) -> list:
    """Per-trade booleans: does this trade show a bot pattern?"""
    trades = _require_trades(trades)
    wallet_counts = Counter(t.wallet for t in trades)
    slot_counts = Counter(t.slot for t in trades if t.slot is not None)
    out = []
    for t in trades:
        tiny = t.amount_sol < TINY_SOL
        high_freq = wallet_counts[t.wallet] >= HIGH_FREQ_TRADES
        crowded = t.slot is not None and slot_counts[t.slot] >= CROWDED_SLOT
        out.append(bool(tiny or high_freq or crowded))
    return out


def bot_share(trades) -> float:
    mask = bot_like_mask(trades)
    return sum(mask) / len(mask)


def bot_volume_share(trades) -> float:
    trades = _require_trades(trades)
    mask = bot_like_mask(trades)
    bot_vol = sum(t.amount_sol for t, m in zip(trades, mask) if m)
    return bot_vol / total_volume(trades)


def bump_mask(trades) -> list:
    trades = _require_trades(trades)
    small_buy_counts = Counter(
        t.wallet for t in trades if t.side == "buy" and t.amount_sol < TINY_SOL
    )
    return [
        bool(
            t.side == "buy"
            and t.amount_sol < TINY_SOL
            and small_buy_counts[t.wallet] >= BUMP_MIN_REPEATS
        )
        for t in trades
    ]


def bump_share(trades) -> float:
    mask = bump_mask(trades)
    return sum(mask) / len(mask)


def distinct_wallets(trades) -> int:
    return len({t.wallet for t in _require_trades(trades)})


def same_slot_clusters(trades) -> int:
    """Number of slots carrying >= CROWDED_SLOT trades."""
    trades = _require_trades(trades)
    counts = Counter(t.slot for t in trades if t.slot is not None)
    return sum(1 for c in counts.values() if c >= CROWDED_SLOT)


def negative_signal_score(trades) -> float:
    """Bot share expressed as a penalty in [-100, 0]. Never positive."""
    return -100.0 * bot_share(trades)


def toxicity_score(trades) -> float:
    """Combined manipulation burden in [0, 100]; higher is worse."""
    trades = _require_trades(trades)
    score = 50.0 * wash_share(trades) + 30.0 * bot_share(trades) + 20.0 * bump_share(trades)
    return min(100.0, score)


def flags_for(wash: float, bot: float, bump: float) -> list:
    """Threshold flags; a share exactly AT the threshold flags."""
    for name, v in (("wash", wash), ("bot", bot), ("bump", bump)):
        if v is None or not (0.0 <= v <= 1.0):
            raise ValueError(f"{name} share must be in [0, 1], got {v!r}")
    flags = []
    if wash >= WASH_THRESHOLD:
        flags.append("WASH_SKEW")
    if bot >= BOT_THRESHOLD:
        flags.append("BOT_SWARM")
    if bump >= BUMP_THRESHOLD:
        flags.append("BUMP_FARM")
    return flags


def assess(trades, min_trades: int = MIN_TRADES_FOR_VERDICT) -> dict:
    """Fingerprint verdict. Missing/thin data -> 'unknown'.

    'unknown' is neither a pass nor a veto: no shares are reported and
    no flags are raised, exactly because there is not enough evidence
    to say either way.
    """
    if min_trades is None or min_trades <= 0:
        raise ValueError(f"min_trades must be positive, got {min_trades!r}")
    if trades is None or len(list(trades)) < min_trades:
        return {
            "status": "unknown",
            "flags": [],
            "wash_share": None,
            "bot_share": None,
            "bump_share": None,
            "toxicity_score": None,
            "trade_count": 0 if trades is None else len(list(trades)),
        }
    trades = list(trades)
    w, b, u = wash_share(trades), bot_share(trades), bump_share(trades)
    flags = flags_for(w, b, u)
    return {
        "status": "flagged" if flags else "clean",
        "flags": flags,
        "wash_share": w,
        "bot_share": b,
        "bump_share": u,
        "toxicity_score": toxicity_score(trades),
        "trade_count": len(trades),
    }
