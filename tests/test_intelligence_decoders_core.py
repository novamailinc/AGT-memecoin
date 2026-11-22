#!/usr/bin/env python3
"""Phase 3a core Solana decoder tests (2026-11-22).

Plain script, no network. Covers daemon/intelligence/decoders/:
_base58, instruction_decoder, token_decoder, account_decoder and
solana_transaction. Every decoder function has a test; undecodable
input must yield unknown/unverified, never an invented event.

Real on-chain fixtures live in tests/data/intelligence/ (NOTAI
create/buy/sell legacy/v0/v1 + base64 wire form, NOTAI + PYUSD
mint accounts, both jsonParsed and base64). The raw-bytes form of
each mint is cross-checked against the RPC's own jsonParsed form:
extension names, every field value and the safety verdict must
agree exactly. Synthetic bytes pin the layouts the fixtures
verified (canonical Token-2022 TLV start at offset 166 with the
AccountType byte at 165; SIMD-0437 rent at 5,080 lamports/byte).
"""
import base64
import copy
import json
import os
import struct
import sys

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, REPO)
FIX = os.path.join(REPO, "tests", "data", "intelligence")

from daemon.intelligence.decoders import _base58 as B58
from daemon.intelligence.decoders import instruction_decoder as ID
from daemon.intelligence.decoders import token_decoder as TD
from daemon.intelligence.decoders import account_decoder as AD
from daemon.intelligence.decoders import solana_transaction as ST

NOTAI = "8bGA7uQRGURruJLacPopiTSJtzS49VvLL7C5yDBpump"
PYUSD = "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"
PUMP_FUN = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def load(name):
    with open(os.path.join(FIX, name)) as fh:
        return json.load(fh)


def _pubkey_bytes(seed):
    """Deterministic fake 32-byte pubkey (seed byte repeated)."""
    return bytes([seed]) * 32


# ---------------------------------------------------------------------
# _base58
# ---------------------------------------------------------------------
check("b58 roundtrip",
      B58.b58decode(B58.b58encode(b"\x01\x02\x03\x04"))
      == b"\x01\x02\x03\x04")
check("b58 empty roundtrip",
      B58.b58encode(b"") == "" and B58.b58decode("") == b"")
check("b58 leading zeros",
      B58.b58decode("1112") == b"\x00\x00\x00\x01")
check("b58 known pubkey roundtrip",
      B58.b58encode(B58.b58decode(NOTAI)) == NOTAI)
try:
    B58.b58decode("0OIl")
    check("b58 invalid char raises", False)
except ValueError:
    check("b58 invalid char raises", True)
try:
    B58.b58decode(123)
    check("b58 non-str raises", False)
except ValueError:
    check("b58 non-str raises", True)
try:
    B58.b58encode("not-bytes")
    check("b58 encode non-bytes raises", False)
except ValueError:
    check("b58 encode non-bytes raises", True)
check("b58 is_b58 true", B58.is_b58(NOTAI))
check("b58 is_b58 false on 0", not B58.is_b58("abc0"))
check("b58 is_b58 false on empty", not B58.is_b58(""))
check("b58 is_b58 false on non-str", not B58.is_b58(42))

# ---------------------------------------------------------------------
# instruction_decoder — decode_instruction_data (raw form)
# ---------------------------------------------------------------------
SYSTEM = ID.SYSTEM_PROGRAM_ID
TOKEN = ID.TOKEN_PROGRAM_ID
TOKEN_2022 = ID.TOKEN_2022_PROGRAM_ID
COMPUTE_BUDGET = ID.COMPUTE_BUDGET_PROGRAM_ID
ALT_PROGRAM = ID.ADDRESS_LOOKUP_TABLE_PROGRAM_ID
ATA = ID.ASSOCIATED_TOKEN_PROGRAM_ID

fields, name, status = ID.decode_instruction_data(
    SYSTEM, struct.pack("<IQ", 2, 12345))
check("system transfer decodes",
      name == "system_transfer" and status == "verified"
      and fields["lamports"] == 12345)

owner = _pubkey_bytes(7)
fields, name, status = ID.decode_instruction_data(
    SYSTEM, struct.pack("<IQQ", 0, 1838960, 234) + owner)
check("system create_account decodes",
      name == "system_create_account" and status == "verified"
      and fields["lamports"] == 1838960 and fields["space"] == 234
      and fields["owner"] == B58.b58encode(owner))

fields, name, status = ID.decode_instruction_data(
    SYSTEM, struct.pack("<I", 99))
check("system unknown discriminant",
      name == "unknown" and status == "unknown")

fields, name, status = ID.decode_instruction_data(
    SYSTEM, b"\x02\x00\x00\x00" + b"\x00" * 7)
check("system truncated transfer unverified",
      name == "system_transfer" and status == "unverified")

mint_auth = _pubkey_bytes(9)
freeze_auth = _pubkey_bytes(11)
fields, name, status = ID.decode_instruction_data(
    TOKEN_2022, b"\x14" + bytes([6]) + mint_auth
    + struct.pack("<I", 1) + freeze_auth)
check("token_2022 initialize_mint2 decodes",
      name == "token_2022_initialize_mint2" and status == "verified"
      and fields["decimals"] == 6
      and fields["mint_authority"] == B58.b58encode(mint_auth)
      and fields["freeze_authority"] == B58.b58encode(freeze_auth))

fields, name, status = ID.decode_instruction_data(
    TOKEN, bytes([12]) + struct.pack("<QB", 777, 6))
check("token transfer_checked decodes",
      name == "token_transfer_checked" and status == "verified"
      and fields["amount"] == 777 and fields["decimals"] == 6)

fields, name, status = ID.decode_instruction_data(
    TOKEN, bytes([9]))
check("token close_account decodes",
      name == "token_close_account" and status == "verified")

fields, name, status = ID.decode_instruction_data(
    TOKEN_2022, bytes([21]) + struct.pack("<IH", 1, 7))
check("token_2022 get_account_data_size decodes",
      name == "token_2022_get_account_data_size"
      and status == "verified" and fields["extension_types"] == [7])

fields, name, status = ID.decode_instruction_data(
    TOKEN, bytes([12]) + struct.pack("<Q", 5))
check("token truncated transfer_checked unverified",
      name == "token_transfer_checked" and status == "unverified")

fields, name, status = ID.decode_instruction_data(
    TOKEN_2022, bytes([40]))
check("token_2022 extension instruction >=32 named not guessed",
      name == "token_2022_extension_instruction"
      and status == "unverified" and fields["discriminant"] == 40)

fields, name, status = ID.decode_instruction_data(
    TOKEN, b"")
check("token empty data unknown",
      name == "unknown" and status == "unknown")

fields, name, status = ID.decode_instruction_data(
    COMPUTE_BUDGET, bytes([2]) + struct.pack("<I", 200000))
check("compute budget set_compute_unit_limit decodes",
      name == "compute_budget_set_compute_unit_limit"
      and status == "verified" and fields["units"] == 200000)

fields, name, status = ID.decode_instruction_data(
    COMPUTE_BUDGET, bytes([3]) + struct.pack("<Q", 5000))
check("compute budget set_compute_unit_price decodes",
      name == "compute_budget_set_compute_unit_price"
      and status == "verified" and fields["micro_lamports"] == 5000)

fields, name, status = ID.decode_instruction_data(
    COMPUTE_BUDGET, bytes([9]))
check("compute budget unknown discriminant",
      name == "unknown" and status == "unknown")

fields, name, status = ID.decode_instruction_data(
    ALT_PROGRAM, bytes([0]))
check("alt create_lookup_table named, unverified",
      name == "address_lookup_table_create_lookup_table"
      and status == "unverified")

fields, name, status = ID.decode_instruction_data(ATA, b"")
check("associated token empty data is create",
      name == "associated_token_create" and status == "verified")
fields, name, status = ID.decode_instruction_data(ATA, b"\x01")
check("associated token 0x01 is create_idempotent",
      name == "associated_token_create_idempotent"
      and status == "verified")
fields, name, status = ID.decode_instruction_data(ATA, b"\x02")
check("associated token unknown byte",
      name == "unknown" and status == "unknown")

fields, name, status = ID.decode_instruction_data(
    "UnknownProgram111111111111111111111111111111", b"\x00")
check("unknown program data unknown",
      fields == {} and name == "unknown" and status == "unknown")

# ---------------------------------------------------------------------
# instruction_decoder — decode_instruction (parsed + raw forms)
# ---------------------------------------------------------------------
parsed_ix = {
    "programId": TOKEN_2022,
    "program": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "parsed": {"type": "initializeMint2",
               "info": {"decimals": 6, "mintAuthority": "abc"}},
    "accounts": ["acct1", "acct2"],
}
dec = ID.decode_instruction(parsed_ix)
check("parsed instruction keeps rpc parse",
      dec["program_name"] == "token_2022"
      and dec["instruction_type"] == "token_2022_initializeMint2"
      and dec["validation_status"] == "verified"
      and dec["accounts"] == ["acct1", "acct2"]
      and dec["data"]["decimals"] == 6)

parsed_unknown = {
    "programId": "SomeRandomProgram11111111111111111111111111",
    "parsed": {"type": "doSomething", "info": {}},
    "accounts": [],
}
dec = ID.decode_instruction(parsed_unknown)
check("parsed unknown program unverified",
      dec["program_name"] == "unknown_program"
      and dec["validation_status"] == "unverified")

keys = [SYSTEM, "acctA11111111111111111111111111111111",
        "acctB11111111111111111111111111111111"]
raw_ix = {"programIdIndex": 0, "accounts": [1, 2],
          "data": B58.b58encode(struct.pack("<IQ", 2, 999))}
dec = ID.decode_instruction(raw_ix, account_keys=keys)
check("raw instruction resolves indexes",
      dec["program_name"] == "system"
      and dec["instruction_type"] == "system_transfer"
      and dec["validation_status"] == "verified"
      and dec["accounts"] == keys[1:]
      and dec["data"]["lamports"] == 999)

dec = ID.decode_instruction(
    {"programIdIndex": 9, "accounts": [], "data": "111"},
    account_keys=keys)
check("raw instruction bad program index unknown",
      dec["validation_status"] == "unknown"
      and dec["instruction_type"] == "unknown")

dec = ID.decode_instruction({"programIdIndex": 0, "accounts": [99],
                             "data": "111"}, account_keys=keys)
check("raw instruction bad account index unknown",
      dec["validation_status"] == "unknown")

dec = ID.decode_instruction(None)
check("non-dict instruction unknown",
      dec["validation_status"] == "unknown"
      and dec["instruction_type"] == "unknown")
dec = ID.decode_instruction({})
check("empty instruction unknown",
      dec["validation_status"] == "unknown")
dec = ID.decode_instruction({"programId": SYSTEM})
check("instruction with no data unknown",
      dec["validation_status"] == "unknown")


# ---------------------------------------------------------------------
# token_decoder — TLV parsing on synthetic bytes
# ---------------------------------------------------------------------
def _tlv_entry(type_id, value):
    return struct.pack("<HH", type_id, len(value)) + value


def _coption36(pubkey_bytes):
    """COption<Pubkey> as a fixed 36-byte block (flag + 32 bytes)."""
    if pubkey_bytes is None:
        return struct.pack("<I", 0) + b"\x00" * 32
    return struct.pack("<I", 1) + pubkey_bytes


def _mint_base(decimals=6, supply=1000, mint_auth=None, freeze_auth=None):
    data = bytearray(82)
    data[0:36] = _coption36(mint_auth)
    struct.pack_into("<Q", data, 36, supply)
    data[44] = decimals
    data[45] = 1
    data[46:82] = _coption36(freeze_auth)
    return bytes(data)


def _token2022_mint_bytes(tlv_entries, account_type=1):
    """Canonical layout: base 82 + 83 zero bytes + AccountType + TLV."""
    return (_mint_base() + b"\x00" * 83 + bytes([account_type])
            + b"".join(tlv_entries))


def _token_account_base(mint=None, owner=None, amount=0, state=1):
    data = bytearray(165)
    data[0:32] = mint or _pubkey_bytes(21)
    data[32:64] = owner or _pubkey_bytes(22)
    struct.pack_into("<Q", data, 64, amount)
    data[72:108] = _coption36(None)        # delegate: none
    data[108] = state
    data[109:121] = struct.pack("<I", 0) + b"\x00" * 8  # is_native: none
    struct.pack_into("<Q", data, 121, 0)   # delegated_amount
    data[129:165] = _coption36(None)       # close_authority: none
    return bytes(data)


# Zero (0, 0) slots inside the TLV region are skipped, not an end.
exts, status, notes = TD.parse_tlv_extensions(
    _tlv_entry(0, b"") + _tlv_entry(7, b""), 0)
check("tlv zero slot skipped",
      status == "verified" and len(exts) == 1
      and exts[0]["type_name"] == "immutableOwner")

exts, status, notes = TD.parse_tlv_extensions(
    _tlv_entry(99, b"abcd"), 0)
check("tlv unknown type unverified",
      status == "unverified" and len(exts) == 1
      and exts[0]["type_name"] == "unknown_extension_99"
      and any("unknown extension type 99" in n for n in notes))

exts, status, notes = TD.parse_tlv_extensions(
    struct.pack("<HH", 7, 100) + b"short", 0)
check("tlv truncated value unverified",
      status == "unverified" and len(exts) == 0
      and any("truncated TLV value" in n for n in notes))

exts, status, notes = TD.parse_tlv_extensions(b"\x07\x00", 0)
check("tlv trailing partial slot ends cleanly",
      status == "verified" and exts == [])

# ConfidentialTransferFeeConfig field order, verified on PYUSD:
# authority (32), elgamal pubkey (32), harvest bool (1), withheld (64).
fee_value = (b"\x11" * 32 + b"\x22" * 32 + b"\x01" + b"\x33" * 64)
fields, ok = TD._decode_extension_value(16, fee_value)
check("confidentialTransferFeeConfig field order",
      ok and fields["authority"] == B58.b58encode(b"\x11" * 32)
      and fields["withdraw_withheld_authority_elgamal_pubkey"]
      == base64.b64encode(b"\x22" * 32).decode()
      and fields["harvest_to_mint_enabled"] is True
      and fields["withheld_amount"]
      == base64.b64encode(b"\x33" * 64).decode())

ctm_value = b"\x44" * 32 + b"\x01" + b"\x55" * 32
fields, ok = TD._decode_extension_value(4, ctm_value)
check("confidentialTransferMint fields",
      ok and fields["authority"] == B58.b58encode(b"\x44" * 32)
      and fields["auto_approve_new_accounts"] is True
      and fields["auditor_elgamal_pubkey"]
      == base64.b64encode(b"\x55" * 32).decode())

fields, ok = TD._decode_extension_value(4, b"\x00" * 64)
check("confidentialTransferMint wrong length unparsed",
      not ok and fields == {})

# TokenMetadata Borsh: update auth, mint, name/symbol/uri, extra vec.
def _borsh_str(text):
    raw = text.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


meta_value = (b"\x00" * 32 + _pubkey_bytes(31) + _borsh_str("Test Coin")
              + _borsh_str("TEST") + _borsh_str("https://x.test/m")
              + struct.pack("<I", 1) + _borsh_str("k") + _borsh_str("v"))
fields, ok = TD._decode_token_metadata(meta_value)
check("tokenMetadata borsh decodes",
      ok and fields["update_authority"] is None
      and fields["mint"] == B58.b58encode(_pubkey_bytes(31))
      and fields["name"] == "Test Coin" and fields["symbol"] == "TEST"
      and fields["uri"] == "https://x.test/m"
      and fields["additional_metadata"] == [{"key": "k",
                                              "value": "v"}])
fields, ok = TD._decode_token_metadata(meta_value + b"\x00")
check("tokenMetadata trailing garbage unparsed", not ok)
fields, ok = TD._decode_token_metadata(meta_value[:80])
check("tokenMetadata truncated unparsed", not ok)

# ---------------------------------------------------------------------
# token_decoder — decode_mint on synthetic bytes
# ---------------------------------------------------------------------
classic = TD.decode_mint(_mint_base(decimals=9, supply=42),
                         owner=TD.TOKEN_PROGRAM_ID)
check("classic mint decodes",
      classic["program"] == "spl_token"
      and classic["decimals"] == 9 and classic["supply"] == 42
      and classic["mint_authority"] is None
      and classic["freeze_authority"] is None
      and classic["extensions"] == []
      and classic["validation_status"] == "verified")

mp_value = b"\x00" * 32 + B58.b58decode(NOTAI)
synth = TD.decode_mint(
    _token2022_mint_bytes([_tlv_entry(18, mp_value)]),
    owner=TD.TOKEN_2022_PROGRAM_ID)
check("token-2022 mint tlv at offset 166",
      synth["program"] == "token_2022"
      and synth["extension_names"] == ["metadataPointer"]
      and synth["account_type"] == "mint"
      and synth["extensions"][0]["fields"]["authority"] is None
      and synth["extensions"][0]["fields"]["metadata_address"] == NOTAI
      and synth["validation_status"] == "verified")

wrong_type = TD.decode_mint(
    _token2022_mint_bytes([_tlv_entry(7, b"")], account_type=2),
    owner=TD.TOKEN_2022_PROGRAM_ID)
check("wrong account-type byte noted, extensions still parsed",
      wrong_type["account_type"] == "account"
      and wrong_type["extension_names"] == ["immutableOwner"]
      and any("expected mint" in n for n in wrong_type["notes"]))

short = TD.decode_mint(b"\x00" * 50, owner=TD.TOKEN_2022_PROGRAM_ID)
check("short mint unknown",
      short["validation_status"] == "unknown"
      and short["supply"] is None)
check("non-bytes mint unknown",
      TD.decode_mint("not-bytes")["validation_status"] == "unknown")

# ---------------------------------------------------------------------
# token_decoder — decode_token_account on synthetic bytes
# ---------------------------------------------------------------------
acct = TD.decode_token_account(_token_account_base(amount=123456),
                               owner=TD.TOKEN_PROGRAM_ID)
check("classic token account decodes",
      acct["program"] == "spl_token"
      and acct["mint"] == B58.b58encode(_pubkey_bytes(21))
      and acct["owner"] == B58.b58encode(_pubkey_bytes(22))
      and acct["amount"] == 123456 and acct["state"] == "initialized"
      and acct["delegate"] is None and acct["is_native"] is None
      and acct["validation_status"] == "verified")

acct22 = TD.decode_token_account(
    _token_account_base(amount=5) + bytes([2])
    + _tlv_entry(7, b""), owner=TD.TOKEN_2022_PROGRAM_ID)
check("token-2022 account tlv at offset 166",
      acct22["program"] == "token_2022"
      and acct22["extension_names"] == ["immutableOwner"]
      and acct22["account_type"] == "account"
      and acct22["validation_status"] == "verified"
      and acct22["data_length"] == 170)

check("short token account unknown",
      TD.decode_token_account(b"\x00" * 100)["validation_status"]
      == "unknown")

# ---------------------------------------------------------------------
# token_decoder — real fixtures: raw vs jsonParsed must agree exactly
# ---------------------------------------------------------------------
def _mint_fixture(name):
    fx = load(name)
    raw = base64.b64decode(fx["rpc_result_base64"]["value"]["data"][0])
    owner = fx["rpc_result_base64"]["value"]["owner"]
    dec_raw = TD.decode_mint(raw, owner=owner)
    info = fx["rpc_result_parsed"]["value"]["data"]["parsed"]["info"]
    dec_parsed = TD.decode_mint_from_parsed(info, owner=owner)
    return dec_raw, dec_parsed


notai_raw, notai_parsed = _mint_fixture("notai_mint.json")
check("notai raw mint supply/decimals",
      notai_raw["supply"] == 989994050051393
      and notai_raw["decimals"] == 6)
check("notai raw mint authorities revoked",
      notai_raw["mint_authority"] is None
      and notai_raw["freeze_authority"] is None)
check("notai raw extensions",
      notai_raw["extension_names"] == ["metadataPointer",
                                       "tokenMetadata"])
check("notai raw and parsed agree",
      notai_raw["supply"] == notai_parsed["supply"]
      and notai_raw["decimals"] == notai_parsed["decimals"]
      and notai_raw["extension_names"]
      == notai_parsed["extension_names"]
      and all(r["fields"] == p["fields"] for r, p in zip(
          notai_raw["extensions"], notai_parsed["extensions"]))
      and notai_raw["validation_status"] == "verified"
      and notai_parsed["validation_status"] == "verified")
check("notai tokenMetadata fields",
      notai_raw["extensions"][1]["fields"]["name"] == "This Is Not AI"
      and notai_raw["extensions"][1]["fields"]["symbol"] == "NOTAI"
      and notai_raw["extensions"][1]["fields"]["uri"]
      == "https://ipfs.io/ipfs/QmSh1oFHAYXzv6Ugp7sFeUfJ1SdKPALxiKr6jAFci472K2"
      and notai_raw["extensions"][1]["fields"]["update_authority"]
      is None
      and notai_raw["extensions"][1]["fields"]["mint"] == NOTAI
      and notai_raw["extensions"][1]["fields"]
      ["additional_metadata"] == [])
check("notai assess pass both forms",
      TD.assess_mint(notai_raw) == {"fails": [], "flags": [],
                                    "verdict": "pass"}
      and TD.assess_mint(notai_parsed) == TD.assess_mint(notai_raw))

pyusd_raw, pyusd_parsed = _mint_fixture("pyusd_mint.json")
check("pyusd raw mint supply/decimals/authorities",
      pyusd_raw["supply"] == 720480615162772
      and pyusd_raw["decimals"] == 6
      and pyusd_raw["mint_authority"]
      == "DRXA1H9SjZPyPCEeuAWpfwk8GqVW6dZsKFAtdfpFwApX"
      and pyusd_raw["freeze_authority"]
      == "2apBGMsS6ti9RyF5TwQTDswXBWskiJP2LD4cUEDqYJjk")
check("pyusd raw extensions",
      pyusd_raw["extension_names"]
      == ["mintCloseAuthority", "permanentDelegate",
          "transferFeeConfig", "confidentialTransferMint",
          "confidentialTransferFeeConfig", "transferHook",
          "metadataPointer", "tokenMetadata"])
check("pyusd raw and parsed agree",
      pyusd_raw["supply"] == pyusd_parsed["supply"]
      and pyusd_raw["extension_names"]
      == pyusd_parsed["extension_names"]
      and all(r["fields"] == p["fields"] for r, p in zip(
          pyusd_raw["extensions"], pyusd_parsed["extensions"]))
      and pyusd_raw["validation_status"] == "verified"
      and pyusd_parsed["validation_status"] == "verified")
check("pyusd transfer fee is zero bps",
      pyusd_raw["extensions"][2]["fields"]
      ["transfer_fee_basis_points"] == 0
      and pyusd_raw["extensions"][2]["fields"]
      ["older_transfer_fee"]["epoch"] == 605)
check("pyusd confidential fee config fields",
      pyusd_raw["extensions"][4]["fields"]
      ["harvest_to_mint_enabled"] is True
      and pyusd_raw["extensions"][4]["fields"]
      ["withdraw_withheld_authority_elgamal_pubkey"]
      == "HDfmQztzBN2Cc3rkDZuL88SfWw5sSajVMyiz5QaQHFc="
      and pyusd_raw["extensions"][4]["fields"]["withheld_amount"]
      == base64.b64encode(b"\x00" * 64).decode())
check("pyusd tokenMetadata fields",
      pyusd_raw["extensions"][7]["fields"]["name"] == "PayPal USD"
      and pyusd_raw["extensions"][7]["fields"]["symbol"] == "PYUSD"
      and pyusd_raw["extensions"][7]["fields"]["update_authority"]
      == "2apBGMsS6ti9RyF5TwQTDswXBWskiJP2LD4cUEDqYJjk")
check("pyusd assess fail both forms",
      TD.assess_mint(pyusd_raw) == TD.assess_mint(pyusd_parsed)
      and TD.assess_mint(pyusd_raw)["verdict"] == "fail"
      and TD.assess_mint(pyusd_raw)["fails"] == [
          "Token-2022 permanent delegate can seize/burn "
          "any holder's tokens",
          "Token-2022 transfer hook can block sells arbitrarily"]
      and TD.assess_mint(pyusd_raw)["flags"] == [
          "mint authority enabled (infinite mint risk)",
          "freeze authority enabled",
          "Token-2022 mint close authority set",
          "Token-2022 confidential transfer mint"])

bad_supply = TD.decode_mint_from_parsed(
    {"mintAuthority": None, "supply": "not-a-number", "decimals": 6,
     "isInitialized": True, "freezeAuthority": None,
     "extensions": []})
check("parsed mint bad supply unverified",
      bad_supply["supply"] is None
      and bad_supply["validation_status"] == "unverified")

# ---------------------------------------------------------------------
# token_decoder — assess_mint rules on synthetic decoded mints
# ---------------------------------------------------------------------
def _assess_mint(extension_names, extensions, mint_auth=None,
                 freeze_auth=None):
    return TD.assess_mint({
        "account_kind": "mint", "validation_status": "verified",
        "mint_authority": mint_auth, "freeze_authority": freeze_auth,
        "extension_names": extension_names, "extensions": extensions})


def _fee_ext(bps):
    return {"type_name": "transferFeeConfig",
            "fields": {"transfer_fee_basis_points": bps,
                       "older_transfer_fee": {"epoch": 1,
                                              "maximum_fee": 0,
                                              "transfer_fee_basis_points": bps},
                       "newer_transfer_fee": {"epoch": 1,
                                              "maximum_fee": 0,
                                              "transfer_fee_basis_points": bps}}}


check("assess fee above 10 percent fails",
      _assess_mint(["transferFeeConfig"], [_fee_ext(1500)])
      ["fails"] == ["Token-2022 transfer fee 15.0% (>10%)"])
check("assess fee above zero flags",
      _assess_mint(["transferFeeConfig"], [_fee_ext(50)])
      == {"fails": [], "flags": ["Token-2022 transfer fee 0.5%"],
          "verdict": "flag"})
check("assess zero fee passes",
      _assess_mint(["transferFeeConfig"], [_fee_ext(0)])
      ["verdict"] == "pass")
check("assess non-transferable fails",
      "Token-2022 non-transferable tokens cannot be sold or "
      "transferred" in _assess_mint(
          ["nonTransferable"],
          [{"type_name": "nonTransferable", "fields": {}}])["fails"])
check("assess default-frozen fails",
      _assess_mint(
          ["defaultAccountState"],
          [{"type_name": "defaultAccountState",
            "fields": {"state": "frozen"}}])["verdict"] == "fail")
check("assess default-initialized does not fail",
      _assess_mint(
          ["defaultAccountState"],
          [{"type_name": "defaultAccountState",
            "fields": {"state": "initialized"}}])["verdict"] == "pass")
check("assess mint close authority flags",
      _assess_mint(
          ["mintCloseAuthority"],
          [{"type_name": "mintCloseAuthority",
            "fields": {"mint_close_authority": "abc"}}])
      ["flags"] == ["Token-2022 mint close authority set"])
check("assess confidential transfer mint flags",
      _assess_mint(["confidentialTransferMint"],
                   [{"type_name": "confidentialTransferMint",
                     "fields": {}}])
      ["flags"] == ["Token-2022 confidential transfer mint"])
check("assess mint authority flags",
      _assess_mint([], [], mint_auth="abc")["flags"]
      == ["mint authority enabled (infinite mint risk)"])
check("assess mint authority is curve pda wording",
      _assess_mint([], [], mint_auth="CURVE")["flags"]
      == ["mint authority enabled (infinite mint risk)"]
      and TD.assess_mint({
          "account_kind": "mint", "validation_status": "verified",
          "mint_authority": "CURVE", "freeze_authority": None,
          "extension_names": [], "extensions": []},
          curve_pda="CURVE")["flags"]
      == ["mint authority = pump.fun curve (pre-graduation)"])
check("assess non-mint unknown",
      TD.assess_mint({"account_kind": "token_account"})
      ["verdict"] == "unknown")
check("assess undecodable mint unknown",
      TD.assess_mint({"account_kind": "mint",
                      "validation_status": "unknown"})
      ["verdict"] == "unknown")

# ---------------------------------------------------------------------
# token_decoder — parsed-form key normalisation
# ---------------------------------------------------------------------
check("snake case dict converts nested camelCase",
      TD._snake_case_dict(
          {"mintAuthority": "X",
           "newerTransferFee": {"transferFeeBasisPoints": 5}})
      == {"mint_authority": "X",
          "newer_transfer_fee": {"transfer_fee_basis_points": 5}})


# ---------------------------------------------------------------------
# account_decoder — rent, ALT accounts, decode_account
# ---------------------------------------------------------------------
check("rent minimum 404 bytes is 2702560",
      AD.rent_exempt_minimum(404) == 2702560)
check("rent minimum 866 bytes is 5049520",
      AD.rent_exempt_minimum(866) == 5049520)
check("rent minimum zero bytes is overhead only",
      AD.rent_exempt_minimum(0) == 128 * 5080)
check("rent minimum honours an explicit rate",
      AD.rent_exempt_minimum(404, 6960) == 532 * 6960)
check("rent exempt at exactly the minimum",
      AD.is_rent_exempt(2702560, 404) is True)
check("rent not exempt one lamport short",
      AD.is_rent_exempt(2702559, 404) is False)
check("rent exempt bad input is None",
      AD.is_rent_exempt("x", 404) is None)

ALT_META = b"\x00" * 56
alt_addrs = [B58.b58encode(_pubkey_bytes(41)),
             B58.b58encode(_pubkey_bytes(42))]
alt_data = ALT_META + _pubkey_bytes(41) + _pubkey_bytes(42)
check("alt account addresses parsed",
      AD.lookup_table_addresses_from_account_data(alt_data)
      == alt_addrs)
check("alt account short data is None",
      AD.lookup_table_addresses_from_account_data(b"\x00" * 56)
      is None)
check("alt account misaligned data is None",
      AD.lookup_table_addresses_from_account_data(
          ALT_META + b"\x00" * 33) is None)
check("alt account non-bytes is None",
      AD.lookup_table_addresses_from_account_data("nope") is None)

check("decode_account_data system empty",
      AD.decode_account_data(b"", AD.SYSTEM_PROGRAM_ID)
      == {"account_kind": "system", "decoded": {"empty": True},
          "data_length": 0, "validation_status": "verified",
          "notes": []})
dec = AD.decode_account_data(_mint_base(), TD.TOKEN_PROGRAM_ID)
check("decode_account_data classic mint",
      dec["account_kind"] == "mint"
      and dec["validation_status"] == "verified")
dec = AD.decode_account_data(
    base64.b64decode(
        load("notai_mint.json")["rpc_result_base64"]["value"]["data"][0]),
    TD.TOKEN_2022_PROGRAM_ID)
check("decode_account_data token-2022 mint with extensions",
      dec["account_kind"] == "mint"
      and dec["validation_status"] == "verified"
      and dec["decoded"]["extension_names"]
      == ["metadataPointer", "tokenMetadata"])
dec = AD.decode_account_data(
    _token2022_mint_bytes([_tlv_entry(7, b"")]),
    TD.TOKEN_2022_PROGRAM_ID)
check("decode_account_data long mint is a mint not an account",
      dec["account_kind"] == "mint"
      and dec["validation_status"] == "verified"
      and dec["decoded"]["extension_names"] == ["immutableOwner"])
dec = AD.decode_account_data(
    _token_account_base(amount=5) + bytes([2])
    + _tlv_entry(7, b""), TD.TOKEN_2022_PROGRAM_ID)
check("decode_account_data long account is an account",
      dec["account_kind"] == "token_account"
      and dec["validation_status"] == "verified"
      and dec["decoded"]["extension_names"] == ["immutableOwner"])
dec = AD.decode_account_data(
    _token2022_mint_bytes([_tlv_entry(7, b"")], account_type=0),
    TD.TOKEN_2022_PROGRAM_ID)
check("decode_account_data bad account-type byte unknown",
      dec["account_kind"] == "unknown"
      and dec["validation_status"] == "unknown"
      and any("AccountType byte" in n for n in dec["notes"]))
dec = AD.decode_account_data(b"\x00" * 50, TD.TOKEN_PROGRAM_ID)
check("decode_account_data short token data unknown",
      dec["account_kind"] == "unknown"
      and dec["validation_status"] == "unknown")
dec = AD.decode_account_data(b"\x00" * 10,
                             "UnknownOwner11111111111111111111111111")
check("decode_account_data unknown owner unknown",
      dec["account_kind"] == "unknown"
      and any("no layout decoder" in n for n in dec["notes"]))

# Real mint accounts, both encodings, via decode_account.
for mint_name, mint_addr, lamports, data_len, rent_min in (
        ("notai_mint.json", NOTAI, 2702560, 404, 2702560),
        ("pyusd_mint.json", PYUSD, 2078412094, 866, 5049520)):
    fx = load(mint_name)
    acct = AD.decode_account(
        fx["rpc_result_parsed"]["value"], address=mint_addr)
    check("%s parsed account decodes" % mint_name,
          acct["exists"] is True
          and acct["lamports"] == lamports
          and acct["data_length"] == data_len
          and acct["rent_exempt_minimum_lamports"] == rent_min
          and acct["rent_exempt"] is True
          and acct["decoded_data"]["account_kind"] == "mint"
          and acct["validation_status"] == "verified")
    acct64 = AD.decode_account(
        fx["rpc_result_base64"]["value"], address=mint_addr)
    check("%s base64 account agrees with parsed" % mint_name,
          acct64["lamports"] == lamports
          and acct64["data_length"] == data_len
          and acct64["rent_exempt"] is True
          and acct64["decoded_data"]["decoded"]["supply"]
          == acct["decoded_data"]["decoded"]["supply"]
          and acct64["decoded_data"]["decoded"]["extension_names"]
          == acct["decoded_data"]["decoded"]["extension_names"])

check("decode_account missing account unknown",
      AD.decode_account(None)["exists"] is False
      and AD.decode_account(None)["validation_status"] == "unknown")
check("decode_account non-dict unknown",
      AD.decode_account("nope")["validation_status"] == "unknown")

parsed_acct = {
    "lamports": 2039280, "owner": TD.TOKEN_PROGRAM_ID,
    "executable": False, "rentEpoch": 123,
    "data": {"parsed": {"type": "account",
                        "info": {"mint": NOTAI,
                                 "owner": B58.b58encode(
                                     _pubkey_bytes(23)),
                                 "tokenAmount": {"amount": "123",
                                                 "decimals": 6,
                                                 "uiAmount": 0.000123},
                                 "state": "initialized",
                                 "delegate": None}},
             "program": "spl-token", "space": 165},
}
acct = AD.decode_account(parsed_acct, address="someATA")
check("decode_account parsed token account",
      acct["exists"] is True
      and acct["decoded_data"]["account_kind"] == "token_account"
      and acct["decoded_data"]["decoded"]["mint"] == NOTAI
      and acct["decoded_data"]["decoded"]["amount"] == "123"
      and acct["decoded_data"]["decoded"]["state"] == "initialized"
      and acct["rent_exempt"] is True
      and acct["validation_status"] == "verified")

bad_encoding = {"lamports": 100, "owner": TD.TOKEN_PROGRAM_ID,
                "executable": False, "rentEpoch": 0,
                "data": ["xx", "base99"]}
check("decode_account unsupported encoding noted",
      any("unsupported data encoding" in n
          for n in AD.decode_account(bad_encoding)["notes"]))

# ---------------------------------------------------------------------
# solana_transaction — real fixtures
# ---------------------------------------------------------------------
TX_CASES = [
    # name, signature, slot, block_time, version, fee_payer prefix,
    # fee, account count, unknown instructions
    ("notai_create.json",
     "55EDrGhrE7rsPQZ4Q1UJNJNFzVcQdXNGUsPPswaKR8PMFfLbsHAaeHfeC1AqGp13ujB9pLBadz6DhrMPZFHLN71",
     472082477, 1795251956, 1, "5s51HvzZjAaq9VeZ", 10000, 24, 4),
    ("notai_buy.json",
     "5CDsDmsimWorDr7DdSzTAgGUoTToZEWL3isMHkuTUSV8MrSVFSGK8bUGr2NbjQrznpMUoSNHWYjmmauFXS26vqpb",
     472092557, 1795254121, "legacy", "BW5FCNbncgs6UsXF", 19531, 27, 3),
    ("notai_sell_legacy.json",
     "4HDorwMqN82RqBj9fqm48TjXMRTFHhbqha12qEcXJEemqnXsjgRcKjZwPUgPTGa5uiLWzFSBhtSgaxtnHXLEL7ca",
     472099146, 1795255535, "legacy", "6ZFNNZFvitneSY6A", 25803, 25, 3),
    ("notai_sell_v0.json",
     "sWEfVeRnQcAUU4vtkfv5QKBwZtCPGrbvC5f6wAUSJXaqph1mSgeSV2gw4KSPQvoHJ6pKSjGJH5jyMnAbGtKrrD5",
     472192455, 1795275586, 0, "Fg6oxcQVj7BfqZhH", 5120, 25, 3),
    ("notai_sell_v1.json",
     "5vi9enW6eJshSmW96RfTYuUnHzzws2mfu6PNxyRjQpuqF3YxPwzt6AVchaFgD2WrrgGaH8e5x2U2oke2hjNhuUgg",
     472279994, 1795294422, 1, "6QU8Y6vQs7tcQRwz", 54001, 20, 2),
]
decoded_txs = {}
for (name, sig, slot, block_time, version, payer, fee, n_acct,
     n_unknown) in TX_CASES:
    fx = load(name)
    dec = ST.decode_transaction(fx["rpc_result"])
    decoded_txs[name] = dec
    check("%s signature/slot/block_time/version" % name,
          dec["signature"] == sig and dec["slot"] == slot
          and dec["block_time"] == block_time
          and dec["version"] == version
          and dec["raw"] is fx["rpc_result"])
    check("%s fee payer/fee/accounts" % name,
          dec["fee_payer"].startswith(payer)
          and dec["meta"]["fee"] == fee
          and len(dec["account_keys"]) == n_acct
          and dec["meta"]["status"] == "success"
          and dec["meta"]["err"] is None
          and len(dec["log_messages"]) > 0)
    check("%s unverified at core layer (pump.fun/pumpswap unknown)"
          % name,
          dec["validation_status"] == "unverified"
          and dec["unknown_instructions"] == n_unknown
          and dec["unverified_instructions"] == 0)
    check("%s swap program is the unknown instruction" % name,
          any(ix["program_id"] in (PUMP_FUN, PUMPSWAP)
              and ix["validation_status"] == "unknown"
              for ix in dec["instructions"]))

create = decoded_txs["notai_create.json"]
check("create outer instructions",
      create["instructions"][0]["program_id"] == PUMP_FUN
      and create["instructions"][1]["instruction_type"]
      == "associated_token_createIdempotent"
      and create["instructions"][1]["validation_status"] == "verified")
inner0_types = [ix["instruction_type"] for ix in
                create["inner_instructions"][0]["instructions"]]
check("create inner instructions decode",
      "system_createAccount" in inner0_types
      and "token_2022_initializeMint2" in inner0_types
      and "token_2022_initializeMetadataPointer" in inner0_types
      and "token_2022_initializeTokenMetadata" in inner0_types
      and "token_2022_mintTo" in inner0_types
      and "token_2022_setAuthority" in inner0_types)
check("create transaction config",
      create["transaction_config"]["computeUnitLimit"] == 411200
      and create["transaction_config"]["priorityFee"] == 0)

sell_v1 = decoded_txs["notai_sell_v1.json"]
check("sell v1 transaction config",
      sell_v1["transaction_config"]["computeUnitLimit"] == 78011
      and sell_v1["transaction_config"]["priorityFee"] == 49001
      and sell_v1["transaction_config"]
      ["loadedAccountsDataSizeLimit"] == 67108864)


def _notai_deltas(dec):
    """(account_index, delta) for non-zero NOTAI balance changes."""
    out = []
    for c in ST.token_balance_changes_for_mint(dec, NOTAI):
        if c["delta"]:
            out.append((c["account_index"], c["delta"]))
    return sorted(out)


check("create notai deltas",
      _notai_deltas(create)
      == [(8, 997532928283542), (10, 2467071716458)])
check("buy notai deltas",
      _notai_deltas(decoded_txs["notai_buy.json"])
      == [(6, -163914309400), (10, 163914309400)])
check("sell legacy notai deltas",
      _notai_deltas(decoded_txs["notai_sell_legacy.json"])
      == [(4, 805527491472), (8, -805527491472)])
check("sell v0 notai deltas",
      _notai_deltas(decoded_txs["notai_sell_v0.json"])
      == [(1, -50465743747), (5, 50465743747)])
check("sell v1 notai deltas",
      _notai_deltas(sell_v1)
      == [(4, -71088002495), (5, 71088002495)])
check("token deltas for absent mint are empty",
      ST.token_balance_changes_for_mint(
          decoded_txs["notai_buy.json"], PYUSD) == [])

# Wire (base64) vs jsonParsed of the same v0 sell transaction.
wire = ST.decode_transaction(load("notai_sell_v0_base64.json")
                             ["rpc_result"])
parsed = decoded_txs["notai_sell_v0.json"]
check("wire vs parsed same signature/version/accounts/fee",
      wire["signature"] == parsed["signature"]
      and wire["version"] == parsed["version"] == 0
      and len(wire["account_keys"]) == len(parsed["account_keys"])
      and wire["meta"]["fee"] == parsed["meta"]["fee"] == 5120
      and wire["validation_status"]
      == parsed["validation_status"] == "unverified")
check("wire vs parsed same program sequence",
      [ix["program_name"] for ix in wire["instructions"]]
      == [ix["program_name"] for ix in parsed["instructions"]])
check("wire vs parsed instruction naming differs only by case",
      wire["instructions"][4]["instruction_type"]
      == "token_close_account"
      and parsed["instructions"][4]["instruction_type"]
      == "token_closeAccount")
check("wire form noted",
      any("base64 wire bytes" in n for n in wire["notes"]))

# ---------------------------------------------------------------------
# solana_transaction — wire version detection
# ---------------------------------------------------------------------
wire_bytes = base64.b64decode(
    load("notai_sell_v0_base64.json")["rpc_result"]
    ["transaction"][0])
check("wire version of v0 fixture",
      ST.wire_transaction_version(wire_bytes)
      == {"version": 0, "is_versioned": True, "is_v1": False,
          "has_config": False})
v1_bytes = b"\x01" + b"\x00" * 64 + b"\x81" + b"\x00" * 10
check("wire version v1 detected",
      ST.wire_transaction_version(v1_bytes)
      == {"version": 1, "is_versioned": True, "is_v1": True,
          "has_config": True})
legacy_bytes = b"\x01" + b"\x00" * 64 + b"\x00" + b"\x00" * 10
check("wire version legacy detected",
      ST.wire_transaction_version(legacy_bytes)
      == {"version": None, "is_versioned": False, "is_v1": False,
          "has_config": False})
check("wire version empty bytes",
      ST.wire_transaction_version(b"")["version"] is None)
check("wire version truncated bytes",
      ST.wire_transaction_version(b"\x01\x02")["version"] is None)

# ---------------------------------------------------------------------
# solana_transaction — bad / edge inputs
# ---------------------------------------------------------------------
check("decode None transaction unknown",
      ST.decode_transaction(None)["validation_status"] == "unknown")
check("decode non-dict transaction unknown",
      ST.decode_transaction("nope")["validation_status"] == "unknown")
check("decode empty transaction unknown",
      ST.decode_transaction({})["validation_status"] == "unknown")
check("decode bad base64 envelope unknown",
      ST.decode_transaction(
          {"transaction": ["!!!not-base64!!!", "base64"]})
      ["validation_status"] == "unknown")
check("decode truncated wire unknown",
      ST.decode_transaction(
          {"transaction": [base64.b64encode(b"\x01\x02\x03").decode(),
                           "base64"]})["validation_status"] == "unknown")
dec = ST.decode_transaction(
    {"transaction": [base64.b64encode(v1_bytes).decode(), "base64"]})
check("decode v1 wire unknown with honest note",
      dec["validation_status"] == "unknown"
      and any("version 1 wire format is not decoded" in n
              for n in dec["notes"]))

no_meta = copy.deepcopy(load("notai_sell_v0.json")["rpc_result"])
del no_meta["meta"]
dec = ST.decode_transaction(no_meta)
check("missing meta is unverified not unknown",
      dec["validation_status"] == "unverified"
      and any("meta is missing" in n for n in dec["notes"]))

no_keys = {"slot": 1, "blockTime": 1,
           "transaction": {"message": {"accountKeys": [],
                                       "instructions": []},
                           "signatures": []},
           "meta": {"err": None, "fee": 5000, "preBalances": [],
                    "postBalances": []}}
dec = ST.decode_transaction(no_keys)
check("transaction with no account keys unverified",
      dec["validation_status"] == "unverified"
      and any("no account keys" in n for n in dec["notes"]))

failed = copy.deepcopy(load("notai_sell_v0.json")["rpc_result"])
failed["meta"]["err"] = {"InstructionError": [0, {"Custom": 1}]}
failed["meta"]["status"] = {"Err": {"InstructionError": [0, {}]}}
dec = ST.decode_transaction(failed)
check("failed transaction status failed",
      dec["meta"]["status"] == "failed"
      and dec["meta"]["err"] is not None)

# ---------------------------------------------------------------------
# solana_transaction — address lookup table resolution
# ---------------------------------------------------------------------
lookups = [{"accountKey": "TABLE1",
            "writableIndexes": [1, 0], "readonlyIndexes": [2]}]
loaded, status, notes = ST.resolve_address_table_lookups(
    lookups, lambda table: ["A", "B", "C"])
check("alt resolution writable then readonly order",
      loaded == ["B", "A", "C"] and status == "verified")
loaded, status, notes = ST.resolve_address_table_lookups(lookups, None)
check("alt resolution without resolver unverified",
      loaded == [] and status == "unverified")
loaded, status, notes = ST.resolve_address_table_lookups([], None)
check("alt resolution empty lookups verified",
      loaded == [] and status == "verified")
loaded, status, notes = ST.resolve_address_table_lookups(
    [{"accountKey": "TABLE1", "writableIndexes": [5],
      "readonlyIndexes": []}], lambda table: ["A", "B"])
check("alt resolution out of range unverified",
      status == "unverified" and any("out of range" in n
                                      for n in notes))


def _failing_resolver(table):
    raise RuntimeError("boom")


loaded, status, notes = ST.resolve_address_table_lookups(
    lookups, _failing_resolver)
check("alt resolution resolver failure unverified",
      status == "unverified")

# End-to-end ALT: a synthetic v0 tx whose loaded accounts come
# only from a resolver.
PAYER = B58.b58encode(_pubkey_bytes(51))
TABLE = B58.b58encode(_pubkey_bytes(52))
LOADED = B58.b58encode(_pubkey_bytes(53))


def _v0_tx(resolver):
    tx = {"slot": 9, "blockTime": 1795000000, "version": 0,
          "transaction": {
              "message": {
                  "accountKeys": [PAYER, TABLE],
                  "addressTableLookups": [
                      {"accountKey": TABLE, "writableIndexes": [0],
                       "readonlyIndexes": []}],
                  "instructions": [],
                  "recentBlockhash": "11111111111111111111111111111111"},
              "signatures": [B58.b58encode(_pubkey_bytes(54))]},
          "meta": {"err": None, "status": {"Ok": None}, "fee": 5000,
                   "preBalances": [1000000, 0, 0],
                   "postBalances": [995000, 0, 0],
                   "preTokenBalances": [], "postTokenBalances": [],
                   "logMessages": []}}
    return ST.decode_transaction(tx, resolver=resolver)


dec = _v0_tx(lambda table: [LOADED] if table == TABLE else None)
check("alt end to end resolved and verified",
      dec["validation_status"] == "verified"
      and dec["account_keys"] == [PAYER, TABLE, LOADED]
      and dec["loaded_account_keys"] == [LOADED]
      and dec["fee_payer"] == PAYER)
dec = _v0_tx(None)
check("alt end to end without resolver unverified",
      dec["validation_status"] == "unverified"
      and dec["loaded_account_keys"] == [])

# RPC-resolved loaded keys that do not match the lookup indexes.
mismatch = {"slot": 9, "blockTime": 1795000000, "version": 0,
            "transaction": {
                "message": {
                    "accountKeys": [
                        {"pubkey": PAYER, "signer": True,
                         "source": "transaction", "writable": True},
                        {"pubkey": TABLE, "signer": False,
                         "source": "transaction", "writable": False},
                        {"pubkey": LOADED, "signer": False,
                         "source": "lookupTable", "writable": False}],
                    "addressTableLookups": [
                        {"accountKey": TABLE, "writableIndexes": [0, 1],
                         "readonlyIndexes": []}],
                    "instructions": [],
                    "recentBlockhash":
                        "11111111111111111111111111111111"},
                "signatures": [B58.b58encode(_pubkey_bytes(54))]},
            "meta": {"err": None, "status": {"Ok": None}, "fee": 5000,
                     "preBalances": [1000000, 0, 0],
                     "postBalances": [995000, 0, 0],
                     "preTokenBalances": [], "postTokenBalances": [],
                     "logMessages": []}}
dec = ST.decode_transaction(mismatch)
check("alt rpc-resolved count mismatch unverified",
      dec["validation_status"] == "unverified"
      and any("do not match the lookup indexes" in n
              for n in dec["notes"]))

# ---------------------------------------------------------------------
print()
if fails:
    print("FAILURES:", len(fails))
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("ALL INTELLIGENCE CORE DECODER TESTS PASSED")
