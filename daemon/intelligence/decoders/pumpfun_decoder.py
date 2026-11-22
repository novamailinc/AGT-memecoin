"""Pump.fun decoder (Phase 3b, 2026-11-22).

Decodes the Pump.fun bonding-curve program
``6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`` from standard Solana
``getTransaction`` (jsonParsed) responses and from raw account data:
token creation, bonding-curve initialisation, curve buys/sells,
SOL / token movement, reserve changes, curve progress, creator
activity, graduation (completion) and migration events.

Pure Python stdlib. Base58 comes from the sibling Phase 3a helper
``daemon.intelligence.decoders._base58`` when this file is imported
as part of the package; a local fallback is kept so the module also
works as a bare file import.

Layouts verified against the official program IDL
(https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/idl/pump.json,
``address`` field matches the program id above) AND against real
on-chain transactions captured in ``tests/data/intelligence/``
(ATX mint ``BiLAn35QaVqQC8f8BGrE92r2n5dcDEJmqJcx1v9Apump`` and NOTAI
mint ``8bGA7uQRGURruJLacPopiTSJtzS49VvLL7C5yDBpump``).

Discriminators follow the Anchor rule ``sha256("global:<name>")[:8]``
for instructions and ``sha256("event:<Name>")[:8]`` for events; every
discriminator used here was checked against the IDL's explicit
arrays and/or fixture bytes. The one exception is the Anchor
event-emission self-CPI prefix ``EMIT_DISC`` below: it is NOT
``sha256("global:emit")`` and is not derivable from the IDL. It is
an observed constant — in every fixture the bytes after it are a
known event discriminator followed by that event's exact body, so
it is treated structurally (prefix + event), never assumed.

Deployed-program notes (all fixture-verified, 2026-11-22):
* ``TradeEvent`` bodies carry one trailing u64 (8 zero bytes) beyond
  the main-branch IDL layout. The decoder reads it as
  ``trailing_u64``; validation is ``verified`` only when it is 0,
  ``unverified`` otherwise. Its semantics are not interpreted.
* ``create_v2`` args are EOF-tolerant: name, symbol, uri, creator
  (pubkey), is_mayhem_mode (bool), is_cashback_enabled (OptionBool,
  1 byte), creator_fee_bps (OptionU64, 8 bytes), is_holder_reward
  (OptionBool, 1 byte). The ``creator`` ARG is reported as
  ``creator_arg`` and is NOT treated as the creator: for ATX it is
  ``9hUgTsXY7E2ByYvuSuDW5Yw97r913w5GJrmJ1aGFfuAw`` while the curve
  creator (CreateEvent / BondingCurve account / first signer) is
  ``An7SUk47W5C3Hvoqgw3y4rHafw244vyHq2HXXTJrKm7``; for NOTAI the arg
  matches the creator. The authoritative creator is the event /
  account creator.
* ``buy_exact_quote_in_v3`` args are spendable_quote_in (u64),
  min_tokens_out (u64), partial_fill (OptionBool, 1 byte).
* The legacy ``buy`` / ``sell`` / ``buy_exact_quote_in``
  instructions are decoded by the Anchor rule + layout by analogy
  with the v2/v3 variants; no legacy curve trade was observed in
  fixtures, so those decodes are always ``unverified``.
* The newer bonding-curve program ``BQFtMNSBKP3qLsoyocazbKA7fB8313jizRCSs1R6hBma``
  (create_v2 / buy_v2 / sell_v2 on a different program id) is OUT OF
  SCOPE for this phase: transactions on it yield ``unknown``.

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
DECODER_NAME = "pumpfun_decoder"

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

PUMP_PROGRAM_ID = _solana_ids.PUMP_PROGRAM_ID
# Newer bonding-curve program (pumpdocs.fun, 2026-11-22): recognised
# but NOT decoded by this module — out of scope for Phase 3b.
PUMP_PROGRAM_ID_V2 = "BQFtMNSBKP3qLsoyocazbKA7fB8313jizRCSs1R6hBma"
PUMPSWAP_PROGRAM_ID = _solana_ids.PUMPSWAP_PROGRAM_ID

WSOL_MINT = _solana_ids.WSOL_MINT
USDC_MINT = _solana_ids.USDC_MINT
SPL_TOKEN_PROGRAM = _solana_ids.TOKEN_PROGRAM_ID
TOKEN_2022_PROGRAM = _solana_ids.TOKEN2022_PROGRAM_ID
SYSTEM_PROGRAM = _solana_ids.SYSTEM_PROGRAM_ID

BONDING_CURVE_DISC = (23, 183, 248, 55, 96, 216, 172, 96)
BONDING_CURVE_LEN = 166

# Anchor event-emission self-CPI prefix, observed in every fixture
# on both the pump and the AMM program. NOT sha256("global:emit").
EMIT_DISC = (228, 69, 165, 46, 81, 203, 154, 29)

# Standard pump.fun curve constant: at creation the real token
# reserves are 79.31% of total supply (793.1B of a 1T supply).
# Used only as a fallback when the creation-time real reserves are
# not known; progress computed this way is labelled an estimate.
INITIAL_REAL_RESERVES_BPS = 7931


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
# arg-layout entries are ("u64", field) / ("u16", field) /
# ("bool", field) / ("option_bool", field) / ("option_u64", field) /
# ("pubkey", field) / ("string", field) / ("raw", field, nbytes).
# "observed" is True when the layout was verified against a real
# fixture transaction; False means Anchor-rule discriminator +
# layout by analogy, always decoded as "unverified".
# ---------------------------------------------------------------------------
INSTRUCTIONS = {
    "create_v2": (
        (214, 144, 76, 236, 95, 139, 49, 180),
        [("string", "name"), ("string", "symbol"), ("string", "uri"),
         ("pubkey", "creator_arg"), ("bool", "is_mayhem_mode"),
         ("option_bool", "is_cashback_enabled"),
         ("option_u64", "creator_fee_bps"),
         ("option_bool", "is_holder_reward")],
        True),
    "buy_v2": (
        (184, 23, 238, 97, 103, 197, 211, 61),
        [("u64", "base_amount_out"), ("u64", "max_quote_amount_in")],
        True),
    "buy_v3": (
        (7, 5, 29, 196, 245, 23, 101, 80),
        [("u64", "base_amount_out"), ("u64", "max_quote_amount_in")],
        True),
    "buy_exact_quote_in_v3": (
        (225, 247, 80, 30, 213, 179, 132, 136),
        [("u64", "spendable_quote_in"), ("u64", "min_tokens_out"),
         ("option_bool", "partial_fill")],
        True),
    "sell_v3": (
        (28, 146, 222, 119, 38, 196, 105, 213),
        [("u64", "base_amount_in"), ("u64", "min_quote_amount_out")],
        True),
    "migrate_v2": (
        (187, 203, 18, 31, 206, 237, 254, 41),
        [],
        True),
    "close_user_volume_accumulator": (
        (249, 69, 164, 218, 150, 103, 84, 138),
        [],
        True),
    # Legacy variants: discriminator by the Anchor rule, layout by
    # analogy with the v2/v3 variants. No fixture observed: always
    # decoded "unverified".
    "buy": (
        _ix_disc("buy"),
        [("u64", "base_amount_out"), ("u64", "max_quote_amount_in")],
        False),
    "sell": (
        _ix_disc("sell"),
        [("u64", "base_amount_in"), ("u64", "min_quote_amount_out")],
        False),
    "buy_exact_quote_in": (
        _ix_disc("buy_exact_quote_in"),
        [("u64", "spendable_quote_in"), ("u64", "min_tokens_out"),
         ("option_bool", "partial_fill")],
        False),
}

# Reverse lookup: discriminator tuple -> instruction name.
IX_BY_DISC = {disc: name for name, (disc, _args, _obs)
              in INSTRUCTIONS.items()}

# Event table: name -> discriminator. All verified against the IDL
# explicit arrays and/or fixture bytes.
EVENTS = {
    "CreateEvent": _ev_disc("CreateEvent"),
    "TradeEvent": _ev_disc("TradeEvent"),
    "CompleteEvent": _ev_disc("CompleteEvent"),
    "CompletePumpAmmMigrationEvent":
        _ev_disc("CompletePumpAmmMigrationEvent"),
    "PostCompleteBuyEvent": _ev_disc("PostCompleteBuyEvent"),
    "CloseUserVolumeAccumulatorEvent":
        _ev_disc("CloseUserVolumeAccumulatorEvent"),
}
EV_BY_DISC = {disc: name for name, disc in EVENTS.items()}


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


def derive_curve_pda(mint):
    """Bonding-curve PDA for a mint: seeds [b"bonding-curve", mint]."""
    addr, _bump = find_program_address(
        [b"bonding-curve", b58decode(mint)], PUMP_PROGRAM_ID)
    return addr


# ---------------------------------------------------------------------------
# BondingCurve account (166 bytes, IDL layout, fixture-verified exact)
# ---------------------------------------------------------------------------
def decode_bonding_curve(data):
    """Decode a BondingCurve account.

    `data` is raw bytes or a base64 string (getAccountInfo shape).
    Returns a dict with the account fields plus ``validation``:
    "verified" when the discriminator matches and all 166 bytes are
    consumed exactly, "unverified" when the length differs but the
    discriminator matches, "unknown" otherwise. Never raises on bad
    input — bad input yields "unknown".
    """
    out = {"account_type": "BondingCurve",
           "program_id": PUMP_PROGRAM_ID,
           "decoder_version": DECODER_VERSION}
    try:
        raw = base64.b64decode(data) if isinstance(data, str) else bytes(data)
    except Exception:
        out["validation"] = "unknown"
        out["notes"] = ["account data is not bytes or base64"]
        return out
    if len(raw) < 8 or tuple(raw[:8]) != BONDING_CURVE_DISC:
        out["validation"] = "unknown"
        out["notes"] = ["discriminator does not match BondingCurve"]
        return out
    if len(raw) != BONDING_CURVE_LEN:
        out["validation"] = "unverified"
        out["notes"] = ["unexpected account length %d (expected %d)"
                        % (len(raw), BONDING_CURVE_LEN)]
        return out
    r = _Reader(raw)
    r.bytes(8)  # discriminator
    out.update({
        "virtual_token_reserves": r.u64(),
        "virtual_quote_reserves": r.u64(),
        "real_token_reserves": r.u64(),
        "real_quote_reserves": r.u64(),
        "token_total_supply": r.u64(),
        "complete": r.boolean(),
        "creator": r.pubkey(),
        "is_mayhem_mode": r.boolean(),
        "is_cashback_coin": r.boolean(),
        "quote_mint": r.pubkey(),
        "creator_fee_bps": r.u64(),
        "can_edit_creator_fee": r.boolean(),
        "is_holder_reward": r.boolean(),
        "creator_fee": r.u64(),
        "protocol_fees": r.u64(),
        "depth": r.u8(),
        "initial_virtual_quote_reserves": r.u64(),
        "post_complete_base_out": r.u64(),
        "post_complete_quote_in": r.u64(),
    })
    # Legacy SOL-reserve aliases (the curve stores quote reserves;
    # for a WSOL quote the legacy SOL fields are 0 and the quote
    # fields carry the amounts — see TradeEvent notes).
    out["virtual_sol_reserves"] = out["virtual_quote_reserves"]
    out["real_sol_reserves"] = out["real_quote_reserves"]
    out["validation"] = "verified" if r.left() == 0 else "unverified"
    if r.left():
        out["notes"] = ["%d unconsumed bytes" % r.left()]
    return out


def is_graduated(curve):
    """True when the curve is complete (graduated / migrated-eligible).

    `curve` is a dict from decode_bonding_curve. Graduation is the
    `complete` flag, or equivalently real token reserves reaching 0.
    """
    if not isinstance(curve, dict):
        return False
    if curve.get("complete") is True:
        return True
    return curve.get("real_token_reserves") == 0


def curve_progress(curve, initial_real_token_reserves=None):
    """Curve progress and price from a decoded BondingCurve dict.

    Returns {"progress_pct", "price_quote_per_token",
    "complete", "progress_basis"}. Progress is the fraction of the
    initial real token reserves already sold. When the creation-time
    real reserves are not supplied, the standard pump.fun constant
    (79.31% of total supply) is used and the basis is labelled
    "estimated_standard_constant". Price is quote units per base
    unit from the virtual reserves (for a WSOL quote with 6-decimal
    base and quote, this is SOL per token).
    """
    total = curve.get("token_total_supply") or 0
    real = curve.get("real_token_reserves")
    v_token = curve.get("virtual_token_reserves") or 0
    v_quote = curve.get("virtual_quote_reserves") or 0
    if real is None:
        return {"progress_pct": None, "price_quote_per_token": None,
                "complete": is_graduated(curve),
                "progress_basis": "unknown_no_reserves"}
    if initial_real_token_reserves is None:
        initial = total * INITIAL_REAL_RESERVES_BPS // 10000 \
            if total else 0
        basis = "estimated_standard_constant"
    else:
        initial = initial_real_token_reserves
        basis = "creation_real_reserves"
    if initial and initial > 0:
        progress = (initial - real) * 100.0 / initial
        progress = max(0.0, min(100.0, progress))
    else:
        progress = 100.0 if real == 0 else None
    price = (v_quote / v_token) if v_token else None
    return {"progress_pct": progress, "price_quote_per_token": price,
            "complete": is_graduated(curve), "progress_basis": basis}


# ---------------------------------------------------------------------------
# Instruction decoding
# ---------------------------------------------------------------------------
def _decode_args(layout, reader):
    """Decode instruction args per a layout list.

    Returns (fields, notes, truncated). A short read at an Option*
    field (or later) is EOF-tolerant per the IDL docs — the missing
    trailing option fields default to None and truncated stays
    False. A short read at a required field sets truncated True.
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
                # OptionBool is a 1-byte bool in the deployed encoding.
                fields[name] = reader.boolean()
            elif kind == "option_u64":
                # OptionU64 is an 8-byte u64 in the deployed encoding.
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
    """Decode one pump-program instruction's data bytes.

    `data` is raw bytes (or a base58 string, as in jsonParsed).
    `accounts` is the instruction's account list (base58 strings),
    attached verbatim when supplied. Returns a dict with
    ``instruction_name``, ``args``, ``validation`` and ``notes``.
    Unknown discriminators yield instruction_name "unknown" and
    validation "unknown" — never an invented instruction.
    """
    out = {"program_id": PUMP_PROGRAM_ID,
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
        # Event-emission self-CPI: handled by decode_event, not here.
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
        out["notes"] = ["unrecognised pump instruction discriminator"]
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
    if not observed:
        out["validation"] = "unverified"
        notes.append("layout by analogy with the v2/v3 variants; "
                     "no fixture observed for %s" % name)
    elif truncated or any("trailing arg bytes" in n for n in notes):
        out["validation"] = "unverified"
    else:
        # EOF-tolerant missing option args are documented behaviour.
        out["validation"] = "verified"
    return out


# ---------------------------------------------------------------------------
# Event decoding
# ---------------------------------------------------------------------------
def _read_shareholders(reader):
    n = reader.u32()
    holders = []
    for _ in range(n):
        holders.append({"address": reader.pubkey(),
                        "share_bps": reader.u16()})
    return holders


def _decode_event_body(name, body):
    """Decode an event body (discriminator already stripped).

    Returns (fields, validation, notes). The deployed program appends
    one trailing u64 (8 zero bytes) to TradeEvent bodies; it is read
    as ``trailing_u64`` and must be 0 for "verified".
    """
    r = _Reader(body)
    f = {}
    try:
        if name == "CreateEvent":
            f["name"] = r.string()
            f["symbol"] = r.string()
            f["uri"] = r.string()
            f["mint"] = r.pubkey()
            f["bonding_curve"] = r.pubkey()
            f["user"] = r.pubkey()
            f["creator"] = r.pubkey()
            f["timestamp"] = r.i64()
            f["virtual_token_reserves"] = r.u64()
            f["virtual_sol_reserves"] = r.u64()
            f["real_token_reserves"] = r.u64()
            f["token_total_supply"] = r.u64()
            f["token_program"] = r.pubkey()
            f["is_mayhem_mode"] = r.boolean()
            f["is_cashback_enabled"] = r.boolean()
            f["quote_mint"] = r.pubkey()
            f["virtual_quote_reserves"] = r.u64()
            f["creator_fee_bps"] = r.u64()
            f["is_holder_reward"] = r.boolean()
            f["depth"] = r.u8()
        elif name == "TradeEvent":
            f["mint"] = r.pubkey()
            f["sol_amount"] = r.u64()
            f["token_amount"] = r.u64()
            f["is_buy"] = r.boolean()
            f["user"] = r.pubkey()
            f["timestamp"] = r.i64()
            # Legacy SOL reserve fields: 0 for WSOL-quoted trades;
            # the live amounts are in the quote fields below.
            f["virtual_sol_reserves"] = r.u64()
            f["virtual_token_reserves"] = r.u64()
            f["real_sol_reserves"] = r.u64()
            f["real_token_reserves"] = r.u64()
            f["fee_recipient"] = r.pubkey()
            f["fee_basis_points"] = r.u64()
            f["fee"] = r.u64()
            f["creator"] = r.pubkey()
            f["creator_fee_basis_points"] = r.u64()
            f["creator_fee"] = r.u64()
            f["track_volume"] = r.boolean()
            f["total_unclaimed_tokens"] = r.u64()
            f["total_claimed_tokens"] = r.u64()
            f["current_sol_volume"] = r.u64()
            f["last_update_timestamp"] = r.i64()
            f["ix_name"] = r.string()
            f["mayhem_mode"] = r.boolean()
            f["cashback_fee_basis_points"] = r.u64()
            f["cashback"] = r.u64()
            f["buyback_fee_basis_points"] = r.u64()
            f["buyback_fee"] = r.u64()
            f["shareholders"] = _read_shareholders(r)
            f["quote_mint"] = r.pubkey()
            f["quote_amount"] = r.u64()
            f["virtual_quote_reserves"] = r.u64()
            f["real_quote_reserves"] = r.u64()
            f["holder_rewards_bps"] = r.u64()
            f["holder_rewards"] = r.u64()
            f["creator_fee_unclaimed"] = r.u64()
            if r.left() == 8:
                f["trailing_u64"] = r.u64()
            elif r.left() > 0:
                f["trailing_bytes_hex"] = r.bytes(r.left()).hex()
        elif name == "CompleteEvent":
            f["user"] = r.pubkey()
            f["mint"] = r.pubkey()
            f["bonding_curve"] = r.pubkey()
            f["timestamp"] = r.i64()
            f["quote_mint"] = r.pubkey()
        elif name == "CompletePumpAmmMigrationEvent":
            f["user"] = r.pubkey()
            f["mint"] = r.pubkey()
            f["mint_amount"] = r.u64()
            f["sol_amount"] = r.u64()
            f["pool_migration_fee"] = r.u64()
            f["bonding_curve"] = r.pubkey()
            f["timestamp"] = r.i64()
            f["pool"] = r.pubkey()
            f["quote_mint"] = r.pubkey()
        elif name == "PostCompleteBuyEvent":
            f["user"] = r.pubkey()
            f["mint"] = r.pubkey()
            f["bonding_curve"] = r.pubkey()
            f["quote_mint"] = r.pubkey()
            f["timestamp"] = r.i64()
            f["base_out"] = r.u64()
            f["quote_in"] = r.u64()
            f["fee_basis_points"] = r.u64()
            f["fee"] = r.u64()
            f["creator_fee_basis_points"] = r.u64()
            f["creator_fee"] = r.u64()
            f["buyback_fee"] = r.u64()
            f["pool_base_reserves_before"] = r.u64()
            f["pool_quote_reserves_before"] = r.u64()
            f["pool_base_reserves_after"] = r.u64()
            f["pool_quote_reserves_after"] = r.u64()
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
    if name == "TradeEvent":
        trailing = f.get("trailing_u64")
        if trailing is None:
            return f, "unverified", ["TradeEvent missing trailing u64"]
        if trailing != 0:
            return f, "unverified", [
                "trailing_u64 is %d, expected 0" % trailing]
    return f, "verified", notes


def decode_event(data):
    """Decode one emitted pump event.

    `data` is the raw event bytes (discriminator + body), or the full
    Anchor emit-CPI payload (EMIT_DISC + event), as bytes or a base58
    string. Returns a dict with ``event_name``, ``fields``,
    ``validation`` and ``notes``. Unknown discriminators yield
    event_name "unknown" and validation "unknown".
    """
    out = {"program_id": PUMP_PROGRAM_ID,
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
        out["notes"] = ["unrecognised pump event discriminator"]
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
    """Instruction data as bytes, or None when absent/undecodable.

    jsonParsed instructions carry base58 ``data``; parsed (named)
    instructions (system/token program etc.) carry no ``data``.
    """
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
    """Decode a getTransaction (jsonParsed) result for the pump program.

    `tx` is the RPC result dict (or the ``transaction`` value inside
    a fixture wrapper — both accepted). Returns a list of event
    dicts, each carrying signature, slot, block time, program id,
    instruction info, event type, fields, raw data, decoder version
    and validation status.

    A failed transaction (``meta.err`` non-null) yields a single
    ``transaction_failed`` entry and NO trade events: a reverted
    instruction must never be reported as a trade. Unknown
    programs/instructions/events yield ``unknown`` entries, never
    an invented event.
    """
    if not isinstance(tx, dict):
        return [_envelope(signature, None, None, PUMP_PROGRAM_ID,
                          "unknown", {}, {},
                          None, "unknown",
                          ["transaction is not a dict"])]
    # Accept a fixture wrapper {"capture": ..., "transaction": {...}}.
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
            sig, slot, block_time, PUMP_PROGRAM_ID,
            "transaction_failed",
            {"instruction_name": None, "index": None, "accounts": []},
            {"error": meta.get("err")}, None, "unverified",
            ["transaction failed; no trade events decoded"])]

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
                sig, slot, block_time, PUMP_PROGRAM_ID, "unknown",
                ix_info, {"discriminator": list(disc)},
                raw, "unknown",
                ["unrecognised pump event discriminator"]))
            return
        key = (PUMP_PROGRAM_ID, disc, bytes(body))
        if key in seen:
            return
        seen.add(key)
        fields, validation, notes = _decode_event_body(name, body)
        events.append(_envelope(
            sig, slot, block_time, PUMP_PROGRAM_ID, "event:" + name,
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
                    sig, slot, block_time, PUMP_PROGRAM_ID, "unknown",
                    ix_info, {"discriminator": dec.get("discriminator")},
                    raw, "unknown", dec.get("notes") or []))
            return
        events.append(_envelope(
            sig, slot, block_time, PUMP_PROGRAM_ID,
            "instruction:" + name, ix_info, dec.get("args") or {},
            raw, dec.get("validation", "unverified"),
            dec.get("notes") or []))

    # Top-level instructions first, then inner (CPI) instructions,
    # preserving on-chain order as closely as jsonParsed allows.
    top = message.get("instructions") or []
    inner = meta.get("innerInstructions") or []
    inner_by_index = {}
    for group in inner:
        inner_by_index.setdefault(group.get("index"), []).extend(
            group.get("instructions") or [])
    for i, ix in enumerate(top):
        if not isinstance(ix, dict):
            continue
        if ix.get("programId") != PUMP_PROGRAM_ID:
            # Still walk its inner instructions: a router/Jupiter
            # top-level instruction can wrap pump CPIs.
            for sub in inner_by_index.get(i, []):
                if isinstance(sub, dict) \
                        and sub.get("programId") == PUMP_PROGRAM_ID:
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
                    and sub.get("programId") == PUMP_PROGRAM_ID:
                _handle_ix(
                    _ix_data_bytes(sub),
                    {"instruction_name": None, "index": i,
                     "inner_of": i,
                     "accounts": _ix_accounts(sub, keys)},
                    _ix_accounts(sub, keys))

    # Log fallback: "Program data:" lines carry the same event bytes
    # as the emit CPIs; use them only for events not already seen
    # (e.g. when inner instruction data is absent).
    for prog, data_b64 in _log_program_stack(meta.get("logMessages")):
        if prog != PUMP_PROGRAM_ID:
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
        key = (PUMP_PROGRAM_ID, disc, raw[8:])
        if key in seen:
            continue
        seen.add(key)
        name = EV_BY_DISC[disc]
        fields, validation, notes = _decode_event_body(name, raw[8:])
        events.append(_envelope(
            sig, slot, block_time, PUMP_PROGRAM_ID, "event:" + name,
            {"instruction_name": None, "index": None,
             "accounts": [], "source": "log"},
            fields if fields is not None else {},
            b"".join([bytes(EMIT_DISC), raw]), validation,
            notes + ["decoded from Program data log (no inner ix)"]))

    return events


def find_migration(events):
    """Return the CompletePumpAmmMigrationEvent envelope from a
    decode_transaction result, or None."""
    for ev in events or []:
        if ev.get("event_type") == "event:CompletePumpAmmMigrationEvent":
            return ev
    return None


def canonical_pool_from_migration(tx, events=None):
    """Canonical PumpSwap pool for a migration transaction.

    The migration event names the pool; the pool is canonical when
    it also appears in the transaction's account keys (mirrors the
    membership check in grad_select._canonical_pool_from_chain).
    Returns the pool address, or None when no verified migration
    event names a pool present in the tx.
    """
    if events is None:
        events = decode_transaction(tx)
    mig = find_migration(events)
    if mig is None:
        return None
    pool = (mig.get("fields") or {}).get("pool")
    if not pool:
        return None
    inner = tx.get("transaction") if isinstance(tx.get("transaction"),
                                                dict) else tx
    keys = _account_keys((inner or {}).get("message") or {})
    return pool if pool in keys else None
