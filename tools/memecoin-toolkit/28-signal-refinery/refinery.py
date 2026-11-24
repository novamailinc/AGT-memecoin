"""Signal refinery lab (synthetic, pure stdlib).

Four-chains-one-destination signal bus: schema-v1 validation (fail-closed),
source-code map, per-chain feed kill switches, tap-journal win definition.
Fail-closed: missing/invalid fields and unknown chains reject with reasons.
"""
from __future__ import annotations

from dataclasses import dataclass, field

CHAINS = ("solana", "base", "ethereum", "hyperliquid")
SOURCE_CODES = {
    "df": "degen-feed",
    "sw": "smart-watcher",
    "br": "base-runner-feed",
    "er": "eth-runner-feed",
    "hl": "hl-listing-watch",
    "ll": "locked-liq-scan",
    "hb": "heartbeat",
}
SCHEMA_VERSION = 1
REQUIRED_FIELDS = ("schema", "chain", "source", "mint", "ts", "score")

WIN_PEAK_MULT = 2.0
AUTO_PAUSE_MIN_TAPS = 10
AUTO_PAUSE_MAX_WIN_RATE = 0.20  # strictly below 20% pauses


def validate_record(rec) -> tuple:
    """Returns (ok, reasons). Fail-closed on anything missing/invalid."""
    reasons = []
    if not isinstance(rec, dict):
        return (False, ["record_not_a_dict"])
    for f in REQUIRED_FIELDS:
        if f not in rec:
            reasons.append(f"missing:{f}")
    if reasons:
        return (False, reasons)
    if rec["schema"] != SCHEMA_VERSION:
        reasons.append("bad_schema_version")
    if rec["chain"] not in CHAINS:
        reasons.append("unknown_chain")
    if rec["source"] not in SOURCE_CODES:
        reasons.append("unknown_source")
    mint = rec["mint"]
    if not isinstance(mint, str) or len(mint) < 4:
        reasons.append("bad_mint")
    ts = rec["ts"]
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or ts <= 0:
        reasons.append("bad_ts")
    score = rec["score"]
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not (0 <= score <= 100):
        reasons.append("bad_score")
    # chain/source consistency: br must be base, er ethereum, hl hyperliquid
    expected = {"br": "base", "er": "ethereum", "hl": "hyperliquid"}
    src = rec.get("source")
    if src in expected and rec.get("chain") != expected[src]:
        reasons.append("source_chain_mismatch")
    return (len(reasons) == 0, reasons)


def source_name(code: str) -> str:
    if code not in SOURCE_CODES:
        raise ValueError(f"unknown source code: {code!r}")
    return SOURCE_CODES[code]


def code_for_source(name: str) -> str:
    for k, v in SOURCE_CODES.items():
        if v == name:
            return k
    raise ValueError(f"unknown source name: {name!r}")


@dataclass
class TapOutcome:
    chain: str
    mint: str
    closed: bool
    peak_mult: float | None

    def __post_init__(self):
        if self.chain not in CHAINS:
            raise ValueError(f"unknown chain: {self.chain!r}")
        if not isinstance(self.mint, str) or not self.mint:
            raise ValueError("mint must be non-empty string")
        if self.closed and self.peak_mult is None:
            raise ValueError("closed outcome needs peak_mult")
        if self.peak_mult is not None and self.peak_mult < 0:
            raise ValueError("peak_mult must be >= 0")

    @property
    def is_win(self) -> bool:
        return bool(self.closed and self.peak_mult is not None and self.peak_mult >= WIN_PEAK_MULT)

    @property
    def is_loss(self) -> bool:
        return bool(self.closed and self.peak_mult is not None and self.peak_mult < WIN_PEAK_MULT)


@dataclass
class TapJournal:
    outcomes: list = field(default_factory=list)

    def add(self, outcome: TapOutcome) -> None:
        if not isinstance(outcome, TapOutcome):
            raise ValueError("expected TapOutcome")
        self.outcomes.append(outcome)

    def closed_for_chain(self, chain: str) -> list:
        if chain not in CHAINS:
            raise ValueError(f"unknown chain: {chain!r}")
        return [o for o in self.outcomes if o.chain == chain and o.closed]

    def tap_count(self, chain: str) -> int:
        return len(self.closed_for_chain(chain))

    def win_rate(self, chain: str) -> float | None:
        closed = self.closed_for_chain(chain)
        if not closed:
            return None
        return sum(1 for o in closed if o.is_win) / len(closed)


@dataclass
class FeedSwitches:
    """Per-chain kill switches. Solana can NEVER be paused; no auto-resume."""

    _paused: dict = field(default_factory=dict)
    _flags: list = field(default_factory=list)

    def __post_init__(self):
        if not self._paused:
            self._paused = {c: False for c in CHAINS}

    def is_paused(self, chain: str) -> bool:
        if chain not in CHAINS:
            raise ValueError(f"unknown chain: {chain!r}")
        return self._paused[chain]

    def pause(self, chain: str, *, by: str = "human") -> bool:
        """Returns True if paused. Solana pause is refused and flagged."""
        if chain not in CHAINS:
            raise ValueError(f"unknown chain: {chain!r}")
        if chain == "solana":
            self._flags.append(f"SOLANA_PAUSE_REFUSED by={by}")
            return False
        self._paused[chain] = True
        return True

    def resume(self, chain: str, *, by: str = "human") -> bool:
        """Human-only resume. by='auto' is refused."""
        if chain not in CHAINS:
            raise ValueError(f"unknown chain: {chain!r}")
        if by != "human":
            self._flags.append(f"AUTO_RESUME_REFUSED chain={chain} by={by}")
            return False
        self._paused[chain] = False
        return True

    @property
    def flags(self) -> list:
        return list(self._flags)

    def evaluate_auto_pause(self, journal: TapJournal) -> list:
        """Pause chains meeting >=10 taps AND win rate <20%. Returns paused chains."""
        paused = []
        for chain in CHAINS:
            if chain == "solana":
                continue
            if self._paused[chain]:
                continue
            n = journal.tap_count(chain)
            wr = journal.win_rate(chain)
            if n >= AUTO_PAUSE_MIN_TAPS and wr is not None and wr < AUTO_PAUSE_MAX_WIN_RATE:
                self.pause(chain, by="auto")
                paused.append(chain)
        return paused

    def publish_allowed(self, rec: dict) -> tuple:
        ok, reasons = validate_record(rec)
        if not ok:
            return (False, reasons)
        if self.is_paused(rec["chain"]):
            return (False, ["chain_paused"])
        return (True, [])
