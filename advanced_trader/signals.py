"""Multi-provider pump signals: Helius, Chainstack, Alchemy.

One interface, three adapters, zero keys in code. Each provider is
configured by the owner through environment variables (endpoint URLs
only — any API key lives in the owner's environment, never in this
code). Every adapter fails closed: a malformed payload, a missing
field, or a non-positive price yields ``None`` — no signal, no trade,
never a guess.

Design:

* :class:`Signal` — the normalized signal every provider maps into.
* :class:`SignalSource` — the adapter interface. An adapter takes a raw
  payload dict (fetched by the *owner's* transport — this package makes
  no network calls) and normalizes it, or returns ``None``.
* :class:`SignalAggregator` — merges signals by token inside a short
  corroboration window. Failover is structural: a down provider simply
  contributes nothing, the others keep flowing, and nothing ever blocks
  on a single provider. A signal confirmed by 2+ providers scores
  higher than a lone signal; a lone uncorroborated signal needs a
  stronger evidence bar (enforced in ``trader.evaluate``).

Latency: aggregation is in-memory. Signal arrival to entry decision is
measured in milliseconds; the 30s entry-age rule from the AGT design
is the outer bound, not the target.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from .config import TraderConfig


@dataclass(frozen=True)
class Signal:
    """One normalized pump signal, from any provider."""

    token: str
    deployer: str
    source: str  # "helius" | "chainstack" | "alchemy"
    received_ts: float  # seconds since epoch, set by the owner on receipt
    price: float  # quote price in SOL at signal time
    evidence_score: float = 1.0  # 0..1 from the owner's evidence filters
    raw: Optional[dict] = None  # original payload, for audit only


class SignalSource(ABC):
    """Adapter interface: raw provider payload -> normalized Signal.

    Implementations must be pure functions of the payload: no I/O, no
    network, no guessing. Return ``None`` for anything malformed.
    """

    name: str = "unknown"
    env_var: str = ""

    @classmethod
    def endpoint(cls) -> str:
        """Provider endpoint URL, from the environment. Empty = not
        configured; the owner must set it. No API keys are read here —
        if the owner's URL embeds a key, that is their environment's
        secret, never this code's."""
        return os.environ.get(cls.env_var, "")

    @abstractmethod
    def normalize(self, raw: dict, received_ts: float) -> Optional[Signal]:
        """Normalize one raw payload, or return None (fail closed)."""

    @staticmethod
    def _clean_str(value: object) -> str:
        return value.strip() if isinstance(value, str) and value.strip() else ""

    @staticmethod
    def _clean_price(value: object) -> Optional[float]:
        try:
            price = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None


class HeliusSignalAdapter(SignalSource):
    """Helius webhook / RPC payload shape.

    Expected payload (owner maps their real Helius payload onto this):
    ``{"mint": str, "deployer": str, "priceSol": number,
    "timestamp": number}``.
    """

    name = "helius"
    env_var = "HELIUS_RPC_URL"

    def normalize(self, raw: dict, received_ts: float) -> Optional[Signal]:
        if not isinstance(raw, dict):
            return None
        token = self._clean_str(raw.get("mint"))
        deployer = self._clean_str(raw.get("deployer"))
        price = self._clean_price(raw.get("priceSol"))
        if not token or price is None:
            return None
        return Signal(
            token=token,
            deployer=deployer,
            source=self.name,
            received_ts=float(received_ts),
            price=price,
            raw=raw,
        )


class ChainstackSignalAdapter(SignalSource):
    """Chainstack RPC payload shape.

    Expected payload: ``{"token": str, "deployer": str, "price": number,
    "timestamp": number}``.
    """

    name = "chainstack"
    env_var = "CHAINSTACK_RPC_URL"

    def normalize(self, raw: dict, received_ts: float) -> Optional[Signal]:
        if not isinstance(raw, dict):
            return None
        token = self._clean_str(raw.get("token"))
        deployer = self._clean_str(raw.get("deployer"))
        price = self._clean_price(raw.get("price"))
        if not token or price is None:
            return None
        return Signal(
            token=token,
            deployer=deployer,
            source=self.name,
            received_ts=float(received_ts),
            price=price,
            raw=raw,
        )


class AlchemySignalAdapter(SignalSource):
    """Alchemy webhook / RPC payload shape.

    Expected payload: ``{"mint": str, "creator": str, "price": number,
    "blockTime": number}``.
    """

    name = "alchemy"
    env_var = "ALCHEMY_RPC_URL"

    def normalize(self, raw: dict, received_ts: float) -> Optional[Signal]:
        if not isinstance(raw, dict):
            return None
        token = self._clean_str(raw.get("mint"))
        deployer = self._clean_str(raw.get("creator"))
        price = self._clean_price(raw.get("price"))
        if not token or price is None:
            return None
        return Signal(
            token=token,
            deployer=deployer,
            source=self.name,
            received_ts=float(received_ts),
            price=price,
            raw=raw,
        )


ADAPTERS: Tuple[type, ...] = (
    HeliusSignalAdapter,
    ChainstackSignalAdapter,
    AlchemySignalAdapter,
)


class SignalAggregator:
    """Merges signals by token inside a corroboration window.

    In-memory only, no I/O, no blocking: a provider that is slow or
    down simply contributes nothing. Corroboration is counted per token
    over the window; a token seen by 2+ distinct providers is
    corroborated.
    """

    def __init__(self, config: TraderConfig | None = None) -> None:
        self.config = config or TraderConfig()
        self._by_token: Dict[str, List[Signal]] = {}

    def add(self, signal: Signal) -> int:
        """Add a signal; return the current corroboration count for its
        token (distinct providers inside the window)."""
        bucket = self._by_token.setdefault(signal.token, [])
        bucket.append(signal)
        return self.corroboration(signal.token, signal.received_ts)[0]

    def corroboration(self, token: str, now_ts: float) -> Tuple[int, Set[str]]:
        """(distinct-provider count, source names) for a token in-window."""
        window = self.config.corroboration_window_s
        seen: Set[str] = set()
        for sig in self._by_token.get(token, []):
            if now_ts - sig.received_ts <= window:
                seen.add(sig.source)
        return len(seen), seen

    def latest(self, token: str, now_ts: float) -> Optional[Signal]:
        """Most recent in-window signal for a token, or None."""
        window = self.config.corroboration_window_s
        fresh = [
            s for s in self._by_token.get(token, []) if now_ts - s.received_ts <= window
        ]
        return fresh[-1] if fresh else None

    def sources_seen(self, now_ts: float) -> Set[str]:
        """Providers that delivered a signal inside the window — the
        failover view: if only one source is here, the others are down
        or silent, and trading continues on the stricter single-source
        evidence bar."""
        window = self.config.corroboration_window_s
        seen: Set[str] = set()
        for bucket in self._by_token.values():
            for sig in bucket:
                if now_ts - sig.received_ts <= window:
                    seen.add(sig.source)
        return seen

    def prune(self, now_ts: float) -> None:
        """Drop signals older than the window (call on a slow cadence,
        never in the decision hot path)."""
        window = self.config.corroboration_window_s
        for token in list(self._by_token):
            fresh = [
                s for s in self._by_token[token] if now_ts - s.received_ts <= window
            ]
            if fresh:
                self._by_token[token] = fresh
            else:
                del self._by_token[token]
