"""Account decoding — pure stdlib.

Decodes a getAccountInfo value: owner, lamports, data layout,
rent-exempt state. Data may arrive base64, base58 or jsonParsed
encoded. When the owner is a token program the bytes are further
decoded by token_decoder; when the owner is the address lookup
table program the stored addresses are parsed (56-byte
LookupTableMeta header + 32-byte addresses — the same on-chain
format broker/memecoin/execution/tx.py parses, verified there
against solders' AddressLookupTable.deserialize on a live Jupiter
swap tx).

Rent-exempt state uses the Solana rent formula with the live
network constant (128-byte account storage overhead):
    minimum = (128 + data_len) * lamports_per_byte
lamports_per_byte is a MOVING network parameter, not a constant:
SIMD-0437 cut it from 6,960 to 5,080 in steps (step 2 live on
mainnet since 2026-09-11, epoch 1033; further steps to 696 were
still pending as of 2026-11-02). The default here is the verified
live rate at capture time, 5,080 — confirmed twice: by the
published step-2 rate and by five accounts in the real NOTAI
creation transaction, each funded at exactly (128 + space) * 5080
(mint 234B -> 1,838,960; curve 166B -> 1,493,520; token accounts
170B -> 1,513,840; program account 137B -> 1,346,200). Callers
that need the authoritative figure for a later rate should pass
lamports_per_byte explicitly (or query
getMinimumBalanceForRentExemption); an old account funded at
6,960 still holds the larger deposit and reads rent-exempt under
either rate.
"""
import base64

from ._base58 import b58decode, b58encode
from . import token_decoder
from daemon import solana_ids as _solana_ids

DECODER_VERSION = "1.0.0"

# Canonical program IDs live in daemon/solana_ids.py — never
# redefine the literals here (Phase 2, D-01/D-04/D-05).
SYSTEM_PROGRAM_ID = _solana_ids.SYSTEM_PROGRAM_ID
TOKEN_PROGRAM_ID = _solana_ids.TOKEN_PROGRAM_ID
TOKEN_2022_PROGRAM_ID = _solana_ids.TOKEN2022_PROGRAM_ID
ADDRESS_LOOKUP_TABLE_PROGRAM_ID = _solana_ids.ADDRESS_LOOKUP_TABLE_PROGRAM_ID

KNOWN_OWNERS = {
    SYSTEM_PROGRAM_ID: "system",
    TOKEN_PROGRAM_ID: "token",
    TOKEN_2022_PROGRAM_ID: "token_2022",
    ADDRESS_LOOKUP_TABLE_PROGRAM_ID: "address_lookup_table",
}

# Solana rent constants (see module docstring). LAMPORTS_PER_BYTE
# is the SIMD-0437 step-2 rate, live on mainnet since 2026-09-11.
ACCOUNT_STORAGE_OVERHEAD = 128
LAMPORTS_PER_BYTE = 5080

# Address lookup table on-chain format (see module docstring).
_ALT_META_SIZE = 56


def rent_exempt_minimum(data_len, lamports_per_byte=LAMPORTS_PER_BYTE):
    """Minimum lamports for rent exemption at `data_len` bytes.

    `lamports_per_byte` defaults to the verified live SIMD-0437
    step-2 rate (5,080); pass the current network rate explicitly
    once a later step activates.
    """
    return (ACCOUNT_STORAGE_OVERHEAD + int(data_len)) \
        * int(lamports_per_byte)


def is_rent_exempt(lamports, data_len,
                   lamports_per_byte=LAMPORTS_PER_BYTE):
    """True when `lamports` covers the rent-exempt minimum."""
    try:
        return int(lamports) >= rent_exempt_minimum(
            data_len, lamports_per_byte)
    except (TypeError, ValueError):
        return None


def lookup_table_addresses_from_account_data(data):
    """Addresses stored in an ALT account (raw bytes).

    Returns a list of base58 addresses, or None when the data is
    not a well-formed lookup-table account (short, misaligned).
    Never raises. This is the stdlib twin of
    broker/memecoin/execution/tx.py::lookup_table_addresses_from_account_data
    (which uses solders); the manual 56-byte-header parse is what
    that module verified against solders' deserialize.
    """
    try:
        if not isinstance(data, (bytes, bytearray)):
            return None
        raw = bytes(data)
        if len(raw) < _ALT_META_SIZE + 32:
            return None
        if (len(raw) - _ALT_META_SIZE) % 32 != 0:
            return None
        return [b58encode(raw[_ALT_META_SIZE + 32 * i:
                              _ALT_META_SIZE + 32 * (i + 1)])
                for i in range((len(raw) - _ALT_META_SIZE) // 32)]
    except Exception:
        return None


def decode_account_data(data, owner):
    """Decode account data bytes by owning program.

    Returns {"account_kind", "decoded" (program-specific dict or
    None), "data_length", "validation_status", "notes"}. Kinds:
    "mint" / "token_account" (token programs, by data length),
    "address_lookup_table", "system" (empty data), "unknown".
    """
    out = {
        "account_kind": "unknown",
        "decoded": None,
        "data_length": len(data) if isinstance(data, (bytes, bytearray))
        else None,
        "validation_status": "unknown",
        "notes": [],
    }
    if not isinstance(data, (bytes, bytearray)):
        out["notes"].append("account data is not bytes")
        return out
    raw = bytes(data)
    if owner in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
        if len(raw) == token_decoder.MINT_BASE_SIZE:
            decoded = token_decoder.decode_mint(raw, owner=owner)
            out["account_kind"] = "mint"
            out["decoded"] = decoded
            out["validation_status"] = decoded["validation_status"]
        elif len(raw) == token_decoder.TOKEN_ACCOUNT_BASE_SIZE:
            decoded = token_decoder.decode_token_account(raw, owner=owner)
            out["account_kind"] = "token_account"
            out["decoded"] = decoded
            out["validation_status"] = decoded["validation_status"]
        elif len(raw) > token_decoder.MINT_BASE_SIZE and \
                owner == TOKEN_2022_PROGRAM_ID:
            # Token-2022 mint or account with extensions. Length
            # alone cannot tell them apart once a mint passes 165
            # bytes (NOTAI is 404, PYUSD 866) — that is exactly
            # what the AccountType byte at offset 165 is for
            # (0x01 mint, 0x02 account; see token_decoder). Length
            # decides only below 165, where an account base
            # (165 bytes) cannot fit.
            if len(raw) < token_decoder.TOKEN_ACCOUNT_BASE_SIZE:
                decoded = token_decoder.decode_mint(raw, owner=owner)
                out["account_kind"] = "mint"
                out["decoded"] = decoded
                out["validation_status"] = \
                    decoded["validation_status"]
            elif raw[token_decoder.ACCOUNT_TYPE_OFFSET] == 1:
                decoded = token_decoder.decode_mint(raw, owner=owner)
                out["account_kind"] = "mint"
                out["decoded"] = decoded
                out["validation_status"] = \
                    decoded["validation_status"]
            elif raw[token_decoder.ACCOUNT_TYPE_OFFSET] == 2:
                decoded = token_decoder.decode_token_account(
                    raw, owner=owner)
                out["account_kind"] = "token_account"
                out["decoded"] = decoded
                out["validation_status"] = \
                    decoded["validation_status"]
            else:
                out["notes"].append(
                    "AccountType byte is %d, neither mint (1) nor "
                    "account (2); cannot tell a long Token-2022 "
                    "mint from a token account"
                    % raw[token_decoder.ACCOUNT_TYPE_OFFSET])
        else:
            out["notes"].append(
                "token account data %d bytes matches neither the "
                "mint (%d) nor the token-account (%d) layout"
                % (len(raw), token_decoder.MINT_BASE_SIZE,
                   token_decoder.TOKEN_ACCOUNT_BASE_SIZE))
    elif owner == ADDRESS_LOOKUP_TABLE_PROGRAM_ID:
        addresses = lookup_table_addresses_from_account_data(raw)
        out["account_kind"] = "address_lookup_table"
        if addresses is None:
            out["notes"].append(
                "lookup-table account data is not well-formed")
            out["validation_status"] = "unknown"
        else:
            out["decoded"] = {"addresses": addresses,
                              "address_count": len(addresses)}
            out["validation_status"] = "verified"
    elif owner == SYSTEM_PROGRAM_ID:
        out["account_kind"] = "system"
        if len(raw) == 0:
            out["decoded"] = {"empty": True}
            out["validation_status"] = "verified"
        else:
            out["notes"].append(
                "system-owned account carries %d data bytes"
                % len(raw))
            out["validation_status"] = "unverified"
    else:
        out["notes"].append(
            "no layout decoder for owner program %s" % owner)
    return out


def decode_account(account, address=None):
    """Decode one getAccountInfo value dict.

    `account` is the "value" of a getAccountInfo result (or None
    for a missing account). Returns owner, lamports, executable,
    rent epoch, data length/encoding, rent-exempt state, decoded
    data (when the owner has a layout decoder), decoder_version
    and validation_status. A missing account is "unknown", never
    an invented empty account.
    """
    out = {
        "address": address,
        "exists": False,
        "lamports": None,
        "owner": None,
        "owner_name": "unknown_program",
        "executable": None,
        "rent_epoch": None,
        "data_encoding": None,
        "data_length": None,
        "data_bytes": None,
        "parsed": None,
        "rent_exempt_minimum_lamports": None,
        "rent_exempt": None,
        "rent_lamports_per_byte": None,
        "decoded_data": None,
        "decoder_version": DECODER_VERSION,
        "validation_status": "unknown",
        "notes": [],
    }
    if account is None:
        out["notes"].append("account does not exist")
        return out
    if not isinstance(account, dict):
        out["notes"].append("account value is not an object")
        return out
    out["exists"] = True
    out["lamports"] = account.get("lamports")
    out["owner"] = account.get("owner")
    out["owner_name"] = KNOWN_OWNERS.get(out["owner"],
                                         "unknown_program")
    out["executable"] = account.get("executable")
    out["rent_epoch"] = account.get("rentEpoch")
    data = account.get("data")
    raw = None
    if isinstance(data, dict) and "parsed" in data:
        out["data_encoding"] = "jsonParsed"
        parsed = data.get("parsed")
        out["parsed"] = parsed
        if isinstance(parsed, dict):
            out["data_length"] = data.get("space")
            ptype = parsed.get("type")
            info = parsed.get("info")
            if ptype == "mint" and isinstance(info, dict):
                decoded = token_decoder.decode_mint_from_parsed(
                    info, owner=out["owner"])
                out["decoded_data"] = {
                    "account_kind": "mint", "decoded": decoded,
                    "data_length": out["data_length"],
                    "validation_status":
                        decoded["validation_status"],
                    "notes": decoded["notes"],
                }
                out["validation_status"] = \
                    decoded["validation_status"]
            elif ptype == "account" and isinstance(info, dict):
                # jsonParsed token account: mint/owner/amount are
                # carried in info; the raw layout is not needed.
                out["decoded_data"] = {
                    "account_kind": "token_account",
                    "decoded": {
                        "mint": info.get("mint"),
                        "owner": info.get("owner"),
                        "amount": info.get("tokenAmount", {}).get(
                            "amount") if isinstance(
                                info.get("tokenAmount"), dict) else None,
                        "state": info.get("state"),
                        "delegate": info.get("delegate"),
                    },
                    "data_length": out["data_length"],
                    "validation_status": "verified",
                    "notes": ["source=jsonParsed"],
                }
                out["validation_status"] = "verified"
            else:
                out["notes"].append(
                    "jsonParsed type %r has no layout decoder"
                    % (ptype,))
                out["validation_status"] = "unverified"
    elif isinstance(data, (list, tuple)) and len(data) >= 2:
        payload, encoding = data[0], data[1]
        out["data_encoding"] = encoding
        try:
            if encoding == "base64":
                raw = base64.b64decode(payload, validate=True)
            elif encoding == "base58":
                raw = b58decode(payload)
            else:
                out["notes"].append(
                    "unsupported data encoding %r" % (encoding,))
        except Exception:
            out["notes"].append(
                "account data is not valid %s" % (encoding,))
            return out
        if raw is not None:
            out["data_bytes"] = raw
            out["data_length"] = len(raw)
            decoded = decode_account_data(raw, out["owner"])
            out["decoded_data"] = decoded
            out["validation_status"] = decoded["validation_status"]
    elif isinstance(data, str):
        out["data_encoding"] = "base58"
        try:
            raw = b58decode(data)
        except ValueError:
            out["notes"].append("account data is not valid base58")
            return out
        out["data_bytes"] = raw
        out["data_length"] = len(raw)
        decoded = decode_account_data(raw, out["owner"])
        out["decoded_data"] = decoded
        out["validation_status"] = decoded["validation_status"]
    else:
        out["notes"].append("account data has unexpected shape")
        return out

    if out["data_length"] is not None and out["lamports"] is not None:
        out["rent_exempt_minimum_lamports"] = rent_exempt_minimum(
            out["data_length"])
        out["rent_exempt"] = is_rent_exempt(
            out["lamports"], out["data_length"])
        out["rent_lamports_per_byte"] = LAMPORTS_PER_BYTE
    return out
