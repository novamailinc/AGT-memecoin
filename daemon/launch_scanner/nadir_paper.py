#!/usr/bin/env python3
"""Nadir-v0 Phase 2: isolated paper-trading ledger.

Pure logic: fills, exits, position state. No I/O, no network.
Uses synthetic/test data until Phase 1 provides real candles.

Blueprint spec:
- Fixed 0.01 SOL per position, no compounding
- Max 5 concurrent, 3 entries/day, 7-day cooldown per mint
- Buy fill = observed price * 1.015 ; Sell fill = observed price * 0.985
- Exits: safety, -35% hard stop, 50% at 2x, 25% trail on runner,
         14-day time exit, liquidity exit
"""
from typing import List, Dict, Optional
from dataclasses import dataclass, field
import time

CFG = {
    "entry_sol": 0.01,
    "max_open": 5,
    "max_per_day": 3,
    "cooldown_days": 7,
    "buy_slip": 0.015,
    "sell_slip": 0.015,
    "hard_stop_pct": -35.0,
    "tp_mult": 2.0,
    "tp_fraction": 0.50,
    "trail_pct": 25.0,
    "max_hold_days": 14,
    "liq_min": 15_000,
}


@dataclass
class Position:
    mint: str
    symbol: str
    entry_ts: float
    entry_price_raw: float      # observed price
    entry_price_adj: float      # * 1.015
    qty_total: float            # in SOL notional -> token qty
    qty_open: float
    peak_price: float           # for trailing
    tp_hit: bool = False
    exit_ts: Optional[float] = None
    exit_reason: str = ""
    realized_pnl: float = 0.0   # in SOL


@dataclass
class Ledger:
    positions: List[Position] = field(default_factory=list)
    closed: List[Position] = field(default_factory=list)
    cooldown_until: Dict[str, float] = field(default_factory=dict)
    entries_today: Dict[str, int] = field(default_factory=dict)  # date->count

    def _today(self, ts: float) -> str:
        import datetime
        return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d")

    def open_count(self) -> int:
        return sum(1 for p in self.positions if p.exit_ts is None)

    def can_enter(self, mint: str, ts: float) -> tuple:
        if self.open_count() >= CFG["max_open"]:
            return False, "max open positions"
        if any(p.mint == mint and p.exit_ts is None for p in self.positions):
            return False, "already open"
        if self.cooldown_until.get(mint, 0) > ts:
            return False, "cooldown"
        if self.entries_today.get(self._today(ts), 0) >= CFG["max_per_day"]:
            return False, "daily cap"
        return True, "ok"

    def enter(self, mint: str, symbol: str, ts: float,
              observed_price: float) -> Optional[Position]:
        ok, reason = self.can_enter(mint, ts)
        if not ok or observed_price <= 0:
            return None
        adj = observed_price * (1 + CFG["buy_slip"])
        qty = CFG["entry_sol"] / adj
        pos = Position(mint=mint, symbol=symbol, entry_ts=ts,
                       entry_price_raw=observed_price, entry_price_adj=adj,
                       qty_total=qty, qty_open=qty, peak_price=adj)
        self.positions.append(pos)
        day = self._today(ts)
        self.entries_today[day] = self.entries_today.get(day, 0) + 1
        return pos

    def _close(self, pos: Position, ts: float, observed_price: float,
               reason: str, fraction: float = 1.0):
        """Close fraction of position. fraction=1.0 closes all."""
        adj_sell = observed_price * (1 - CFG["sell_slip"])
        qty_close = pos.qty_open * fraction
        # pnl = (sell - entry) * qty  [in SOL terms via entry notional]
        pnl = (adj_sell - pos.entry_price_adj) * qty_close
        # scale: qty was computed as SOL/adj_entry, so pnl is in SOL
        pos.realized_pnl += pnl
        pos.qty_open -= qty_close
        if fraction >= 1.0 or pos.qty_open <= 1e-12:
            pos.exit_ts = ts
            pos.exit_reason = reason
            self.closed.append(pos)
            self.cooldown_until[pos.mint] = ts + CFG["cooldown_days"] * 86400
        return pnl

    def on_price(self, mint: str, ts: float, observed_price: float,
                 liquidity: float = 1e9, liq_drop_24h: float = 0.0,
                 safety_ok: bool = True) -> List[str]:
        """Process a price observation. Returns list of exit events."""
        events = []
        for pos in self.positions:
            if pos.exit_ts is not None or pos.mint != mint:
                continue
            if observed_price <= 0:
                continue
            # 1. Safety exit
            if not safety_ok:
                self._close(pos, ts, observed_price, "safety")
                events.append("%s SAFETY EXIT" % pos.symbol)
                continue
            # 2. Liquidity exit
            if liquidity < CFG["liq_min"] or liq_drop_24h > 0.50:
                self._close(pos, ts, observed_price, "liquidity")
                events.append("%s LIQUIDITY EXIT" % pos.symbol)
                continue
            # update peak
            if observed_price > pos.peak_price:
                pos.peak_price = observed_price
            ret_pct = 100.0 * (observed_price / pos.entry_price_adj - 1.0)
            # 3. Hard stop -35%
            if ret_pct <= CFG["hard_stop_pct"]:
                self._close(pos, ts, observed_price, "hard_stop")
                events.append("%s HARD STOP %.1f%%" % (pos.symbol, ret_pct))
                continue
            # 4. Take profit 2x -> sell 50%
            if not pos.tp_hit and observed_price >= pos.entry_price_adj * CFG["tp_mult"]:
                self._close(pos, ts, observed_price, "take_profit_2x",
                            fraction=CFG["tp_fraction"])
                pos.tp_hit = True
                events.append("%s TP 2x (half)" % pos.symbol)
                continue
            # 5. Runner trail 25% (only after TP)
            if pos.tp_hit:
                dd = 100.0 * (1.0 - observed_price / pos.peak_price)
                if dd >= CFG["trail_pct"]:
                    self._close(pos, ts, observed_price, "trail")
                    events.append("%s TRAIL -%.0f%%" % (pos.symbol, dd))
                    continue
            # 6. Time exit 14 days
            if ts - pos.entry_ts >= CFG["max_hold_days"] * 86400:
                self._close(pos, ts, observed_price, "time_exit")
                events.append("%s TIME EXIT" % pos.symbol)
        return events

    def expectancy(self, cost_mult: float = 1.0) -> Dict:
        """Net expectancy after costs. cost_mult=1.0 -> 3% round-trip
        as modeled in fills; use for stress by scaling."""
        if not self.closed:
            return {"n": 0, "expectancy": 0.0}
        pnls = [p.realized_pnl for p in self.closed]
        n = len(pnls)
        wins = [x for x in pnls if x > 0]
        return {
            "n": n,
            "net": round(sum(pnls), 6),
            "expectancy": round(sum(pnls) / n, 6),
            "win_rate": round(len(wins) / n, 3),
            "profit_factor": round(sum(wins) / abs(sum(x for x in pnls if x < 0) or 1e-9), 3),
            "max_dd": round(min(pnls), 6),
        }
