"""General Solana transaction parsing — pure stdlib.

Decodes a getTransaction result in any of the encodings the RPC
serves:

* "jsonParsed" — instructions already parsed by the RPC; account
  keys arrive as objects {pubkey, signer, source, writable}
  (source "transaction" or "lookupTable") on current RPC versions,
  or as plain strings on older ones.
* "json" — instructions in raw form (programIdIndex / account
  indexes / base58 data); account keys are plain strings.
* "base64" — the raw wire transaction; the message is parsed
  locally (legacy and v0 fully; see _parse_wire_message).

Versioned transactions and address lookup tables: the message's
addressTableLookups are resolved through a caller-supplied
resolver (table address -> [addresses] or None). Resolution is
fail-closed, mirroring broker/memecoin/execution/tx.py's
versioned_tx_loaded_account_keys: a lookup table that cannot be
resolved, or an index out of range, leaves the transaction
"unverified" with the account list marked incomplete — never a
partial account set presented as complete. When the RPC already
resolved the loaded addresses (jsonParsed accountKeys with source
"lookupTable", or meta.loadedAddresses) those are used directly
and the resolver is not needed. The ALT account-data format
(56-byte LookupTableMeta header + 32-byte addresses) is the same
one tx.py verifies against solders' AddressLookupTable.deserialize;
see account_decoder.lookup_table_addresses_from_account_data.

Transaction versions: legacy, 0 and 1 are all seen on mainnet
(v1 carries its compute budget inline as Message.config,
SIMD-0385 — the repo's tx.py documents the model in
tx_version_from_bytes). The jsonParsed/json paths decode every
version opaquely. The wire path decodes legacy and v0 fully; a v1
wire message is reported "unknown" with a note — the v1 wire
layout of Message.config was not verified for this decoder
version, and a layout must never be guessed.

Every decoded transaction carries signature, slot, block time
(when available), program ids, instruction info, relevant account
addresses, decoded event type, raw or referenced transaction data,
decoder version and validation status. A transaction that cannot
be decoded confidently is "unknown"/"unverified" — never an
invented event.
"""
import base64

from ._base58 import b58decode, b58encode
from . import account_decoder
from . import instruction_decoder

DECODER_VERSION = "1.0.0"

# The transaction version byte: 0x80 | version (SIMD-0385 / v0 model).
_VERSION_PREFIX_MASK = 0x80


# ---------------------------------------------------------------------
# Short-vector (compact-u16) codec — Solana wire format
# ---------------------------------------------------------------------

def _read_shortvec(data, offset):
    """Read a compact-u16 length prefix. Returns (value, next_offset)
    or (None, None) on truncation / overflow."""
    value = 0
    shift = 0
    pos = offset
    for _ in range(3):
        if pos >= len(data):
            return None, None
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
    return None, None


def _read_pubkey(data, offset):
    if len(data) < offset + 32:
        return None, None
    return b58encode(data[offset:offset + 32]), offset + 32


# ---------------------------------------------------------------------
# Wire-message parsing (legacy + v0; v1 reported unknown, see docstring)
# ---------------------------------------------------------------------

def _parse_wire_message(data):
    """Parse raw message bytes.

    Returns a dict with version ("legacy" | 0), header, account_keys
    (static, base58), recent_blockhash, instructions (raw form) and
    address_table_lookups, or None when the bytes are truncated or
    the version is not legacy/v0.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 1:
        return None
    raw = bytes(data)
    pos = 0
    version = "legacy"
    if raw[0] & _VERSION_PREFIX_MASK:
        version = raw[0] & 0x7F
        if version != 0:
            return None  # v1+ wire layout not decoded (see docstring).
        pos = 1
    if len(raw) < pos + 3:
        return None
    header = {
        "num_required_signatures": raw[pos],
        "num_readonly_signed_accounts": raw[pos + 1],
        "num_readonly_unsigned_accounts": raw[pos + 2],
    }
    pos += 3
    count, pos = _read_shortvec(raw, pos)
    if count is None:
        return None
    account_keys = []
    for _ in range(count):
        key, pos = _read_pubkey(raw, pos)
        if key is None:
            return None
        account_keys.append(key)
    if len(raw) < pos + 32:
        return None
    blockhash = b58encode(raw[pos:pos + 32])
    pos += 32
    count, pos = _read_shortvec(raw, pos)
    if count is None:
        return None
    instructions = []
    for _ in range(count):
        if pos >= len(raw):
            return None
        program_index = raw[pos]
        pos += 1
        n_accts, pos = _read_shortvec(raw, pos)
        if n_accts is None or len(raw) < pos + n_accts:
            return None
        accounts = list(raw[pos:pos + n_accts])
        pos += n_accts
        n_data, pos = _read_shortvec(raw, pos)
        if n_data is None or len(raw) < pos + n_data:
            return None
        ix_data = raw[pos:pos + n_data]
        pos += n_data
        instructions.append({
            "programIdIndex": program_index,
            "accounts": accounts,
            "data": b58encode(ix_data),
        })
    lookups = []
    if version == 0:
        count, pos = _read_shortvec(raw, pos)
        if count is None:
            return None
        for _ in range(count):
            table, pos = _read_pubkey(raw, pos)
            if table is None:
                return None
            n_w, pos = _read_shortvec(raw, pos)
            if n_w is None or len(raw) < pos + n_w:
                return None
            writable = list(raw[pos:pos + n_w])
            pos += n_w
            n_r, pos = _read_shortvec(raw, pos)
            if n_r is None or len(raw) < pos + n_r:
                return None
            readonly = list(raw[pos:pos + n_r])
            pos += n_r
            lookups.append({
                "accountKey": table,
                "writableIndexes": writable,
                "readonlyIndexes": readonly,
            })
    return {
        "version": version,
        "header": header,
        "account_keys": account_keys,
        "recent_blockhash": blockhash,
        "instructions": instructions,
        "address_table_lookups": lookups,
    }


def _parse_wire_transaction(data):
    """Parse base64-decoded transaction bytes.

    Returns {"signatures": [...], "message": {...}} or None.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 1:
        return None
    raw = bytes(data)
    count, pos = _read_shortvec(raw, 0)
    if count is None or len(raw) < pos + 64 * count:
        return None
    signatures = [b58encode(raw[pos + 64 * i:pos + 64 * (i + 1)])
                  for i in range(count)]
    pos += 64 * count
    message = _parse_wire_message(raw[pos:])
    if message is None:
        return None
    return {"signatures": signatures, "message": message}


def wire_transaction_version(data):
    """Version of raw transaction bytes, mirroring the repo's
    tx.py::tx_version_from_bytes model: {"version": 0|1|None,
    "is_versioned": bool, "is_v1": bool, "has_config": bool}.
    Never raises; None version when the bytes are too short or the
    prefix is unrecognized."""
    try:
        if not isinstance(data, (bytes, bytearray)) or len(data) < 2:
            return {"version": None, "is_versioned": False,
                    "is_v1": False, "has_config": False}
        count, pos = _read_shortvec(bytes(data), 0)
        if count is None or len(data) < pos + 64 * count + 1:
            return {"version": None, "is_versioned": False,
                    "is_v1": False, "has_config": False}
        prefix = data[pos + 64 * count]
        if prefix & _VERSION_PREFIX_MASK:
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


# ---------------------------------------------------------------------
# Address lookup table resolution
# ---------------------------------------------------------------------

def resolve_address_table_lookups(lookups, resolver):
    """Resolve message addressTableLookups via `resolver`.

    `resolver` takes a table address (base58) and returns its
    addresses (base58 list) or None. Returns (loaded_accounts,
    status, notes) where loaded_accounts are in message order
    (writable then readonly per lookup — the same order
    tx.py::versioned_tx_loaded_account_keys uses). A lookup that
    cannot be resolved, or an index out of range, yields status
    "unverified" and an incomplete list; the caller must not
    present it as complete. `resolver=None` with non-empty lookups
    is "unverified" too.
    """
    loaded = []
    notes = []
    if not lookups:
        return loaded, "verified", notes
    if resolver is None:
        notes.append("transaction loads accounts via address lookup "
                     "table(s) but no resolver was provided")
        return loaded, "unverified", notes
    status = "verified"
    for lk in lookups:
        table = lk.get("accountKey")
        try:
            addresses = resolver(table)
        except Exception as exc:
            notes.append("lookup-table resolver failed for %s: %s"
                         % (table, exc))
            return loaded, "unverified", notes
        if addresses is None:
            notes.append("address lookup table %s cannot be resolved"
                         % table)
            return loaded, "unverified", notes
        for idx in list(lk.get("writableIndexes") or []) + \
                list(lk.get("readonlyIndexes") or []):
            try:
                i = int(idx)
            except (TypeError, ValueError):
                notes.append("lookup-table index %r is not an integer"
                             % (idx,))
                return loaded, "unverified", notes
            if i < 0 or i >= len(addresses):
                notes.append(
                    "address lookup table %s index %d out of range "
                    "(%d addresses)" % (table, i, len(addresses)))
                return loaded, "unverified", notes
            loaded.append(str(addresses[i]))
    return loaded, status, notes


# ---------------------------------------------------------------------
# Account-key normalisation (jsonParsed objects vs plain strings)
# ---------------------------------------------------------------------

def _normalise_account_keys(raw_keys):
    """Split message accountKeys into (static, loaded, sources).

    Entries may be plain base58 strings (older RPCs, "json"
    encoding) or objects {pubkey, signer, source, writable}
    (current RPCs). Loaded entries are those with source
    "lookupTable". Returns (static_keys, loaded_keys, sources)
    where sources maps pubkey -> "transaction"/"lookupTable"/None.
    """
    static_keys, loaded_keys, sources = [], [], {}
    if not isinstance(raw_keys, list):
        return static_keys, loaded_keys, sources
    for entry in raw_keys:
        if isinstance(entry, str):
            static_keys.append(entry)
            sources[entry] = None
        elif isinstance(entry, dict) and entry.get("pubkey"):
            pubkey = str(entry["pubkey"])
            if entry.get("source") == "lookupTable":
                loaded_keys.append(pubkey)
            else:
                static_keys.append(pubkey)
            sources[pubkey] = entry.get("source")
    return static_keys, loaded_keys, sources


# ---------------------------------------------------------------------
# Meta / balance decoding
# ---------------------------------------------------------------------

def _token_balance_changes(meta):
    """Pre/post token-balance deltas keyed by (account_index, mint).

    Amounts are raw base-unit integers parsed from the RPC's
    uiTokenAmount.amount strings; a balance that cannot be parsed
    is skipped, never guessed.
    """
    changes = []
    pre = {}
    post = {}
    for b in (meta.get("preTokenBalances") or []):
        if isinstance(b, dict) and isinstance(b.get("accountIndex"), int):
            pre[(b["accountIndex"], b.get("mint"))] = b
    for b in (meta.get("postTokenBalances") or []):
        if isinstance(b, dict) and isinstance(b.get("accountIndex"), int):
            post[(b["accountIndex"], b.get("mint"))] = b
    for key in sorted(set(pre) | set(post)):
        before = pre.get(key)
        after = post.get(key)
        try:
            pre_amt = int(before["uiTokenAmount"]["amount"]) \
                if before else 0
            post_amt = int(after["uiTokenAmount"]["amount"]) \
                if after else 0
        except (KeyError, TypeError, ValueError):
            continue
        owner = (after or before).get("owner")
        program = (after or before).get("programId")
        changes.append({
            "account_index": key[0],
            "mint": key[1],
            "owner": owner,
            "program_id": program,
            "pre_amount": pre_amt,
            "post_amount": post_amt,
            "delta": post_amt - pre_amt,
        })
    return changes


def token_balance_changes_for_mint(decoded, mint):
    """Net token-balance deltas for one mint in a decoded tx."""
    return [c for c in decoded.get("meta", {}).get(
        "token_balance_changes", [])
        if c.get("mint") == mint]


# ---------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------

def _base_result():
    return {
        "event_type": "solana_transaction",
        "signature": None,
        "slot": None,
        "block_time": None,
        "version": None,
        "fee_payer": None,
        "account_keys": [],
        "static_account_keys": [],
        "loaded_account_keys": [],
        "account_key_sources": {},
        "instructions": [],
        "inner_instructions": [],
        "log_messages": [],
        "transaction_config": None,
        "program_ids": [],
        "unknown_instructions": 0,
        "unverified_instructions": 0,
        "meta": {
            "err": None,
            "status": None,
            "fee": None,
            "cost_units": None,
            "pre_balances": [],
            "post_balances": [],
            "balance_changes": [],
            "pre_token_balances": [],
            "post_token_balances": [],
            "token_balance_changes": [],
            "loaded_addresses": None,
        },
        "decoder_version": DECODER_VERSION,
        "validation_status": "unknown",
        "notes": [],
        "raw": None,
    }


def decode_transaction(tx, resolver=None):
    """Decode one getTransaction result dict.

    `tx` is the RPC result object: {"slot", "blockTime",
    "transaction": {"message": {...}, "signatures": [...]},
    "meta": {...}} — message in jsonParsed, json or base64 form
    (base64 arrives as transaction: [b64, "base64"]). `resolver`
    is an optional address-lookup-table resolver callable (see
    resolve_address_table_lookups); it is only consulted when the
    message carries addressTableLookups the RPC has not already
    resolved.

    Validation: "verified" only when the message, the full account
    list (static + ALT-loaded), the meta, AND every instruction
    parsed with no gaps (an instruction for a program this core
    decoder does not know — e.g. the pump.fun program, which the
    sibling pumpfun decoder owns — leaves the transaction
    "unverified" at this layer, with the per-instruction statuses
    carrying the detail); "unverified" when ALT resolution failed,
    the meta is missing, or any instruction is unknown/unverified;
    "unknown" when the input is not a recognizable transaction at
    all.
    """
    out = _base_result()
    if not isinstance(tx, dict):
        out["notes"].append("transaction result is not an object")
        return out
    out["raw"] = tx
    out["slot"] = tx.get("slot")
    out["block_time"] = tx.get("blockTime")
    # Top-level "version" is the tx version: "legacy" | 0 | 1 | ...
    version = tx.get("version", "legacy")
    out["version"] = version

    envelope = tx.get("transaction")
    message = None
    signatures = []
    wire_form = False
    if isinstance(envelope, dict):
        message = envelope.get("message")
        signatures = envelope.get("signatures") or []
    elif isinstance(envelope, (list, tuple)) and len(envelope) >= 2 \
            and envelope[1] == "base64":
        wire_form = True
        try:
            raw_bytes = base64.b64decode(envelope[0], validate=True)
        except Exception:
            out["notes"].append(
                "transaction is not valid base64")
            return out
        parsed_wire = _parse_wire_transaction(raw_bytes)
        if parsed_wire is None:
            wv = wire_transaction_version(raw_bytes)
            if wv.get("is_v1"):
                out["notes"].append(
                    "version 1 wire format is not decoded by "
                    "decoder version %s" % DECODER_VERSION)
            else:
                out["notes"].append(
                    "wire transaction bytes are truncated or malformed")
            return out
        signatures = parsed_wire["signatures"]
        message = parsed_wire["message"]
        out["version"] = message.get("version", "legacy")
    else:
        out["notes"].append(
            "transaction envelope has unexpected shape")
        return out

    if not isinstance(message, dict):
        out["notes"].append("transaction message is missing")
        return out
    if signatures:
        out["signature"] = str(signatures[0])

    raw_keys = message.get("accountKeys") or message.get("account_keys")
    static_keys, loaded_keys, sources = _normalise_account_keys(raw_keys)
    # jsonParsed may also carry loaded addresses in meta; the
    # message's own "source" markers are authoritative when present.
    lookups = message.get("addressTableLookups") or []
    if not isinstance(lookups, list):
        lookups = []

    meta = tx.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        out["notes"].append("transaction meta is missing")
        meta_missing = True
    else:
        meta_missing = False

    # Resolve ALT-loaded accounts when the RPC has not already
    # supplied them in the accountKeys list.
    alt_status = "verified"
    alt_notes = []
    if lookups and not loaded_keys:
        loaded_keys, alt_status, alt_notes = \
            resolve_address_table_lookups(lookups, resolver)
    elif lookups and loaded_keys:
        # Cross-check the RPC-resolved loaded set against the
        # lookups: the count must match the referenced indexes.
        expected = sum(
            len(lk.get("writableIndexes") or []) +
            len(lk.get("readonlyIndexes") or [])
            for lk in lookups)
        if expected != len(loaded_keys):
            alt_status = "unverified"
            alt_notes.append(
                "RPC-resolved loaded accounts (%d) do not match "
                "the lookup indexes referenced (%d)"
                % (len(loaded_keys), expected))
    out["notes"].extend(alt_notes)

    out["static_account_keys"] = static_keys
    out["loaded_account_keys"] = loaded_keys
    out["account_keys"] = static_keys + loaded_keys
    out["account_key_sources"] = sources
    if out["account_keys"]:
        out["fee_payer"] = out["account_keys"][0]
    if message.get("transactionConfig") is not None:
        out["transaction_config"] = message.get("transactionConfig")

    # Meta.
    m = out["meta"]
    m["err"] = meta.get("err")
    status = meta.get("status")
    if isinstance(status, dict):
        m["status"] = "success" if "Ok" in status else "failed"
    elif m["err"] is None and meta:
        m["status"] = "success"
    elif meta:
        m["status"] = "failed"
    m["fee"] = meta.get("fee")
    m["cost_units"] = meta.get("costUnits")
    m["pre_balances"] = list(meta.get("preBalances") or [])
    m["post_balances"] = list(meta.get("postBalances") or [])
    if len(m["pre_balances"]) == len(m["post_balances"]):
        m["balance_changes"] = [
            {"account_index": i,
             "account": out["account_keys"][i]
             if i < len(out["account_keys"]) else None,
             "delta": post - pre}
            for i, (pre, post) in enumerate(
                zip(m["pre_balances"], m["post_balances"]))]
    m["pre_token_balances"] = list(meta.get("preTokenBalances") or [])
    m["post_token_balances"] = list(meta.get("postTokenBalances") or [])
    m["token_balance_changes"] = _token_balance_changes(meta)
    m["loaded_addresses"] = meta.get("loadedAddresses")
    out["log_messages"] = list(meta.get("logMessages") or [])

    # Instructions (outer + inner).
    program_ids = []
    unknown = 0
    unverified = 0
    decoded_ixs = []
    for pos, ix in enumerate(message.get("instructions") or []):
        decoded = instruction_decoder.decode_instruction(
            ix, account_keys=out["account_keys"])
        decoded["index"] = pos
        decoded_ixs.append(decoded)
        if decoded["program_id"] and decoded["program_id"] \
                not in program_ids:
            program_ids.append(decoded["program_id"])
        if decoded["validation_status"] == "unknown":
            unknown += 1
        elif decoded["validation_status"] == "unverified":
            unverified += 1
    out["instructions"] = decoded_ixs

    inner = []
    for group in (meta.get("innerInstructions") or []):
        if not isinstance(group, dict):
            continue
        group_ixs = []
        for ix in group.get("instructions") or []:
            decoded = instruction_decoder.decode_instruction(
                ix, account_keys=out["account_keys"])
            group_ixs.append(decoded)
            if decoded["program_id"] and decoded["program_id"] \
                    not in program_ids:
                program_ids.append(decoded["program_id"])
            if decoded["validation_status"] == "unknown":
                unknown += 1
            elif decoded["validation_status"] == "unverified":
                unverified += 1
        inner.append({"index": group.get("index"),
                      "instructions": group_ixs})
    out["inner_instructions"] = inner
    out["unknown_instructions"] = unknown
    out["unverified_instructions"] = unverified
    out["program_ids"] = program_ids

    # Validation.
    if meta_missing or not out["account_keys"]:
        out["validation_status"] = "unverified"
        if not out["account_keys"]:
            out["notes"].append("transaction has no account keys")
    elif alt_status != "verified":
        out["validation_status"] = "unverified"
    elif unknown or unverified:
        out["validation_status"] = "unverified"
        out["notes"].append(
            "%d instruction(s) unknown, %d unverified"
            % (unknown, unverified))
    else:
        out["validation_status"] = "verified"
    if wire_form:
        out["notes"].append("decoded from base64 wire bytes")
    return out


# Re-export the ALT account-data parser for callers that resolve
# tables themselves (e.g. a resolver built on getAccountInfo).
lookup_table_addresses_from_account_data = \
    account_decoder.lookup_table_addresses_from_account_data
