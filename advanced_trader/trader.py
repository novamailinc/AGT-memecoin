"""AdvancedTrader — the orchestrator.

Ties the decision layer together. All dependencies are injected, never
hardcoded:

* quotes arrive as arguments (``evaluate`` / ``manage`` take the quote) —
  the owner wires their own feed;
* journaling goes through an injected callable, or an in-memory list
  when none is supplied;
* the ticket counter persists to the file the config names.

The hot path is pure and I/O-free:

* :meth:`AdvancedTrader.evaluate` — candidate in, enter/reject out.
* :meth:`AdvancedTrader.manage` — position + quote in, exit actions out.

Ticket allocation (file I/O) happens only in :meth:`AdvancedTrader.enter`,
on buy, never inside a decision.

Decision order in ``evaluate`` (fixed, no discretion):

1. Risk governor pre-check (limits cannot be bypassed).
2. Edge check — lane tradable only on positive out-of-sample expectancy.
2b. Evidence bar — corroborated (2+ provider) signals need a lower
    evidence score than lone, uncorroborated signals.
3. Price guard — a bad quote can never become an entry.
4. Kelly sizing — stake from measured edge, capped, streak-throttled.
4b. Experience weight — losing patterns down-weighted and eventually
    blocked; winning patterns earn more size (inside the caps).
5. Risk governor final gate with the real stake, then ``enter`` re-checks
   once more before allocating a ticket.

``manage`` routes a bad quote to the dead-quote path (only the time-stop
write-off can fire there), so a 5000x spike can never trigger a leg sale
and a dead feed can never leave a position open past its time stop.

``on_close`` is where the loop learns: it records the close in the edge
validator, updates the sizing streak, books the pnl in the risk
governor, and fires the kill switch (halt the lane) if the lane's
rolling expectancy has turned negative.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .config import TraderConfig
from .edge import EdgeValidator
from .exits import ExitManager, new_position
from .experience import ExperienceStore
from .risk import RiskGovernor
from .sanity import PriceGuard
from .sizing import KellySizer
from .tickets import TicketAllocator


class AdvancedTrader:
    """Deterministic, paper-only trading brain. No I/O in the hot path."""

    def __init__(
        self,
        config: TraderConfig | None = None,
        journal: Callable[[Dict[str, Any]], None] | None = None,
        ticket_state_file: str | None = None,
        experience_state_file: str | None = None,
    ) -> None:
        self.config = config or TraderConfig()
        self.edge = EdgeValidator(self.config)
        self.sizer = KellySizer(self.config)
        self.exits = ExitManager(self.config)
        self.risk = RiskGovernor(self.config, self.config.bankroll_start)
        self.guard = PriceGuard(self.config)
        self.experience = ExperienceStore(
            experience_state_file or self.config.experience_state_file,
            self.config,
        )
        self.tickets = TicketAllocator(
            ticket_state_file or self.config.ticket_state_file,
            self.config.ticket_start,
        )
        self._journal_fn = journal
        self._journal_mem: List[Dict[str, Any]] = []
        self.rejected: Dict[str, int] = {}
        self._open: Dict[int, Dict[str, Any]] = {}

    # -- journal ----------------------------------------------------------
    def _emit(self, event: Dict[str, Any]) -> None:
        if self._journal_fn is not None:
            self._journal_fn(event)
        else:
            if len(self._journal_mem) >= 10000:
                self._journal_mem.pop(0)
            self._journal_mem.append(event)

    def journal_events(self) -> List[Dict[str, Any]]:
        """In-memory journal (only when no journal callable was injected)."""
        return list(self._journal_mem)

    def open_positions(self) -> Dict[int, Dict[str, Any]]:
        return dict(self._open)

    # -- entry ------------------------------------------------------------
    def evaluate(
        self, candidate: Dict[str, Any], bankroll: float, day: str
    ) -> Dict[str, Any]:
        """Decide on a candidate. Pure: no I/O, no randomness.

        ``candidate`` needs ``lane``, ``token``, ``quote``; optional
        ``deployer`` and ``entry_price`` (defaults to ``quote``).
        Returns ``{"decision": "enter", "stake": ..., ...}`` or
        ``{"decision": "reject", "reason": ...}``.
        """
        lane = str(candidate["lane"])
        token = str(candidate["token"])
        deployer = str(candidate.get("deployer", ""))
        quote = candidate.get("quote")
        entry_price = float(candidate.get("entry_price", quote if quote else 0.0))

        def _reject(reason: str) -> Dict[str, Any]:
            self.rejected[reason] = self.rejected.get(reason, 0) + 1
            return {"decision": "reject", "reason": reason, "lane": lane, "token": token}

        # 1. Risk governor pre-check (stake unknown yet; exposure is
        #    re-checked with the real stake in step 5 and in enter()).
        ok, reason = self.risk.allow_entry(
            lane, token, deployer, 0.0, bankroll, day, len(self._open)
        )
        if not ok:
            return _reject(reason)

        # 2. Edge check — no measured edge, no trade.
        if not self.edge.is_tradable(lane):
            return _reject("no_edge")

        # 2b. Evidence bar — a pump signal confirmed by 2+ providers
        #     needs a lower evidence score than a lone, uncorroborated
        #     signal. Corroboration is computed by the owner's
        #     SignalAggregator and passed in as ``corroboration``.
        corroboration = int(candidate.get("corroboration", 1))
        evidence = float(candidate.get("evidence_score", 1.0))
        bar = (
            self.config.min_evidence_corroborated
            if corroboration >= 2
            else self.config.min_evidence_single
        )
        if evidence < bar:
            return _reject("weak_evidence")

        # 3. Price guard — a bad quote can never become an entry.
        price_ok, guard_reason = self.guard.check(quote, entry_price)
        if not price_ok:
            return _reject(guard_reason)

        # 4. Kelly sizing from measured edge.
        stake, streak_halted = self.sizer.stake_for(
            lane,
            self.edge.win_rate(lane),
            self.edge.avg_win(lane),
            self.edge.avg_loss(lane),
            bankroll,
        )
        if streak_halted:
            return _reject("streak_halt")
        if stake <= 0:
            return _reject("stake_zero")

        # 4b. Experience weight — losing patterns are down-weighted and
        #     eventually blocked; winning patterns earn more size (still
        #     inside the Kelly caps, applied below).
        exp_keys = {
            "source": str(candidate.get("source", "")),
            "deployer": deployer,
            "lane": lane,
            "pattern": str(candidate.get("pattern", "")),
        }
        weight = self.experience.weight_for(exp_keys)
        if weight <= 0.0:
            return _reject("experience_blocked")
        stake = min(
            stake * weight,
            self.config.max_stake,
            self.config.max_bankroll_pct_per_trade * bankroll,
        )

        # 5. Risk governor final gate with the real stake.
        ok, reason = self.risk.allow_entry(
            lane, token, deployer, stake, bankroll, day, len(self._open)
        )
        if not ok:
            return _reject(reason)

        return {
            "decision": "enter",
            "stake": stake,
            "lane": lane,
            "token": token,
            "deployer": deployer,
            "entry_price": entry_price,
            "quote": quote,
            "experience_weight": weight,
            "corroboration": corroboration,
            "evidence_score": evidence,
        }

    def enter(
        self, decision: Dict[str, Any], day: str, now_ts: float, bankroll: float
    ) -> Optional[Dict[str, Any]]:
        """Open a position from an ``enter`` decision.

        Re-checks the risk governor, then allocates a ticket (the only
        file I/O in the trading path). Raises if the ticket cannot be
        persisted — fail-closed, no position is opened. Returns None if
        the decision was not an entry or the re-check fails.
        """
        if decision.get("decision") != "enter":
            return None
        lane = str(decision["lane"])
        token = str(decision["token"])
        deployer = str(decision.get("deployer", ""))
        stake = float(decision["stake"])
        ok, reason = self.risk.allow_entry(
            lane,
            token,
            deployer,
            stake,
            bankroll,
            day,
            len(self._open),
        )
        if not ok:
            self.rejected[reason] = self.rejected.get(reason, 0) + 1
            return None
        ticket = self.tickets.next_ticket()  # persists first; raises on failure
        position = new_position(
            ticket,
            token,
            float(decision["entry_price"]),
            now_ts,
            stake,
            deployer=deployer,
            lane=lane,
        )
        self._open[ticket] = position
        self.risk.record_open(ticket, token, deployer, stake)
        self._emit(
            {
                "event": "position.opened",
                "ticket": ticket,
                "lane": lane,
                "token": token,
                "entry_price": float(decision["entry_price"]),
                "stake": stake,
                "ts": now_ts,
            }
        )
        return position

    # -- management -------------------------------------------------------
    def manage(
        self,
        position: Dict[str, Any],
        quote: Optional[float],
        now_ts: float,
    ) -> List[Dict[str, Any]]:
        """Decide exits for a position. Pure: no I/O.

        A bad quote is routed to the dead-quote path: only the time-stop
        write-off can fire there, so a spike can never trigger a leg sale
        and a dead feed can never strand a position past its time stop.
        """
        if position.get("closed"):
            return []
        if quote is not None:
            ok, reason = self.guard.check(quote, float(position["entry_price"]))
            if not ok:
                self._emit(
                    {
                        "event": "quote_rejected",
                        "ticket": position.get("ticket"),
                        "token": position.get("token"),
                        "reason": reason,
                        "quote": quote,
                        "ts": now_ts,
                    }
                )
                return self.exits.manage(position, None, now_ts)
        return self.exits.manage(position, quote, now_ts)

    # -- closes -----------------------------------------------------------
    def on_close(self, fill: Dict[str, Any]) -> None:
        """Book a completed close. Updates edge stats, sizing streak,
        risk governor, and fires the kill switch if the lane's rolling
        expectancy has turned negative.

        ``fill`` needs ``ticket``, ``lane``, ``pnl``, ``day`` and
        ``equity_after``; ``token`` and ``deployer`` release exposure.
        """
        lane = str(fill["lane"])
        pnl = float(fill["pnl"])
        ticket = int(fill["ticket"])
        self.edge.record_close(lane, pnl)
        if pnl > 0:
            self.sizer.on_win(lane)
        else:
            self.sizer.on_loss(lane)
        # Experience learns ONLY from closed, journaled trades.
        self.experience.record(
            {
                "pnl": pnl,
                "lane": lane,
                "source": fill.get("source", ""),
                "deployer": fill.get("deployer", ""),
                "pattern": fill.get("pattern", ""),
                "hold_seconds": fill.get("hold_seconds"),
            }
        )
        self.risk.record_close(
            ticket, pnl, lane, str(fill["day"]), float(fill["equity_after"])
        )
        self._open.pop(ticket, None)
        self._emit(
            {
                "event": "position.closed",
                "ticket": ticket,
                "lane": lane,
                "token": fill.get("token"),
                "pnl": pnl,
                "reason": fill.get("reason", ""),
                "write_off": bool(fill.get("write_off", False)),
            }
        )
        # Kill switch: negative rolling expectancy halts the lane.
        if (
            self.edge.n_trades(lane) >= self.config.edge_min_trades
            and self.edge.expectancy(lane) <= self.config.edge_min_expectancy
            and not self.risk.is_lane_halted(lane)
        ):
            self.risk.halt_lane(lane, "negative_expectancy")
            self._emit(
                {
                    "event": "lane_halted",
                    "lane": lane,
                    "reason": "negative_expectancy",
                    "expectancy": self.edge.expectancy(lane),
                }
            )
