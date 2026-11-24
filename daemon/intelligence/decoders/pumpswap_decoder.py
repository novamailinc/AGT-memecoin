"""PumpSwap decoder (Phase 3b, 2026-11-22).

Decodes the PumpSwap AMM program
``pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`` from standard
Solana ``getTransaction`` (jsonParsed) responses and from raw
account data: new pool creation, pool addresses and authorities,
token pairs, initial liquidity, SOL and token reserves, buy and
sell transactions, liquidity changes (deposit / withdraw), pool
activity and migration from bonding curves.

Pure Python stdlib. Base58 comes from the sibling Phase 3a helper
``daemon.intelligence.decoders._base58`` when this file is imported
as part of the package; a local fallback is kept so the module also
works as a bare file import.

Layouts verified against the official program IDL
(https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/idl/pump_amm.json,
``address`` field matches the program id above) AND against real
on-chain transactions captured in ``tests/data/intelligence/``
(ATX mint ``BiLAn35QaVqQC8f8BGrE92r2n5dcDEJmqJcx1v9Apump``, pool
``3Y7Knabn84dpB6yy5LbCFt574E8ifTMeWkBMTPvGZCwE``; NOTAI AMM
trades).

Discriminators follow the Anchor rule ``sha256("global:<name>")[:8]``
for instructions and ``sha256("event:<Name>")[:8]`` for events; every
discriminator used here was checked against the IDL's explicit
arrays and/or fixture bytes. The Anchor event-emission self-CPI
prefix ``EMIT_DISC`` is an observed constant (see the pumpfun
decoder docstring — it is NOT ``sha256("global:emit")``).

Deployed-program notes (all fixture-verified, 2026-11-22):
* ``BuyEvent`` / ``SellEvent`` bodies carry one trailing u64
  (8 zero bytes) beyond the main-branch IDL layout. The decoder
  reads it as ``trailing_u64``; validation is ``verified`` only
  when it is 0, ``unverified`` otherwise. Its semantics are not
  interpreted.
* ``Pool`` accounts are 301 bytes: the 287-byte IDL struct plus 14
  trailing zero bytes, kept opaque as ``trailing_bytes_hex``.
* Pool vault order follows the IDL: ``pool_base_token_account``
  at offset 139 and ``pool_quote_token_account`` at offset 171.
  This is PROVEN by mint-matching in the migrate fixture: vault
  ``4P8j3pzDfVY797ffYFipPtRe7uu6DkpPvnj45LGUoVZP`` holds the ATX
  mint and ``AwvRfGX9WAz2E4dVRAJzw7ppHJmBh4Qxw5SXovzUBLNS`` holds
  WSOL. NOTE: the in-repo ``daemon/launch_scanner/reserves.py``
  labels these two offsets the other way round (quote @139,
  base @171); its ``resolve_vaults`` mint-matches, so its returned
  vaults are still correct, but the labels are swapped relative to
  the IDL. This decoder follows the IDL.
* ``create_pool`` args are EOF-tolerant trailing options:
  index (u16), base_amount_in (u64), quote_amount_in (u64),
  coin_creator (pubkey), is_mayhem_mode (bool),
  is_cashback_coin (OptionBool, 1 byte), creator_fee_bps
  (OptionU64, 8 bytes), can_edit_creator_fee (OptionBool, 1 byte),
  is_holder_reward (OptionBool, 1 byte).
* The events' ``coin_creator_fee_bps`` (75 in both ATX AMM trades)
  does NOT match the Pool account's stored ``creator_fee_bps`` (0
  in the live snapshot). The AMM GlobalConfig account carries a
  ``coin_creator_fee_basis_points`` field, which is the likely
  source of the effective rate; this is NOT yet verified from the
  live config and is recorded here as an open question, not a
  reconciliation. The decoder reports both values faithfully.
* The legacy AMM ``buy`` / ``buy_v2`` / ``sell`` instructions are
  decoded by the Anchor rule + layout by analogy with the observed
  ``sell_v2`` / ``buy_exact_quote_in`` / ``buy_exact_quote_in_v2``
  variants; no fixture observed them, so those decodes are always
  ``unverified``. ``deposit`` / ``withdraw`` and their events are
  IDL-verified layouts with no fixture observed; same treatment.

Every decoded event carries: signature, slot, block time (when
available), program id, instruction info, relevant account
addresses, decoded event type, raw or referenced transaction data,
decoder version, validation status. Where a transaction cannot be
decoded confidently it is marked unknown/unverified — an event is
never invented. Assets are identified by mint address and canonical
account relationships, never by ticker symbol or token name.
"""

import base64
import hashlib
import struct

try:  # package import (normal path): share the sibling's helper
    from ._base58 import b58decode, b58encode
except ImportError:  # bare-file import: local fallback, same alphabet
    _B58_ALPHABET = ("123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
                     "abcdefghijkmnopqrstuvwxyz")
    _B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}

    def b58decode(text):
        if not isinstance(text, str):
            raise ValueError("base58 input must be str")
        n = 0
        for ch in text:
            n = n * 58 + _B58_INDEX[ch]
        pad = len(text) - len(text.lstrip("1"))
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
        return b"\x00" * pad + raw

    def b58encode(data):
        raw = bytes(data)
        pad = len(raw) - len(raw.lstrip(b"\x00"))
        n = int.from_bytes(raw, "big") if raw else 0
        out = []
        while n:
            n, rem = divmod(n, 58)
            out.append(_B58_ALPHABET[rem])
        return "1" * pad + "".join(reversed(out))


DECODER_VERSION = "1.0.0"
DECODER_NAME = "pumpswap_decoder"

# Canonical Solana program IDs / mints (Phase 2, D-04): the single
# source of truth is daemon/solana_ids.py — never redefine these
# literals here. WSOL_MINT in this file used to be the USDC mint
# (``EPjFWdd5...``); the correct wSOL mint is ``So111...1112``.
try:
    from daemon import solana_ids as _solana_ids
except ImportError:  # bare-file import without the repo root on path
    import os as _os, sys as _sys
    _root = _os.path.dirname(_os.path.dirname(_os.path.dirname(
        _os.path.dirname(_os.path.abspath(__file__)))))
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from daemon import solana_ids as _solana_ids

PUMPSWAP_PROGRAM_ID = _solana_ids.PUMPSWAP_PROGRAM_ID
PUMP_PROGRAM_ID = _solana_ids.PUMP_PROGRAM_ID

WSOL_MINT = _solana_ids.WSOL_MINT
USDC_MINT = _solana_ids.USDC_MINT
SPL_TOKEN_PROGRAM = _solana_ids.TOKEN_PROGRAM_ID
TOKEN_2022_PROGRAM = _solana_ids.TOKEN2022_PROGRAM_ID
SYSTEM_PROGRAM = _solana_ids.SYSTEM_PROGRAM_ID

POOL_DISC = (241, 154, 109, 4, 17, 177, 109, 188)
POOL_IDL_LEN = 287
POOL_LEN = 301

# Anchor event-emission self-CPI prefix, observed in every fixture
# on both the pump and the AMM program. NOT sha256("global:emit").
EMIT_DISC = (228, 69, 165, 46, 81, 203, 154, 29)


def _anchor_disc(kind, name):
    """Anchor discriminator: first 8 bytes of sha256("<kind>:<name>")."""
    return tuple(hashlib.sha256(("%s:%s" % (kind, name)).encode())
                 .digest()[:8])


def _ix_disc(name):
    return _anchor_disc("global", name)


def _ev_disc(name):
    return _anchor_disc("event", name)


# ---------------------------------------------------------------------------
# Instruction table: name -> (discriminator, arg-layout, observed?)
# See the pumpfun decoder for the layout-spec format. "observed" is
# True when the layout was verified against a real fixture tx.
# ---------------------------------------------------------------------------
INSTRUCTIONS = {
    "create_pool": (
        (233, 146, 209, 142, 207, 104, 64, 188),
        [("u16", "index"), ("u64", "base_amount_in"),
         ("u64", "quote_amount_in"), ("pubkey", "coin_creator"),
         ("bool", "is_mayhem_mode"),
         ("option_bool", "is_cashback_coin"),
         ("option_u64", "creator_fee_bps"),
         ("option_bool", "can_edit_creator_fee"),
         ("option_bool", "is_holder_reward")],
        True),
    "buy_exact_quote_in_v2": (
        (194, 171, 28, 70, 104, 77, 91, 47),
        [("u64", "spendable_quote_in"), ("u64", "min_base_amount_out")],
        True),
    "sell_v2": (
        (93, 246, 130, 60, 231, 233, 64, 178),
        [("u64", "base_amount_in"), ("u64", "min_quote_amount_out")],
        True),
    "init_boost": (
        (140, 233, 33, 94, 132, 90, 194, 143),
        [],
        True),
    "deposit": (
        (242, 35, 198, 137, 82, 225, 242, 182),
        [("u64", "lp_token_amount_out"), ("u64", "max_base_amount_in"),
         ("u64", "max_quote_amount_in")],
        False),
    "withdraw": (
        (183, 18, 70, 156, 148, 109, 161, 34),
        [("u64", "lp_token_amount_in"), ("u64", "min_base_amount_out"),
         ("u64", "min_quote_amount_out")],
        False),
    # Legacy variants: discriminator by the Anchor rule, layout by
    # analogy with the observed v2 variants. The legacy `sell` and
    # `buy_exact_quote_in` WERE observed on the AMM in the NOTAI
    # fixtures (see test file); `buy` / `buy_v2` / `sell` (v2-less)
    # are marked observed only where a fixture proves them.
    "sell": (
        (51, 230, 133, 164, 1, 127, 131, 173),
        [("u64", "base_amount_in"), ("u64", "min_quote_amount_out")],
        True),
    "buy_exact_quote_in": (
        (198, 46, 21, 82, 180, 217, 232, 112),
        [("u64", "spendable_quote_in"), ("u64", "min_base_amount_out"),
         ("option_bool", "partial_fill")],
        True),
    "buy": (
        _ix_disc("buy"),
        [("u64", "base_amount_out"), ("u64", "max_quote_amount_in")],
        False),
    "buy_v2": (
        (184, 23, 238, 97, 103, 197, 211, 61),
        [("u64", "base_amount_out"), ("u64", "max_quote_amount_in")],
        False),
}

IX_BY_DISC = {disc: name for name, (disc, _args, _obs)
              in INSTRUCTIONS.items()}

# Event table: name -> discriminator. All verified against the IDL
# explicit arrays and/or fixture bytes.
EVENTS = {
    "CreatePoolEvent": _ev_disc("CreatePoolEvent"),
    "BuyEvent": _ev_disc("BuyEvent"),
    "SellEvent": _ev_disc("SellEvent"),
    "DepositEvent": _ev_disc("DepositEvent"),
    "WithdrawEvent": _ev_disc("WithdrawEvent"),
    "InitBoostEvent": _ev_disc("InitBoostEvent"),
    "CloseUserVolumeAccumulatorEvent":
        _ev_disc("CloseUserVolumeAccumulatorEvent"),
}
EV_BY_DISC = {disc: name for name, disc in EVENTS.items()}

# IDL account order for create_pool (used to name accounts in the
# decoded instruction record).
CREATE_POOL_ACCOUNTS = [
    "pool", "global_config", "creator", "base_mint", "quote_mint",
    "lp_mint", "user_base_token_account", "user_quote_token_account",
    "user_pool_token_account", "pool_base_token_account",
    "pool_quote_token_account", "system_program",
    "token_2022_program", "base_token_program",
    "quote_token_program", "associated_token_program",
    "event_authority", "program",
]

# IDL account order shared by the AMM trade instructions
# (buy / buy_v2 / buy_exact_quote_in / buy_exact_quote_in_v2 /
# sell / sell_v2).
TRADE_ACCOUNTS = [
    "pool", "user", "global_config", "base_mint", "quote_mint",
    "user_base_token_account", "user_quote_token_account",
    "pool_base_token_account", "pool_quote_token_account",
    "base_token_program", "quote_token_program", "system_program",
    "user_volume_accumulator", "fee_config", "buyback_fee_recipient",
    "event_authority", "program",
]

INSTRUCTION_ACCOUNTS = {
    "create_pool": CREATE_POOL_ACCOUNTS,
    "buy": TRADE_ACCOUNTS,
    "buy_v2": TRADE_ACCOUNTS,
    "buy_exact_quote_in": TRADE_ACCOUNTS,
    "buy_exact_quote_in_v2": TRADE_ACCOUNTS,
    "sell": TRADE_ACCOUNTS,
    "sell_v2": TRADE_ACCOUNTS,
}


class _ShortRead(ValueError):
    """Raised when a layout read runs past the end of the data."""


class _Reader(object):
    """Little-endian Borsh-style reader over a bytes buffer."""

    def __init__(self, data):
        self.buf = bytes(data)
        self.off = 0

    def left(self):
        return len(self.buf) - self.off

    def _need(self, n):
        if self.left() < n:
            raise _ShortRead("need %d bytes, %d left at offset %d"
                             % (n, self.left(), self.off))

    def u8(self):
        self._need(1)
        v = self.buf[self.off]
        self.off += 1
        return v

    def u16(self):
        self._need(2)
        v = struct.unpack_from("<H", self.buf, self.off)[0]
        self.off += 2
        return v

    def u32(self):
        self._need(4)
        v = struct.unpack_from("<I", self.buf, self.off)[0]
        self.off += 4
        return v

    def u64(self):
        self._need(8)
        v = struct.unpack_from("<Q", self.buf, self.off)[0]
        self.off += 8
        return v

    def i64(self):
        self._need(8)
        v = struct.unpack_from("<q", self.buf, self.off)[0]
        self.off += 8
        return v

    def i128(self):
        self._need(16)
        v = int.from_bytes(self.buf[self.off:self.off + 16],
                           "little", signed=True)
        self.off += 16
        return v

    def boolean(self):
        return bool(self.u8())

    def pubkey(self):
        self._need(32)
        v = b58encode(self.buf[self.off:self.off + 32])
        self.off += 32
        return v

    def string(self):
        n = self.u32()
        self._need(n)
        try:
            s = self.buf[self.off:self.off + n].decode("utf-8")
        except UnicodeDecodeError:
            raise _ShortRead("string field is not valid utf-8")
        self.off += n
        return s

    def bytes(self, n):
        self._need(n)
        v = self.buf[self.off:self.off + n]
        self.off += n
        return v


# ---------------------------------------------------------------------------
# Program-derived addresses (pure stdlib ed25519 off-curve check)
# ---------------------------------------------------------------------------
_ED25519_P = 2 ** 255 - 19
_ED25519_D = (-121665 * pow(121666, _ED25519_P - 2, _ED25519_P)) % _ED25519_P


def _is_on_curve(data32):
    """True when the 32 bytes decode to a point on ed25519."""
    if len(data32) != 32:
        return False
    y = int.from_bytes(data32, "little") & ((1 << 255) - 1)
    y2 = (y * y) % _ED25519_P
    num = (y2 - 1) % _ED25519_P
    den = (_ED25519_D * y2 + 1) % _ED25519_P
    xx = (num * pow(den, _ED25519_P - 2, _ED25519_P)) % _ED25519_P
    return xx == 0 or pow(xx, (_ED25519_P - 1) // 2, _ED25519_P) == 1


def find_program_address(seeds, program_id):
    """Return (address, bump) for a PDA, or raise ValueError."""
    prog = b58decode(program_id)
    for bump in range(255, -1, -1):
        data = b"".join(seeds) + bytes([bump]) + prog \
            + b"ProgramDerivedAddress"
        addr = hashlib.sha256(data).digest()
        if not _is_on_curve(addr):
            return b58encode(addr), bump
    raise ValueError("no PDA bump found for seeds %r" % (seeds,))


def derive_pool_pda(index, creator, base_mint, quote_mint):
    """Pool PDA: seeds [b"pool", index u16 LE, creator, base, quote]."""
    addr, _bump = find_program_address(
        [b"pool", struct.pack("<H", index), b58decode(creator),
         b58decode(base_mint), b58decode(quote_mint)],
        PUMPSWAP_PROGRAM_ID)
    return addr


def derive_pool_authority_pda(base_mint):
    """Pool authority PDA: seeds [b"pool-authority", base_mint]."""
    addr, _bump = find_program_address(
        [b"pool-authority", b58decode(base_mint)], PUMPSWAP_PROGRAM_ID)
    return addr


# ---------------------------------------------------------------------------
# Pool account (301 bytes: 287-byte IDL struct + 14 trailing bytes)
# ---------------------------------------------------------------------------
def decode_pool(data):
    """Decode a Pool account.

    `data` is raw bytes or a base64 string (getAccountInfo shape).
    Returns a dict with the account fields plus ``validation``:
    "verified" when the discriminator matches, the IDL fields parse
    and the 14 trailing bytes are all zero; "unverified" when the
    length differs or the trailing bytes are non-zero; "unknown"
    when the discriminator does not match. Never raises on bad input.
    """
    out = {"account_type": "Pool",
           "program_id": PUMPSWAP_PROGRAM_ID,
           "decoder_version": DECODER_VERSION}
    try:
        raw = base64.b64decode(data) if isinstance(data, str) else bytes(data)
    except Exception:
        out["validation"] = "unknown"
        out["notes"] = ["account data is not bytes or base64"]
        return out
    if len(raw) < 8 or tuple(raw[:8]) != POOL_DISC:
        out["validation"] = "unknown"
        out["notes"] = ["discriminator does not match Pool"]
        return out
    if len(raw) != POOL_LEN:
        out["validation"] = "unverified"
        out["notes"] = ["unexpected account length %d (expected %d)"
                        % (len(raw), POOL_LEN)]
        return out
    r = _Reader(raw)
    r.bytes(8)  # discriminator
    out.update({
        "pool_bump": r.u8(),
        "index": r.u16(),
        "creator": r.pubkey(),
        "base_mint": r.pubkey(),
        "quote_mint": r.pubkey(),
        "lp_mint": r.pubkey(),
        # IDL order: base vault @139, quote vault @171 (proven by
        # mint-matching in the migrate fixture; see module docstring).
        "pool_base_token_account": r.pubkey(),
        "pool_quote_token_account": r.pubkey(),
        "lp_supply": r.u64(),
        "coin_creator": r.pubkey(),
        "is_mayhem_mode": r.boolean(),
        "is_cashback_coin": r.boolean(),
        "virtual_quote_reserves": r.i128(),
        "creator_fee_bps": r.u64(),
        "can_edit_creator_fee": r.boolean(),
        "is_holder_reward": r.boolean(),
        "protocol_fees": r.u64(),
        "creator_fees": r.u64(),
    })
    fields_end = r.off
    trailing = r.bytes(r.left()) if r.left() else b""
    out["trailing_bytes_hex"] = trailing.hex()
    if fields_end != POOL_IDL_LEN:
        out["validation"] = "unverified"
        out["notes"] = ["IDL fields consumed %d bytes, expected %d"
                        % (fields_end, POOL_IDL_LEN)]
    elif trailing and any(trailing):
        out["validation"] = "unverified"
        out["notes"] = ["trailing account bytes are non-zero"]
    else:
        out["validation"] = "verified"
    return out


def pool_price_from_vaults(base_balance, quote_balance,
                           base_decimals=6, quote_decimals=6):
    """Quote-per-base price from pool vault balances.

    Returns None when either balance is missing or base is 0.
    Decimals default to 6/6 (pump.fun tokens vs WSOL).
    """
    if not base_balance or not quote_balance:
        return None
    base = base_balance / (10 ** base_decimals)
    quote = quote_balance / (10 ** quote_decimals)
    if base <= 0:
        return None
    return quote / base


# ---------------------------------------------------------------------------
# Instruction decoding
# ---------------------------------------------------------------------------
def _decode_args(layout, reader):
    """Decode instruction args per a layout list.

    Returns (fields, notes, truncated). A short read at an Option*
    field (or later) is EOF-tolerant — the missing trailing option
    fields default to None and truncated stays False. A short read
    at a required field sets truncated True.
    """
    fields = {}
    notes = []
    truncated = False
    seen_option = False
    for spec in layout:
        kind, name = spec[0], spec[1]
        if kind in ("option_bool", "option_u64"):
            seen_option = True
        try:
            if kind == "u64":
                fields[name] = reader.u64()
            elif kind == "u16":
                fields[name] = reader.u16()
            elif kind == "bool":
                fields[name] = reader.boolean()
            elif kind == "option_bool":
                fields[name] = reader.boolean()
            elif kind == "option_u64":
                fields[name] = reader.u64()
            elif kind == "pubkey":
                fields[name] = reader.pubkey()
            elif kind == "string":
                fields[name] = reader.string()
            elif kind == "raw":
                fields[name] = reader.bytes(spec[2]).hex()
            else:
                notes.append("unknown arg kind %r for %s" % (kind, name))
        except _ShortRead:
            fields[name] = None
            if seen_option:
                notes.append("trailing arg %s missing (EOF-tolerant)"
                             % name)
            else:
                truncated = True
                notes.append("truncated at required arg %s" % name)
            break
    return fields, notes, truncated


def decode_instruction(data, accounts=None):
    """Decode one PumpSwap instruction's data bytes.

    `data` is raw bytes (or a base58 string, as in jsonParsed).
    `accounts` is the instruction's account list (base58 strings);
    when supplied it is attached verbatim and, for known
    instructions, also mapped to IDL account names under
    ``named_accounts``. Unknown discriminators yield
    instruction_name "unknown" and validation "unknown" — never an
    invented instruction.
    """
    out = {"program_id": PUMPSWAP_PROGRAM_ID,
           "decoder_version": DECODER_VERSION}
    if accounts is not None:
        out["accounts"] = list(accounts)
    try:
        raw = b58decode(data) if isinstance(data, str) else bytes(data)
    except Exception:
        out["instruction_name"] = "unknown"
        out["validation"] = "unknown"
        out["notes"] = ["instruction data is not bytes or base58"]
        return out
    if len(raw) < 8:
        out["instruction_name"] = "unknown"
        out["validation"] = "unknown"
        out["notes"] = ["instruction data shorter than 8-byte "
                        "discriminator"]
        return out
    disc = tuple(raw[:8])
    if disc == EMIT_DISC:
        out["instruction_name"] = "emit_event"
        out["validation"] = "unverified"
        out["notes"] = ["event-emission self-CPI; decode with "
                        "decode_event"]
        return out
    name = IX_BY_DISC.get(disc)
    if name is None:
        out["instruction_name"] = "unknown"
        out["validation"] = "unknown"
        out["discriminator"] = list(disc)
        out["notes"] = ["unrecognised PumpSwap instruction "
                        "discriminator"]
        return out
    _disc, layout, observed = INSTRUCTIONS[name]
    reader = _Reader(raw[8:])
    args, notes, truncated = _decode_args(layout, reader)
    if not truncated and reader.left():
        n_trailing = reader.left()
        args["trailing_bytes_hex"] = reader.bytes(n_trailing).hex()
        notes.append("%d trailing arg bytes kept opaque" % n_trailing)
    out["instruction_name"] = name
    out["args"] = args
    out["notes"] = notes
    if accounts is not None and name in INSTRUCTION_ACCOUNTS:
        names = INSTRUCTION_ACCOUNTS[name]
        out["named_accounts"] = {
            names[i]: accounts[i]
            for i in range(min(len(names), len(accounts)))}
    if not observed:
        out["validation"] = "unverified"
        notes.append("IDL layout, no fixture observed for %s" % name)
    elif truncated or any("trailing arg bytes" in n for n in notes):
        out["validation"] = "unverified"
    else:
        out["validation"] = "verified"
    return out


# ---------------------------------------------------------------------------
# Event decoding
# ---------------------------------------------------------------------------
def _decode_trade_event(r, is_buy):
    """Shared BuyEvent / SellEvent body layout."""
    f = {}
    f["timestamp"] = r.i64()
    if is_buy:
        f["base_amount_out"] = r.u64()
        f["max_quote_amount_in"] = r.u64()
    else:
        f["base_amount_in"] = r.u64()
        f["min_quote_amount_out"] = r.u64()
    f["user_base_token_reserves"] = r.u64()
    f["user_quote_token_reserves"] = r.u64()
    f["pool_base_token_reserves"] = r.u64()
    f["pool_quote_token_reserves"] = r.u64()
    if is_buy:
        f["quote_amount_in"] = r.u64()
    else:
        f["quote_amount_out"] = r.u64()
    f["lp_fee_basis_points"] = r.u64()
    f["lp_fee"] = r.u64()
    f["protocol_fee_basis_points"] = r.u64()
    f["protocol_fee"] = r.u64()
    if is_buy:
        f["quote_amount_in_with_lp_fee"] = r.u64()
        f["user_quote_amount_in"] = r.u64()
    else:
        f["quote_amount_out_without_lp_fee"] = r.u64()
        f["user_quote_amount_out"] = r.u64()
    f["pool"] = r.pubkey()
    f["user"] = r.pubkey()
    f["user_base_token_account"] = r.pubkey()
    f["user_quote_token_account"] = r.pubkey()
    f["protocol_fee_recipient"] = r.pubkey()
    f["protocol_fee_recipient_token_account"] = r.pubkey()
    f["coin_creator"] = r.pubkey()
    f["coin_creator_fee_basis_points"] = r.u64()
    f["coin_creator_fee"] = r.u64()
    if is_buy:
        f["track_volume"] = r.boolean()
        f["total_unclaimed_tokens"] = r.u64()
        f["total_claimed_tokens"] = r.u64()
        f["current_sol_volume"] = r.u64()
        f["last_update_timestamp"] = r.i64()
        f["min_base_amount_out"] = r.u64()
        f["ix_name"] = r.string()
    f["cashback_fee_basis_points"] = r.u64()
    f["cashback"] = r.u64()
    f["buyback_fee_basis_points"] = r.u64()
    f["buyback_fee"] = r.u64()
    f["virtual_quote_reserves"] = r.i128()
    f["can_boost"] = r.boolean()
    f["base_supply"] = r.u64()
    f["holder_rewards_bps"] = r.u64()
    f["holder_rewards"] = r.u64()
    f["creator_fee_unclaimed"] = r.u64()
    if r.left() == 8:
        f["trailing_u64"] = r.u64()
    elif r.left() > 0:
        f["trailing_bytes_hex"] = r.bytes(r.left()).hex()
    return f


def _decode_event_body(name, body):
    """Decode an event body (discriminator already stripped).

    Returns (fields, validation, notes). BuyEvent / SellEvent
    bodies carry one trailing u64 (8 zero bytes) beyond the IDL
    layout; it is read as ``trailing_u64`` and must be 0 for
    "verified".
    """
    r = _Reader(body)
    f = {}
    try:
        if name == "CreatePoolEvent":
            f["timestamp"] = r.i64()
            f["index"] = r.u16()
            f["creator"] = r.pubkey()
            f["base_mint"] = r.pubkey()
            f["quote_mint"] = r.pubkey()
            f["base_decimals"] = r.u8()
            f["quote_decimals"] = r.u8()
            f["base_amount_in"] = r.u64()
            f["quote_amount_in"] = r.u64()
            f["pool_base_token_reserves"] = r.u64()
            f["pool_quote_token_reserves"] = r.u64()
            f["minimum_liquidity"] = r.u64()
            f["initial_liquidity"] = r.u64()
            f["lp_token_amount_out"] = r.u64()
            f["pool_bump"] = r.u8()
            f["pool"] = r.pubkey()
            f["lp_mint"] = r.pubkey()
            f["user_base_token_account"] = r.pubkey()
            f["user_quote_token_account"] = r.pubkey()
            f["coin_creator"] = r.pubkey()
            f["is_mayhem_mode"] = r.boolean()
            f["creator_fee_bps"] = r.u64()
            f["can_edit_creator_fee"] = r.boolean()
            f["is_holder_reward"] = r.boolean()
        elif name == "BuyEvent":
            f = _decode_trade_event(r, True)
        elif name == "SellEvent":
            f = _decode_trade_event(r, False)
        elif name == "DepositEvent":
            f["timestamp"] = r.i64()
            f["lp_token_amount_out"] = r.u64()
            f["max_base_amount_in"] = r.u64()
            f["max_quote_amount_in"] = r.u64()
            f["user_base_token_reserves"] = r.u64()
            f["user_quote_token_reserves"] = r.u64()
            f["pool_base_token_reserves"] = r.u64()
            f["pool_quote_token_reserves"] = r.u64()
            f["base_amount_in"] = r.u64()
            f["quote_amount_in"] = r.u64()
            f["lp_mint_supply"] = r.u64()
            f["pool"] = r.pubkey()
            f["user"] = r.pubkey()
            f["user_base_token_account"] = r.pubkey()
            f["user_quote_token_account"] = r.pubkey()
            f["user_pool_token_account"] = r.pubkey()
        elif name == "WithdrawEvent":
            f["timestamp"] = r.i64()
            f["lp_token_amount_in"] = r.u64()
            f["min_base_amount_out"] = r.u64()
            f["min_quote_amount_out"] = r.u64()
            f["user_base_token_reserves"] = r.u64()
            f["user_quote_token_reserves"] = r.u64()
            f["pool_base_token_reserves"] = r.u64()
            f["pool_quote_token_reserves"] = r.u64()
            f["base_amount_out"] = r.u64()
            f["quote_amount_out"] = r.u64()
            f["lp_mint_supply"] = r.u64()
            f["pool"] = r.pubkey()
            f["user"] = r.pubkey()
            f["user_base_token_account"] = r.pubkey()
            f["user_quote_token_account"] = r.pubkey()
            f["user_pool_token_account"] = r.pubkey()
        elif name == "InitBoostEvent":
            f["timestamp"] = r.i64()
            f["mint"] = r.pubkey()
            f["bonding_curve"] = r.pubkey()
            f["pool"] = r.pubkey()
            f["virtual_quote_reserves"] = r.i128()
            f["real_quote_reserves_after"] = r.u64()
        elif name == "CloseUserVolumeAccumulatorEvent":
            f["user"] = r.pubkey()
            f["timestamp"] = r.i64()
            f["total_unclaimed_tokens"] = r.u64()
            f["total_claimed_tokens"] = r.u64()
            f["current_sol_volume"] = r.u64()
            f["last_update_timestamp"] = r.i64()
        else:
            return None, "unknown", ["no layout for event %s" % name]
    except _ShortRead as exc:
        return f, "unverified", ["truncated event body: %s" % exc]
    notes = []
    if r.left():
        notes.append("%d unconsumed body bytes" % r.left())
        return f, "unverified", notes
    if name in ("BuyEvent", "SellEvent"):
        trailing = f.get("trailing_u64")
        if trailing is None:
            return f, "unverified", [
                "%s missing trailing u64" % name]
        if trailing != 0:
            return f, "unverified", [
                "trailing_u64 is %d, expected 0" % trailing]
    return f, "verified", notes


def decode_event(data):
    """Decode one emitted PumpSwap event.

    `data` is the raw event bytes (discriminator + body), or the full
    Anchor emit-CPI payload (EMIT_DISC + event), as bytes or a base58
    string. Returns a dict with ``event_name``, ``fields``,
    ``validation`` and ``notes``. Unknown discriminators yield
    event_name "unknown" and validation "unknown".
    """
    out = {"program_id": PUMPSWAP_PROGRAM_ID,
           "decoder_version": DECODER_VERSION}
    try:
        raw = b58decode(data) if isinstance(data, str) else bytes(data)
    except Exception:
        out["event_name"] = "unknown"
        out["validation"] = "unknown"
        out["notes"] = ["event data is not bytes or base58"]
        return out
    if len(raw) >= 8 and tuple(raw[:8]) == EMIT_DISC:
        raw = raw[8:]
    if len(raw) < 8:
        out["event_name"] = "unknown"
        out["validation"] = "unknown"
        out["notes"] = ["event data shorter than 8-byte discriminator"]
        return out
    name = EV_BY_DISC.get(tuple(raw[:8]))
    if name is None:
        out["event_name"] = "unknown"
        out["validation"] = "unknown"
        out["discriminator"] = list(raw[:8])
        out["notes"] = ["unrecognised PumpSwap event discriminator"]
        return out
    fields, validation, notes = _decode_event_body(name, raw[8:])
    out["event_name"] = name
    out["fields"] = fields if fields is not None else {}
    out["validation"] = validation
    out["notes"] = notes
    return out


# ---------------------------------------------------------------------------
# Transaction decoding (minimal local parse of getTransaction jsonParsed)
# ---------------------------------------------------------------------------
def _account_keys(message):
    keys = message.get("accountKeys") or []
    out = []
    for k in keys:
        out.append(k.get("pubkey") if isinstance(k, dict) else k)
    return out


def _ix_accounts(ix, keys):
    out = []
    for item in ix.get("accounts") or []:
        if isinstance(item, int) and 0 <= item < len(keys):
            out.append(keys[item])
        elif isinstance(item, str):
            # jsonParsed inner instructions carry pubkey strings
            # directly, not indices into accountKeys.
            out.append(item)
    return out


def _ix_data_bytes(ix):
    """Instruction data as bytes, or None when absent/undecodable."""
    data = ix.get("data")
    if not isinstance(data, str) or not data:
        return None
    try:
        return b58decode(data)
    except ValueError:
        return None


def _envelope(signature, slot, block_time, program_id, event_type,
              instruction, fields, raw, validation, notes):
    return {
        "signature": signature,
        "slot": slot,
        "block_time": block_time,
        "program_id": program_id,
        "event_type": event_type,
        "instruction": instruction,
        "fields": fields,
        "raw": base64.b64encode(raw).decode() if raw is not None else None,
        "decoder_version": DECODER_VERSION,
        "validation": validation,
        "notes": list(notes or []),
    }


def _log_program_stack(logs):
    """Attribute each 'Program data:' log line to the program most
    recently invoked and not yet finished. Returns a list of
    (program_id, base64_data) for lines attributable to a program."""
    out = []
    stack = []
    for line in logs or []:
        if not isinstance(line, str):
            continue
        if line.startswith("Program ") and " invoke [" in line:
            stack.append(line[len("Program "):].split(" invoke [")[0])
        elif line.startswith("Program ") and line.endswith(" success"):
            prog = line[len("Program "):].rsplit(" success", 1)[0]
            if prog in stack:
                stack.remove(prog)
        elif line.startswith("Program data: "):
            if stack:
                out.append((stack[-1], line[len("Program data: "):]))
    return out


def decode_transaction(tx, signature=None):
    """Decode a getTransaction (jsonParsed) result for PumpSwap.

    Same envelope contract as the pumpfun decoder: a failed
    transaction yields a single ``transaction_failed`` entry and NO
    trade events; unknown programs/instructions/events yield
    ``unknown`` entries, never an invented event.
    """
    if not isinstance(tx, dict):
        return [_envelope(signature, None, None, PUMPSWAP_PROGRAM_ID,
                          "unknown", {}, {},
                          None, "unknown",
                          ["transaction is not a dict"])]
    inner = tx.get("transaction") if isinstance(tx.get("transaction"),
                                                dict) else tx
    meta = tx.get("meta") or {}
    if not meta and isinstance(inner.get("meta"), dict):
        meta = inner["meta"]
    message = inner.get("message") or {}
    sig = signature
    if sig is None:
        # jsonParsed: signatures live on the transaction object,
        # not in the message.
        sigs = inner.get("signatures") or message.get("signatures") or []
        sig = sigs[0] if sigs else None
    slot = tx.get("slot")
    if slot is None:
        slot = inner.get("slot")
    block_time = tx.get("blockTime")
    if block_time is None:
        block_time = inner.get("blockTime")
    keys = _account_keys(message)

    if meta.get("err") is not None:
        return [_envelope(
            sig, slot, block_time, PUMPSWAP_PROGRAM_ID,
            "transaction_failed",
            {"instruction_name": None, "index": None, "accounts": []},
            {"error": meta.get("err")}, None, "unverified",
            ["transaction failed; no trade events decoded"])]
    if block_time is None and isinstance(tx.get("blockTime"), int):
        block_time = tx.get("blockTime")

    events = []
    seen = set()  # (program_id, disc, body) dedupe: inner ix vs logs

    def _emit_event(raw, ix_info):
        if len(raw) < 16 or tuple(raw[:8]) != EMIT_DISC:
            return
        disc = tuple(raw[8:16])
        body = raw[16:]
        name = EV_BY_DISC.get(disc)
        if name is None:
            events.append(_envelope(
                sig, slot, block_time, PUMPSWAP_PROGRAM_ID, "unknown",
                ix_info, {"discriminator": list(disc)},
                raw, "unknown",
                ["unrecognised PumpSwap event discriminator"]))
            return
        key = (PUMPSWAP_PROGRAM_ID, disc, bytes(body))
        if key in seen:
            return
        seen.add(key)
        fields, validation, notes = _decode_event_body(name, body)
        events.append(_envelope(
            sig, slot, block_time, PUMPSWAP_PROGRAM_ID, "event:" + name,
            ix_info, fields if fields is not None else {}, raw,
            validation, notes))

    def _handle_ix(raw, ix_info, accounts):
        if raw is None:
            return
        if len(raw) >= 8 and tuple(raw[:8]) == EMIT_DISC:
            _emit_event(raw, ix_info)
            return
        dec = decode_instruction(raw, accounts)
        name = dec.get("instruction_name")
        if name in (None, "unknown", "emit_event"):
            if name == "unknown":
                events.append(_envelope(
                    sig, slot, block_time, PUMPSWAP_PROGRAM_ID, "unknown",
                    ix_info, {"discriminator": dec.get("discriminator")},
                    raw, "unknown", dec.get("notes") or []))
            return
        fields = dict(dec.get("args") or {})
        if dec.get("named_accounts"):
            fields["named_accounts"] = dec["named_accounts"]
        events.append(_envelope(
            sig, slot, block_time, PUMPSWAP_PROGRAM_ID,
            "instruction:" + name, ix_info, fields,
            raw, dec.get("validation", "unverified"),
            dec.get("notes") or []))

    top = message.get("instructions") or []
    inner = meta.get("innerInstructions") or []
    inner_by_index = {}
    for group in inner:
        inner_by_index.setdefault(group.get("index"), []).extend(
            group.get("instructions") or [])
    for i, ix in enumerate(top):
        if not isinstance(ix, dict):
            continue
        if ix.get("programId") != PUMPSWAP_PROGRAM_ID:
            # A router/Jupiter top-level instruction can wrap AMM CPIs.
            for sub in inner_by_index.get(i, []):
                if isinstance(sub, dict) \
                        and sub.get("programId") == PUMPSWAP_PROGRAM_ID:
                    _handle_ix(
                        _ix_data_bytes(sub),
                        {"instruction_name": None, "index": i,
                         "inner_of": i,
                         "accounts": _ix_accounts(sub, keys)},
                        _ix_accounts(sub, keys))
            continue
        ix_info = {"instruction_name": None, "index": i,
                   "accounts": _ix_accounts(ix, keys)}
        _handle_ix(_ix_data_bytes(ix), ix_info,
                   _ix_accounts(ix, keys))
        for sub in inner_by_index.get(i, []):
            if isinstance(sub, dict) \
                    and sub.get("programId") == PUMPSWAP_PROGRAM_ID:
                _handle_ix(
                    _ix_data_bytes(sub),
                    {"instruction_name": None, "index": i,
                     "inner_of": i,
                     "accounts": _ix_accounts(sub, keys)},
                    _ix_accounts(sub, keys))

    # Log fallback for events whose inner instruction data is absent.
    for prog, data_b64 in _log_program_stack(meta.get("logMessages")):
        if prog != PUMPSWAP_PROGRAM_ID:
            continue
        try:
            raw = base64.b64decode(data_b64)
        except Exception:
            continue
        if len(raw) < 8:
            continue
        disc = tuple(raw[:8])
        if disc not in EV_BY_DISC:
            continue
        key = (PUMPSWAP_PROGRAM_ID, disc, raw[8:])
        if key in seen:
            continue
        seen.add(key)
        name = EV_BY_DISC[disc]
        fields, validation, notes = _decode_event_body(name, raw[8:])
        events.append(_envelope(
            sig, slot, block_time, PUMPSWAP_PROGRAM_ID, "event:" + name,
            {"instruction_name": None, "index": None,
             "accounts": [], "source": "log"},
            fields if fields is not None else {},
            b"".join([bytes(EMIT_DISC), raw]), validation,
            notes + ["decoded from Program data log (no inner ix)"]))

    return events
