"""Transaction builders for devnet execution.

Requires the ``solders`` package. All builders produce UNSIGNED
solders Transactions; signing happens in signer.py so key material
stays in one place.
"""

import base64
import logging
from typing import List

from broker import BROKER_UNAVAILABLE, BrokerError
from daemon import solana_ids as _solana_ids

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
# Canonical program IDs live in daemon/solana_ids.py — never
# redefine the literals here (Phase 2, D-01/D-04/D-05).
_TOKEN_PROGRAM_ID = _solana_ids.TOKEN_PROGRAM_ID
_TOKEN_2022_PROGRAM_ID = _solana_ids.TOKEN2022_PROGRAM_ID
_ASSOCIATED_TOKEN_PROGRAM_ID = _solana_ids.ASSOCIATED_TOKEN_PROGRAM_ID
_SYSTEM_PROGRAM_ID = _solana_ids.SYSTEM_PROGRAM_ID
_COMPUTE_BUDGET_PROGRAM_ID = _solana_ids.COMPUTE_BUDGET_PROGRAM_ID
# NOTE (2026-11-20): this was previously misspelled with six extra
# "1"s (49 chars — impossible for a 32-byte pubkey). The wrong value
# sat harmlessly in the program allowlist (swap txs never invoke
# the ALT program) but broke the ALT owner check in
# rpc_lookup_table_resolver, which refused every real table. The
# correct value is pinned by a test against solders'
# ADDRESS_LOOKUP_TABLE_ID and the live owner string.
_ADDRESS_LOOKUP_TABLE_PROGRAM_ID = \
    _solana_ids.ADDRESS_LOOKUP_TABLE_PROGRAM_ID

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
    """All STATIC account keys (base58) in a versioned
    transaction's message, in order. The first is the fee payer.

    Static only: accounts a v0 tx loads via address lookup
    tables are NOT in this list — see
    versioned_tx_loaded_account_keys for the full set."""
    _require_solders()
    return [str(k) for k in vtx.message.account_keys]


# Address lookup table (ALT) account data, on-chain format: a
# 56-byte LookupTableMeta header, then the addresses, 32 bytes
# each, with NO length prefix (solders exposes the header size
# as LOOKUP_TABLE_META_SIZE). NOTE: solders'
# AddressLookupTable.from_bytes expects a borsh encoding and
# FAILS on real on-chain data ("unexpected end of file") —
# AddressLookupTable.deserialize is the on-chain-format parser.
# Verified against a live Jupiter swap tx (2026-11-20):
# deserialize and the manual 56-byte parse agree exactly
# (236 addresses, SOL mint present).
_ALT_META_SIZE = 56


def lookup_table_addresses_from_account_data(data: bytes):
    """Addresses stored in an ALT account (base64-decoded bytes).

    Returns a list of base58 addresses, or None when the data is
    not a well-formed lookup-table account (short, misaligned, or
    unparseable). Never raises — callers treat None as
    "unresolvable" and refuse the tx."""
    try:
        if not isinstance(data, (bytes, bytearray)):
            return None
        raw = bytes(data)
        if len(raw) < _ALT_META_SIZE + 32:
            return None
        if (len(raw) - _ALT_META_SIZE) % 32 != 0:
            return None
        from solders.address_lookup_table_account import \
            AddressLookupTable
        tbl = AddressLookupTable.deserialize(raw)
        return [str(a) for a in tbl.addresses]
    except Exception:
        return None


def rpc_lookup_table_resolver(rpc):
    """Build a lookup-table resolver from an RPC client.

    The resolver takes a table address (base58) and returns the
    addresses stored in that table (base58 list), or None when the
    table cannot be resolved (missing account, wrong owner, bad
    data, RPC failure). It never raises — check_swap_tx treats
    None as unresolvable and refuses the tx (fail-closed).

    `rpc` is any object with a `.call(method, params)` JSON-RPC
    method (MainnetRPC / DevnetRPC both qualify). One
    getAccountInfo call per table, no caching: a swap check must
    see the table as it is now."""
    def resolve(table_pubkey: str):
        try:
            result = rpc.call(
                "getAccountInfo",
                [table_pubkey, {"encoding": "base64",
                                "commitment": "confirmed"}])
            value = (result or {}).get("value")
            if not value:
                return None
            if value.get("owner") != _ADDRESS_LOOKUP_TABLE_PROGRAM_ID:
                return None
            data = value.get("data")
            if (not isinstance(data, (list, tuple)) or len(data) < 2
                    or data[1] != "base64"):
                return None
            raw = base64.b64decode(data[0], validate=True)
            return lookup_table_addresses_from_account_data(raw)
        except Exception:
            return None
    return resolve


def versioned_tx_loaded_account_keys(vtx, lookup_table_resolver=None):
    """Every account a versioned tx actually touches (base58).

    Static message keys PLUS the ALT-loaded accounts the tx's
    instructions reference (by writable/readonly index). Accounts
    stored in a table but NOT referenced by this tx are NOT
    included — the mint check must only pass for mints the tx
    really touches.

    Fail-closed: raises BROKER_UNAVAILABLE when the tx references
    a lookup table and no resolver was given, when a table cannot
    be resolved, or when a referenced index is out of range. Never
    returns a partial set."""
    _require_solders()
    keys = list(versioned_tx_account_keys(vtx))
    lookups = list(getattr(vtx.message, "address_table_lookups", None)
                   or [])
    if not lookups:
        return keys
    if lookup_table_resolver is None:
        raise BrokerError(
            BROKER_UNAVAILABLE,
            "swap transaction loads accounts via address lookup "
            "table(s) but no lookup-table resolver was provided — "
            "refusing to sign")
    for lk in lookups:
        table = str(lk.account_key)
        try:
            addresses = lookup_table_resolver(table)
        except Exception as exc:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "lookup-table resolver failed for %s — refusing "
                "to sign" % table[:16]) from exc
        if addresses is None:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "address lookup table %s cannot be resolved — "
                "refusing to sign" % table[:16])
        indexes = (list(lk.writable_indexes)
                   + list(lk.readonly_indexes))
        for idx in indexes:
            if int(idx) < 0 or int(idx) >= len(addresses):
                raise BrokerError(
                    BROKER_UNAVAILABLE,
                    "address lookup table %s index %d out of range "
                    "(%d addresses) — refusing to sign"
                    % (table[:16], int(idx), len(addresses)))
            keys.append(str(addresses[int(idx)]))
    return keys


def tx_version_from_bytes(raw: bytes):
    """Transaction version from serialized bytes, no solders.

    Versioned transactions prefix the message with 0x80|version
    (v0 -> 0x80, v1 -> 0x81, SIMD-0385); legacy transactions have
    no prefix — the first message byte is the legacy header
    (num_required_signatures). Returns {"version": 0|1|None,
    "is_versioned": bool, "is_v1": bool, "has_config": bool}.
    A v1 message carries its compute budget inline on the message
    (Message.config) by definition — that is what makes it v1 —
    so has_config is True exactly when version == 1. None when
    the bytes are too short or the prefix is unrecognized (never
    guess a version). Never raises."""
    try:
        if not isinstance(raw, (bytes, bytearray)) or len(raw) < 2:
            return {"version": None, "is_versioned": False,
                    "is_v1": False, "has_config": False}
        n_sigs = raw[0]
        # Signatures are a compact array: 1 byte here for <128.
        # (A count >= 128 uses multi-byte compact-u16; our txs
        # never have that many signers — refuse to guess.)
        if n_sigs >= 128:
            return {"version": None, "is_versioned": False,
                    "is_v1": False, "has_config": False}
        off = 1 + 64 * n_sigs
        if off >= len(raw):
            return {"version": None, "is_versioned": False,
                    "is_v1": False, "has_config": False}
        prefix = raw[off]
        if prefix & 0x80:
            version = prefix & 0x7F
            if version not in (0, 1):
                return {"version": None, "is_versioned": True,
                        "is_v1": False, "has_config": False}
            return {"version": version, "is_versioned": True,
                    "is_v1": version == 1,
                    "has_config": version == 1}
        return {"version": None, "is_versioned": False,
                "is_v1": False, "has_config": False}
    except Exception:
        return {"version": None, "is_versioned": False,
                "is_v1": False, "has_config": False}


def tx_version(vtx):
    """Version of a deserialized transaction (solders object).

    Re-serializes and reads the version prefix (see
    tx_version_from_bytes) — the solders message object itself
    does not expose the v1 config. Returns the same dict.
    Never raises."""
    try:
        return tx_version_from_bytes(bytes(vtx))
    except Exception:
        return {"version": None, "is_versioned": False,
                "is_v1": False, "has_config": False}


def compute_budget_summary(vtx):
    """ComputeBudget instructions of a versioned tx, decoded.

    Returns {"cu_limit": int|None, "cu_price_micro_lamports":
    int|None, "total_fee_lamports": int|None,
    "instruction_count": int, "account_count": int,
    "is_v1": bool}.

    The v0 model (what Jupiter builds): setComputeUnitLimit
    (discriminator 0x02, u32 LE) and setComputeUnitPrice
    (0x03, u64 LE, MICRO-lamports per CU). The TOTAL fee a v0
    tx pays is price x limit / 1e6 — the policy fee and
    Jupiter's prioritizationFeeLamports are TOTAL lamports, and
    the two must never be confused: writing a total-lamports
    figure into setComputeUnitPrice overpays by the CU limit,
    and writing a per-CU figure into a v1 Message.config
    (which takes a TOTAL, SIMD-0385) underpays by it. Our send
    path never hand-builds v1 messages — Jupiter builds v0 swap
    txs and the tip tx is legacy — so this reader exists to make
    the model explicit and to feed the simulation harness, not
    to patch a live bug. Never raises."""
    out = {"cu_limit": None, "cu_price_micro_lamports": None,
           "total_fee_lamports": None, "instruction_count": 0,
           "account_count": 0, "is_v1": False}
    try:
        _require_solders()
        msg = vtx.message
        try:
            out["instruction_count"] = len(msg.instructions)
        except Exception:
            out["instruction_count"] = 0
        try:
            out["account_count"] = len(msg.account_keys)
        except Exception:
            out["account_count"] = 0
        try:
            out["is_v1"] = bool(
                tx_version(vtx).get("is_v1"))
        except Exception:
            out["is_v1"] = False
        for ix in msg.instructions:
            try:
                pid = ix.program_id()
            except Exception:
                try:
                    pid = msg.account_keys[ix.program_id_index]
                except Exception:
                    continue
            if str(pid) != _COMPUTE_BUDGET_PROGRAM_ID:
                continue
            try:
                data = bytes(ix.data)
            except Exception:
                continue
            if len(data) < 1:
                continue
            disc = data[0]
            if disc == 2 and len(data) >= 5:
                # setComputeUnitLimit: u32 LE.
                out["cu_limit"] = int.from_bytes(
                    data[1:5], "little")
            elif disc == 3 and len(data) >= 9:
                # setComputeUnitPrice: u64 LE micro-lamports/CU.
                out["cu_price_micro_lamports"] = int.from_bytes(
                    data[1:9], "little")
        limit = out["cu_limit"]
        price = out["cu_price_micro_lamports"]
        if limit is not None and price is not None:
            out["total_fee_lamports"] = (price * limit) // 1_000_000
        return out
    except Exception:
        return out


def check_swap_tx(vtx, payer: str, input_mint: str,
                  output_mint: str, lookup_table_resolver=None) -> None:
    """Sanity-check a Jupiter swap transaction BEFORE signing.

    Refuses (raises BROKER_UNAVAILABLE) when:
      * the fee payer is not our wallet,
      * any instruction uses a program outside the allowlist,
      * the expected input/output mints do not appear among the
        transaction's accounts — static keys OR accounts loaded
        via its address lookup tables,
      * the tx uses a lookup table that cannot be resolved
        (no resolver given, table missing/unparseable, or a
        referenced index out of range) — fail-closed, never sign
        a tx whose mints cannot be verified.
    Multi-hop routes may touch intermediate mints — those are not
    rejected, only the program allowlist is strict. Never raises on
    a clean tx.

    `lookup_table_resolver` is a callable taking a table address
    (base58) and returning its addresses (base58 list) or None —
    see rpc_lookup_table_resolver. A tx with no lookup tables
    never calls it."""
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
    # Mint check over the FULL loaded account set (static keys +
    # ALT-loaded accounts the tx references). Resolving the tables
    # is fail-closed: any unresolvable table refuses the tx.
    loaded = versioned_tx_loaded_account_keys(vtx,
                                              lookup_table_resolver)
    for mint in (input_mint, output_mint):
        if mint and mint not in loaded:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "swap transaction does not touch expected mint %s — "
                "refusing to sign" % mint[:16])
