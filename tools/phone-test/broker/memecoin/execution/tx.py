"""Transaction builders for devnet execution.

Requires the ``solders`` package. All builders produce UNSIGNED
solders Transactions; signing happens in signer.py so key material
stays in one place.
"""

import base64
import logging
from typing import List

from broker import BROKER_UNAVAILABLE, BrokerError

logger = logging.getLogger("broker.memecoin.execution.tx")

try:
    from solders.hash import Hash
    from solders.instruction import Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.transaction import Transaction
    _SOLDERS = True
except ImportError:  # pragma: no cover
    _SOLDERS = False


def _require_solders() -> None:
    if not _SOLDERS:
        raise BrokerError(BROKER_UNAVAILABLE,
                          "devnet execution needs the 'solders' package "
                          "(pip install solders)")


def pubkey(s: str):
    """Parse a base58 public key, with a clean error on garbage."""
    _require_solders()
    try:
        return Pubkey.from_string(s)
    except Exception as exc:
        raise BrokerError(BROKER_UNAVAILABLE,
                          f"invalid Solana public key {s!r}") from exc


def build_transfer_tx(from_pubkey: str, to_pubkey: str,
                      lamports: int, blockhash: str):
    """Unsigned SOL transfer transaction."""
    _require_solders()
    if lamports <= 0:
        raise BrokerError(BROKER_UNAVAILABLE,
                          f"transfer amount must be positive, got {lamports}")
    src = pubkey(from_pubkey)
    dst = pubkey(to_pubkey)
    try:
        blockhash_h = Hash.from_string(blockhash)
    except Exception as exc:
        raise BrokerError(BROKER_UNAVAILABLE,
                          "invalid blockhash for transfer") from exc
    ix = transfer(TransferParams(from_pubkey=src, to_pubkey=dst,
                                 lamports=int(lamports)))
    msg = Message.new_with_blockhash([ix], src, blockhash_h)
    return Transaction.new_unsigned(msg)


def build_tx(instructions: List, payer: str, blockhash: str):
    """Unsigned transaction from arbitrary instructions."""
    _require_solders()
    if not instructions:
        raise BrokerError(BROKER_UNAVAILABLE,
                          "cannot build a transaction with no instructions")
    payer_k = pubkey(payer)
    try:
        blockhash_h = Hash.from_string(blockhash)
    except Exception as exc:
        raise BrokerError(BROKER_UNAVAILABLE,
                          "invalid blockhash for transaction") from exc
    msg = Message.new_with_blockhash(instructions, payer_k, blockhash_h)
    return Transaction.new_unsigned(msg)


def tx_to_base64(tx) -> str:
    """Serialize a SIGNED transaction for sendTransaction."""
    _require_solders()
    return base64.b64encode(bytes(tx)).decode("ascii")


def signature_str(tx) -> str:
    """Base58 signature of a signed transaction (safe to log)."""
    _require_solders()
    if not tx.is_signed() or not tx.signatures:
        raise BrokerError(BROKER_UNAVAILABLE,
                          "transaction is not signed yet")
    return str(tx.signatures[0])


# ---------------------------------------------------------------------
# Versioned (v0) transactions — Jupiter's swap API returns base64
# versioned transactions. Legacy builders above cannot represent
# them (address lookup tables, v0 message). These helpers mirror
# the legacy ones: deserialize -> sign -> serialize.
# ---------------------------------------------------------------------

# Program ids a Jupiter swap transaction is allowed to touch. Any
# other program id in a swap tx is refused before signing (the swap
# tx the API returns is trusted as built — this is the check that
# it only touches the expected programs).
JUPITER_AGGREGATOR_PROGRAM_ID = \
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
_ASSOCIATED_TOKEN_PROGRAM_ID = \
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
_SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
_COMPUTE_BUDGET_PROGRAM_ID = \
    "ComputeBudget111111111111111111111111111111"
_ADDRESS_LOOKUP_TABLE_PROGRAM_ID = \
    "AddressLookupTab1e1111111111111111111111111111111"

_ALLOWED_SWAP_PROGRAM_IDS = frozenset([
    JUPITER_AGGREGATOR_PROGRAM_ID,
    _TOKEN_PROGRAM_ID,
    _TOKEN_2022_PROGRAM_ID,
    _ASSOCIATED_TOKEN_PROGRAM_ID,
    _SYSTEM_PROGRAM_ID,
    _COMPUTE_BUDGET_PROGRAM_ID,
    _ADDRESS_LOOKUP_TABLE_PROGRAM_ID,
])


def deserialize_versioned_tx(b64_tx: str):
    """Decode a base64 versioned (v0) transaction. Raises
    BROKER_UNAVAILABLE on garbage."""
    _require_solders()
    from solders.transaction import VersionedTransaction
    try:
        raw = base64.b64decode((b64_tx or "").strip(), validate=True)
    except Exception as exc:
        raise BrokerError(BROKER_UNAVAILABLE,
                          "swap transaction is not valid base64"
                          ) from exc
    try:
        return VersionedTransaction.from_bytes(raw)
    except Exception as exc:
        raise BrokerError(BROKER_UNAVAILABLE,
                          "swap transaction is not a valid versioned "
                          "transaction") from exc


def sign_versioned_tx(vtx, keypair):
    """Sign a versioned transaction with one keypair.

    solders 0.20 has no in-place sign on VersionedTransaction —
    signing happens at construction — so this rebuilds the
    transaction from its message with the keypair (deterministic
    Ed25519: the rebuilt signature equals a constructor-signed one
    byte for byte). Returns (signed_vtx, signature_bytes)."""
    _require_solders()
    from solders.transaction import VersionedTransaction
    try:
        signed = VersionedTransaction(vtx.message, [keypair])
    except Exception as exc:
        raise BrokerError(BROKER_UNAVAILABLE,
                          "failed to sign versioned transaction"
                          ) from exc
    sigs = signed.signatures
    if not sigs:
        raise BrokerError(BROKER_UNAVAILABLE,
                          "versioned transaction has no signature "
                          "after signing")
    return signed, bytes(sigs[0])


def versioned_tx_to_base64(vtx) -> str:
    """Serialize a SIGNED versioned transaction for sendTransaction."""
    _require_solders()
    return base64.b64encode(bytes(vtx)).decode("ascii")


def versioned_signature_str(vtx) -> str:
    """Base58 signature of a signed versioned transaction."""
    _require_solders()
    sigs = vtx.signatures
    if not sigs or not sigs[0]:
        raise BrokerError(BROKER_UNAVAILABLE,
                          "versioned transaction is not signed yet")
    return str(sigs[0])


def versioned_tx_program_ids(vtx):
    """Program ids (base58) used by a versioned transaction's
    instructions."""
    _require_solders()
    out = []
    for ix in vtx.message.instructions:
        try:
            pid = ix.program_id()
        except Exception:
            pid = vtx.message.account_keys[ix.program_id_index]
        out.append(str(pid))
    return out


def versioned_tx_account_keys(vtx):
    """All account keys (base58) in a versioned transaction's
    message, in order. The first is the fee payer."""
    _require_solders()
    return [str(k) for k in vtx.message.account_keys]


def check_swap_tx(vtx, payer: str, input_mint: str,
                  output_mint: str) -> None:
    """Sanity-check a Jupiter swap transaction BEFORE signing.

    Refuses (raises BROKER_UNAVAILABLE) when:
      * the fee payer is not our wallet,
      * any instruction uses a program outside the allowlist,
      * the expected input/output mints do not appear among the
        transaction's accounts.
    Multi-hop routes may touch intermediate mints — those are not
    rejected, only the program allowlist is strict. Never raises on
    a clean tx."""
    _require_solders()
    keys = versioned_tx_account_keys(vtx)
    if not keys or keys[0] != payer:
        raise BrokerError(
            BROKER_UNAVAILABLE,
            "swap transaction fee payer is not our wallet — "
            "refusing to sign")
    for pid in versioned_tx_program_ids(vtx):
        if pid not in _ALLOWED_SWAP_PROGRAM_IDS:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "swap transaction uses unexpected program %s — "
                "refusing to sign" % pid)
    for mint in (input_mint, output_mint):
        if mint and mint not in keys:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "swap transaction does not touch expected mint %s — "
                "refusing to sign" % mint[:16])
