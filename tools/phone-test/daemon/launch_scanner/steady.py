#!/usr/bin/env python3
"""Steady lane: consistent-result swing trader for Solana memecoins.

Design (from 2026-10-14 consistency research):
- Swing trade mean reversion, NOT launch snipe
- $50K-$10M mcap, 60-85% off 7-day high
- Smart money cluster confirmation (3+ wallets / 30 min)
- Exits: 50% @ 1.5x, 25% @ 2x, trail 25% on rest, -25% stop, 24h max
- Risk: 1% per trade, max 3 concurrent, 3% daily loss → halt
- Consistency gate: pause if last-20 WR <40% OR fail ex-top-3 profit test

This is the "boring" edge: Gake-style nadir buying with discipline.
"""
from typing import Dict, List, Optional
from dataclasses import dataclass, field
import time

CFG = {
    # Universe
    "mcap_min": 50_000,
    "mcap_max": 10_000_000,
    "liq_min": 25_000,  # RUNNER-ONLY rule 2026-10-18: serious liquidity everywhere
                      # (fail-closed: unknown/missing liquidity → 0 → HARD BLOCK below)
    "dd_min_pct": 60.0,   # 60% off high minimum
    "dd_max_pct": 85.0,   # 85% off high maximum (research: deeper = deader)
    # Drawdown sub-zones (2026-10-15 research): escalating confirmation
    "dd_zone_standard_max": 75.0,  # 60-75%: standard confirmation
    "dd_zone_strong_max": 85.0,   # 75-85%: REQUIRES fresh smart-wallet buys
    # Panic-flush filter (2026-10-15 research): the drawdown must have
    # developed fast (panic) not slow (bleed). Max hours to reach dd_min.
    "dd_max_develop_h": 48.0,
    # Regime gate (2026-10-15 research): skip all signals when BTC 24h < -5%.
    # Buying memecoin dips in BTC selloffs is the #1 killer.
    "btc_regime_min_24h_pct": -5.0,
    # Entry
    "smart_wallets_needed": 3,  # cluster confirmation
    "smart_window_min": 30,
    "score_bar": 65,
    # Exits
    "tp1_mult": 1.5, "tp1_frac": 0.50,
    "tp2_mult": 2.0, "tp2_frac": 0.25,  # of original
    "trail_pct": 25.0,
    "hard_stop_pct": -25.0,
    "max_hold_h": 6,   # 2026-10-15 research: own data 6h+ = 1W/49L. Was 24h.
    # Risk
    "risk_per_trade_pct": 1.0,
    "max_concurrent": 3,
    "daily_loss_limit_pct": 3.0,
    # Consistency gate
    "gate_window": 20,
    "gate_min_wr": 0.40,
}

def score_steady(t: Dict) -> Dict:
    """Score 0-100 for steady lane entry."""
    score = 0
    reasons = []

    # Drawdown zone (core signal): 60-85% off 7d high
    dd = t.get("drawdown_7d_pct", 0)
    if CFG["dd_min_pct"] <= dd <= CFG["dd_max_pct"]:
        # Sweet spot 70-80% gets more
        if 70 <= dd <= 80:
            score += 35
            reasons.append(f"+35 dd={dd:.0f}% sweet spot")
        else:
            score += 25
            reasons.append(f"+25 dd={dd:.0f}%")
    else:
        return {"score": 0, "pass": False,
                "reasons": [f"dd={dd:.0f}% outside 60-85%"]}

    # Smart money cluster
    sm = t.get("smart_buyers_30m", 0)
    if sm >= CFG["smart_wallets_needed"]:
        score += 25
        reasons.append(f"+25 sm_cluster={sm}")
    elif sm >= 1:
        score += 10
        reasons.append(f"+10 sm={sm}")

    # Volume confirmation: 24h vol > $50k
    vol = t.get("vol24h", 0)
    if vol >= 50_000:
        score += 15
        reasons.append(f"+15 vol=${vol:,.0f}")

    # Liquidity depth
    liq = t.get("liquidity", 0)
    if liq >= 50_000:
        score += 10
        reasons.append(f"+10 liq=${liq:,.0f}")
    elif liq < CFG["liq_min"]:
        return {"score": 0, "pass": False,
                "reasons": reasons + ["HARD BLOCK low liq"]}

    # Not in freefall: bounce off lows
    bounce = t.get("bounce_pct", 0)
    if bounce >= 10:
        score += 10
        reasons.append(f"+10 bounce={bounce:.0f}%")
    elif bounce < 0:
        score -= 15
        reasons.append(f"-15 still falling")

    # Catalyst: dev activity or social spike
    if t.get("has_catalyst"):
        score += 5
        reasons.append("+5 catalyst")

    score = max(0, min(100, score))
    return {"score": score, "reasons": reasons,
            "pass": score >= CFG["score_bar"]}


@dataclass
class SteadyPosition:
    mint: str
    symbol: str
    entry_ts: float
    entry_price: float
    size_usd: float
    tp1_done: bool = False
    tp2_done: bool = False
    peak_price: float = 0.0
    exit_ts: Optional[float] = None
    exit_reason: str = ""
    pnl: float = 0.0
    # Fraction of the ORIGINAL size still open (1.0 at entry). Partial
    # exits decrement it; PnL is only ever banked on the fraction
    # actually closed (2026-10-24 accounting fix — see _close_frac).
    remaining: float = 1.0
    # Drawdown zone at entry (60-75 or 75-85) for sub-zone validation
    dd_zone: str = "unknown"

    def __post_init__(self):
        self.peak_price = self.entry_price


@dataclass
class SteadyLedger:
    positions: List[SteadyPosition] = field(default_factory=list)
    closed: List[SteadyPosition] = field(default_factory=list)
    daily_pnl: Dict[str, float] = field(default_factory=dict)
    # Audit trail for book corrections (e.g. the 2026-10-24 accounting
    # restatement). Preserved across saves so the record can't be lost
    # by the next scan's ledger write.
    corrections: List[Dict] = field(default_factory=list)

    def _today(self, ts: float) -> str:
        import datetime
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

    def can_enter(self, ts: float) -> tuple:
        # Max concurrent
        open_n = sum(1 for p in self.positions if p.exit_ts is None)
        if open_n >= CFG["max_concurrent"]:
            return False, "max concurrent"
        # Daily loss limit
        today = self._today(ts)
        if self.daily_pnl.get(today, 0) <= -CFG["daily_loss_limit_pct"]:
            return False, "daily loss limit"
        # Kill switch (fail closed: an uncheckable gate blocks entries)
        try:
            import killswitch
            if not killswitch.gate("steady"):
                return False, "killswitch halted"
        except Exception as e:
            return False, f"killswitch gate error: {e}"
        return True, "ok"

    def enter(self, mint: str, symbol: str, ts: float,
              price: float, portfolio_usd: float = 100.0,
              dd_zone: str = "unknown") -> Optional[SteadyPosition]:
        ok, reason = self.can_enter(ts)
        if not ok:
            return None
        if any(p.mint == mint and p.exit_ts is None for p in self.positions):
            return None
        # Exclusion-first veto engine (2026-10-16): hard vetoes apply to
        # the Steady lane too. No score can override a veto.
        try:
            from . import veto as veto_mod
            v = veto_mod.veto_check(mint, "solana", symbol, lane="steady")
            if not v["passed"]:
                return None
        except Exception:
            pass  # fail-open on veto infra error
        # Honest fill (2026-10-16): entry fills adverse vs the quote.
        try:
            from . import fills as fills_mod
            price = fills_mod.honest_entry(price, 1.0, None)
        except Exception:
            pass  # fail-open: fall back to quoted price
        size = portfolio_usd * CFG["risk_per_trade_pct"] / 100.0
        pos = SteadyPosition(mint=mint, symbol=symbol, entry_ts=ts,
                             entry_price=price, size_usd=size,
                             dd_zone=dd_zone)
        self.positions.append(pos)
        return pos

    def _close_frac(self, pos: SteadyPosition, ts: float, price: float,
                    reason: str, frac: float):
        # Honest exit fill (2026-10-16): exits fill adverse vs the quote.
        try:
            from . import fills as fills_mod
            price = fills_mod.honest_exit(price, pos.size_usd, None)
        except Exception:
            pass  # fail-open: fall back to quoted price
        # Fraction ACTUALLY closed (2026-10-24 accounting fix): partial
        # calls bank min(frac, remaining); a final close (frac >= 0.999)
        # closes only what is still open. The old code computed the final
        # leg on 100% of the ORIGINAL size even after TP1/TP2 had already
        # banked fractions of it — a TP1+trail position banked 1.5x its
        # size, inflating every partial-exit winner in the ledger.
        if frac >= 0.999:
            close_frac = pos.remaining
        else:
            close_frac = min(frac, pos.remaining)
        if close_frac <= 0:
            return
        gross = (price / pos.entry_price - 1.0) * pos.size_usd * close_frac
        # Cost: 4% round-trip (from research), allocated by fraction
        cost = pos.size_usd * 0.04 * close_frac
        pos.pnl += gross - cost
        pos.remaining = max(0.0, pos.remaining - close_frac)
        if pos.remaining <= 1e-9:
            pos.exit_ts = ts
            pos.exit_reason = reason
            self.closed.append(pos)
            # Ledger hygiene (2026-10-16): a fully-closed position leaves the
            # open list, so `positions` always means "currently open".
            # Callers iterating positions must use list(self.positions).
            self.positions = [p for p in self.positions if p is not pos]
            today = self._today(ts)
            # Daily P&L as % of PORTFOLIO for the limit check. The limit
            # (daily_loss_limit_pct) is portfolio-percent, so the tally
            # must be too — position-percent trips the limit ~100x too
            # early and blocks entries on almost every trading day
            # (2026-10-17 fix). size_usd is risk_per_trade_pct of portfolio.
            risk_frac = CFG["risk_per_trade_pct"] / 100.0
            portfolio_usd = (pos.size_usd / risk_frac
                             if risk_frac > 0 else pos.size_usd)
            self.daily_pnl[today] = self.daily_pnl.get(today, 0) + (
                pos.pnl / portfolio_usd * 100)

    def on_price(self, mint: str, ts: float, price: float) -> List[str]:
        events = []
        # Iterate a copy: _close_frac now removes fully-closed positions
        # from self.positions (2026-10-16 hygiene fix).
        for pos in list(self.positions):
            if pos.exit_ts is not None or pos.mint != mint:
                continue
            if price > pos.peak_price:
                pos.peak_price = price
            ret = 100.0 * (price / pos.entry_price - 1.0)
            age_h = (ts - pos.entry_ts) / 3600

            # Hard stop
            if ret <= CFG["hard_stop_pct"]:
                self._close_frac(pos, ts, price, "hard_stop", 1.0)
                events.append(f"{pos.symbol} STOP {ret:.1f}%")
                continue
            # TP1: 50% at 1.5x
            if not pos.tp1_done and price >= pos.entry_price * CFG["tp1_mult"]:
                self._close_frac(pos, ts, price, "tp1", CFG["tp1_frac"])
                pos.tp1_done = True
                events.append(f"{pos.symbol} TP1 1.5x (half)")
                continue
            # TP2: 25% of original at 2x (= half of what remains after
            # TP1). Was hard-coded 0.5, which banked half the ORIGINAL
            # size a second time (2026-10-24 accounting fix).
            if pos.tp1_done and not pos.tp2_done and price >= pos.entry_price * CFG["tp2_mult"]:
                self._close_frac(pos, ts, price, "tp2", CFG["tp2_frac"])
                pos.tp2_done = True
                events.append(f"{pos.symbol} TP2 2x (quarter)")
                continue
            # Trail 25% on remainder
            if pos.tp1_done:
                dd = 100.0 * (1.0 - price / pos.peak_price)
                if dd >= CFG["trail_pct"]:
                    self._close_frac(pos, ts, price, "trail", 1.0)
                    events.append(f"{pos.symbol} TRAIL -{dd:.0f}%")
                    continue
            # Time exit 24h
            if age_h >= CFG["max_hold_h"]:
                self._close_frac(pos, ts, price, "time_exit", 1.0)
                events.append(f"{pos.symbol} TIME EXIT")
        return events

    def consistency_check(self) -> Dict:
        """The consistency gate: WR >=40% AND profitable ex-top-3."""
        w = CFG["gate_window"]
        recent = self.closed[-w:]
        if len(recent) < w:
            return {"pass": True, "reason": "warming up",
                    "n": len(recent)}
        pnls = sorted([p.pnl for p in recent])
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / len(pnls)
        # Ex-top-3: remove 3 best
        ex3 = pnls[:-3] if len(pnls) > 3 else pnls
        ex3_profit = sum(ex3) > 0
        ok = wr >= CFG["gate_min_wr"] and ex3_profit
        return {
            "pass": ok,
            "n": len(recent),
            "win_rate": round(wr, 3),
            "ex_top3_profitable": ex3_profit,
            "reason": "ok" if ok else (
                f"WR {wr:.1%} < 40%" if wr < CFG["gate_min_wr"]
                else "fails ex-top-3 profit test"),
        }

    def to_dict(self) -> Dict:
        def _p(p):
            return {k: getattr(p, k) for k in (
                "mint", "symbol", "entry_ts", "entry_price", "size_usd",
                "tp1_done", "tp2_done", "peak_price", "exit_ts",
                "exit_reason", "pnl", "remaining", "dd_zone")}
        return {"positions": [_p(p) for p in self.positions],
                "closed": [_p(p) for p in self.closed],
                "daily_pnl": self.daily_pnl,
                "corrections": self.corrections}

    @classmethod
    def from_dict(cls, d: Dict) -> "SteadyLedger":
        led = cls()
        if not isinstance(d, dict):
            return led
        for key in ("positions", "closed"):
            for pd in d.get(key, []) or []:
                # Self-healing (2026-10-16): dead rows never belong in the
                # open list, even if an old save put them there.
                if key == "positions" and pd.get("exit_ts") is not None:
                    continue
                try:
                    p = SteadyPosition(
                        mint=pd["mint"], symbol=pd.get("symbol", "?"),
                        entry_ts=float(pd["entry_ts"]),
                        entry_price=float(pd["entry_price"]),
                        size_usd=float(pd.get("size_usd", 1.0)),
                        dd_zone=pd.get("dd_zone", "unknown"))
                    for k in ("tp1_done", "tp2_done", "peak_price",
                              "exit_ts", "exit_reason", "pnl"):
                        if k in pd and pd[k] is not None:
                            setattr(p, k, pd[k])
                    # Remaining fraction: take the saved value when the
                    # save has one; old saves predate the field, so derive
                    # it from the banked fractions (TP1 50%, TP2 25% of
                    # original).
                    if pd.get("remaining") is not None:
                        p.remaining = float(pd["remaining"])
                    else:
                        p.remaining = max(0.0, 1.0
                                          - (CFG["tp1_frac"] if p.tp1_done else 0.0)
                                          - (CFG["tp2_frac"] if p.tp2_done else 0.0))
                    getattr(led, key).append(p)
                except Exception:
                    continue
        led.daily_pnl = d.get("daily_pnl") or {}
        led.corrections = d.get("corrections") or []
        return led

    def bucket_stats(self) -> Dict:
        """Win rate and expectancy per drawdown zone.

        Validates the sub-zone calibration (research #4). Buckets are
        tagged on position entry via dd_zone in the signal record.
        """
        buckets = {}
        for p in self.closed:
            zone = getattr(p, "dd_zone", "unknown")
            b = buckets.setdefault(zone, {"n": 0, "wins": 0, "pnl": 0.0})
            b["n"] += 1
            if p.pnl > 0:
                b["wins"] += 1
            b["pnl"] += p.pnl
        for b in buckets.values():
            b["wr"] = round(b["wins"] / b["n"], 3) if b["n"] else 0
            b["pnl"] = round(b["pnl"], 4)
        return buckets
