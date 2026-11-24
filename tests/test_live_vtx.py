#!/usr/bin/env python3
"""Step 1 tests for versioned-transaction support (plain script):

    ~/workspace/Forex-agent/.venv/bin/python tests/test_live_vtx.py

Covers: a canned Jupiter-format v0 transaction round-trips
(deserialize -> sign -> serialize -> signature verifies, no network),
and check_swap_tx passes clean txs and refuses wrong-payer /
unknown-program / missing-mint txs. No real network calls.
"""

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from broker import BrokerError
from broker.memecoin.execution import tx as tx_mod

_PASSED = 0


def check(name, cond):
    global _PASSED
    if not cond:
        raise AssertionError("FAIL: %s" % name)
    _PASSED += 1
    print("PASS: %s" % name)


def _b58encode(raw: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(raw, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = alphabet[r] + out
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + (out or "")


def _fake_blockhash() -> str:
    return _b58encode(bytes(range(32)))


def _build_canned_vtx(kp, payer=None, program_id=None,
                      extra_mints=()):
    """A v0 tx shaped like a Jupiter swap: one dummy instruction
    under program_id touching payer + the given mints."""
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    payer = payer or str(kp.pubkey())
    program_id = program_id or tx_mod.JUPITER_AGGREGATOR_PROGRAM_ID
    accounts = [AccountMeta(Pubkey.from_string(payer), True, True)]
    for m in extra_mints:
        accounts.append(AccountMeta(Pubkey.from_string(m), False, True))
    ix = Instruction(Pubkey.from_string(program_id), b"\x00\x01\x02",
                     accounts)
    msg = MessageV0.try_compile(
        Pubkey.from_string(payer), [ix], [],
        Hash.from_string(_fake_blockhash()))
    return VersionedTransaction(msg, [kp])


def _build_alt_vtx(kp, table_key, table_addrs, touched=(),
                   payer=None, program_id=None):
    """A v0 tx shaped like a real Jupiter swap whose accounts live
    ONLY in an address lookup table: static keys are payer +
    program, and `touched` are the table addresses the instruction
    references (they move into the lookup at compile time)."""
    from solders.address_lookup_table_account import \
        AddressLookupTableAccount
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    payer = payer or str(kp.pubkey())
    program_id = program_id or tx_mod.JUPITER_AGGREGATOR_PROGRAM_ID
    table = AddressLookupTableAccount(
        key=Pubkey.from_string(table_key),
        addresses=[Pubkey.from_string(a) for a in table_addrs])
    accounts = [AccountMeta(Pubkey.from_string(payer), True, True)]
    for m in touched:
        accounts.append(AccountMeta(Pubkey.from_string(m), False, True))
    ix = Instruction(Pubkey.from_string(program_id), b"\x00\x01\x02",
                     accounts)
    msg = MessageV0.try_compile(
        Pubkey.from_string(payer), [ix], [table],
        Hash.from_string(_fake_blockhash()))
    return VersionedTransaction(msg, [kp])


# Real on-chain ALT meta (56 bytes), captured from mainnet table
# 6rqx7NGgRAtGiAsBE6RAxWMaNQfSgzRy1tMSNML4GzQm on 2026-11-20.
# The meta's first 4 bytes are part of the on-chain layout — a
# hand-built zero meta does NOT parse (solders raises
# UninitializedAccount / InvalidAccountData). Appending synthetic
# addresses after this meta gives a fixture blob that
# AddressLookupTable.deserialize accepts.
_ALT_META_HEX = (
    "01000000ffffffffffffffffa3c4e11400000000"
    "58017d0ea3424053053bbbd860f74c32f04bc3dc"
    "872adf9a4f4ed170c24ee0f43ed30000")
_ALT_PROGRAM_ID = \
    "AddressLookupTab1e1111111111111111111111111"


def _alt_account_blob(addrs):
    """Base64 ALT account data: the real meta + synthetic
    addresses (32 bytes each, no length prefix)."""
    from solders.pubkey import Pubkey
    raw = bytes.fromhex(_ALT_META_HEX) + b"".join(
        bytes(Pubkey.from_string(a)) for a in addrs)
    return base64.b64encode(raw).decode("ascii")


class _FakeAltRPC:
    """Serves one canned getAccountInfo result."""

    def __init__(self, table, blob_b64=None, owner=None, fail=False):
        self._table = table
        self._blob = blob_b64
        self._owner = owner or _ALT_PROGRAM_ID
        self._fail = fail

    def call(self, method, params):
        if self._fail:
            raise RuntimeError("rpc down")
        assert method == "getAccountInfo"
        if params[0] != self._table:
            return {"context": {"slot": 1}, "value": None}
        return {"context": {"slot": 1},
                "value": {"owner": self._owner,
                          "data": [self._blob, "base64"]}}


def main():
    from solders.keypair import Keypair
    kp = Keypair()
    payer = str(kp.pubkey())
    sol_mint = "So11111111111111111111111111111111111111112"
    tok_mint = _b58encode(bytes(range(1, 33)))

    # The ALT program id constant is pinned to solders' canonical
    # value: a typo here (the old value had six extra "1"s, 49
    # chars) silently breaks the ALT owner check in the resolver,
    # which then refuses every real table. The live owner string
    # (mainnet table 6rqx7NGg..., 2026-11-20) matches solders.
    from solders.address_lookup_table_account import (
        ADDRESS_LOOKUP_TABLE_ID)
    check("ALT program id matches solders canonical value",
          tx_mod._ADDRESS_LOOKUP_TABLE_PROGRAM_ID
          == str(ADDRESS_LOOKUP_TABLE_ID)
          and len(tx_mod._ADDRESS_LOOKUP_TABLE_PROGRAM_ID) == 43)

    # -- round-trip: build -> base64 -> deserialize -> sign ---------
    vtx = _build_canned_vtx(kp, extra_mints=(sol_mint, tok_mint))
    b64 = base64.b64encode(bytes(vtx)).decode("ascii")
    vtx2 = tx_mod.deserialize_versioned_tx(b64)
    check("v0 deserialize returns a versioned tx",
          bytes(vtx2) == bytes(vtx))
    signed, sig = tx_mod.sign_versioned_tx(vtx2, kp)
    check("v0 sign returns 64-byte signature", len(sig) == 64)
    check("v0 signature verifies locally",
          all(signed.verify_with_results()))
    check("v0 signed bytes equal constructor-signed bytes",
          bytes(signed) == bytes(vtx))
    b64_signed = tx_mod.versioned_tx_to_base64(signed)
    vtx3 = tx_mod.deserialize_versioned_tx(b64_signed)
    check("signed v0 re-deserializes with same signature",
          tx_mod.versioned_signature_str(vtx3)
          == tx_mod.versioned_signature_str(signed))
    check("unsigned v0 has no signature string",
          _unsigned_has_no_sig(kp))

    # -- garbage refuses --------------------------------------------
    for bad in ("", "not-base64!!!",
                base64.b64encode(b"hello world").decode()):
        try:
            tx_mod.deserialize_versioned_tx(bad)
            raise AssertionError("deserialized garbage %r" % bad[:20])
        except BrokerError:
            pass
    check("deserialize refuses garbage", True)

    # -- check_swap_tx: clean passes ---------------------------------
    clean = _build_canned_vtx(kp, extra_mints=(sol_mint, tok_mint))
    # _build_canned_vtx already signs at construction.
    tx_mod.check_swap_tx(clean, payer, sol_mint, tok_mint)
    check("check_swap_tx passes a clean tx", True)
    pids = tx_mod.versioned_tx_program_ids(clean)
    check("program ids list the Jupiter aggregator",
          tx_mod.JUPITER_AGGREGATOR_PROGRAM_ID in pids)
    keys = tx_mod.versioned_tx_account_keys(clean)
    check("first account key is the payer", keys[0] == payer)

    # -- check_swap_tx: refusals -------------------------------------
    wrong_payer_tx = _build_canned_vtx(kp, extra_mints=(sol_mint,
                                                        tok_mint))
    try:
        tx_mod.check_swap_tx(wrong_payer_tx,
                             _b58encode(bytes(range(2, 34))),
                             sol_mint, tok_mint)
        raise AssertionError("accepted wrong payer")
    except BrokerError:
        pass
    check("check_swap_tx refuses wrong payer", True)

    bad_prog = _build_canned_vtx(
        kp, program_id="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        extra_mints=(sol_mint, tok_mint))
    # Token program IS allowed; use a truly unknown program id.
    bad_prog = _build_canned_vtx(
        kp, program_id=_b58encode(bytes(range(3, 35))),
        extra_mints=(sol_mint, tok_mint))
    try:
        tx_mod.check_swap_tx(bad_prog, payer, sol_mint, tok_mint)
        raise AssertionError("accepted unknown program")
    except BrokerError:
        pass
    check("check_swap_tx refuses unknown program", True)

    missing_mint = _build_canned_vtx(kp, extra_mints=(sol_mint,))
    try:
        tx_mod.check_swap_tx(missing_mint, payer, sol_mint, tok_mint)
        raise AssertionError("accepted missing output mint")
    except BrokerError:
        pass
    check("check_swap_tx refuses missing mint", True)

    # -- check_swap_tx: ALT-loaded mints ------------------------------
    # Real Jupiter swap txs usually load the SOL/USDC mints via an
    # address lookup table (static keys = payer + program only).
    table_key = _b58encode(bytes(range(5, 37)))
    extra_addr = _b58encode(bytes(range(9, 41)))
    table_addrs = (sol_mint, tok_mint, extra_addr)

    def _resolver_for(addrs_by_table):
        def resolve(table):
            return addrs_by_table.get(table)
        return resolve

    def _raising_resolver(table):
        raise RuntimeError("resolver down")

    alt_vtx = _build_alt_vtx(kp, table_key, table_addrs,
                             touched=(sol_mint, tok_mint))
    check("ALT tx has no mints in static keys",
          sol_mint not in tx_mod.versioned_tx_account_keys(alt_vtx)
          and tok_mint not in tx_mod.versioned_tx_account_keys(alt_vtx))
    tx_mod.check_swap_tx(
        alt_vtx, payer, sol_mint, tok_mint,
        lookup_table_resolver=_resolver_for(
            {table_key: list(table_addrs)}))
    check("check_swap_tx passes ALT-loaded mints", True)
    # The loaded set is exactly the referenced indexes: an address
    # stored in the table but NOT referenced by the tx must not
    # satisfy the mint check.
    alt_unreferenced = _build_alt_vtx(kp, table_key, table_addrs,
                                      touched=(sol_mint,))
    try:
        tx_mod.check_swap_tx(
            alt_unreferenced, payer, sol_mint, tok_mint,
            lookup_table_resolver=_resolver_for(
                {table_key: list(table_addrs)}))
        raise AssertionError("accepted unreferenced table mint")
    except BrokerError:
        pass
    check("check_swap_tx ignores unreferenced table addresses", True)
    # Fail-closed: no resolver, unresolvable table, resolver
    # failure, a resolver list too short for a referenced index,
    # and a table whose referenced index holds a different mint.
    for name, resolver in (
            ("no resolver", None),
            ("unresolvable table", _resolver_for({})),
            ("raising resolver", _raising_resolver),
            ("short resolver list",
             _resolver_for({table_key: [sol_mint]})),
            ("wrong mint at referenced index",
             _resolver_for({table_key: [sol_mint, extra_addr]}))):
        try:
            tx_mod.check_swap_tx(
                alt_vtx, payer, sol_mint, tok_mint,
                lookup_table_resolver=resolver)
            raise AssertionError("accepted ALT tx: %s" % name)
        except BrokerError:
            pass
        check("check_swap_tx refuses ALT tx (%s)" % name, True)

    # -- rpc_lookup_table_resolver ------------------------------------
    blob = _alt_account_blob(table_addrs)
    resolver = tx_mod.rpc_lookup_table_resolver(
        _FakeAltRPC(table_key, blob))
    check("rpc resolver returns the table addresses",
          resolver(table_key) == list(table_addrs))
    check("rpc resolver returns None for an unknown table",
          resolver(_b58encode(bytes(range(7, 39)))) is None)
    check("rpc resolver returns None for a missing account",
          tx_mod.rpc_lookup_table_resolver(
              _FakeAltRPC(_b58encode(bytes(range(6, 38))), blob))
          (table_key) is None)
    check("rpc resolver returns None for a wrong owner",
          tx_mod.rpc_lookup_table_resolver(
              _FakeAltRPC(table_key, blob,
                          owner="11111111111111111111111111111111"))
          (table_key) is None)
    check("rpc resolver returns None for unparseable data",
          tx_mod.rpc_lookup_table_resolver(
              _FakeAltRPC(table_key, base64.b64encode(
                  b"too short").decode("ascii")))
          (table_key) is None)
    check("rpc resolver returns None when the RPC fails",
          tx_mod.rpc_lookup_table_resolver(
              _FakeAltRPC(table_key, blob, fail=True))
          (table_key) is None)
    check("account-data parser accepts a well-formed table",
          tx_mod.lookup_table_addresses_from_account_data(
              base64.b64decode(blob)) == list(table_addrs))
    check("account-data parser rejects short data",
          tx_mod.lookup_table_addresses_from_account_data(
              b"\x00" * 10) is None)
    check("account-data parser rejects misaligned data",
          tx_mod.lookup_table_addresses_from_account_data(
              base64.b64decode(blob) + b"\x00") is None)

    print("ALL %d CHECKS PASSED" % _PASSED)
    return 0


def _unsigned_has_no_sig(kp):
    from solders.transaction import VersionedTransaction
    vtx = VersionedTransaction.default()
    try:
        tx_mod.versioned_signature_str(vtx)
        return False
    except BrokerError:
        return True


if __name__ == "__main__":
    raise SystemExit(main())
