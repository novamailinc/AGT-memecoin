"""Risk governor — the layer the trader cannot bypass.

Daily loss limits, the drawdown circuit breaker and exposure caps live
here, in a separate object from the decision code. The trader must ask
this governor before every entry, and there is no code path that opens
a position without its approval. A bad decision may lose a capped
stake; it may never breach a limit.

Rules:

* A lane halts for the day once its net daily loss reaches
  ``daily_loss_limit_lane``; all trading halts at
  ``daily_loss_limit_global``. Limits reset on a new day (the owner
  passes the day string, e.g. ``"2026-11-24"``).
* Drawdown breaker: if current equity falls ``max_drawdown_pct`` below
  peak equity, all trading halts until a human calls
  :meth:`RiskGovernor.reset_breaker`.
* Exposure: maximum open stake per token and per deployer, and a cap on
  simultaneous open positions.
* Closes are risk-reducing and are never blocked by this governor.
* A lane can be halted for any reason (e.g. the kill-switch hook when
  its rolling expectancy turns negative) and re-enabled only by a human
  via :meth:`RiskGovernor.reenable_lane`.
"""

from __future__ import annotations

from typing import Dict, Tuple

from .config import TraderConfig


class RiskGovernor:
    """Separate enforcement layer for all risk limits."""

    def __init__(
        self, config: TraderConfig | None = None, start_bankroll: float = 0.0
    ) -> None:
        self.config = config or TraderConfig()
        self._peak_equity = float(start_bankroll)
        self._daily_lane: Dict[Tuple[str, str], float] = {}
        self._daily_global: Dict[str, float] = {}
        self._halted_lanes: Dict[str, str] = {}
        self._breaker_tripped = False
        self._open: Dict[int, Dict[str, float | str]] = {}
        self._token_exposure: Dict[str, float] = {}
        self._deployer_exposure: Dict[str, float] = {}

    # -- halts ----------------------------------------------------------
    def halt_lane(self, lane: str, reason: str) -> None:
        """Halt a lane. Human re-enable only."""
        self._halted_lanes[lane] = reason

    def reenable_lane(self, lane: str) -> None:
        self._halted_lanes.pop(lane, None)

    def is_lane_halted(self, lane: str) -> bool:
        return lane in self._halted_lanes

    def halt_reason(self, lane: str) -> str:
        return self._halted_lanes.get(lane, "")

    def reset_breaker(self, current_equity: float | None = None) -> None:
        """Human re-enable after the drawdown breaker trips. If the
        current equity is given, it becomes the new peak baseline — the
        human accepts the drawdown and trading resumes from there.
        Without it, the flag clears but the gate re-trips while equity
        is still below the drawdown threshold (correct: still halted)."""
        self._breaker_tripped = False
        if current_equity is not None:
            self._peak_equity = float(current_equity)

    def breaker_tripped(self) -> bool:
        return self._breaker_tripped

    # -- entry gate -----------------------------------------------------
    def allow_entry(
        self,
        lane: str,
        token: str,
        deployer: str,
        stake: float,
        bankroll: float,
        day: str,
        open_count: int,
    ) -> Tuple[bool, str]:
        """The single gate every entry must pass. Returns ``(ok, reason)``.

        ``reason`` is ``"ok"`` on approval, otherwise one of
        ``"drawdown_breaker"``, ``"lane_halted"``, ``"daily_loss_lane"``,
        ``"daily_loss_global"``, ``"max_positions"``,
        ``"token_exposure"`` or ``"deployer_exposure"``.
        """
        cfg = self.config
        if self._breaker_tripped:
            return False, "drawdown_breaker"
        if lane in self._halted_lanes:
            return False, "lane_halted"
        if -self._daily_lane.get((day, lane), 0.0) >= cfg.daily_loss_limit_lane:
            return False, "daily_loss_lane"
        if -self._daily_global.get(day, 0.0) >= cfg.daily_loss_limit_global:
            return False, "daily_loss_global"
        if self._peak_equity > 0 and bankroll < self._peak_equity:
            dd = (self._peak_equity - bankroll) / self._peak_equity
            if dd >= cfg.max_drawdown_pct:
                self._breaker_tripped = True
                return False, "drawdown_breaker"
        if open_count >= cfg.max_open_positions:
            return False, "max_positions"
        if self._token_exposure.get(token, 0.0) + stake > cfg.max_exposure_per_token:
            return False, "token_exposure"
        if (
            self._deployer_exposure.get(deployer, 0.0) + stake
            > cfg.max_exposure_per_deployer
        ):
            return False, "deployer_exposure"
        return True, "ok"

    # -- bookkeeping ------------------------------------------------------
    def record_open(
        self, ticket: int, token: str, deployer: str, stake: float
    ) -> None:
        self._open[int(ticket)] = {
            "token": token,
            "deployer": deployer,
            "stake": float(stake),
        }
        self._token_exposure[token] = (
            self._token_exposure.get(token, 0.0) + float(stake)
        )
        self._deployer_exposure[deployer] = (
            self._deployer_exposure.get(deployer, 0.0) + float(stake)
        )

    def record_close(
        self,
        ticket: int,
        pnl: float,
        lane: str,
        day: str,
        equity_after: float,
    ) -> None:
        """Release exposure, book daily pnl, track peak equity, and trip
        the drawdown breaker if the limit is breached."""
        key = (day, lane)
        self._daily_lane[key] = self._daily_lane.get(key, 0.0) + float(pnl)
        self._daily_global[day] = self._daily_global.get(day, 0.0) + float(pnl)
        if equity_after > self._peak_equity:
            self._peak_equity = float(equity_after)
        elif self._peak_equity > 0:
            dd = (self._peak_equity - float(equity_after)) / self._peak_equity
            if dd >= self.config.max_drawdown_pct:
                self._breaker_tripped = True
        info = self._open.pop(int(ticket), None)
        if info is not None:
            token = str(info["token"])
            deployer = str(info["deployer"])
            stake = float(info["stake"])
            self._token_exposure[token] = max(
                0.0, self._token_exposure.get(token, 0.0) - stake
            )
            self._deployer_exposure[deployer] = max(
                0.0, self._deployer_exposure.get(deployer, 0.0) - stake
            )

    # -- reporting --------------------------------------------------------
    def daily_pnl(self, day: str, lane: str | None = None) -> float:
        if lane is None:
            return self._daily_global.get(day, 0.0)
        return self._daily_lane.get((day, lane), 0.0)

    def peak_equity(self) -> float:
        return self._peak_equity

    def open_positions(self) -> int:
        return len(self._open)
