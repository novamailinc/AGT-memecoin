"""Token mint / token account decoding — pure stdlib.

Decodes SPL Token and Token-2022 mint and token-account data from
raw account bytes (base64 getAccountInfo) or from the RPC's
jsonParsed form. The two forms are cross-checked against each other
in the tests on real on-chain accounts.

Layouts (official program source / docs):

* Mint base (both programs), 82 bytes: mint_authority
  COption<Pubkey> (4 + 32), supply u64, decimals u8,
  is_initialized bool, freeze_authority COption<Pubkey> (4 + 32).
* Token account base, 165 bytes: mint pubkey, owner pubkey,
  amount u64, delegate COption<Pubkey>, state u8 (0 uninitialized,
  1 initialized, 2 frozen), is_native COption<u64> (4 + 8),
  delegated_amount u64, close_authority COption<Pubkey>.
* Token-2022 extensions live as TLV entries (u16 type LE, u16
  length LE, value bytes) starting at offset 166 — NOT at the end
  of the 82-byte mint base. The layout is [base state][padding
  to 165 bytes][1-byte AccountType][TLV]: a mint base is 82
  bytes, followed by 83 zero bytes, then the AccountType byte
  (0x01 = Mint, 0x02 = Account) at offset 165, then the entries.
  This is the program's own documented layout
  (spl_token_2022 extension/mod.rs: "Mint: 82 bytes... + 83 bytes
  of other extension data + [2, 0, 3, 0, 100, ....]") and it is
  verified byte-for-byte on two live mints (NOTAI, PYUSD): both
  carry 83 zero bytes after the base, 0x01 at 165, and a perfect
  entry chain from 166 to exactly the account length. A token
  account base is 165 bytes, so its AccountType also sits at 165
  and its TLV also starts at 166. Zero (type 0, length 0)
  entries inside the TLV region are empty slots and are skipped.
  Authority fields inside extension values are OptionalNonZero
  Pubkeys — 32 bytes, the all-zero pubkey meaning None — NOT the
  36-byte COption used in the base Mint/Account structs. (The
  first version of this decoder read the TLV from offset 82 and
  found zero extensions; the live fixtures caught it.)

Extension value layouts decoded here (field order per the
spl-token-2022 extension structs):

* TransferFeeConfig (1), 108 bytes: transfer_fee_config_authority
  pubkey, withdraw_withheld_authority pubkey, withheld_amount u64,
  older_transfer_fee {epoch u64, maximum_fee u64,
  transfer_fee_basis_points u16}, newer_transfer_fee (same).
  The newer window is the fee that applies once its epoch arrives.
* MintCloseAuthority (3), 32 bytes: OptionalNonZeroPubkey
  (all-zero = None).
* DefaultAccountState (6), 1 byte: 0 uninitialized, 1 initialized,
  2 frozen.
* PermanentDelegate (12), 32 bytes: delegate pubkey.
* TransferHook (14), 64 bytes: authority OptionalNonZeroPubkey
  (32), program_id OptionalNonZeroPubkey (32).
* MetadataPointer (18) / GroupPointer (20) /
  GroupMemberPointer (22), 64 bytes: authority
  OptionalNonZeroPubkey (32), address pubkey (32).
* NonTransferable (9), ImmutableOwner (7),
  NonTransferableAccount (13): empty. MemoTransfer (8),
  CpiGuard (11), TransferHookAccount (15): 1 byte bool.
* InterestBearingConfig (10), 52 bytes: rate_authority
  OptionalNonZeroPubkey (32), initialization_timestamp i64,
  pre_update_average_rate i16, last_update_timestamp i64,
  current_rate i16.
* ConfidentialTransferMint (4), 65 bytes: authority
  OptionalNonZeroPubkey (32), auto_approve_new_accounts bool
  (1), auditor_elgamal_pubkey OptionalNonZeroElGamalPubkey
  (32, base64 here to match the RPC's jsonParsed form — the
  RPC represents ElGamal pubkeys as base64, unlike plain
  pubkeys which are base58).
* ConfidentialTransferFeeConfig (16), 129 bytes: authority
  pubkey (32), withdraw_withheld_authority_elgamal_pubkey
  (32, base64), harvest_to_mint_enabled bool (1),
  withheld_amount EncryptedBalance (64 raw bytes, base64 here
  to match the RPC's jsonParsed representation). Field order
  verified against the live PYUSD bytes: the RPC's elgamal
  key is exactly bytes 32:64 and the bool is byte 64 — the
  bool does NOT come second, despite the field order some
  versions of the program source suggest.
* TokenMetadata (19), VARIABLE length (Borsh): update_authority
  OptionalNonZeroPubkey (32), mint pubkey (32), name string,
  symbol string, uri string, additional_metadata vec of
  {key, value} string pairs. The parser must consume exactly
  the value length, or the extension is unparsed.

Every other extension is recorded by type id and length only —
presence is a fact, its fields are not guessed.

Safety assessment mirrors daemon/launch_scanner/instant_safety.py
(Token-2022 checks, 2026-10-20) WITHOUT importing that module:
permanentDelegate / transferHook / nonTransferable /
default-frozen = hard fail; transfer fee > 10% = fail; fee > 0,
mintCloseAuthority set, confidentialTransferMint = flag. The
decoder's assess_mint() differs in one documented respect: the
mint-authority verdict. instant_safety fails when the mint
authority is an on-curve address (pump.fun curve PDA) and flags
otherwise; this generic decoder has no curve PDA, so a set mint
authority is a flag and the caller (which knows the curve) may
pass curve_pda to recover the pre-graduation flag wording.
"""
import base64
import re
import struct

from ._base58 import b58encode
from daemon import solana_ids as _solana_ids

DECODER_VERSION = "1.0.0"

# Canonical program IDs live in daemon/solana_ids.py — never
# redefine the literals here (Phase 2, D-01/D-04/D-05).
TOKEN_PROGRAM_ID = _solana_ids.TOKEN_PROGRAM_ID
TOKEN_2022_PROGRAM_ID = _solana_ids.TOKEN2022_PROGRAM_ID

MINT_BASE_SIZE = 82
TOKEN_ACCOUNT_BASE_SIZE = 165

# Canonical Token-2022 layout: base state, padded to 165 bytes,
# then a 1-byte AccountType, then the TLV entries (see module
# docstring). AccountType values: 0 uninitialized, 1 mint,
# 2 account.
ACCOUNT_TYPE_OFFSET = 165
TLV_START_OFFSET = 166
ACCOUNT_TYPES = {0: "uninitialized", 1: "mint", 2: "account"}

# ExtensionType discriminants (docs.rs spl_token_2022, #[repr(u16)]).
EXTENSION_TYPES = {
    0: "uninitialized",
    1: "transferFeeConfig",
    2: "transferFeeAmount",
    3: "mintCloseAuthority",
    4: "confidentialTransferMint",
    5: "confidentialTransferAccount",
    6: "defaultAccountState",
    7: "immutableOwner",
    8: "memoTransfer",
    9: "nonTransferable",
    10: "interestBearingConfig",
    11: "cpiGuard",
    12: "permanentDelegate",
    13: "nonTransferableAccount",
    14: "transferHook",
    15: "transferHookAccount",
    16: "confidentialTransferFeeConfig",
    17: "confidentialTransferFeeAmount",
    18: "metadataPointer",
    19: "tokenMetadata",
    20: "groupPointer",
    21: "tokenGroup",
    22: "groupMemberPointer",
    23: "tokenGroupMember",
}

ACCOUNT_STATES = {0: "uninitialized", 1: "initialized", 2: "frozen"}

# Transfer-fee hard-fail bar, mirroring instant_safety.py: >10%.
TRANSFER_FEE_FAIL_BPS = 1000


def _coption_pubkey(data, offset):
    """COption<Pubkey> at offset -> (base58_or_None, next_offset)."""
    if len(data) < offset + 4:
        return None, None
    (flag,) = struct.unpack_from("<I", data, offset)
    if flag == 0:
        return None, offset + 4
    if flag != 1 or len(data) < offset + 36:
        return None, None
    return b58encode(data[offset + 4:offset + 36]), offset + 36


def _transfer_fee(value):
    """One TransferFee window: {epoch, maximum_fee, basis_points}."""
    if len(value) < 18:
        return None
    epoch, maximum_fee, bps = struct.unpack_from("<QQH", value, 0)
    return {"epoch": epoch, "maximum_fee": maximum_fee,
            "transfer_fee_basis_points": bps}


def _optional_pubkey(raw32):
    """OptionalNonZeroPubkey: 32 bytes, all-zero means None."""
    if len(raw32) != 32:
        return None
    if raw32 == b"\x00" * 32:
        return None
    return b58encode(raw32)


def _optional_elgamal_pubkey(raw32):
    """OptionalNonZeroElGamalPubkey: 32 bytes, all-zero means None.

    Base64-encoded to match the RPC's jsonParsed representation
    of ElGamal pubkeys (unlike plain pubkeys, which are base58).
    """
    if len(raw32) != 32:
        return None
    if raw32 == b"\x00" * 32:
        return None
    return base64.b64encode(raw32).decode("ascii")


def _read_borsh_string(value, pos):
    """One Borsh string at pos -> (str, next_pos) or (None, None)."""
    if len(value) - pos < 4:
        return None, None
    (length,) = struct.unpack_from("<I", value, pos)
    pos += 4
    if len(value) - pos < length:
        return None, None
    try:
        text = value[pos:pos + length].decode("utf-8")
    except UnicodeDecodeError:
        return None, None
    return text, pos + length


def _decode_token_metadata(value):
    """TokenMetadata Borsh value -> (fields, ok).

    update_authority (32) + mint (32) + name/symbol/uri strings +
    additional_metadata vec of {key, value} strings. Must consume
    exactly the value length; anything else is unparsed.
    """
    if len(value) < 64:
        return {}, False
    fields = {
        "update_authority": _optional_pubkey(value[0:32]),
        "mint": b58encode(value[32:64]),
    }
    pos = 64
    for key in ("name", "symbol", "uri"):
        text, pos = _read_borsh_string(value, pos)
        if text is None:
            return {}, False
        fields[key] = text
    if len(value) - pos < 4:
        return {}, False
    (count,) = struct.unpack_from("<I", value, pos)
    pos += 4
    extra = []
    for _ in range(count):
        key, pos = _read_borsh_string(value, pos)
        if key is None:
            return {}, False
        val, pos = _read_borsh_string(value, pos)
        if val is None:
            return {}, False
        extra.append({"key": key, "value": val})
    if pos != len(value):
        return {}, False
    fields["additional_metadata"] = extra
    return fields, True


def _decode_extension_value(type_id, value):
    """Decode one extension's value bytes.

    Returns (fields_dict, parsed_bool). Unparsed extensions return
    ({}, False) — presence recorded, fields not guessed.
    """
    name = EXTENSION_TYPES.get(type_id)
    if name == "transferFeeConfig":
        if len(value) != 108:
            return {}, False
        older = _transfer_fee(value[72:90])
        newer = _transfer_fee(value[90:108])
        if older is None or newer is None:
            return {}, False
        return {
            "transfer_fee_config_authority": b58encode(value[0:32]),
            "withdraw_withheld_authority": b58encode(value[32:64]),
            "withheld_amount": struct.unpack_from("<Q", value, 64)[0],
            "older_transfer_fee": older,
            "newer_transfer_fee": newer,
            # The fee that applies once the newer epoch arrives.
            "transfer_fee_basis_points":
                newer["transfer_fee_basis_points"],
        }, True
    if name == "transferFeeAmount":
        if len(value) != 8:
            return {}, False
        return {"withheld_amount": struct.unpack_from("<Q", value, 0)[0]}, \
            True
    if name == "mintCloseAuthority":
        if len(value) != 32:
            return {}, False
        return {"mint_close_authority": _optional_pubkey(value)}, True
    if name == "defaultAccountState":
        if len(value) != 1:
            return {}, False
        state = value[0]
        return {"state": ACCOUNT_STATES.get(state, "unknown_state_%d"
                                            % state)}, True
    if name == "permanentDelegate":
        if len(value) != 32:
            return {}, False
        return {"delegate": b58encode(value)}, True
    if name == "transferHook":
        if len(value) != 64:
            return {}, False
        return {"authority": _optional_pubkey(value[0:32]),
                "program_id": _optional_pubkey(value[32:64])}, True
    if name in ("metadataPointer", "groupPointer",
                "groupMemberPointer"):
        if len(value) != 64:
            return {}, False
        authority = _optional_pubkey(value[0:32])
        address = b58encode(value[32:64])
        key = ("metadata_address" if name == "metadataPointer"
               else "group_address" if name == "groupPointer"
               else "member_address")
        return {"authority": authority, key: address}, True
    if name in ("memoTransfer", "cpiGuard", "transferHookAccount"):
        if len(value) != 1:
            return {}, False
        return {"enabled": bool(value[0])}, True
    if name == "interestBearingConfig":
        if len(value) != 52:
            return {}, False
        rate_auth = _optional_pubkey(value[0:32])
        init_ts, pre_rate, last_ts, cur_rate = struct.unpack_from(
            "<qhqh", value, 32)
        return {
            "rate_authority": rate_auth,
            "initialization_timestamp": init_ts,
            "pre_update_average_rate": pre_rate,
            "last_update_timestamp": last_ts,
            "current_rate": cur_rate,
        }, True
    if name in ("nonTransferable", "immutableOwner",
                "nonTransferableAccount"):
        if len(value) != 0:
            return {}, False
        return {"present": True}, True
    if name == "confidentialTransferMint":
        if len(value) != 65:
            return {}, False
        return {
            "authority": _optional_pubkey(value[0:32]),
            "auto_approve_new_accounts": bool(value[32]),
            "auditor_elgamal_pubkey":
                _optional_elgamal_pubkey(value[33:65]),
        }, True
    if name == "confidentialTransferFeeConfig":
        if len(value) != 129:
            return {}, False
        return {
            "authority": b58encode(value[0:32]),
            "withdraw_withheld_authority_elgamal_pubkey":
                _optional_elgamal_pubkey(value[32:64]),
            "harvest_to_mint_enabled": bool(value[64]),
            "withheld_amount": base64.b64encode(value[65:129])
            .decode("ascii"),
        }, True
    if name == "tokenMetadata":
        return _decode_token_metadata(value)
    return {}, False


def _tlv_offset(data, base_size, expected_account_type):
    """Where the TLV entries start for a Token-2022 account.

    Returns (offset, account_type_name, notes). Canonical layout:
    entries start at 166 with the AccountType byte at 165. An
    account too short for that layout (base < len < 166) cannot
    hold canonical extensions; the caller falls back to scanning
    from the base end and the result is unverified.
    """
    notes = []
    if len(data) >= TLV_START_OFFSET:
        type_byte = data[ACCOUNT_TYPE_OFFSET]
        account_type = ACCOUNT_TYPES.get(
            type_byte, "unknown_account_type_%d" % type_byte)
        if expected_account_type is not None and \
                account_type != expected_account_type:
            notes.append(
                "AccountType byte is %s, expected %s"
                % (account_type, expected_account_type))
        return TLV_START_OFFSET, account_type, notes
    if len(data) > base_size:
        notes.append(
            "account is %d bytes, too short for the canonical "
            "166-byte TLV start; scanning from the base end instead"
            % len(data))
        return base_size, None, notes
    return None, None, notes


def parse_tlv_extensions(data, offset):
    """Parse Token-2022 TLV extensions starting at `offset`.

    Returns (extensions, status, notes). Each extension is a dict
    with type_id, type_name, length, fields (when parsed) and
    parsed_fields (bool). A truncated tail or a bad length yields
    status "unverified" with the extensions parsed so far. A zero
    (type 0, length 0) entry is an empty TLV slot inside the TLV
    region: it is SKIPPED, never treated as the end of the list.
    Fewer than 4 bytes left ends the scan cleanly. Callers pass
    the canonical TLV start (offset 166); the padding between the
    base end and 166 is NOT scanned by this function.
    """
    extensions = []
    notes = []
    pos = offset
    status = "verified"
    while pos < len(data):
        if len(data) - pos < 4:
            break  # Trailing partial slot: end of the TLV area.
        type_id, length = struct.unpack_from("<HH", data, pos)
        if type_id == 0 and length == 0:
            pos += 4  # Empty TLV slot — skip it.
            continue
        if len(data) - pos - 4 < length:
            notes.append(
                "truncated TLV value for extension type %d" % type_id)
            status = "unverified"
            break
        value = data[pos + 4:pos + 4 + length]
        fields, parsed = _decode_extension_value(type_id, value)
        extensions.append({
            "type_id": type_id,
            "type_name": EXTENSION_TYPES.get(
                type_id, "unknown_extension_%d" % type_id),
            "length": length,
            "fields": fields,
            "parsed_fields": parsed,
        })
        if type_id not in EXTENSION_TYPES:
            notes.append("unknown extension type %d" % type_id)
            status = "unverified"
        elif not parsed:
            notes.append(
                "extension %s has unexpected length %d"
                % (EXTENSION_TYPES[type_id], length))
            status = "unverified"
        pos += 4 + length
    return extensions, status, notes


def _extension_names(extensions):
    return [e["type_name"] for e in extensions]


def decode_mint(data, owner=None):
    """Decode mint account bytes (base64-decoded getAccountInfo data).

    `owner` is the owning program id when known (Token vs
    Token-2022); when omitted it is inferred from the data length
    (extensions present implies Token-2022). Returns a dict with
    supply, decimals, authorities, extensions, decoder_version and
    validation_status. Data shorter than the 82-byte base mint is
    "unknown" — never a guessed mint.
    """
    out = {
        "account_kind": "mint",
        "program": None,
        "mint_authority": None,
        "supply": None,
        "decimals": None,
        "is_initialized": None,
        "freeze_authority": None,
        "extensions": [],
        "extension_names": [],
        "account_type": None,
        "data_length": len(data) if isinstance(data, (bytes, bytearray))
        else None,
        "decoder_version": DECODER_VERSION,
        "validation_status": "unknown",
        "notes": [],
    }
    if not isinstance(data, (bytes, bytearray)):
        out["notes"].append("mint data is not bytes")
        return out
    raw = bytes(data)
    if len(raw) < MINT_BASE_SIZE:
        out["notes"].append(
            "mint data %d bytes < %d-byte base layout"
            % (len(raw), MINT_BASE_SIZE))
        return out
    mint_auth, _ = _coption_pubkey(raw, 0)
    if mint_auth is None and struct.unpack_from("<I", raw, 0)[0] not in (0,):
        # _coption_pubkey returns (None, None) on a bad flag; a 0
        # flag legitimately means "no authority". Distinguish them.
        if struct.unpack_from("<I", raw, 0)[0] != 0:
            out["notes"].append("bad mint_authority option flag")
            return out
    supply = struct.unpack_from("<Q", raw, 36)[0]
    decimals = raw[44]
    is_initialized = bool(raw[45])
    freeze_auth, _ = _coption_pubkey(raw, 46)
    if freeze_auth is None and struct.unpack_from("<I", raw, 46)[0] != 0:
        out["notes"].append("bad freeze_authority option flag")
        return out
    out["mint_authority"] = mint_auth
    out["supply"] = supply
    out["decimals"] = decimals
    out["is_initialized"] = is_initialized
    out["freeze_authority"] = freeze_auth

    has_extensions = len(raw) > MINT_BASE_SIZE
    if owner == TOKEN_2022_PROGRAM_ID or (
            owner is None and has_extensions):
        out["program"] = "token_2022"
    elif owner == TOKEN_PROGRAM_ID:
        out["program"] = "spl_token"
    elif owner is None:
        out["program"] = "spl_token"
    else:
        out["program"] = "unknown_program"
        out["notes"].append("mint owned by unexpected program %s"
                            % owner)
        out["validation_status"] = "unverified"
        return out

    if has_extensions:
        if out["program"] != "token_2022":
            out["notes"].append(
                "classic SPL Token mint carries %d trailing bytes"
                % (len(raw) - MINT_BASE_SIZE))
            out["validation_status"] = "unverified"
            return out
        tlv_offset, account_type, offset_notes = _tlv_offset(
            raw, MINT_BASE_SIZE, "mint")
        out["account_type"] = account_type
        out["notes"].extend(offset_notes)
        if tlv_offset is None:
            out["validation_status"] = "unverified"
            return out
        extensions, status, notes = parse_tlv_extensions(
            raw, tlv_offset)
        out["extensions"] = extensions
        out["extension_names"] = _extension_names(extensions)
        out["notes"].extend(notes)
        # A fallback scan from the base end is less certain than
        # the canonical 166 start.
        if tlv_offset != TLV_START_OFFSET:
            status = "unverified"
        out["validation_status"] = status
    else:
        out["validation_status"] = "verified"
    return out


_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _snake_case_dict(mapping):
    """Recursively convert a dict's camelCase keys to snake_case."""
    out = {}
    for key, value in mapping.items():
        snake = _CAMEL_BOUNDARY.sub("_", str(key)).lower()
        if isinstance(value, dict):
            value = _snake_case_dict(value)
        out[snake] = value
    return out


def decode_mint_from_parsed(info, owner=None):
    """Decode a mint from getAccountInfo jsonParsed info.

    `info` is the parsed.info dict (mintAuthority, supply, decimals,
    isInitialized, freezeAuthority, extensions [{extension, state}]).
    This is the same structure instant_safety.py reads; the field
    semantics are identical. Extension states arrive by name here,
    so value fields are taken from the RPC's parse and marked
    parsed_fields True only when the expected keys are present.
    """
    out = {
        "account_kind": "mint",
        "program": ("token_2022" if owner == TOKEN_2022_PROGRAM_ID
                    else "spl_token" if owner == TOKEN_PROGRAM_ID
                    else None),
        "mint_authority": info.get("mintAuthority"),
        "supply": None,
        "decimals": info.get("decimals"),
        "is_initialized": info.get("isInitialized"),
        "freeze_authority": info.get("freezeAuthority"),
        "extensions": [],
        "extension_names": [],
        "data_length": None,
        "decoder_version": DECODER_VERSION,
        "validation_status": "verified",
        "notes": ["source=jsonParsed"],
    }
    try:
        out["supply"] = int(info.get("supply"))
    except (TypeError, ValueError):
        out["supply"] = None
        out["validation_status"] = "unverified"
        out["notes"].append("supply is not an integer string")
    raw_extensions = info.get("extensions")
    if isinstance(raw_extensions, list):
        for ext in raw_extensions:
            if not isinstance(ext, dict):
                continue
            name = str(ext.get("extension") or "")
            state = ext.get("state") if isinstance(
                ext.get("state"), dict) else {}
            # Normalise the RPC's camelCase state keys to the
            # snake_case keys the raw decoder produces, so the two
            # forms agree field for field and assess_mint() reads
            # both identically. The RPC names the mint-close
            # authority "closeAuthority"; the raw decoder calls
            # the same value "mint_close_authority".
            fields = _snake_case_dict(state)
            if name == "mintCloseAuthority" and \
                    "close_authority" in fields:
                fields["mint_close_authority"] = fields.pop(
                    "close_authority")
            if name == "transferFeeConfig":
                newer = fields.get("newer_transfer_fee") or {}
                if isinstance(newer, dict) and \
                        "transfer_fee_basis_points" in newer:
                    try:
                        fields["transfer_fee_basis_points"] = int(
                            newer["transfer_fee_basis_points"])
                    except (TypeError, ValueError):
                        pass
            out["extensions"].append({
                "type_id": None,
                "type_name": name,
                "length": None,
                "fields": fields,
                "parsed_fields": True,
            })
        out["extension_names"] = _extension_names(out["extensions"])
    return out


def decode_token_account(data, owner=None):
    """Decode token-account bytes (165-byte base + optional TLV).

    Returns mint, owner, amount, delegate, state, is_native,
    delegated_amount, close_authority and any Token-2022 account
    extensions. Data shorter than 165 bytes is "unknown".
    """
    out = {
        "account_kind": "token_account",
        "program": None,
        "mint": None,
        "owner": None,
        "amount": None,
        "delegate": None,
        "state": None,
        "is_native": None,
        "delegated_amount": None,
        "close_authority": None,
        "extensions": [],
        "extension_names": [],
        "account_type": None,
        "data_length": len(data) if isinstance(data, (bytes, bytearray))
        else None,
        "decoder_version": DECODER_VERSION,
        "validation_status": "unknown",
        "notes": [],
    }
    if not isinstance(data, (bytes, bytearray)):
        out["notes"].append("token account data is not bytes")
        return out
    raw = bytes(data)
    if len(raw) < TOKEN_ACCOUNT_BASE_SIZE:
        out["notes"].append(
            "token account data %d bytes < %d-byte base layout"
            % (len(raw), TOKEN_ACCOUNT_BASE_SIZE))
        return out
    out["mint"] = b58encode(raw[0:32])
    out["owner"] = b58encode(raw[32:64])
    out["amount"] = struct.unpack_from("<Q", raw, 64)[0]
    delegate, _ = _coption_pubkey(raw, 72)
    if delegate is None and struct.unpack_from("<I", raw, 72)[0] != 0:
        out["notes"].append("bad delegate option flag")
        return out
    out["delegate"] = delegate
    state = raw[108]
    out["state"] = ACCOUNT_STATES.get(state, "unknown_state_%d" % state)
    if struct.unpack_from("<I", raw, 109)[0] == 1:
        out["is_native"] = struct.unpack_from("<Q", raw, 113)[0]
    elif struct.unpack_from("<I", raw, 109)[0] == 0:
        out["is_native"] = None
    else:
        out["notes"].append("bad is_native option flag")
        return out
    out["delegated_amount"] = struct.unpack_from("<Q", raw, 121)[0]
    close_auth, _ = _coption_pubkey(raw, 129)
    if close_auth is None and struct.unpack_from("<I", raw, 129)[0] != 0:
        out["notes"].append("bad close_authority option flag")
        return out
    out["close_authority"] = close_auth
    out["program"] = ("token_2022" if owner == TOKEN_2022_PROGRAM_ID
                      else "spl_token" if owner == TOKEN_PROGRAM_ID
                      else "spl_token" if owner is None
                      else "unknown_program")
    if len(raw) > TOKEN_ACCOUNT_BASE_SIZE:
        if out["program"] != "token_2022":
            out["notes"].append(
                "classic token account carries %d trailing bytes"
                % (len(raw) - TOKEN_ACCOUNT_BASE_SIZE))
            out["validation_status"] = "unverified"
            return out
        tlv_offset, account_type, offset_notes = _tlv_offset(
            raw, TOKEN_ACCOUNT_BASE_SIZE, "account")
        out["account_type"] = account_type
        out["notes"].extend(offset_notes)
        if tlv_offset is None:
            out["validation_status"] = "unverified"
            return out
        extensions, status, notes = parse_tlv_extensions(
            raw, tlv_offset)
        out["extensions"] = extensions
        out["extension_names"] = _extension_names(extensions)
        out["notes"].extend(notes)
        if tlv_offset != TLV_START_OFFSET:
            status = "unverified"
        out["validation_status"] = status
    else:
        out["validation_status"] = "verified"
    return out


def _extension_fields(decoded, type_name):
    """Fields dict of the first extension with this type name."""
    for ext in decoded.get("extensions") or []:
        if ext.get("type_name") == type_name:
            return ext.get("fields") or {}
    return None


def assess_mint(decoded, curve_pda=None):
    """Safety assessment of a decoded mint.

    Mirrors the Token-2022 checks in
    daemon/launch_scanner/instant_safety.py (2026-10-20) without
    importing it: permanentDelegate / transferHook / nonTransferable
    / default-frozen = hard fail; transfer fee > 10% = fail; fee > 0,
    mintCloseAuthority set, confidentialTransferMint = flag. Mint and
    freeze authorities set are flags. One documented difference: the
    mint-authority fail in instant_safety applies when the authority
    is an on-curve address; this generic decoder cannot make that
    call, so a set mint authority is a flag here (pass curve_pda to
    get the pre-graduation flag wording when the authority is the
    curve itself).

    Returns {"fails": [...], "flags": [...], "verdict":
    "fail" | "flag" | "pass" | "unknown"}.
    """
    fails, flags = [], []
    if not isinstance(decoded, dict) or decoded.get("account_kind") \
            != "mint":
        return {"fails": ["not a decoded mint"], "flags": [],
                "verdict": "unknown"}
    if decoded.get("validation_status") == "unknown":
        return {"fails": [], "flags": ["mint could not be decoded"],
                "verdict": "unknown"}

    mint_auth = decoded.get("mint_authority")
    if mint_auth:
        if curve_pda and str(mint_auth) == str(curve_pda):
            flags.append("mint authority = pump.fun curve "
                         "(pre-graduation)")
        else:
            flags.append("mint authority enabled (infinite mint risk)")
    if decoded.get("freeze_authority"):
        flags.append("freeze authority enabled")

    names = decoded.get("extension_names") or []
    if "permanentDelegate" in names:
        fails.append("Token-2022 permanent delegate can seize/burn "
                     "any holder's tokens")
    if "transferHook" in names:
        fails.append("Token-2022 transfer hook can block sells "
                     "arbitrarily")
    if "nonTransferable" in names:
        fails.append("Token-2022 non-transferable tokens cannot "
                     "be sold or transferred")
    if "defaultAccountState" in names:
        fields = _extension_fields(decoded, "defaultAccountState") or {}
        if str(fields.get("state") or "").lower() == "frozen":
            fails.append("Token-2022 new accounts frozen by default "
                         "(defaultAccountState=frozen)")
    if "transferFeeConfig" in names:
        fields = _extension_fields(decoded, "transferFeeConfig") or {}
        bps = fields.get("transfer_fee_basis_points")
        if bps is None:
            # jsonParsed form carries the fee under the newer window.
            newer = fields.get("newer_transfer_fee") or \
                fields.get("newerTransferFee") or {}
            if isinstance(newer, dict):
                try:
                    bps = int(newer.get("transfer_fee_basis_points")
                              or newer.get("transferFeeBasisPoints"))
                except (TypeError, ValueError):
                    bps = None
        if bps is not None:
            if bps > TRANSFER_FEE_FAIL_BPS:
                fails.append("Token-2022 transfer fee %.1f%% (>10%%)"
                             % (bps / 100.0))
            elif bps > 0:
                flags.append("Token-2022 transfer fee %.1f%%"
                             % (bps / 100.0))
    if "mintCloseAuthority" in names:
        fields = _extension_fields(decoded, "mintCloseAuthority") or {}
        if fields.get("mintCloseAuthority") or \
                fields.get("mint_close_authority") or \
                fields.get("close_authority"):
            flags.append("Token-2022 mint close authority set")
    if "confidentialTransferMint" in names:
        flags.append("Token-2022 confidential transfer mint")

    verdict = "fail" if fails else "flag" if flags else "pass"
    return {"fails": fails, "flags": flags, "verdict": verdict}
