"""Momentum scanner lab (synthetic, pure stdlib).

Trending-pool qualification funnel. No upper gain ceiling (regression:
the invented 300% ceiling blocked real runners). Fail-closed: missing
fields reject; safety via an injected predicate (absent predicate rejects).
"""
from __future__ import annotations

from dataclasses import dataclass, field

MIN_LIQUIDITY = 50_000.0
MIN_VOLUME_24H = 100_000.0
MIN_GAIN_24H = 20.0  # percent; NO upper ceiling
DUMP_GUARD_H1 = -15.0  # h1 change < -15% rejects
COOLDOWN_S = 6 * 3600

REJECT_REASONS = ("gain24_low", "liq_low", "vol_low", "dump_guard",
                  "no_momentum", "safety", "cooldown", "bad_pool")


@dataclass
class Pool:
    mint: str
    liquidity_usd: float
    volume_24h: float
    change_h24_pct: float
    change_h1_pct: float
    change_m5_pct: float | None = None

    def __post_init__(self):
        if not isinstance(self.mint, str) or not self.mint:
            raise ValueError("mint must be non-empty string")
        for name in ("liquidity_usd", "volume_24h", "change_h24_pct", "change_h1_pct"):
            v = getattr(self, name)
            if v is None or not isinstance(v, (int, float)) or isinstance(v, bool):
                raise ValueError(f"{name} must be a number, got {v!r}")


@dataclass
class CooldownBook:
    _last_card: dict = field(default_factory=dict)

    def on_cooldown(self, mint: str, now: float) -> bool:
        last = self._last_card.get(mint)
        if last is None:
            return False
        return (now - last) < COOLDOWN_S

    def record(self, mint: str, now: float) -> None:
        if not isinstance(mint, str) or not mint:
            raise ValueError("mint must be non-empty string")
        if now is None or now < 0:
            raise ValueError("now must be >= 0")
        self._last_card[mint] = float(now)


def new_stats() -> dict:
    s = {r: 0 for r in REJECT_REASONS}
    s["qualified"] = 0
    s["scanned"] = 0
    return s


def has_positive_momentum(pool: Pool) -> bool:
    """Positive-momentum presence: h1 > 0 or m5 > 0."""
    if pool.change_h1_pct > 0:
        return True
    if pool.change_m5_pct is not None and pool.change_m5_pct > 0:
        return True
    return False


def qualify(pool, *, safety_ok=None, cooldown: CooldownBook | None = None,
            now: float = 0.0, stats: dict | None = None) -> tuple:
    """Returns (qualified: bool, reason: str|None). Fills stats if given."""
    def _done(ok, reason):
        if stats is not None:
            stats["scanned"] = stats.get("scanned", 0) + 1
            if ok:
                stats["qualified"] = stats.get("qualified", 0) + 1
            else:
                stats[reason] = stats.get(reason, 0) + 1
        return (ok, reason)

    if not isinstance(pool, Pool):
        return _done(False, "bad_pool")
    if cooldown is not None and cooldown.on_cooldown(pool.mint, now):
        return _done(False, "cooldown")
    if pool.liquidity_usd < MIN_LIQUIDITY:
        return _done(False, "liq_low")
    if pool.volume_24h < MIN_VOLUME_24H:
        return _done(False, "vol_low")
    if pool.change_h24_pct < MIN_GAIN_24H:
        return _done(False, "gain24_low")
    if pool.change_h1_pct < DUMP_GUARD_H1:
        return _done(False, "dump_guard")
    if not has_positive_momentum(pool):
        return _done(False, "no_momentum")
    if safety_ok is None:
        return _done(False, "safety")  # fail-closed: no predicate, no card
    try:
        safe = bool(safety_ok(pool.mint))
    except Exception:
        return _done(False, "safety")
    if not safe:
        return _done(False, "safety")
    if cooldown is not None:
        cooldown.record(pool.mint, now)
    return _done(True, None)


def scan(pools, *, safety_ok=None, cooldown: CooldownBook | None = None,
         now: float = 0.0) -> tuple:
    """Returns (qualified_pools, stats)."""
    stats = new_stats()
    out = []
    for p in pools:
        ok, _ = qualify(p, safety_ok=safety_ok, cooldown=cooldown, now=now, stats=stats)
        if ok:
            out.append(p)
    return (out, stats)


def doomsday_pool() -> Pool:
    """Regression fixture shape: h24 +2540%, liq $96k, high volume, h1 positive."""
    return Pool(mint="DoomsdayMint", liquidity_usd=96_000, volume_24h=900_000,
                change_h24_pct=2540.0, change_h1_pct=19.8, change_m5_pct=5.0)


def good_pool(**over) -> Pool:
    base = dict(mint="GoodMint", liquidity_usd=120_000, volume_24h=500_000,
                change_h24_pct=80.0, change_h1_pct=10.0, change_m5_pct=3.0)
    base.update(over)
    return Pool(**base)
