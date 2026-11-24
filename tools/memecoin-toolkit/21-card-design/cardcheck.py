"""Signal-card completeness validator (synthetic lab).

Pure stdlib, no network.  A signal card is the message a human is
about to gamble real money on, so its contract is strict: no bare
contract addresses, ever.  Every card must carry market cap / FDV,
liquidity, a context line, a safety status line, a valid CA, a
copy-CA button marker, a total-supply line, chain provenance, and a
per-chain exit hint.  Pick cards must additionally carry the
'📍 I bought this' track button, so the tap that journals the trade
is always there.

``validate(card)`` returns a list of problems; an empty list means
valid.  Each problem names exactly the missing/broken element, so a
fixture missing one thing fails with exactly that one problem.
Cards may be dicts or ``Card`` dataclass instances.
"""
from __future__ import annotations

from dataclasses import dataclass, field

BASE58_ALPHABET = frozenset(
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
)
CHAINS = ("solana", "base", "ethereum", "hyperliquid")
COPY_CA_MARKER = "📋 Copy CA"
TRACK_MARKER = "📍 I bought this"
# Words that make an exit hint chain-specific rather than generic.
_CHAIN_HINT_WORDS = {
    "solana": ("solana", "sol", "raydium", "pumpswap", "pump.fun"),
    "base": ("base", "aerodrome", "coinbase"),
    "ethereum": ("ethereum", "eth", "gas", "uniswap"),
    "hyperliquid": ("hyperliquid", "hl", "mark price", "perp"),
}

P_MCAP = "missing mcap/FDV"
P_LIQ = "missing liquidity"
P_CONTEXT = "missing context line"
P_SAFETY = "missing safety status line"
P_CA_MISSING = "missing CA"
P_CA_INVALID = "invalid CA"
P_COPY = "missing copy-CA button"
P_SUPPLY = "missing total supply line"
P_PROVENANCE = "missing chain provenance"
P_EXIT = "missing per-chain exit hint"
P_EXIT_WRONG_CHAIN = "exit hint does not match chain"
P_TRACK = "missing 📍 I bought this track button"
P_CHAIN = "missing chain provenance"  # alias: unknown chain is a provenance failure


@dataclass
class Card:
    chain: str = ""
    mint: str = ""
    mcap_usd: float | None = None
    fdv_usd: float | None = None
    liquidity_usd: float | None = None
    context_line: str = ""
    safety_line: str = ""
    supply_line: str = ""
    provenance_line: str = ""
    exit_hint: str = ""
    buttons: list = field(default_factory=list)
    is_pick: bool = False


def _get(card, name, default=None):
    if isinstance(card, dict):
        return card.get(name, default)
    return getattr(card, name, default)


def is_valid_ca(ca) -> bool:
    """Synthetic mint check: base58 charset, 32-44 chars."""
    if not isinstance(ca, str):
        return False
    if not (32 <= len(ca) <= 44):
        return False
    return all(ch in BASE58_ALPHABET for ch in ca)


def _positive(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _buttons(card) -> list:
    buttons = _get(card, "buttons", []) or []
    if isinstance(buttons, str):
        return [buttons]
    return list(buttons)


def has_copy_button(card) -> bool:
    return any(COPY_CA_MARKER in str(b) for b in _buttons(card))


def has_track_button(card) -> bool:
    return any(TRACK_MARKER in str(b) for b in _buttons(card))


def exit_hint_matches_chain(chain, exit_hint) -> bool:
    if chain not in _CHAIN_HINT_WORDS or not _nonempty(exit_hint):
        return False
    text = exit_hint.lower()
    return any(word in text for word in _CHAIN_HINT_WORDS[chain])


def validate(card) -> list:
    """List of problems with the card; [] means the card is valid."""
    if card is None:
        raise ValueError("card must not be None")
    problems = []

    chain = _get(card, "chain", "")
    chain_ok = chain in CHAINS
    provenance_ok = chain_ok and _nonempty(_get(card, "provenance_line", ""))
    if not provenance_ok:
        problems.append(P_PROVENANCE)

    if not (_positive(_get(card, "mcap_usd")) or _positive(_get(card, "fdv_usd"))):
        problems.append(P_MCAP)
    if not _positive(_get(card, "liquidity_usd")):
        problems.append(P_LIQ)
    if not _nonempty(_get(card, "context_line", "")):
        problems.append(P_CONTEXT)
    if not _nonempty(_get(card, "safety_line", "")):
        problems.append(P_SAFETY)

    ca = _get(card, "mint", None)
    if ca is None or (isinstance(ca, str) and not ca.strip()):
        # Also accept a 'ca' key on dict cards.
        ca = _get(card, "ca", None)
    if ca is None or (isinstance(ca, str) and not ca.strip()):
        problems.append(P_CA_MISSING)
    elif not is_valid_ca(ca):
        problems.append(P_CA_INVALID)

    if not has_copy_button(card):
        problems.append(P_COPY)
    if not _nonempty(_get(card, "supply_line", "")):
        problems.append(P_SUPPLY)

    exit_hint = _get(card, "exit_hint", "")
    if not _nonempty(exit_hint):
        problems.append(P_EXIT)
    elif chain_ok and not exit_hint_matches_chain(chain, exit_hint):
        problems.append(P_EXIT_WRONG_CHAIN)

    if _get(card, "is_pick", False) and not has_track_button(card):
        problems.append(P_TRACK)

    return problems


def is_valid(card) -> bool:
    return validate(card) == []


def required_fields() -> tuple:
    return (
        "mcap/FDV", "liquidity", "context line", "safety status line",
        "CA", "copy-CA button", "total supply line", "chain provenance",
        "per-chain exit hint",
    )


def summarize(card) -> dict:
    problems = validate(card)
    return {
        "valid": not problems,
        "problems": problems,
        "chain": _get(card, "chain", ""),
        "is_pick": bool(_get(card, "is_pick", False)),
    }
