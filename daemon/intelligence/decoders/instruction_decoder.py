"""Instruction decoding for known Solana programs — pure stdlib.

Decodes one instruction from either form a getTransaction response
carries:

* parsed form (encoding "jsonParsed"): the RPC already parsed the
  instruction into {"program", "programId", "parsed": {"type",
  "info"}, "accounts"}. The decoder keeps the RPC's parse, names
  the program, and normalises the shape.
* raw form (encoding "json" / "base64" wire data): {"programIdIndex",
  "accounts": [indexes], "data": base58}. The decoder resolves the
  program id and account addresses against the transaction's account
  keys and parses the instruction data against the program's
  documented layout.

Program ids and instruction layouts are taken from official program
documentation/source, never from memory alone:

* SPL Token / Token-2022 TokenInstruction enum order and fields —
  docs.rs spl_token_2022::instruction::TokenInstruction
  (#[repr(C)] enum, discriminants sequential from 0).
* AuthorityType values — SPL Token program (0 MintTokens,
  1 FreezeAccount, 2 AccountOwner, 3 CloseAccount).
* Token-2022 ExtensionType discriminants — docs.rs
  spl_token_2022::extension::ExtensionType (#[repr(u16)]).
* System / Compute Budget / Address Lookup Table program ids —
  broker/memecoin/execution/tx.py (pinned against solders in the
  repo's own tests).

Unknown programs and unknown instruction discriminants are reported
as unknown/unverified with the raw data preserved — an event is
never invented. Token-2022 extension instructions with discriminant
>= 32 (added after spl-token-2022 0.3.0) are named only as
token_2022_extension_instruction: the variant order past
CreateNativeMint was not re-verified for this decoder version.
"""
import struct

from ._base58 import b58decode, b58encode
from daemon import solana_ids as _solana_ids

DECODER_VERSION = "1.0.0"

# ---------------------------------------------------------------------
# Known programs (canonical values live in daemon/solana_ids.py —
# never redefine the literals here; Phase 2, D-01/D-04/D-05).
# ---------------------------------------------------------------------
SYSTEM_PROGRAM_ID = _solana_ids.SYSTEM_PROGRAM_ID
TOKEN_PROGRAM_ID = _solana_ids.TOKEN_PROGRAM_ID
TOKEN_2022_PROGRAM_ID = _solana_ids.TOKEN2022_PROGRAM_ID
ASSOCIATED_TOKEN_PROGRAM_ID = _solana_ids.ASSOCIATED_TOKEN_PROGRAM_ID
COMPUTE_BUDGET_PROGRAM_ID = _solana_ids.COMPUTE_BUDGET_PROGRAM_ID
ADDRESS_LOOKUP_TABLE_PROGRAM_ID = _solana_ids.ADDRESS_LOOKUP_TABLE_PROGRAM_ID

KNOWN_PROGRAMS = {
    SYSTEM_PROGRAM_ID: "system",
    TOKEN_PROGRAM_ID: "token",
    TOKEN_2022_PROGRAM_ID: "token_2022",
    ASSOCIATED_TOKEN_PROGRAM_ID: "associated_token",
    COMPUTE_BUDGET_PROGRAM_ID: "compute_budget",
    ADDRESS_LOOKUP_TABLE_PROGRAM_ID: "address_lookup_table",
}

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _coption_pubkey(data, offset):
    """Read a COption<Pubkey> (u32 flag + 32-byte pubkey) at offset.

    Returns (pubkey_base58_or_None, next_offset) or (None, None) when
    the bytes are truncated / the flag is neither 0 nor 1.
    """
    if len(data) < offset + 4:
        return None, None
    (flag,) = struct.unpack_from("<I", data, offset)
    if flag == 0:
        return None, offset + 4
    if flag != 1 or len(data) < offset + 36:
        return None, None
    return b58encode(data[offset + 4:offset + 36]), offset + 36


def _u64(data, offset):
    if len(data) < offset + 8:
        return None
    return struct.unpack_from("<Q", data, offset)[0]


def _u32(data, offset):
    if len(data) < offset + 4:
        return None
    return struct.unpack_from("<I", data, offset)[0]


def _u8(data, offset):
    if len(data) < offset + 1:
        return None
    return data[offset]


def _pubkey(data, offset):
    if len(data) < offset + 32:
        return None
    return b58encode(data[offset:offset + 32])


def _base(ix):
    """Shared output skeleton for one decoded instruction."""
    return {
        "program_id": None,
        "program_name": "unknown_program",
        "instruction_type": "unknown",
        "instruction_name": "unknown",
        "accounts": [],
        "data": {},
        "data_raw_base58": None,
        "decoder_version": DECODER_VERSION,
        "validation_status": "unknown",
        "notes": [],
    }


# ---------------------------------------------------------------------
# SPL Token / Token-2022 instructions
# (TokenInstruction enum, docs.rs spl_token_2022 0.3.0 — discriminants
#  are the enum order, 0-based, first byte of instruction data.)
# ---------------------------------------------------------------------
_TOKEN_INSTRUCTIONS = {
    0: "initialize_mint",
    1: "initialize_account",
    2: "initialize_multisig",
    3: "transfer",
    4: "approve",
    5: "revoke",
    6: "set_authority",
    7: "mint_to",
    8: "burn",
    9: "close_account",
    10: "freeze_account",
    11: "thaw_account",
    12: "transfer_checked",
    13: "approve_checked",
    14: "mint_to_checked",
    15: "burn_checked",
    16: "initialize_account2",
    17: "sync_native",
    18: "initialize_account3",
    19: "initialize_multisig2",
    20: "initialize_mint2",
    21: "get_account_data_size",
    22: "initialize_immutable_owner",
    23: "amount_to_ui_amount",
    24: "ui_amount_to_amount",
    25: "initialize_mint_close_authority",
    26: "transfer_fee_extension",
    27: "confidential_transfer_extension",
    28: "default_account_state_extension",
    29: "reallocate",
    30: "memo_transfer_extension",
    31: "create_native_mint",
}

_AUTHORITY_TYPES = {
    0: "mint_tokens",
    1: "freeze_account",
    2: "account_owner",
    3: "close_account",
}


def _decode_token_data(data, program_name):
    """Parse SPL Token / Token-2022 instruction data bytes.

    Returns (fields_dict, instruction_name, validation_status).
    Lengths are checked exactly for the fixed-layout instructions;
    a short buffer yields unverified, never a guessed field.
    """
    if not data:
        return {}, "unknown", "unknown"
    disc = data[0]
    name = _TOKEN_INSTRUCTIONS.get(disc)
    if name is None:
        if disc >= 32:
            return ({"discriminant": disc},
                    "token_2022_extension_instruction", "unverified")
        return {"discriminant": disc}, "unknown", "unknown"
    prefix = "token_2022_" if program_name == "token_2022" else "token_"
    full_name = prefix + name
    fields = {"discriminant": disc}
    ok = True

    def need(length):
        nonlocal ok
        if len(data) < length:
            ok = False
            return False
        return True

    if name in ("initialize_mint", "initialize_mint2"):
        # decimals u8, mint_authority pubkey, freeze_authority COption
        if need(70):
            fields["decimals"] = _u8(data, 1)
            fields["mint_authority"] = _pubkey(data, 2)
            freeze, _ = _coption_pubkey(data, 34)
            fields["freeze_authority"] = freeze
            if fields["mint_authority"] is None:
                ok = False
    elif name in ("transfer", "approve", "mint_to", "burn"):
        if need(9):
            fields["amount"] = _u64(data, 1)
    elif name in ("transfer_checked", "approve_checked",
                  "mint_to_checked", "burn_checked"):
        if need(10):
            fields["amount"] = _u64(data, 1)
            fields["decimals"] = _u8(data, 9)
    elif name in ("initialize_multisig", "initialize_multisig2"):
        if need(2):
            fields["m"] = _u8(data, 1)
    elif name == "set_authority":
        if need(2):
            auth_type = _u8(data, 1)
            fields["authority_type"] = _AUTHORITY_TYPES.get(
                auth_type, "unknown_authority_type_%s" % auth_type)
            new_auth, _ = _coption_pubkey(data, 2)
            fields["new_authority"] = new_auth
            if auth_type not in _AUTHORITY_TYPES:
                ok = False
    elif name in ("initialize_account2", "initialize_account3"):
        if need(33):
            fields["owner"] = _pubkey(data, 1)
    elif name == "initialize_mint_close_authority":
        close_auth, _ = _coption_pubkey(data, 1)
        fields["close_authority"] = close_auth
        if len(data) < 5:
            ok = False
    elif name == "amount_to_ui_amount":
        if need(9):
            fields["amount"] = _u64(data, 1)
    elif name == "ui_amount_to_amount":
        try:
            fields["ui_amount"] = data[1:].decode("utf-8")
        except UnicodeDecodeError:
            ok = False
    elif name == "get_account_data_size":
        # Borsh Vec<ExtensionType>: u32 count + u16 per entry.
        if need(5):
            count = _u32(data, 1)
            if count is not None and len(data) >= 5 + 2 * count:
                fields["extension_types"] = [
                    struct.unpack_from("<H", data, 5 + 2 * i)[0]
                    for i in range(count)]
            else:
                ok = False
    elif name in ("transfer_fee_extension",
                  "confidential_transfer_extension",
                  "default_account_state_extension",
                  "memo_transfer_extension"):
        # Sub-instruction byte follows; sub-discriminant layouts are
        # not decoded in this version — record the byte, stay honest.
        if len(data) >= 2:
            fields["sub_discriminant"] = data[1]
        else:
            ok = False
    elif name == "reallocate":
        if need(5):
            count = _u32(data, 1)
            if count is not None and len(data) >= 5 + 2 * count:
                fields["extension_types"] = [
                    struct.unpack_from("<H", data, 5 + 2 * i)[0]
                    for i in range(count)]
            else:
                ok = False
    # initialize_account / revoke / close_account / freeze_account /
    # thaw_account / sync_native / initialize_immutable_owner /
    # create_native_mint carry no data beyond the discriminant.
    return fields, full_name, ("verified" if ok else "unverified")


# ---------------------------------------------------------------------
# System program instructions (u32 LE discriminant, then fields)
# ---------------------------------------------------------------------
_SYSTEM_INSTRUCTIONS = {
    0: "create_account",
    1: "assign",
    2: "transfer",
    3: "create_account_with_seed",
    4: "advance_nonce_account",
    5: "withdraw_nonce_account",
    6: "initialize_nonce_account",
    7: "authorize_nonce_account",
    8: "allocate",
    9: "allocate_with_seed",
    10: "assign_with_seed",
    11: "transfer_with_seed",
    12: "upgrade_nonce_account",
}


def _read_seed(data, offset):
    """Read a borsh string (u64 len + utf-8 bytes) at offset."""
    if len(data) < offset + 8:
        return None, None
    (size,) = struct.unpack_from("<Q", data, offset)
    start = offset + 8
    if len(data) < start + size:
        return None, None
    try:
        return data[start:start + size].decode("utf-8"), start + size
    except UnicodeDecodeError:
        return None, None


def _decode_system_data(data):
    """Parse System program instruction data bytes."""
    if len(data) < 4:
        return {}, "unknown", "unknown"
    (disc,) = struct.unpack_from("<I", data, 0)
    name = _SYSTEM_INSTRUCTIONS.get(disc)
    if name is None:
        return {"discriminant": disc}, "unknown", "unknown"
    full_name = "system_" + name
    fields = {"discriminant": disc}
    ok = True
    if name == "create_account":
        # lamports u64, space u64, owner pubkey (52 bytes total)
        if len(data) == 52:
            fields["lamports"] = _u64(data, 4)
            fields["space"] = _u64(data, 12)
            fields["owner"] = _pubkey(data, 20)
        else:
            ok = False
    elif name == "transfer":
        if len(data) == 12:
            fields["lamports"] = _u64(data, 4)
        else:
            ok = False
    elif name == "assign":
        if len(data) == 36:
            fields["owner"] = _pubkey(data, 4)
        else:
            ok = False
    elif name == "allocate":
        if len(data) == 12:
            fields["space"] = _u64(data, 4)
        else:
            ok = False
    elif name == "withdraw_nonce_account":
        if len(data) == 12:
            fields["lamports"] = _u64(data, 4)
        else:
            ok = False
    elif name in ("initialize_nonce_account", "authorize_nonce_account"):
        if len(data) == 36:
            fields["authority"] = _pubkey(data, 4)
        else:
            ok = False
    elif name in ("create_account_with_seed", "allocate_with_seed",
                  "assign_with_seed", "transfer_with_seed"):
        # base pubkey, seed string, then per-instruction tail. Only
        # the common prefix is parsed; the tail is left raw unless
        # its exact length checks out.
        if len(data) >= 36:
            fields["base"] = _pubkey(data, 4)
            seed, nxt = _read_seed(data, 36)
            fields["seed"] = seed
            if seed is None:
                ok = False
            elif name == "create_account_with_seed":
                if len(data) == nxt + 48:
                    fields["lamports"] = _u64(data, nxt)
                    fields["space"] = _u64(data, nxt + 8)
                    fields["owner"] = _pubkey(data, nxt + 16)
                else:
                    ok = False
            elif name == "allocate_with_seed":
                if len(data) == nxt + 40:
                    fields["space"] = _u64(data, nxt)
                    fields["owner"] = _pubkey(data, nxt + 8)
                else:
                    ok = False
            elif name == "assign_with_seed":
                if len(data) == nxt + 32:
                    fields["owner"] = _pubkey(data, nxt)
                else:
                    ok = False
            elif name == "transfer_with_seed":
                if len(data) == nxt + 40:
                    fields["lamports"] = _u64(data, nxt)
                    fields["seed_owner"] = _pubkey(data, nxt + 8)
                else:
                    ok = False
        else:
            ok = False
    # advance_nonce_account / upgrade_nonce_account: no data.
    return fields, full_name, ("verified" if ok else "unverified")


# ---------------------------------------------------------------------
# Compute Budget program (u8 discriminant, then fields)
# ---------------------------------------------------------------------
_COMPUTE_BUDGET_INSTRUCTIONS = {
    0: "request_heap_frame",
    1: "request_units",
    2: "set_compute_unit_limit",
    3: "set_compute_unit_price",
    4: "set_loaded_accounts_data_size_limit",
}


def _decode_compute_budget_data(data):
    if not data:
        return {}, "unknown", "unknown"
    disc = data[0]
    name = _COMPUTE_BUDGET_INSTRUCTIONS.get(disc)
    if name is None:
        return {"discriminant": disc}, "unknown", "unknown"
    fields = {"discriminant": disc}
    ok = True
    if name == "request_heap_frame":
        if len(data) == 5:
            fields["bytes"] = _u32(data, 1)
        else:
            ok = False
    elif name == "request_units":
        if len(data) == 9:
            fields["units"] = _u32(data, 1)
            fields["additional_fee"] = _u32(data, 5)
        else:
            ok = False
    elif name == "set_compute_unit_limit":
        if len(data) == 5:
            fields["units"] = _u32(data, 1)
        else:
            ok = False
    elif name == "set_compute_unit_price":
        if len(data) == 9:
            fields["micro_lamports"] = _u64(data, 1)
        else:
            ok = False
    elif name == "set_loaded_accounts_data_size_limit":
        if len(data) == 5:
            fields["bytes"] = _u32(data, 1)
        else:
            ok = False
    return fields, "compute_budget_" + name, (
        "verified" if ok else "unverified")


# ---------------------------------------------------------------------
# Address Lookup Table program (u8 discriminant; only the name is
# decoded — field layouts are not parsed in this version)
# ---------------------------------------------------------------------
_ALT_INSTRUCTIONS = {
    0: "create_lookup_table",
    1: "freeze_lookup_table",
    2: "extend_lookup_table",
    3: "deactivate_lookup_table",
    4: "close_lookup_table",
}


def _decode_alt_data(data):
    if not data:
        return {}, "unknown", "unknown"
    disc = data[0]
    name = _ALT_INSTRUCTIONS.get(disc)
    if name is None:
        return {"discriminant": disc}, "unknown", "unknown"
    return ({"discriminant": disc},
            "address_lookup_table_" + name, "unverified")


# ---------------------------------------------------------------------
# Associated Token program: empty data = Create, [1] = CreateIdempotent
# ---------------------------------------------------------------------
def _decode_associated_token_data(data):
    if data == b"":
        return {}, "associated_token_create", "verified"
    if data == b"\x01":
        return {}, "associated_token_create_idempotent", "verified"
    return {"discriminant": data[0] if data else None}, "unknown", "unknown"


# ---------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------

def decode_instruction_data(program_id, data):
    """Decode raw instruction data for a known program.

    `data` is bytes (already base58-decoded). Returns
    (fields, instruction_name, validation_status). Unknown programs
    return ({}, "unknown", "unknown") with the raw bytes preserved
    by the caller.
    """
    program_name = KNOWN_PROGRAMS.get(program_id)
    if program_name is None:
        return {}, "unknown", "unknown"
    if program_name in ("token", "token_2022"):
        return _decode_token_data(data, program_name)
    if program_name == "system":
        return _decode_system_data(data)
    if program_name == "compute_budget":
        return _decode_compute_budget_data(data)
    if program_name == "address_lookup_table":
        return _decode_alt_data(data)
    if program_name == "associated_token":
        return _decode_associated_token_data(data)
    return {}, "unknown", "unknown"


def decode_instruction(ix, account_keys=None):
    """Decode one instruction dict from a getTransaction response.

    `ix` is either a parsed-form instruction (jsonParsed) or a raw
    instruction (json / wire form). `account_keys` is the
    transaction's static account-key list (base58), used to resolve
    raw-form programIdIndex / account indexes; it may be None for
    parsed-form instructions (addresses are already present).

    The returned dict always carries program_id, program_name,
    instruction_type, accounts, data, decoder_version and
    validation_status. Undecodable input yields validation_status
    "unknown" and instruction_type "unknown" — never an invented
    event.
    """
    out = _base(ix)
    if not isinstance(ix, dict):
        out["notes"].append("instruction is not an object")
        return out

    parsed = ix.get("parsed")
    program_id = ix.get("programId")
    if parsed is not None and isinstance(parsed, dict):
        # ---- parsed form: the RPC did the parse; keep it. ----
        if program_id is None:
            program_id = ix.get("program")
        out["program_id"] = program_id
        program_name = KNOWN_PROGRAMS.get(program_id, "unknown_program")
        out["program_name"] = program_name
        parsed_type = parsed.get("type")
        info = parsed.get("info")
        if parsed_type:
            slug = ("token_2022_" if program_name == "token_2022"
                    else "token_" if program_name == "token"
                    else program_name + "_")
            out["instruction_type"] = slug + str(parsed_type)
            out["instruction_name"] = out["instruction_type"]
        if isinstance(info, dict):
            out["data"] = dict(info)
        accounts = ix.get("accounts")
        if isinstance(accounts, list):
            out["accounts"] = [str(a) for a in accounts]
        elif isinstance(info, dict):
            # Fall back to the addresses named in the parsed info.
            for key in ("mint", "account", "source", "destination",
                        "owner", "authority", "delegate"):
                if isinstance(info.get(key), str):
                    out["accounts"].append(info[key])
        out["validation_status"] = (
            "verified" if program_name != "unknown_program"
            else "unverified")
        out["notes"].append("parsed_by=rpc_jsonParsed")
        if "stackHeight" in ix:
            out["data"]["stack_height"] = ix.get("stackHeight")
        return out

    # ---- raw form ----
    raw_data = ix.get("data")
    program_index = ix.get("programIdIndex")
    if program_id is None and program_index is not None \
            and account_keys is not None:
        try:
            program_id = account_keys[int(program_index)]
        except (IndexError, TypeError, ValueError):
            out["notes"].append(
                "programIdIndex %r out of range" % (program_index,))
            out["validation_status"] = "unknown"
            return out
    out["program_id"] = program_id
    program_name = KNOWN_PROGRAMS.get(program_id, "unknown_program")
    out["program_name"] = program_name

    raw_accounts = ix.get("accounts")
    if isinstance(raw_accounts, list) and account_keys is not None:
        resolved = []
        bad = False
        for idx in raw_accounts:
            try:
                resolved.append(account_keys[int(idx)])
            except (IndexError, TypeError, ValueError):
                bad = True
                resolved.append(None)
        out["accounts"] = resolved
        if bad:
            out["notes"].append("account index out of range")
            out["validation_status"] = "unknown"
            return out
    elif isinstance(raw_accounts, list):
        # Already addresses (some json forms carry them directly).
        out["accounts"] = [str(a) for a in raw_accounts]

    if raw_data is None:
        out["notes"].append("no instruction data")
        out["validation_status"] = "unknown"
        return out
    if isinstance(raw_data, str):
        out["data_raw_base58"] = raw_data
        try:
            data = b58decode(raw_data)
        except ValueError:
            out["notes"].append("instruction data is not valid base58")
            out["validation_status"] = "unknown"
            return out
    elif isinstance(raw_data, (bytes, bytearray)):
        data = bytes(raw_data)
        out["data_raw_base58"] = b58encode(data)
    else:
        out["notes"].append("instruction data has unexpected type")
        out["validation_status"] = "unknown"
        return out

    fields, name, status = decode_instruction_data(program_id, data)
    out["data"] = fields
    out["instruction_type"] = name
    out["instruction_name"] = name
    out["validation_status"] = status
    return out
