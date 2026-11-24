"""Per-lane edge validation.

The single most important rule in this package: a lane is tradable ONLY
if its measured, out-of-sample expectancy after costs is positive. No
expectancy, no trades — the lane stays dark until the numbers earn it.

``pnl`` values passed to :meth:`EdgeValidator.record_close` must already
be net of every cost (slippage, fees). Expectancy is the mean net pnl per
closed trade.

Trust requires three things at once:

1. Enough data — at least ``edge_min_trades`` closes in the window.
2. Positive overall expectancy in the rolling window.
3. Positive expectancy on BOTH halves of a walk-forward split
   (train = first 70% of the window, test = last 30%). A lane whose edge
   has decayed fails the test half even if its history still looks good.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict, List

from .config import TraderConfig


class EdgeValidator:
    """Rolling per-lane expectancy, win rate and profit factor."""

    def __init__(self, config: TraderConfig | None = None) -> None:
        self.config = config or TraderConfig()
        self._closes: Dict[str, Deque[float]] = {}

    # -- recording ------------------------------------------------------
    def record_close(self, lane: str, pnl: float) -> None:
        """Record one closed trade's net pnl (after costs) for a lane."""
        window = self._closes.setdefault(
            lane, deque(maxlen=self.config.edge_window)
        )
        window.append(float(pnl))

    def closes(self, lane: str) -> List[float]:
        """Chronological closes in the window, oldest first."""
        return list(self._closes.get(lane, ()))

    def n_trades(self, lane: str) -> int:
        return len(self._closes.get(lane, ()))

    # -- statistics -----------------------------------------------------
    @staticmethod
    def _mean(values: List[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    def expectancy(self, lane: str) -> float:
        """Mean net pnl per closed trade. 0.0 with no data."""
        return self._mean(self.closes(lane))

    def win_rate(self, lane: str) -> float:
        """Fraction of closes with pnl > 0. 0.0 with no data."""
        closes = self.closes(lane)
        if not closes:
            return 0.0
        return sum(1 for p in closes if p > 0) / len(closes)

    def avg_win(self, lane: str) -> float:
        """Mean pnl of winning closes. 0.0 if there are none."""
        wins = [p for p in self.closes(lane) if p > 0]
        return self._mean(wins)

    def avg_loss(self, lane: str) -> float:
        """Mean |pnl| of losing closes. 0.0 if there are none."""
        losses = [abs(p) for p in self.closes(lane) if p <= 0]
        return self._mean(losses)

    def profit_factor(self, lane: str) -> float:
        """Gross wins / gross losses. ``inf`` with wins and no losses,
        0.0 with no wins."""
        wins = sum(p for p in self.closes(lane) if p > 0)
        losses = sum(abs(p) for p in self.closes(lane) if p <= 0)
        if losses == 0:
            return float("inf") if wins > 0 else 0.0
        return wins / losses

    # -- trust ----------------------------------------------------------
    def _walk_forward(self, lane: str) -> tuple[float, float]:
        """(train expectancy, test expectancy) on a 70/30 chronological
        split of the window. Returns (0.0, 0.0) with too little data."""
        closes = self.closes(lane)
        if len(closes) < self.config.edge_min_trades:
            return 0.0, 0.0
        split = int(len(closes) * 0.7)
        return self._mean(closes[:split]), self._mean(closes[split:])

    def is_tradable(self, lane: str) -> bool:
        """True only if the lane has enough data AND positive expectancy
        overall AND positive on both walk-forward halves."""
        if self.n_trades(lane) < self.config.edge_min_trades:
            return False
        floor = self.config.edge_min_expectancy
        if self.expectancy(lane) <= floor:
            return False
        train, test = self._walk_forward(lane)
        return train > floor and test > floor

    # -- reporting ------------------------------------------------------
    def lane_stats(self, lane: str) -> Dict[str, float | bool | int]:
        train, test = self._walk_forward(lane)
        return {
            "n_trades": self.n_trades(lane),
            "expectancy": round(self.expectancy(lane), 6),
            "win_rate": round(self.win_rate(lane), 4),
            "avg_win": round(self.avg_win(lane), 6),
            "avg_loss": round(self.avg_loss(lane), 6),
            "profit_factor": (
                round(self.profit_factor(lane), 4)
                if self.profit_factor(lane) != float("inf")
                else float("inf")
            ),
            "train_expectancy": round(train, 6),
            "test_expectancy": round(test, 6),
            "tradable": self.is_tradable(lane),
        }

    def report(self) -> Dict[str, Dict[str, float | bool | int]]:
        """Per-lane stats for every lane with recorded closes."""
        return {lane: self.lane_stats(lane) for lane in sorted(self._closes)}
