"""Fractional-Kelly position sizing with hard caps and a losing-streak
throttle.

Sizing is a pure function of a lane's *measured* edge: win rate, average
win and average loss, all from :class:`advanced_trader.edge.EdgeValidator`.
No measured edge, no size — ``stake_for`` returns 0.

Rules:

* Kelly fraction ``f* = (p * (b + 1) - 1) / b`` where ``p`` is win rate
  and ``b`` is avg_win / avg_loss. Only a fraction of it is bet
  (quarter-Kelly by default).
* Hard caps always apply: never more than ``max_stake`` per trade, never
  more than ``max_bankroll_pct_per_trade`` of bankroll.
* Losing-streak throttle: after ``streak_halve_after`` consecutive losses
  on a lane, stake halves. After ``streak_halt_after``, the lane halts
  and stays halted until a human calls :meth:`KellySizer.reset_streak`.
"""

from __future__ import annotations

from typing import Dict, Tuple

from .config import TraderConfig


class KellySizer:
    """Deterministic fractional-Kelly sizing with streak discipline."""

    def __init__(self, config: TraderConfig | None = None) -> None:
        self.config = config or TraderConfig()
        self._streaks: Dict[str, int] = {}

    # -- pure Kelly math ------------------------------------------------
    @staticmethod
    def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
        """Full-Kelly fraction in [0, 1]. 0.0 when there is no edge."""
        if avg_win <= 0 or avg_loss <= 0:
            return 0.0
        if not 0.0 < win_rate < 1.0:
            return 0.0
        b = avg_win / avg_loss
        f = (win_rate * (b + 1.0) - 1.0) / b
        return max(0.0, min(1.0, f))

    # -- streaks ----------------------------------------------------------
    def streak(self, lane: str) -> int:
        """Current consecutive-loss count for a lane."""
        return self._streaks.get(lane, 0)

    def on_win(self, lane: str) -> None:
        self._streaks[lane] = 0

    def on_loss(self, lane: str) -> None:
        self._streaks[lane] = self._streaks.get(lane, 0) + 1

    def reset_streak(self, lane: str) -> None:
        """Human re-enable after a streak halt."""
        self._streaks[lane] = 0

    # -- sizing -----------------------------------------------------------
    def stake_for(
        self,
        lane: str,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        bankroll: float,
    ) -> Tuple[float, bool]:
        """Return ``(stake, halted)``.

        ``halted`` is True when the lane is streak-halted (stake 0).
        Stake is always capped; it is halved once the losing streak
        reaches ``streak_halve_after``.
        """
        streak = self._streaks.get(lane, 0)
        if streak >= self.config.streak_halt_after:
            return 0.0, True
        f = self.kelly_fraction(win_rate, avg_win, avg_loss)
        if f <= 0.0 or bankroll <= 0:
            return 0.0, False
        stake = bankroll * f * self.config.kelly_fraction
        cap = min(
            self.config.max_stake,
            self.config.max_bankroll_pct_per_trade * bankroll,
        )
        stake = min(stake, cap)
        if streak >= self.config.streak_halve_after:
            stake *= 0.5
        return stake, False
