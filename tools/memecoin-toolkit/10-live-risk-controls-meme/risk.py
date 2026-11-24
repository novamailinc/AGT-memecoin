"""Live-risk controls for a paper memecoin account (lab, synthetic only).

Pure stdlib. No network. Three independent controls:

* KillSwitch   -- rolling-expectancy halt, human-only re-arm, fail-closed.
* cost_gate    -- pre-registered transaction-cost gate per chain.
* Bankroll     -- fixed $100 bankroll, $1 shots, no top-up / no reset.

Fail-closed means: whenever state is missing, corrupt, or a cost is
unknown, the safe answer is "halted" / "blocked", never "go ahead".
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------- kill switch

class KillSwitch:
    """Halt a lane when its recent paper closes have negative expectancy.

    Expectancy is ``mean(realized_mult) - 1`` over the last ``window``
    closes. The switch halts only once at least ``min_trades`` closes
    exist, so a handful of early losses cannot trip it by themselves.

    Re-arming is human-only: :meth:`rearm` requires a non-empty operator
    and reason, and every attempt (good or bad) is journaled. There is
    deliberately no automatic re-arm method.
    """

    def __init__(self, state_path=None, window: int = 30, min_trades: int = 15):
        if window <= 0 or min_trades <= 0:
            raise ValueError("window and min_trades must be positive")
        self.window = int(window)
        self.min_trades = int(min_trades)
        self.state_path = Path(state_path) if state_path is not None else None
        self.closes: list[float] = []
        self.journal: list[dict] = []
        self.halted = False
        if self.state_path is not None:
            self._load()

    # -- persistence -----------------------------------------------------
    def _load(self) -> None:
        """Load state; any missing/corrupt file leaves the switch halted."""
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            closes = data["closes"]
            if not isinstance(closes, list):
                raise ValueError("closes is not a list")
            self.closes = [float(x) for x in closes]
            self.journal = list(data.get("journal", []))
            self.halted = bool(data["halted"])
        except Exception as exc:  # missing file, bad JSON, bad shape
            self.closes = []
            self.journal = [{"event": "load_failed", "detail": str(exc)}]
            self.halted = True  # fail-closed

    def _save(self) -> None:
        if self.state_path is None:
            return
        payload = {
            "halted": self.halted,
            "closes": self.closes,
            "journal": self.journal,
            "window": self.window,
            "min_trades": self.min_trades,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    # -- core logic ------------------------------------------------------
    def expectancy(self) -> float | None:
        """Mean realised multiple minus 1 over the rolling window."""
        recent = self.closes[-self.window:]
        if not recent:
            return None
        return sum(recent) / len(recent) - 1.0

    def recent_closes(self) -> list[float]:
        """A copy of the closes inside the rolling window (oldest first)."""
        return list(self.closes[-self.window:])

    def loss_streak(self) -> int:
        """Consecutive losing closes (< 1.0x) at the end of the record."""
        streak = 0
        for value in reversed(self.closes):
            if value < 1.0:
                streak += 1
            else:
                break
        return streak

    def would_halt_if(self, realized_mult: float) -> bool:
        """Hypothetical: would one more close of this size halt the lane?

        Purely predictive -- nothing is recorded and no state changes,
        so a caller can size a decision without polluting the ledger.
        A switch that is already halted stays halted either way.
        """
        if realized_mult is None or float(realized_mult) < 0:
            raise ValueError("realized_mult must be >= 0")
        if self.halted:
            return True
        recent = (self.closes + [float(realized_mult)])[-self.window:]
        if len(recent) < self.min_trades:
            return False
        return sum(recent) / len(recent) - 1.0 < 0

    def record_close(self, realized_mult: float) -> bool:
        """Record one closed trade and re-evaluate; returns halted state."""
        if realized_mult is None or float(realized_mult) < 0:
            raise ValueError("realized_mult must be >= 0")
        self.closes.append(float(realized_mult))
        recent = self.closes[-self.window:]
        if len(recent) >= self.min_trades:
            if sum(recent) / len(recent) - 1.0 < 0:
                self.halted = True
        self._save()
        return self.halted

    def is_halted(self) -> bool:
        return self.halted

    def rearm(self, operator: str, reason: str) -> bool:
        """Human-only re-arm. Journals every attempt; never clears closes.

        Raises ValueError when operator or reason is empty/whitespace,
        so an unattended caller cannot silently re-arm the lane. Because
        closes are preserved, the next negative close can re-halt it.
        """
        ok = bool(operator and operator.strip()) and bool(
            reason and reason.strip()
        )
        self.journal.append(
            {
                "event": "rearm_attempt",
                "operator": operator if operator else "",
                "reason": reason if reason else "",
                "success": ok,
            }
        )
        self._save()
        if not ok:
            raise ValueError("rearm requires a non-empty operator and reason")
        self.halted = False
        self._save()
        return True


# ----------------------------------------------------------------- cost gate

# Per-transaction cost fixtures in USD for fixed-fee chains.
CHAIN_COSTS = {"solana": 0.001, "base": 0.03, "ethereum": 1.00}
# Hyperliquid is charged as a fraction of notional per transaction.
HYPERLIQUID_RATE = 0.00035  # 0.035%
WORST_CASE_TXS = 4  # enter, exit, plus two partial / retry transactions
COST_CAP_FRACTION = 0.25  # costs may not exceed 25% of notional


def per_tx_cost(chain: str, notional: float = 1.0) -> float:
    """Cost of one transaction on ``chain``; unknown chains raise."""
    key = str(chain).strip().lower()
    if key == "hyperliquid":
        return HYPERLIQUID_RATE * float(notional)
    if key in CHAIN_COSTS:
        return CHAIN_COSTS[key]
    raise ValueError(f"unknown chain: {chain!r}")  # fail-closed


def cost_gate(chain: str, notional: float = 1.0) -> tuple[bool, float, float]:
    """Return ``(allowed, worst_case_cost_usd, cost_pct_of_notional)``.

    Worst case is ``WORST_CASE_TXS`` transactions. Blocked if and only
    if the worst-case cost exceeds 25% of ``notional``. At $1 notional
    Ethereum costs 4 * $1.00 = $4.00 = 400%, so it is always blocked.
    Unknown chains raise ValueError rather than passing silently.
    """
    if notional <= 0:
        raise ValueError("notional must be positive")
    cost = WORST_CASE_TXS * per_tx_cost(chain, notional)
    pct = cost / float(notional) * 100.0
    allowed = cost <= COST_CAP_FRACTION * float(notional)
    return allowed, round(cost, 10), pct


def min_notional_to_pass(chain: str) -> float:
    """Smallest notional at which ``chain`` passes the cost gate.

    For a fixed-fee chain the worst-case cost is constant, so the
    break-even notional is ``worst_case_cost / COST_CAP_FRACTION``
    (Ethereum: $4.00 / 0.25 = $16.00). A percentage-fee chain whose
    rate is already under the cap passes at any positive notional,
    so the minimum is 0.0. Unknown chains raise, like the gate itself.
    """
    key = str(chain).strip().lower()
    if key == "hyperliquid":
        pct_cost = WORST_CASE_TXS * HYPERLIQUID_RATE
        return 0.0 if pct_cost <= COST_CAP_FRACTION else float("inf")
    if key in CHAIN_COSTS:
        return WORST_CASE_TXS * CHAIN_COSTS[key] / COST_CAP_FRACTION
    raise ValueError(f"unknown chain: {chain!r}")  # fail-closed


def chains_passing(notional: float = 1.0) -> list[str]:
    """Sorted names of every known chain that passes at ``notional``."""
    if notional <= 0:
        raise ValueError("notional must be positive")
    known = sorted(set(CHAIN_COSTS) | {"hyperliquid"})
    return [chain for chain in known if cost_gate(chain, notional)[0]]


# ------------------------------------------------------------------ bankroll

class Bankroll:
    """A fixed $100 paper bankroll traded in $1 shots.

    There is deliberately no ``top_up`` and no ``reset`` method: once
    the cash falls below one shot the bankroll is exhausted for good,
    so a ledger built on this class can never be quietly refilled.
    """

    START_CASH = 100.00
    SHOT = 1.00
    MAX_OPEN = 10

    def __init__(self, start: float = START_CASH):
        self.start_cash = float(start)
        self.cash = float(start)
        self.open_count = 0
        self.total_debited = 0.0
        self.total_credited = 0.0
        self.positions_settled = 0
        self._exhausted = self.cash < self.SHOT

    @property
    def state(self) -> str:
        return "bankroll_exhausted" if self._exhausted else "active"

    def can_open(self) -> bool:
        """Non-raising predicate form of :meth:`open_position`'s checks."""
        return (
            not self._exhausted
            and self.open_count < self.MAX_OPEN
            and self.cash >= self.SHOT
        )

    def shots_affordable(self) -> int:
        """Whole $1 shots the current cash alone could fund."""
        return int(self.cash // self.SHOT) if self.cash > 0 else 0

    def net_pnl(self) -> float:
        """Cash change plus the stake still out in open positions."""
        return round(
            self.cash + self.open_count * self.SHOT - self.start_cash, 10
        )

    def summary(self) -> dict:
        """Ledger snapshot: cash, open count, totals, P/L, and state."""
        return {
            "cash": self.cash,
            "open": self.open_count,
            "total_debited": round(self.total_debited, 10),
            "total_credited": round(self.total_credited, 10),
            "positions_settled": self.positions_settled,
            "net_pnl": self.net_pnl(),
            "state": self.state,
        }

    def _check_exhaustion(self) -> None:
        if self.cash < self.SHOT:
            self._exhausted = True  # permanent: never cleared anywhere

    def open_position(self) -> bool:
        """Debit one $1 shot. Raises when exhausted, full, or broke."""
        if self._exhausted:
            raise RuntimeError("bankroll exhausted")
        if self.open_count >= self.MAX_OPEN:
            raise RuntimeError("MAX_OPEN positions already open")
        if self.cash < self.SHOT:
            self._check_exhaustion()
            raise RuntimeError("insufficient cash for a $1 shot")
        self.cash = round(self.cash - self.SHOT, 10)
        self.total_debited = round(self.total_debited + self.SHOT, 10)
        self.open_count += 1
        self._check_exhaustion()
        return True

    def settle(self, realized_mult: float) -> float:
        """Close one open position, crediting ``1.00 * realized_mult``.

        Returns the amount credited. Raises when nothing is open.
        Settling still credits cash after exhaustion (the money is real
        in the ledger) but the exhausted state itself never reverses.
        """
        if realized_mult is None or float(realized_mult) < 0:
            raise ValueError("realized_mult must be >= 0")
        if self.open_count <= 0:
            raise RuntimeError("no open position to settle")
        credited = self.SHOT * float(realized_mult)
        self.open_count -= 1
        self.positions_settled += 1
        self.total_credited = round(self.total_credited + credited, 10)
        self.cash = round(self.cash + credited, 10)
        self._check_exhaustion()
        return credited
