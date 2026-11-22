#!/usr/bin/env python3
"""Degen v2 paper trader: fast exits, honest costs, kill switch wired.

Exit policy (from research):
- 12% trailing stop (tight trails beat wide)
- 45-min max hold (68% die on day 1)
- -30% hard stop
- Sell half at 2x

Cost: $1.25 all-in per $1 shot.
"""
from typing import Dict, List, Optional
from dataclasses import dataclass, field
import time

from degen_v2 import EXITS, COST_PER_SHOT

@dataclass
class DegenPosition:
    mint: str
    symbol: str
    entry_ts: float
    entry_price: float
    half_sold: bool = False
    peak_price: float = 0.0
    exit_ts: Optional[float] = None
    exit_reason: str = ""
    pnl: float = 0.0  # in USD, net of costs
    source: str = "v2"  # "v2" | "v2_spark" (spark-boosted) | "spark" — for kill-criteria comparison

    def __post_init__(self):
        self.peak_price = self.entry_price

@dataclass
class DegenLedger:
    positions: List[DegenPosition] = field(default_factory=list)
    closed: List[DegenPosition] = field(default_factory=list)

    def enter(self, mint: str, symbol: str, ts: float, price: float,
              source: str = "v2", liquidity_usd: Optional[float] = None,
              ) -> Optional[DegenPosition]:
        # One position per mint
        if any(p.mint == mint and p.exit_ts is None for p in self.positions):
            return None
        # Kill switch gate (fail-closed, matching gate()'s contract:
        # on any error the lane does not trade)
        try:
            import killswitch
            if not killswitch.gate("degen_v2"):
                return None
        except Exception:
            return None
        # Exclusion-first veto engine (2026-10-16): hard vetoes apply to
        # the Degen v2 lane too. No score can override a veto.
        try:
            from . import veto as veto_mod
            v = veto_mod.veto_check(mint, "solana", symbol,
                                    lane="degen_v2")
            if not v["passed"]:
                return None
        except Exception:
            pass  # fail-open on veto infra error
        # Honest fill (2026-10-16): you never get the quoted price.
        # Entry fills adverse vs the quote, scaled by pool liquidity.
        try:
            from . import fills as fills_mod
            price = fills_mod.honest_entry(price, 1.0, liquidity_usd)
        except Exception:
            pass  # fail-open: fall back to quoted price
        pos = DegenPosition(mint=mint, symbol=symbol, entry_ts=ts,
                            entry_price=price, source=source)
        self.positions.append(pos)
        return pos

    def _close(self, pos: DegenPosition, ts: float, price: float,
               reason: str, fraction: float = 1.0,
               liquidity_usd: Optional[float] = None):
        """Close fraction. PnL in USD net of $1.25 cost + honest exit fill."""
        # Honest exit fill (2026-10-16): exits fill adverse vs the quote.
        try:
            from . import fills as fills_mod
            price = fills_mod.honest_exit(price, 1.0, liquidity_usd)
        except Exception:
            pass  # fail-open: fall back to quoted price
        # Gross: (exit/entry - 1) * $1 * fraction
        gross = (price / pos.entry_price - 1.0) * 1.0 * fraction
        # Cost allocated by fraction
        cost = COST_PER_SHOT * fraction
        # Net: gross - cost (cost only on first close to avoid double-count)
        if pos.pnl == 0:  # first close
            net = gross - COST_PER_SHOT * fraction
            # Remaining fraction still owes its cost share on close
        else:
            net = gross - COST_PER_SHOT * fraction
        pos.pnl += net
        if fraction >= 1.0:
            pos.exit_ts = ts
            pos.exit_reason = reason
            self.closed.append(pos)

    def on_price(self, mint: str, ts: float, price: float) -> List[str]:
        events = []
        for pos in self.positions:
            if pos.exit_ts is not None or pos.mint != mint:
                continue
            if price > pos.peak_price:
                pos.peak_price = price
            ret = 100.0 * (price / pos.entry_price - 1.0)
            age_min = (ts - pos.entry_ts) / 60

            # Hard stop -30%
            if ret <= EXITS["hard_stop_pct"]:
                self._close(pos, ts, price, "hard_stop")
                events.append(f"{pos.symbol} HARD STOP {ret:.1f}%")
                continue
            # Take profit: sell half at 2x
            if not pos.half_sold and price >= pos.entry_price * EXITS["take_profit_mult"]:
                self._close(pos, ts, price, "take_profit", fraction=0.5)
                pos.half_sold = True
                events.append(f"{pos.symbol} TP 2x (half)")
                continue
            # Trail 12% on remainder (or full if no TP yet)
            dd = 100.0 * (1.0 - price / pos.peak_price)
            if dd >= EXITS["trail_pct"]:
                self._close(pos, ts, price, "trail")
                events.append(f"{pos.symbol} TRAIL -{dd:.0f}%")
                continue
            # Time exit 45 min
            if age_min >= EXITS["max_hold_min"]:
                self._close(pos, ts, price, "time_exit")
                events.append(f"{pos.symbol} TIME EXIT")
        return events

    def to_dict(self) -> Dict:
        from dataclasses import asdict
        return {
            "positions": [asdict(p) for p in self.positions],
            "closed": [asdict(p) for p in self.closed],
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "DegenLedger":
        led = cls()
        for pd in (d or {}).get("positions", []):
            try:
                led.positions.append(DegenPosition(**pd))
            except Exception:
                continue
        for pd in (d or {}).get("closed", []):
            try:
                led.closed.append(DegenPosition(**pd))
            except Exception:
                continue
        # closed also live in positions (they were appended there at enter);
        # keep positions as the full history, closed as the closed subset.
        return led

    def stats(self) -> Dict:
        if not self.closed:
            return {"n": 0}
        pnls = [p.pnl for p in self.closed]
        wins = [p for p in pnls if p > 0]
        return {
            "n": len(pnls),
            "net": round(sum(pnls), 2),
            "win_rate": round(len(wins) / len(pnls), 3),
            "expectancy": round(sum(pnls) / len(pnls), 3),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
        }

    def source_stats(self) -> Dict:
        """Per-source stats for kill-criteria comparison (v2 vs v2_spark vs spark)."""
        out = {}
        for src in ("v2", "v2_spark", "spark"):
            ps = [p for p in self.closed if p.source == src]
            if not ps:
                out[src] = {"n": 0}
                continue
            pnls = [p.pnl for p in ps]
            wins = [p for p in pnls if p > 0]
            out[src] = {
                "n": len(pnls),
                "net": round(sum(pnls), 2),
                "win_rate": round(len(wins) / len(pnls), 3),
                "expectancy": round(sum(pnls) / len(pnls), 3),
            }
        return out
