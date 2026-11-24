#!/usr/bin/env python3
"""Stacker lane: stacking-multiples strategy engine.

Catches multiple mid-size runners (5x-20x) per day that stack to 10x-20x total.
Entry on 2x CONFIRMATION (not launch snipe). Tiered exits capture outliers.

Research basis: stacking-multiples-strategy-20261014-2222
- Entry: 2x from launch, $6-12k mcap, 15-30% curve, 50+ holders/min, B/S > 1.2
- Exits: 50% @ 2.2x, 30% @ 3.5x (of original), 25% trail on rest
- Hard stop -25%, time stop 2h, max 3 concurrent, 3% daily loss halt

Paper-only. Killswitch lane: "stacker".
"""
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple
import time

# ---- Scoring config ----
SCORE_BAR = 60

# Entry filters (from research)
MCAP_MIN = 6_000       # 2x zone sweet spot
MCAP_MAX = 50_000      # allow post-grad runners
MCAP_SWEET_MIN = 6_000
MCAP_SWEET_MAX = 12_000
LIQ_MIN = 25_000  # RUNNER-ONLY rule 2026-10-18: serious liquidity everywhere
                # (fail-closed: unknown/missing liquidity → 0 → blocked below)
HOLDERS_PER_MIN_MIN = 50
BUY_SELL_MIN = 1.2
CURVE_MIN_PCT = 15     # bonding curve fill %
CURVE_MAX_PCT = 30
MULT_MIN = 1.8         # must be at/near 2x confirmation
MULT_MAX = 4.0         # don't chase already-pumped

# Exit config
TP1_MULT = 2.2
TP1_FRAC = 0.50
TP2_MULT = 3.5
TP2_FRAC = 0.30        # of original position
TRAIL_PCT = 25.0
HARD_STOP_PCT = -25.0
TIME_STOP_SEC = 4 * 3600  # 2026-10-15 research: 2h truncated the proven
                        # 0.5-6h window (37.8% WR). 4h lets winners work;
                        # 6h+ is dead (1W/49L). A/B test 4h vs 6h next.

# Risk config
MAX_CONCURRENT = 3
DAILY_LOSS_HALT_PCT = 3.0   # of bankroll
DEFAULT_BANKROLL = 150.0    # $150 mid-range of $100-200
POSITION_SIZE = 3.0         # $3 fixed per shot


def score_stacker(t: Dict) -> Dict:
    """Score 0-100 for stacker entry. Returns {score, reasons, details}.

    Expected token dict keys:
      multiple_from_launch, mcap, liquidity, holders_per_min,
      buy_sell_ratio, curve_pct, safety ("clean"/reason),
      eagle_momentum (0-100, optional), age_min
    """
    reasons = []
    details = {}
    score = 0.0

    # Hard gates first
    safety = t.get("safety", "unknown")
    if safety != "clean":
        return {"score": 0, "pass": False,
                "reasons": [f"safety: {safety}"], "details": details}

    liq = t.get("liquidity", 0)
    if liq < LIQ_MIN:
        return {"score": 0, "pass": False,
                "reasons": [f"liq ${liq:,.0f} < ${LIQ_MIN:,}"], "details": details}

    mult = t.get("multiple_from_launch", 0)
    if mult < MULT_MIN:
        return {"score": 0, "pass": False,
                "reasons": [f"not at 2x yet ({mult:.1f}x)"], "details": details}
    if mult > MULT_MAX:
        return {"score": 0, "pass": False,
                "reasons": [f"already pumped ({mult:.1f}x)"], "details": details}

    mcap = t.get("mcap", 0)
    if not (MCAP_MIN <= mcap <= MCAP_MAX):
        return {"score": 0, "pass": False,
                "reasons": [f"mcap ${mcap:,.0f} outside $6k-$50k"],
                "details": details}

    # --- Scored components (max 100) ---

    # 1. 2x confirmation strength (30 pts) — closer to 2.0-2.5 = stronger
    if 2.0 <= mult <= 2.5:
        score += 30
        reasons.append(f"2x confirm ({mult:.1f}x) +30")
    elif 1.8 <= mult < 2.0:
        score += 20
        reasons.append(f"approaching 2x ({mult:.1f}x) +20")
    else:
        score += 12
        reasons.append(f"past 2x ({mult:.1f}x) +12")
    details["multiple"] = mult

    # 2. Mcap sweet spot (20 pts)
    if MCAP_SWEET_MIN <= mcap <= MCAP_SWEET_MAX:
        score += 20
        reasons.append(f"mcap sweet ${mcap:,.0f} +20")
    else:
        score += 10
        reasons.append(f"mcap ${mcap:,.0f} +10")
    details["mcap"] = mcap

    # 3. Holder velocity (15 pts)
    hpm = t.get("holders_per_min", 0)
    if hpm >= HOLDERS_PER_MIN_MIN:
        score += 15
        reasons.append(f"{hpm:.0f} holders/min +15")
    elif hpm >= 25:
        score += 8
        reasons.append(f"{hpm:.0f} holders/min +8")
    else:
        reasons.append(f"slow holders ({hpm:.0f}/min) +0")
    details["holders_per_min"] = hpm

    # 4. Buy/sell pressure (15 pts)
    bs = t.get("buy_sell_ratio", 0)
    if bs >= 2.0:
        score += 15
        reasons.append(f"B/S {bs:.1f} strong +15")
    elif bs >= BUY_SELL_MIN:
        score += 10
        reasons.append(f"B/S {bs:.1f} +10")
    else:
        reasons.append(f"B/S {bs:.1f} weak +0")
    details["buy_sell_ratio"] = bs

    # 5. Curve position (10 pts)
    curve = t.get("curve_pct", 0)
    if CURVE_MIN_PCT <= curve <= CURVE_MAX_PCT:
        score += 10
        reasons.append(f"curve {curve:.0f}% +10")
    elif curve > 0:
        score += 4
        reasons.append(f"curve {curve:.0f}% +4")
    details["curve_pct"] = curve

    # 6. Eagle Eye momentum bonus (10 pts)
    eagle = t.get("eagle_momentum", 0)
    if eagle >= 70:
        score += 10
        reasons.append(f"eagle hot ({eagle:.0f}) +10")
    elif eagle >= 50:
        score += 5
        reasons.append(f"eagle warm ({eagle:.0f}) +5")
    details["eagle_momentum"] = eagle

    score = min(100.0, score)
    return {"score": score, "pass": score >= SCORE_BAR,
            "reasons": reasons, "details": details}


@dataclass
class StackerPosition:
    mint: str
    symbol: str
    entry_price: float
    entry_ts: float
    size_usd: float = POSITION_SIZE
    tp1_done: bool = False
    tp2_done: bool = False
    peak_price: float = 0.0
    peak_ts: float = 0.0
    exit_ts: float = 0.0
    exit_reason: str = ""
    realized_pnl: float = 0.0   # USD net of costs
    frac_remaining: float = 1.0
    # ATR trail (2026-10-15 research): 2x ATR(5m) instead of fixed 25%.
    # atr_pct = ATR as % of entry price. 0 = fall back to fixed trail.
    atr_pct: float = 0.0
    # A/B variant tag (journaled for exit-optimization experiments)
    exit_variant: str = "atr2"

    def __post_init__(self):
        if self.peak_price <= 0:
            self.peak_price = self.entry_price

    def trail_pct(self):
        """Trail distance: 2x ATR when known, else fixed 25% fallback."""
        if self.atr_pct > 0:
            return 2.0 * self.atr_pct
        return TRAIL_PCT


@dataclass
class StackerLedger:
    positions: List[StackerPosition] = field(default_factory=list)
    daily_pnl: Dict[str, float] = field(default_factory=dict)  # date -> USD
    bankroll: float = DEFAULT_BANKROLL

    def _today(self, ts: float) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(ts))

    def open_count(self) -> int:
        return sum(1 for p in self.positions if not p.exit_ts)

    def can_enter(self, ts: float) -> Tuple[bool, str]:
        # Killswitch (fail-closed)
        try:
            import killswitch
            if not killswitch.gate("stacker"):
                return False, "killswitch halted"
        except Exception:
            return False, "killswitch error"
        # Max concurrent
        if self.open_count() >= MAX_CONCURRENT:
            return False, f"max {MAX_CONCURRENT} concurrent"
        # Daily loss halt
        today = self._today(ts)
        day_pnl = self.daily_pnl.get(today, 0.0)
        halt_at = -(DAILY_LOSS_HALT_PCT / 100.0) * self.bankroll
        if day_pnl <= halt_at:
            return False, f"daily loss halt (${day_pnl:.2f})"
        return True, "ok"

    def enter(self, mint: str, symbol: str, ts: float,
              price: float, atr_pct: float = 0.0,
              exit_variant: str = "atr2") -> Optional[StackerPosition]:
        ok, why = self.can_enter(ts)
        if not ok:
            return None
        if any(p.mint == mint and not p.exit_ts for p in self.positions):
            return None  # already in
        pos = StackerPosition(mint=mint, symbol=symbol,
                              entry_price=price, entry_ts=ts,
                              atr_pct=atr_pct, exit_variant=exit_variant)
        self.positions.append(pos)
        return pos

    def _realize(self, pos: StackerPosition, ts: float, price: float,
                 reason: str, frac: float):
        """Close `frac` of original position at price. 5% round-trip cost."""
        if frac <= 0 or pos.frac_remaining <= 0:
            return
        frac = min(frac, pos.frac_remaining)
        gross = (price / pos.entry_price - 1.0) * pos.size_usd * frac
        cost = pos.size_usd * 0.05 * frac   # 5% round-trip (research)
        net = gross - cost
        pos.realized_pnl += net
        pos.frac_remaining -= frac
        today = self._today(ts)
        self.daily_pnl[today] = self.daily_pnl.get(today, 0.0) + net
        if pos.frac_remaining <= 1e-9:
            pos.exit_ts = ts
            pos.exit_reason = reason

    def on_price(self, mint: str, ts: float,
                 price: float) -> List[str]:
        """Feed price tick. Returns list of event strings."""
        events = []
        for pos in self.positions:
            if pos.mint != mint or pos.exit_ts:
                continue
            # Track peak
            if price > pos.peak_price:
                pos.peak_price = price
                pos.peak_ts = ts
            ret = (price / pos.entry_price - 1.0) * 100.0
            age = ts - pos.entry_ts

            # Hard stop -25%
            if ret <= HARD_STOP_PCT:
                self._realize(pos, ts, price, "hard_stop", 1.0)
                events.append(f"{pos.symbol} HARD STOP {ret:.1f}%")
                continue
            # Time stop 2h
            if age >= TIME_STOP_SEC:
                self._realize(pos, ts, price, "time_stop", 1.0)
                events.append(f"{pos.symbol} TIME STOP")
                continue
            # TP1: 50% @ 2.2x
            if not pos.tp1_done and price >= pos.entry_price * TP1_MULT:
                self._realize(pos, ts, price, "tp1", TP1_FRAC)
                pos.tp1_done = True
                events.append(f"{pos.symbol} TP1 2.2x (50%)")
                continue
            # TP2: 30% of original @ 3.5x
            if pos.tp1_done and not pos.tp2_done and \
                    price >= pos.entry_price * TP2_MULT:
                self._realize(pos, ts, price, "tp2", TP2_FRAC)
                pos.tp2_done = True
                events.append(f"{pos.symbol} TP2 3.5x (30%)")
                continue
            # Trail on remainder (only after TP1): 2x ATR when known,
            # else fixed 25% fallback (2026-10-15 research)
            if pos.tp1_done and pos.frac_remaining > 0:
                dd = (1.0 - price / pos.peak_price) * 100.0
                trail = pos.trail_pct()
                if dd >= trail:
                    self._realize(pos, ts, price, "trail_atr"
                                  if pos.atr_pct > 0 else "trail", 1.0)
                    events.append(
                        f"{pos.symbol} TRAIL -{dd:.0f}% from peak "
                        f"(trail {trail:.1f}%)")
                    continue
        return events

    def stats(self) -> Dict:
        closed = [p for p in self.positions if p.exit_ts]
        wins = [p for p in closed if p.realized_pnl > 0]
        total_pnl = sum(p.realized_pnl for p in closed)
        gross_mult_sum = sum(
            (p.realized_pnl / p.size_usd) + 1.0 for p in closed
        ) if closed else 0.0
        return {
            "total_trades": len(closed),
            "wins": len(wins),
            "win_rate": len(wins) / len(closed) if closed else 0.0,
            "total_pnl_usd": round(total_pnl, 2),
            "gross_multiple_sum": round(gross_mult_sum, 2),
            "open": self.open_count(),
        }

    def to_dict(self) -> Dict:
        return {"positions": [asdict(p) for p in self.positions],
                "daily_pnl": self.daily_pnl,
                "bankroll": self.bankroll}

    @classmethod
    def from_dict(cls, d: Dict) -> "StackerLedger":
        led = cls(bankroll=d.get("bankroll", DEFAULT_BANKROLL))
        led.daily_pnl = d.get("daily_pnl", {})
        for pd in d.get("positions", []):
            led.positions.append(StackerPosition(**pd))
        return led
