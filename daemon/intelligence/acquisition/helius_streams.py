"""Helius Parsed Streams client (Phase 7, spec §6).

Helius is the SPECIALIZED provider: it receives ONLY workloads
where its distinctive capabilities offer meaningful value. It must
NOT become the default provider for ordinary RPC — routine reads
go through the router to Alchemy/Chainstack/public, never here.

Parsed Streams (Helius docs, read 2026-11-22 — see
``~/workspace/reports/provider-capability-audit-2026-11-22.md`` and
the Parsed Streams Quickstart, updated 2026-11-13):

* WebSocket, JSON-RPC 2.0, endpoint
  ``wss://fs-beta.helius-rpc.com/?api-key=<key>``. One notification
  per matching transaction per subscription, delivered at
  **confirmed** commitment (the only supported commitment).
* Methods: ``parsedTransactionSubscribe`` (filter + optional
  options), ``parsedTransactionUnsubscribe``, ``describeProgram``
  (names the matcher compares against — instruction/role names must
  come from it, never guessed).
* A filter needs at least one of ``programs`` or
  ``accounts.include``; fields combine with AND; unknown fields are
  rejected with -32602 (typos fail loudly). ``includeCpi`` defaults
  true (inner instructions eligible), ``includeFailed`` defaults
  false.
* Options: ``commitment`` ("confirmed" only), ``details`` —
  "full" (whole tx + ``matchedIndexes``), "matched" (hits only, no
  index list), "raw" (matched instructions as position/programId/
  base58 data only, no decoded fields, no accountKeys).
* Every notification carries the whole decoded transaction: named
  accounts, named instruction arguments, transaction ``summary``
  (type + description + structured ``parsedData``), SOL transfers
  (``nativeTransfers``) and token transfers (``tokenTransfers``),
  ``matchedIndexes`` into ``instructions``. Each instruction is in
  one of three states: **decoded** (``decoded`` object with named
  args/accounts), **recognized** (adds a ``summary``), **undecoded**
  (``decoded`` null; ``rawData`` + ``rawAccounts`` instead).
* ``blockTime`` is currently always null — never build on it.
  Swap amounts in ``parsedData`` are VENUE-REPORTED, not what the
  wallet actually received (Token-2022 transfer fees and other
  deductions outside the swap are not accounted for) — compare
  token-account balances before/after for exact amounts.
* Delivery is AT-MOST-ONCE with NO replay; the outbound buffer
  holds 2048 notifications, then the connection is closed. On
  reconnect the gap is recovered via RPC backfill
  (``event_backfill``), never by re-asking the stream. This client
  records ``last_slot`` so the backfill caller knows where to resume.
* Limits (Free): 5 concurrent connections per project, 25
  subscriptions per connection, 10 client messages/s (burst 20),
  64 KiB client message size, programs ≤10 / instructionNames ≤50 /
  accounts.include ≤100 / roles ≤20 per filter. Cost: 1 credit per
  delivered event (capability config).
* ``describeProgram`` is served ONLY on the Parsed Streams
  endpoint, so it needs a SEPARATE connection from subscriptions.

TRUST MODEL — the parsed representation is NOT assumed correct:
every parsed event carries a ``verification_status``. A stream
event is ``parsed_unverified`` until the OWN decoder (see
``daemon/intelligence/decoders/``) has checked the same
transaction from standard RPC and agreed on the facts that matter
(signature, slot, fee, status, program-id multiset). A
disagreement is recorded as a CONFLICT and is NEVER silently
trusted — the parsed fields are flagged, not promoted. An event
whose matched instructions are undecoded is ``undecoded``: the
raw bytes are available, but no decoded claim is made about it.

ROUTING: every subscription goes through the router first
(``provider_router.route_stream("parsed_tx_stream")``) — P1 for
ordinary priority, P0 for CRITICAL. The router is the ONLY place
routing decisions live; this module never picks a provider itself.
If the router refuses (Helius disabled/unconfigured), no socket
is opened.

KEYS: this module never reads a key file and never logs a URL.
The WSS URL (which carries the key) is supplied by the caller —
on AGT, by the deployment wiring that owns the Helius key — or the
client reports unconfigured. ``wss_url`` is never printed, logged,
or stored in stats.

NOT LIVE: no live Helius connection has been made from this VM
(Helius is flapping / auto-quiet; live verification is DEFERRED to
AGT). Everything here is verified against documented sample
structures (labelled SAMPLE in tests) and the real on-chain
fixtures under tests/data/intelligence/acquisition/.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from . import provider_capabilities as caps
from . import provider_router as router_mod
from .provider_router import PARSED_TX_STREAM
from .request_scheduler import PRIORITY_CARD, PRIORITY_CRITICAL
from daemon import solana_ids as _solana_ids

logger = logging.getLogger(
    "forex_agent.daemon.intelligence.acquisition.helius_streams")

# ---------------------------------------------------------------------------
# Protocol constants (Helius Parsed Streams docs, 2026-11-22)
# ---------------------------------------------------------------------------
PARSED_STREAMS_WSS_TMPL = "wss://beta.helius-rpc.com/?api-key={key}"
PARSED_STREAMS_HOST = "beta.helius-rpc.com"
# describeProgram remains on a separate discovery host per current docs.
PROGRAM_DISCOVERY_WSS_TMPL = "wss://fs-beta.helius-rpc.com/?api-key={key}"

METHOD_SUBSCRIBE = "parsedTransactionSubscribe"
METHOD_UNSUBSCRIBE = "parsedTransactionUnsubscribe"
METHOD_DESCRIBE = "describeProgram"
NOTIFICATION_METHOD = "parsedTransactionNotification"

# Documented limits (Quickstart "Limits" table).
MAX_CONCURRENT_CONNECTIONS_FREE = 5
MAX_SUBSCRIPTIONS_PER_CONNECTION = 25
MAX_CLIENT_MESSAGES_PER_S = 10
MAX_PROGRAMS_PER_FILTER = 10
MAX_INSTRUCTION_NAMES_PER_FILTER = 50
MAX_ACCOUNTS_INCLUDE_PER_FILTER = 100
MAX_ACCOUNT_ROLES_PER_FILTER = 20
OUTBOUND_BUFFER_NOTIFICATIONS = 2048

# Cost: 1 credit per delivered event (capability config,
# helius_main.parsed_streams).
CREDITS_PER_EVENT = 1

# Reconnect backoff (same shape as websocket_manager: 2s base,
# doubling, 60s cap).
RECONNECT_BASE_S = 2.0
RECONNECT_MAX_S = 60.0

# Program ids the bot cares about (public, well-known; canonical
# values live in daemon/solana_ids.py — never redefine the literals
# here, Phase 2, D-01/D-04/D-05). Jupiter/Raydium/Orca are DEX-filter
# only and stay local to this module.
PUMP_PROGRAM_ID = _solana_ids.PUMP_PROGRAM_ID
PUMPSWAP_PROGRAM_ID = _solana_ids.PUMPSWAP_PROGRAM_ID
JUPITER_PROGRAM_ID = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
RAYDIUM_AMM_PROGRAM_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
ORCA_WHIRLPOOL_PROGRAM_ID = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
TOKEN_PROGRAM_ID = _solana_ids.TOKEN_PROGRAM_ID
TOKEN_2022_PROGRAM_ID = _solana_ids.TOKEN2022_PROGRAM_ID
SYSTEM_PROGRAM_ID = _solana_ids.SYSTEM_PROGRAM_ID

# High-priority programs for the default DEX filter. Filtered
# subscriptions ONLY — never every Solana transaction.
DEX_PROGRAMS = (
    PUMP_PROGRAM_ID, PUMPSWAP_PROGRAM_ID, JUPITER_PROGRAM_ID,
    RAYDIUM_AMM_PROGRAM_ID, ORCA_WHIRLPOOL_PROGRAM_ID,
)

# Verification statuses a parsed event can carry.
VERIFICATION_PARSED_UNVERIFIED = "parsed_unverified"
VERIFICATION_VERIFIED = "verified"
VERIFICATION_CONFLICT = "conflict"
VERIFICATION_UNVERIFIABLE = "unverifiable"
VERIFICATION_UNDECODED = "undecoded"

# JSON-RPC error codes the Quickstart documents.
ERR_INVALID_PARAMS = -32602   # bad pubkey / unknown field / bad value
ERR_FILTER_LIMIT = -32000     # filter limit exceeded
ERR_SERVER_NOT_READY = -32001
ERR_RATE_LIMITED = -32002     # 10 messages/second
ERR_TOO_MANY_SUBS = -32006    # 25 subscriptions per connection


# ---------------------------------------------------------------------------
# LaserStream evaluation (spec §6)
# ---------------------------------------------------------------------------
def laserstream_evaluation():
    """Documented evaluation of LaserStream as a transport for the
    parsed-stream client (Helius LaserStream WebSocket docs +
    credits page, read 2026-11-22; capability config agrees).

    VERDICT: LaserStream WSS standard is NOT the transport for this
    client, and LaserStream gRPC is NOT built.

    * LaserStream WSS (standard Solana methods) IS included on Free:
      ``wss://mainnet.helius-rpc.com/?api-key=<key>``, 2 credits per
      0.1 MB streamed, 1 credit to open a connection. It serves the
      standard subscription methods (accountSubscribe,
      programSubscribe, logsSubscribe, signatureSubscribe,
      slotSubscribe, ...) — RAW transactions/accounts, NOT decoded
      ones. It cannot deliver named accounts, named instruction
      arguments, or transaction summaries, which is the whole point
      of the parsed-stream client. Use it (via the existing
      ``websocket_manager``, which already supports the standard
      methods) only if a future need for raw high-volume
      subscription arises.
    * LaserStream WSS Helius extensions (``transactionSubscribe``,
      enhanced ``accountSubscribe``) require Developer+ — PAID,
      out of the free-only program.
    * LaserStream gRPC (Yellowstone) is Business $499/mo on mainnet
      — PAID-GATED. No gRPC client is built, and none may be built
      without an explicit paid decision by the operator.
    * Parsed Streams (``wss://fs-beta.helius-rpc.com``) is the
      decoded product and the correct transport here: 1 credit per
      delivered event, included on Free, server-side filtering by
      program / instruction name / account role.

    This is a documentation function, not a client: it returns the
    evaluation so callers and tests can assert it, and so a future
    agent does not re-litigate it.
    """
    return {
        "verdict": "parsed_streams_endpoint",
        "parsed_streams_endpoint": PARSED_STREAMS_WSS_TMPL.format(
            key="<key>"),
        "laserstream_wss_standard": {
            "included_on_free": True,
            "endpoint": "wss://mainnet.helius-rpc.com/?api-key=<key>",
            "cost": "2 credits per 0.1 MB streamed (uncompressed); "
                    "1 credit to open a connection",
            "delivers": "standard Solana subscriptions only — RAW "
                        "transactions/accounts, NO decoding",
            "use": "not the parsed-stream transport; raw subscription "
                   "needs only, via websocket_manager",
        },
        "laserstream_wss_extensions": {
            "included_on_free": False,
            "status": "PAID (Developer+)",
            "note": "transactionSubscribe / enhanced accountSubscribe "
                    "are NOT on Free — out of scope",
        },
        "laserstream_grpc": {
            "included_on_free": False,
            "status": "PAID-GATED (Business $499/mo mainnet)",
            "note": "NO gRPC client built; do not build without an "
                    "explicit paid decision by the operator",
        },
        "capability_config_agrees": (
            caps.supports("helius_main", "laserstream_wss_standard")
            == caps.SUPPORT_YES
            and caps.supports("helius_main", "laserstream_grpc")
            == caps.SUPPORT_PAID
            and caps.supports("helius_main", "parsed_streams")
            == caps.SUPPORT_YES),
    }


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
def build_filter(programs=None, instruction_names=None,
                 accounts_include=None, accounts_roles=None,
                 include_cpi=True, include_failed=False):
    """Build a Parsed Streams filter dict, enforcing the documented
    limits. Raises ValueError on any violation — a bad filter must
    fail HERE, not as a silent no-match on the server.

    At least one of ``programs`` or ``accounts_include`` is required
    (server rule). Fields combine with AND. Instruction/role names
    must come from ``describeProgram`` — this function cannot check
    that, but it refuses empty strings and non-string entries.
    """
    programs = list(programs or [])
    instruction_names = list(instruction_names or [])
    accounts_include = list(accounts_include or [])
    accounts_roles = dict(accounts_roles or {})
    if not programs and not accounts_include:
        raise ValueError(
            "parsed-stream filter requires programs or "
            "accounts.include (at least one)")
    if len(programs) > MAX_PROGRAMS_PER_FILTER:
        raise ValueError("programs per filter: max %d, got %d"
                         % (MAX_PROGRAMS_PER_FILTER, len(programs)))
    if len(instruction_names) > MAX_INSTRUCTION_NAMES_PER_FILTER:
        raise ValueError("instructionNames per filter: max %d, got %d"
                         % (MAX_INSTRUCTION_NAMES_PER_FILTER,
                            len(instruction_names)))
    if len(accounts_include) > MAX_ACCOUNTS_INCLUDE_PER_FILTER:
        raise ValueError("accounts.include per filter: max %d, got %d"
                         % (MAX_ACCOUNTS_INCLUDE_PER_FILTER,
                            len(accounts_include)))
    if len(accounts_roles) > MAX_ACCOUNT_ROLES_PER_FILTER:
        raise ValueError("accounts.roles per filter: max %d, got %d"
                         % (MAX_ACCOUNT_ROLES_PER_FILTER,
                            len(accounts_roles)))
    for label, values in (("programs", programs),
                          ("instructionNames", instruction_names),
                          ("accounts.include", accounts_include)):
        for value in values:
            if not isinstance(value, str) or not value:
                raise ValueError("%s: entries must be non-empty "
                                 "strings, got %r" % (label, value))
    for name, addr in accounts_roles.items():
        if not isinstance(name, str) or not name:
            raise ValueError("accounts.roles: role names must be "
                             "non-empty strings, got %r" % (name,))
        if not isinstance(addr, str) or not addr:
            raise ValueError("accounts.roles[%r]: address must be a "
                             "non-empty string" % (name,))
    filt = {}
    if programs:
        filt["programs"] = programs
    if instruction_names:
        filt["instructionNames"] = instruction_names
    accounts = {}
    if accounts_include:
        accounts["include"] = accounts_include
    if accounts_roles:
        accounts["roles"] = accounts_roles
    if accounts:
        filt["accounts"] = accounts
    filt["includeCpi"] = bool(include_cpi)
    filt["includeFailed"] = bool(include_failed)
    return filt


def validate_filter(filt):
    """Validate an already-built filter dict. Returns a list of
    problems (empty = valid). Unknown top-level fields are a
    problem — the server rejects them with -32602, so catching them
    here saves a round trip."""
    problems = []
    if not isinstance(filt, dict):
        return ["filter is not an object"]
    known = {"programs", "instructionNames", "accounts", "includeCpi",
             "includeFailed"}
    for key in filt:
        if key not in known:
            problems.append("unknown filter field %r (server rejects "
                            "with -32602)" % (key,))
    try:
        rebuilt = build_filter(
            programs=filt.get("programs"),
            instruction_names=filt.get("instructionNames"),
            accounts_include=(filt.get("accounts") or {}).get("include"),
            accounts_roles=(filt.get("accounts") or {}).get("roles"),
            include_cpi=filt.get("includeCpi", True),
            include_failed=filt.get("includeFailed", False))
    except ValueError as exc:
        problems.append(str(exc))
        return problems
    if set(rebuilt) != set(filt):
        problems.append("filter lost fields on rebuild: %s"
                        % sorted(set(filt) - set(rebuilt)))
    return problems


def pumpfun_filter(include_failed=False):
    """Pump.fun bonding-curve program, all instructions."""
    return build_filter(programs=[PUMP_PROGRAM_ID],
                        include_failed=include_failed)


def pumpswap_filter(include_failed=False):
    """PumpSwap AMM program, all instructions."""
    return build_filter(programs=[PUMPSWAP_PROGRAM_ID],
                        include_failed=include_failed)


def dex_filter(include_failed=False):
    """High-priority DEX programs: Pump.fun, PumpSwap, Jupiter,
    Raydium AMM, Orca Whirlpool. Filtered only — never the full
    Solana transaction stream."""
    return build_filter(programs=list(DEX_PROGRAMS),
                        include_failed=include_failed)


def wallet_filter(addresses, programs=None, include_failed=False):
    """Important-wallet activity: any instruction touching one of
    the given wallets (optionally restricted to programs)."""
    return build_filter(programs=programs,
                        accounts_include=list(addresses),
                        include_failed=include_failed)


def liquidity_filter(include_failed=False):
    """High-priority liquidity events: token-program activity on
    the DEX programs (pool creation / adds / removes surface as
    token transfers inside DEX instructions)."""
    return build_filter(programs=list(DEX_PROGRAMS),
                        include_failed=include_failed)


# ---------------------------------------------------------------------------
# Notification parsing
# ---------------------------------------------------------------------------
def _instruction_state(ix):
    """Decoded / recognized / undecoded, per the docs' three-state
    model. Returns (state, summary_type_or_None)."""
    decoded = ix.get("decoded")
    if decoded is None:
        return "undecoded", None
    summary = ix.get("summary") or {}
    if summary.get("type"):
        return "recognized", summary.get("type")
    return "decoded", None


def normalize_parsed_transaction(value):
    """Normalize one parsed transaction (the ``value`` of a
    ``parsedTransactionNotification``, or one entry of a Parsed
    Events API response — the two products share the shape) into
    the module's event dict.

    Returns None when the value is not a recognizable parsed
    transaction. Every event carries ``verification_status``:
    ``parsed_unverified`` by default — the parsed representation
    is NEVER presented as verified. ``undecoded`` when every
    matched instruction is undecoded (no decoded claim is made).
    """
    if not isinstance(value, dict):
        return None
    # Current Parsed Events envelope, verified against an actual response.
    # A parser failure is evidence, never a successfully decoded transaction.
    if "parserStatus" in value:
        if value.get("parserStatus") != "OK" or not isinstance(value.get("parsed"), dict):
            return None
        parsed = dict(value["parsed"])
        parsed["signature"] = value.get("signature")
        execution = parsed.get("transactionStatus")
        parsed["status"] = {"SUCCESS": "ok", "SUCCEEDED": "ok", "ERROR": "error"}.get(execution, "unknown")
        value = parsed
    tx = value.get("transaction") or value
    if not isinstance(tx, dict):
        return None
    signature = tx.get("signature")
    if not isinstance(signature, str) or not signature:
        return None
    instructions = value.get("instructions") if "instructions" in value else tx.get("instructions")
    instructions = instructions or []
    if not isinstance(instructions, list):
        instructions = []
    matched = value.get("matchedIndexes")
    if matched is None:
        matched = tx.get("matchedIndexes")
    if not isinstance(matched, list):
        matched = []
    states = []
    for idx in matched:
        try:
            ix = instructions[int(idx)]
        except (TypeError, ValueError, IndexError):
            continue
        if isinstance(ix, dict):
            states.append(_instruction_state(ix)[0])
    summary = tx.get("summary") or {}
    status = tx.get("status")
    if status is None:
        status = "error" if tx.get("error") is not None else "ok"
    event = {
        "signature": signature,
        "slot": tx.get("slot"),
        "status": status,
        "error": tx.get("error"),
        "fee": tx.get("fee"),
        "fee_payer": tx.get("feePayer"),
        "account_keys": list(tx.get("accountKeys") or []),
        "instructions": instructions,
        "matched_indexes": list(matched),
        "matched_states": states,
        "summary": summary,
        "summary_type": summary.get("type"),
        "execution_status_known": status in ("ok", "error"),
        "native_transfers": list(tx.get("nativeTransfers") or []),
        "token_transfers": list(tx.get("tokenTransfers") or []),
        "verification_status": VERIFICATION_PARSED_UNVERIFIED,
        "verification_notes": [],
        "credits": CREDITS_PER_EVENT,
        "source": "helius_parsed_stream",
    }
    if states and all(s == "undecoded" for s in states):
        event["verification_status"] = VERIFICATION_UNDECODED
        event["verification_notes"].append(
            "every matched instruction is undecoded (decoded null); "
            "rawData/rawAccounts only, no decoded claim made")
    return event


def parse_notification(message):
    """Parse one inbound WebSocket frame (dict, already JSON-decoded)
    into (kind, payload).

    kind: "notification" (payload = normalized event),
    "subscribe_ack" (payload = {"local_id", "server_id"}),
    "unsubscribe_ack" (payload = {"local_id", "result"}),
    "error" (payload = {"local_id", "code", "message"}),
    "ignored" (payload = None — e.g. a describeProgram response
    on the subscription connection, or an unrecognised frame).
    """
    if not isinstance(message, dict):
        return "ignored", None
    if message.get("method") == NOTIFICATION_METHOD:
        params = message.get("params") or {}
        result = params.get("result") or {}
        value = result.get("value") or result
        event = normalize_parsed_transaction(value)
        if event is None:
            return "ignored", None
        event["subscription_id"] = params.get("subscription")
        return "notification", event
    if "id" in message and "result" in message:
        result = message["result"]
        if isinstance(result, int):
            return "subscribe_ack", {"local_id": message["id"],
                                     "server_id": result}
        return "unsubscribe_ack", {"local_id": message["id"],
                                   "result": result}
    if "error" in message and message["error"] is not None:
        err = message["error"] or {}
        return "error", {"local_id": message.get("id"),
                         "code": err.get("code"),
                         "message": err.get("message")}
    return "ignored", None


def extract_transfers(event):
    """SOL + token transfers from a normalized parsed event, in one
    list. Amounts are VENUE-REPORTED (see module docstring) — exact
    amounts received need a before/after token-balance comparison.
    Each entry: {"kind": "sol"|"token", "from", "to", "amount",
    "mint" (token only), "authority"}. Entries with a missing
    amount are dropped, never invented."""
    out = []
    for tr in event.get("native_transfers") or []:
        if not isinstance(tr, dict):
            continue
        amount = tr.get("amount")
        if amount is None:
            continue
        out.append({"kind": "sol", "from": tr.get("fromUserAccount"),
                    "to": tr.get("toUserAccount"), "amount": amount,
                    "mint": None,
                    "authority": tr.get("fromUserAccount")})
    for tr in event.get("token_transfers") or []:
        if not isinstance(tr, dict):
            continue
        amount = tr.get("tokenAmount")
        if amount is None:
            continue
        out.append({"kind": "token", "from": tr.get("fromUserAccount"),
                    "to": tr.get("toUserAccount"), "amount": amount,
                    "mint": tr.get("mint"),
                    "authority": tr.get("fromUserAccount")})
    return out


# ---------------------------------------------------------------------------
# Verification against the own decoder
# ---------------------------------------------------------------------------
def verify_with_own_decoder(event, raw_tx, decoder=None):
    """Verify a parsed event against the OWN decoder run over the
    standard-RPC ``getTransaction`` result for the same signature.

    ``decoder`` defaults to
    ``daemon.intelligence.decoders.solana_transaction.decode_transaction``
    (imported lazily — decoders never import the launch_scanner
    monolith). Returns a NEW event dict (the input is not mutated)
    with ``verification_status`` set to one of:

    * "verified" — signature, slot, fee, status and the program-id
      multiset all agree with the own decode.
    * "conflict" — at least one compared fact disagrees. The
      ``verification_notes`` name every disagreement; the parsed
      fields are flagged, NEVER silently trusted or overwritten.
    * "unverifiable" — the own decoder could not decode the raw
      transaction confidently (validation_status "unknown"), so no
      verdict is possible either way.

    A verified event is still a *parsed* event: verification means
    the two independent readings agree, not that the parsed output
    is authoritative. The own decoder remains authoritative over
    provider parsed output (architectural correction, 2026-11-22).
    """
    out = dict(event)
    notes = list(event.get("verification_notes") or [])
    if decoder is None:
        from daemon.intelligence.decoders import solana_transaction
        decoder = solana_transaction.decode_transaction
    try:
        own = decoder(raw_tx)
    except Exception as exc:  # decoder must never crash verification
        out["verification_status"] = VERIFICATION_UNVERIFIABLE
        notes.append("own decoder raised %s" % type(exc).__name__)
        out["verification_notes"] = notes
        return out
    if not isinstance(own, dict) or own.get("signature") is None:
        out["verification_status"] = VERIFICATION_UNVERIFIABLE
        notes.append("own decoder returned no transaction")
        out["verification_notes"] = notes
        return out
    if own.get("validation_status") == "unknown":
        out["verification_status"] = VERIFICATION_UNVERIFIABLE
        notes.append("own decoder validation_status is unknown: %s"
                     % "; ".join(own.get("notes") or []))
        out["verification_notes"] = notes
        return out
    if event.get("status") not in ("ok", "error"):
        out["verification_status"] = VERIFICATION_UNVERIFIABLE
        notes.append("parsed execution status unavailable")
        out["verification_notes"] = notes
        return out
    required = ("signature", "slot", "fee", "instructions", "status")
    missing = [k for k in required if event.get(k) is None]
    own_meta = own.get("meta") or {}
    if missing or own.get("slot") is None or own_meta.get("fee") is None or not own.get("program_ids") or not event.get("instructions"):
        out["verification_status"] = VERIFICATION_UNVERIFIABLE
        notes.append("comparison evidence incomplete")
        out["verification_notes"] = notes
        return out
    disagreements = []
    if own.get("signature") != event.get("signature"):
        disagreements.append("signature mismatch (own %r vs parsed %r)"
                             % (own.get("signature"),
                                event.get("signature")))
    if own.get("slot") is not None and event.get("slot") is not None \
            and own.get("slot") != event.get("slot"):
        disagreements.append("slot mismatch (own %r vs parsed %r)"
                             % (own.get("slot"), event.get("slot")))
    own_fee = (own.get("meta") or {}).get("fee")
    if own_fee is not None and event.get("fee") is not None \
            and own_fee != event.get("fee"):
        disagreements.append("fee mismatch (own %r vs parsed %r)"
                             % (own_fee, event.get("fee")))
    own_status = "error" if (own.get("meta") or {}).get("err") \
        is not None else "ok"
    if event.get("status") in ("ok", "error") \
            and own_status != event.get("status"):
        disagreements.append("status mismatch (own %r vs parsed %r)"
                             % (own_status, event.get("status")))
    instructions = event.get("instructions") or []
    if any(not isinstance(ix, dict) or not isinstance(ix.get("programId"), str) or not ix.get("programId") for ix in instructions):
        out["verification_status"] = VERIFICATION_UNVERIFIABLE
        notes.append("parsed instruction program identity unavailable")
        out["verification_notes"] = notes
        return out
    own_programs = sorted(p for p in (own.get("program_ids") or [])
                          if p)
    parsed_programs = sorted(
        p for p in {ix.get("programId") for ix in
                    (event.get("instructions") or [])
                    if isinstance(ix, dict)}
        if p)
    if own_programs and parsed_programs \
            and own_programs != parsed_programs:
        disagreements.append("program-id set mismatch (own %r vs "
                             "parsed %r)" % (own_programs,
                                            parsed_programs))
    if disagreements:
        out["verification_status"] = VERIFICATION_CONFLICT
        notes.extend(disagreements)
    else:
        out["verification_status"] = VERIFICATION_VERIFIED
        notes.append("own decoder agrees on signature/slot/fee/"
                     "status/programs")
    out["verification_notes"] = notes
    return out


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class HeliusParsedStreamClient:
    """Parsed Streams client over one WebSocket connection.

    Lifecycle: ``subscribe()`` registers a wanted filter (validated,
    router-checked, max 25/connection); ``connect()`` opens the
    socket; on disconnect ``run_forever()`` reconnects with backoff
    and re-issues every wanted subscription (resume: ``last_slot``
    is recorded so the RPC backfill caller knows where to resume —
    the stream itself never replays). ``stop()`` closes cleanly.

    The transport is injected as ``ws_factory(url, on_message,
    on_error, on_close, on_open)`` (same shape as websocket_manager)
    so no test ever opens a real socket. The default factory uses
    websocket-client lazily. ``wss_url`` must be supplied by the
    caller (the deployment wiring owns the Helius key); when it is
    None the client is unconfigured and ``connect()`` refuses.

    Lifecycle (D-02 fix, Phase 2): ``connected`` is set ONLY in the
    ``on_open`` callback, and subscriptions are sent (and re-sent on
    reconnect) ONLY from ``on_open``. ``connect()`` merely constructs
    the transport; the old code set ``connected = True`` right after
    the factory returned and sent subscriptions on a socket whose
    handshake had never run — the sends failed silently while the
    client reported healthy.
    """

    def __init__(self, wss_url=None, router=None, ws_factory=None,
                 on_event=None, now=None, usage_store=None):
        self._wss_url = wss_url  # never logged (carries the key)
        self._router = router
        self._factory = ws_factory or self._default_factory
        # Real transport (no injected factory) must pass shared-account
        # admission explicitly; injected factories are offline fakes.
        self._real_transport = ws_factory is None
        self._on_event = on_event
        self._now = now
        # Persistent usage accounting (Phase 3, D-07): when a
        # UsageStore is injected, every received event (1 credit
        # each, CREDITS_PER_EVENT), every verified event, and every
        # handshake 429 is recorded to the flock-guarded store, so
        # the counts survive a process restart. Without it the
        # in-memory counters below are the only record (the old
        # behaviour — kept for tests that construct the client
        # standalone). Recording never raises and never blocks the
        # stream: a store failure is logged once per call site.
        self._usage_store = usage_store
        self._wanted = {}      # label -> {filter, options, priority,
        #                        local_id, server_id}
        self._next_local_id = 1
        self._ws = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.connected = False
        self.connect_attempts = 0
        self.reconnects = 0
        self.handshake_429s = 0
        self.events_received = 0
        self.events_verified = 0
        self.events_conflict = 0
        self.events_undecoded = 0
        self.last_slot = None
        self.last_connect_ts = None
        self.last_disconnect_ts = None
        self.last_message_ts = None
        self.accounting_failures = 0
        self._accounting_blocked = False
        self._backoff_s = RECONNECT_BASE_S
        self._pending = {}     # local_id -> label (subscribe acks)

    def _time(self):
        return self._now() if self._now else time.time()

    def configured(self):
        return bool(self._wss_url)

    # -- routing (the router decides; this module never does) --------
    def route_check(self, priority=PRIORITY_CARD):
        """Ask the router whether a parsed stream may be opened.
        Returns the router's route_stream() decision dict. When no
        router was injected, a standalone decision is built from
        the router module's pure function with helius enabled —
        callers that own a router should always inject it."""
        if self._router is not None:
            return self._router.route_stream(PARSED_TX_STREAM,
                                             priority=priority)
        return {"workload": PARSED_TX_STREAM,
                "helius_priority": router_mod.helius_priority(
                    PARSED_TX_STREAM, {}, priority),
                "provider": "helius", "account": "helius_main",
                "stream": "parsed_streams", "refused": False,
                "refusal_reason": None}

    # -- subscriptions --------------------------------------------------
    def subscribe(self, filt, label, priority=PRIORITY_CARD,
                  details="full"):
        """Register a wanted subscription. Validates the filter,
        checks the router, enforces the 25-per-connection limit.
        Returns (ok, note). The live subscribe request is sent when
        connected; after a reconnect all wanted subs are re-issued."""
        problems = validate_filter(filt)
        if problems:
            return False, "invalid filter: %s" % "; ".join(problems)
        if details not in ("full", "matched", "raw"):
            return False, "details must be full|matched|raw"
        decision = self.route_check(priority=priority)
        if decision.get("refused"):
            return False, decision.get("refusal_reason")
        with self._lock:
            if len(self._wanted) >= MAX_SUBSCRIPTIONS_PER_CONNECTION \
                    and label not in self._wanted:
                return False, ("too many subscriptions: %d per "
                               "connection (documented limit)"
                               % MAX_SUBSCRIPTIONS_PER_CONNECTION)
            if label not in self._wanted:
                self._wanted[label] = {
                    "filter": filt, "options": {
                        "commitment": "confirmed", "details": details},
                    "priority": priority,
                    "helius_priority": decision.get("helius_priority"),
                    "local_id": self._next_local_id,
                    "server_id": None}
                self._next_local_id += 1
            entry = self._wanted[label]
        if self.connected:
            self._send_subscribe(entry)
        return True, None

    def unsubscribe(self, label):
        with self._lock:
            entry = self._wanted.pop(label, None)
        if entry is not None and self.connected and self._ws:
            try:
                self._ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": entry["local_id"],
                    "method": METHOD_UNSUBSCRIBE,
                    "params": [entry.get("server_id",
                                         entry["local_id"])]}))
            except Exception:
                pass
        return entry is not None

    def wanted_subscriptions(self):
        with self._lock:
            return [dict(v) for v in self._wanted.values()]

    # -- transport ------------------------------------------------------
    def _default_factory(self, url, on_message, on_error, on_close,
                         on_open):
        import websocket  # lazy: websocket-client, already in the venv
        app = websocket.WebSocketApp(
            url, on_message=on_message, on_error=on_error,
            on_close=on_close, on_open=on_open)
        return app

    def _send_subscribe(self, entry):
        if not self._ws:
            return
        with self._lock:
            self._pending[entry["local_id"]] = entry
        try:
            self._ws.send(json.dumps({
                "jsonrpc": "2.0", "id": entry["local_id"],
                "method": METHOD_SUBSCRIBE,
                "params": [entry["filter"], entry["options"]]}))
        except Exception:
            logger.warning("parsed-stream subscribe send failed")

    def _record_usage(self, method, kind, cost=None, elapsed=0.0):
        """One persistent usage line (D-07). Never raises."""
        store = self._usage_store
        if store is None:
            return None
        try:
            return store.record(
                account="helius_main", provider="helius",
                method=method, workload=PARSED_TX_STREAM,
                priority=PRIORITY_CARD, helius_priority=None,
                cost=cost, kind=kind, elapsed=elapsed)
        except Exception:
            logger.warning("parsed-stream usage record failed",
                           exc_info=True)
            return None

    def record_verified_event(self, event=None):
        """A consumer verified one received event against the own
        decoder. Increments the in-memory counter AND the persistent
        store (D-07). Previously events_verified was never
        incremented anywhere — only initialised and reported — so
        the counter was permanently zero; this is the increment
        site."""
        self.events_verified += 1
        self._record_usage("parsed_streams/verified", "ok",
                           cost=None)
        return self.events_verified

    def persistent_stats(self):
        """Persistent counters from the UsageStore (None when no
        store is attached): calls + cost for the parsed_streams
        feature this month, surviving restarts."""
        store = self._usage_store
        if store is None:
            return None
        rec = store.get("helius_main")
        feat = (rec.get("by_feature") or {}).get("parsed_streams", {})
        return {"calls": feat.get("calls", 0),
                "cost": feat.get("cost", 0),
                "month": rec.get("month"),
                "account_calls": rec.get("calls", 0)}

    def _on_message(self, _ws, message):
        self.last_message_ts = self._time()
        try:
            data = json.loads(message)
        except ValueError:
            return
        kind, payload = parse_notification(data)
        if kind == "subscribe_ack":
            with self._lock:
                entry = self._pending.pop(payload["local_id"], None)
            if entry is not None:
                entry["server_id"] = payload["server_id"]
            return
        if kind == "notification":
            event = payload
            self.events_received += 1
            # 1 credit per delivered event (CREDITS_PER_EVENT).
            self._record_usage("parsed_streams/event", "ok",
                               cost=CREDITS_PER_EVENT)
            if event.get("slot") is not None:
                self.last_slot = event["slot"]
            if event["verification_status"] == VERIFICATION_UNDECODED:
                self.events_undecoded += 1
            if event["verification_status"] == VERIFICATION_VERIFIED:
                self.events_verified += 1
            if self._on_event:
                self._on_event(event)
            return
        if kind == "error":
            logger.warning("parsed-stream error %s: %s",
                           payload.get("code"), payload.get("message"))
            if payload.get("code") == ERR_RATE_LIMITED:
                self.handshake_429s += 1
                self._record_usage("parsed_streams/handshake",
                                   "rate_limited")
                # A subscription rejection is not a failed HTTP
                # upgrade handshake — report it on that lane.
                self._report_subscription_throttled()
            return

    def _on_open(self, _ws):
        """Handshake completed: the connection is established NOW.
        Only here does ``connected`` become True, and only here are
        the wanted subscriptions sent (re-sent on every reconnect,
        exactly once per open). A stop that landed before the open
        leaves the client disconnected."""
        if self._stop.is_set():
            return
        self.connected = True
        self.last_connect_ts = self._time()
        self._backoff_s = RECONNECT_BASE_S
        # Shared-account handshake accounting (success).
        if not self._report_ws_result(None):
            return
        # Live records, not copies: _send_subscribe registers the
        # entry in _pending so the subscribe ack can record the
        # server id back onto the SAME dict.
        with self._lock:
            entries = list(self._wanted.values())
        for entry in entries:
            self._send_subscribe(entry)

    def _report_ws_result(self, error):
        """Shared-account handshake accounting for this stream's URL.
        Accounting must never break the stream: failures are counted
        (accounting_failures, surfaced in stats()) and logged."""
        from . import routine as _routine
        try:
            _routine.websocket_result(self._wss_url, error)
            return True
        except Exception:
            self.accounting_failures += 1
            self._accounting_blocked = True
            self.stop()
            logger.warning("parsed-stream result accounting failed",
                           exc_info=True)
            return False

    def _report_subscription_throttled(self):
        from . import routine as _routine
        try:
            _routine.websocket_subscription_throttled(self._wss_url)
        except Exception:
            self.accounting_failures += 1
            self._accounting_blocked = True
            self.stop()
            logger.warning("parsed-stream throttle accounting failed",
                           exc_info=True)

    def _on_error(self, _ws, error):
        text = str(error)
        # Shared-account handshake accounting (failure / 429).
        self._report_ws_result(error)
        if "429" in text or "Too Many Requests" in text:
            self.handshake_429s += 1
            self._record_usage("parsed_streams/handshake",
                               "rate_limited")
            # A shared-account rate limit is a HOLD: reflect it in
            # connection state and close so the backoff reconnect
            # applies the hold instead of marching on connected.
            self.connected = False
            try:
                if self._ws is not None:
                    self._ws.close()
            except Exception:
                pass
        else:
            logger.warning("parsed-stream WS error: %s", text[:200])

    def _on_close(self, _ws, *_args):
        self.connected = False
        self.last_disconnect_ts = self._time()
        if not self._stop.is_set():
            self._schedule_reconnect()

    def connect(self):
        """Construct the transport (one attempt). Returns True when
        the transport was constructed — NOT when the connection is
        open. The handshake completes asynchronously: ``connected``
        stays False until ``_on_open`` fires (driven by the
        transport's run loop — for the default factory that is
        ``WebSocketApp.run_forever``, called by ``run_forever``).
        Subscriptions are sent from ``_on_open`` only, never from
        here. Refuses when unconfigured (no URL) or when the router
        refuses the stream."""
        if self._accounting_blocked:
            return False
        if not self.configured():
            return False
        decision = self.route_check()
        if decision.get("refused"):
            logger.warning("parsed-stream refused by router: %s",
                           decision.get("refusal_reason"))
            return False
        # Mandatory shared-account admission — the same guard every
        # other WebSocket consumer (logstream, armed_watch) passes
        # through. Budget holds, account disable and the account-wide
        # pacer all apply to Parsed Streams: it is the same Helius
        # account. A refusal raises (fail closed, like logstream);
        # an unrecognized host returns False = "no shared guard
        # applies" and proceeds unchanged (test fakes, diagnostics).
        from . import routine as _routine
        admitted = _routine.websocket_admit(self._wss_url)
        if admitted is not True and self._real_transport:
            # Fail closed: a real socket whose URL the shared-account
            # guard does not recognize must NOT proceed.
            logger.warning("parsed-stream connect refused: URL outside "
                           "shared-account admission policy")
            return False
        self.connect_attempts += 1
        try:
            self._ws = self._factory(
                self._wss_url, self._on_message, self._on_error,
                self._on_close, self._on_open)
        except Exception as exc:
            if "429" in str(exc):
                self.handshake_429s += 1
                self._record_usage("parsed_streams/handshake",
                                   "rate_limited")
            return False
        return True

    def _schedule_reconnect(self):
        self.reconnects += 1
        wait = self._backoff_s
        self._backoff_s = min(RECONNECT_MAX_S, self._backoff_s * 2)
        logger.warning("parsed-stream disconnected; reconnect in "
                       "%.0fs (attempt %d)", wait, self.reconnects)
        return wait

    def next_reconnect_wait_s(self):
        return self._backoff_s

    def resume_info(self):
        """Where a reconnect resumes from. The stream never replays:
        the gap (if any) is recovered by RPC backfill from
        ``last_slot``. ``last_slot`` None means no event was ever
        received on this client — backfill has no anchor."""
        return {"last_slot": self.last_slot,
                "reconnects": self.reconnects,
                "events_received": self.events_received,
                "gap_recovery": "rpc_backfill_from_last_slot",
                "replay_available": False}

    def run_forever(self, sleep=time.sleep):
        """Connect, then reconnect with backoff until stop() is
        called. Intended for a daemon thread. A shared-account
        admission denial (budget hold, cooldown, account disabled)
        backs off and keeps the worker alive; it never kills the
        thread and never bypasses the hold."""
        from .routine import WebsocketAdmissionError
        while not self._stop.is_set():
            try:
                connected = self.connect()
            except WebsocketAdmissionError as exc:
                logger.warning("parsed-stream admission denied: %s "
                               "— backing off", str(exc)[:160])
                self.last_disconnect_ts = self._time()
                wait = self._schedule_reconnect()
                if sleep is time.sleep:
                    self._stop.wait(wait)
                else:
                    sleep(wait)
                continue
            if connected:
                try:
                    self._ws.run_forever()
                except Exception:
                    pass
                self.connected = False
                self.last_disconnect_ts = self._time()
            if self._stop.is_set():
                break
            wait = self._schedule_reconnect()
            if sleep is time.sleep:
                self._stop.wait(wait)
            else:
                sleep(wait)

    def stop(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self.connected = False

    def stats(self):
        out = {"provider": "helius", "stream": "parsed_streams",
                "connected": self.connected,
                "accounting_failures": self.accounting_failures,
                "accounting_blocked": self._accounting_blocked,
                "connect_attempts": self.connect_attempts,
                "reconnects": self.reconnects,
                "handshake_429s": self.handshake_429s,
                "events_received": self.events_received,
                "events_verified": self.events_verified,
                "events_conflict": self.events_conflict,
                "events_undecoded": self.events_undecoded,
                "wanted_subscriptions": len(self._wanted),
                "last_slot": self.last_slot,
                "last_connect_ts": self.last_connect_ts,
                "last_disconnect_ts": self.last_disconnect_ts,
                "last_message_ts": self.last_message_ts}
        # Persistent (restart-surviving) counters alongside the
        # in-memory ones, so a reader can tell a fresh process from
        # a fresh month (D-07).
        out["persistent"] = self.persistent_stats()
        return out
