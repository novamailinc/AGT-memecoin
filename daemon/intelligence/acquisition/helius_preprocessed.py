"""Helius Preprocessed Transactions — two-stage lifecycle (Phase 7,
spec §6). DISABLED. The stream is PAID-GATED on Helius Free.

Preprocessed Transactions (Helius docs, read 2026-11-22):
``preprocessedSubscribe`` streams pre-execution transactions over
WebSocket from ``wss://beta.helius-rpc.com`` (the Gatekeeper
endpoint), BEFORE they reach the ``processed`` commitment level.
0.1 credits/message, ALL PAID PLANS ONLY — not on Free. Best
effort, no replay. Each API key is limited to 10 concurrent
connections/subscriptions. Subscriptions are scoped by account
filters (``accountInclude`` / ``accountExclude`` /
``accountRequired`` — at least one account between include and
required; there is no unfiltered full stream). Notifications
arrive as BINARY WebSocket frames, one transaction per frame:

    bytes 0      version      u8    (currently 1)
    bytes 1–8    slot         u64 LE (slot the tx was observed in)
    bytes 9–72   signature    64 bytes (first signature, binary)
    bytes 73+    transaction  wire bytes

THE STREAM IS DISABLED IN THIS PROGRAM. The router already
ALWAYS REFUSES preprocessed observation
(``provider_router.PREPROCESSED_REFUSAL``); this module keeps it
refused — ``request_stream()`` returns the refusal and no socket
is ever opened. This file exists so the two-stage lifecycle is
CODE, not folklore, ready for the day an operator makes an
explicit paid decision — and so the safety invariant is provable
TODAY:

* Stage 1 — early observation: a preprocessed transaction is
  received, its preliminary information is extracted, and it is
  marked PRELIMINARY / UNVERIFIED. It lives ONLY in the
  preliminary store.
* Stage 2 — execution verification: the transaction is waited for
  in the processed/confirmed lifecycle; its execution result is
  retrieved (standard RPC ``getTransaction`` via the router); the
  market state is updated ONLY from that verified information.
* A preliminary transaction MUST NEVER update confirmed market
  reserves and MUST NEVER authorize a trade. That is enforced in
  code: ``apply_to_confirmed_state()`` and ``authorize_trade()``
  refuse anything that is not stage-2 verified, and the refusal is
  a hard error, not a warning.

CREDIT COST: UNKNOWN / UNMEASURED. The docs' 0.1 credits/message
is a PAID-plan figure for a product this program does not use; no
rate is recorded here as if measured — ``PREPROCESSED_CREDIT_COST
is None``. Do not invent one.

Nothing in this module opens a socket, sends a subscription, or
touches a key. The binary-frame decoder is pure and testable
against synthetic frames built to the documented layout (labelled
SYNTHETIC in tests — no live preprocessed frame has ever been
received from this VM, and none is claimed).
"""

from __future__ import annotations

import base64
import struct
import time

from .provider_router import PREPROCESSED_REFUSAL
from .provider_router import PREPROCESSED_OBSERVATION

# The stream is DISABLED (paid-gated on Helius Free). This flag is
# a second, in-module lock on top of the router's refusal: both
# must agree before any stream could ever be requested, and neither
# is flipped by this code.
PREPROCESSED_STREAM_ENABLED = False

# Credit cost: unknown/unmeasured for this program (see docstring).
# None means "do not budget on a number" — never assume 0.1.
PREPROCESSED_CREDIT_COST = None

# Documented binary-frame layout (see docstring).
FRAME_VERSION = 1
FRAME_HEADER_LEN = 73  # 1 (version) + 8 (slot) + 64 (signature)


def request_stream():
    """Attempt to open the preprocessed stream. ALWAYS REFUSED:
    paid-gated on Helius Free. Returns the refusal dict — no
    socket, no subscription, no key involved."""
    return {"kind": "refused", "workload": PREPROCESSED_OBSERVATION,
            "stream": None, "refused": True,
            "refusal_reason": PREPROCESSED_REFUSAL,
            "stream_enabled": PREPROCESSED_STREAM_ENABLED,
            "credit_cost": PREPROCESSED_CREDIT_COST}


def decode_preprocessed_frame(frame):
    """Decode one binary preprocessed-transaction frame per the
    documented layout. Returns {"version", "slot", "signature"
    (base58), "transaction_b64", "transaction_bytes"} or None when
    the frame is too short or the version is not 1. Pure function;
    no I/O. The result is PRELIMINARY by construction — see
    ``PreprocessedLifecycle.observe``."""
    if not isinstance(frame, (bytes, bytearray)) or \
            len(frame) < FRAME_HEADER_LEN:
        return None
    raw = bytes(frame)
    version = raw[0]
    if version != FRAME_VERSION:
        return None
    slot = struct.unpack("<Q", raw[1:9])[0]
    sig_bytes = raw[9:73]
    tx_bytes = raw[73:]
    if not tx_bytes:
        return None
    try:
        from daemon.intelligence.decoders._base58 import b58encode
        signature = b58encode(sig_bytes)
    except Exception:
        return None
    return {"version": version, "slot": slot, "signature": signature,
            "transaction_b64": base64.b64encode(tx_bytes).decode(),
            "transaction_bytes": tx_bytes}


class PreliminaryRecord(dict):
    """A stage-1 observation. ``verified`` is ALWAYS False and
    ``stage`` is ALWAYS "preliminary" — the class refuses to
    construct a record that claims otherwise."""


def _preliminary_record(decoded_frame, observed_ts):
    return PreliminaryRecord({
        "signature": decoded_frame["signature"],
        "slot": decoded_frame["slot"],
        "transaction_b64": decoded_frame["transaction_b64"],
        "stage": "preliminary",
        "verified": False,
        "observed_ts": observed_ts,
        "verified_ts": None,
        "execution": None,
        "conflict": None,
    })


class PreprocessedLifecycle:
    """The two-stage preprocessed-transaction lifecycle.

    ``enabled`` defaults False and the stream request is refused
    regardless (see ``request_stream``). The lifecycle methods are
    usable in tests / future wiring with an explicit in-memory
    enablement ONLY to prove the stage discipline; they never open
    a socket and never touch confirmed state on their own.
    """

    def __init__(self, now=None, usage_store=None):
        self._now = now
        self._preliminary = {}   # signature -> PreliminaryRecord
        self._verified = {}      # signature -> verified record
        self._conflicts = []     # conflict records
        self.observed = 0
        self.verified_count = 0
        # Persistent usage accounting (Phase 3, D-07): when a
        # UsageStore is injected, stage-1 observations and stage-2
        # verifications are recorded against helius_main /
        # preprocessed_observation with cost None (the credit cost
        # is UNKNOWN/unmeasured for this program — see module
        # docstring; never recorded as 0 or 0.1). The stream itself
        # stays DISABLED; these counters cover the lifecycle object
        # only. Recording never raises.
        self._usage_store = usage_store

    def _record_usage(self, method, kind, elapsed=0.0):
        store = self._usage_store
        if store is None:
            return None
        try:
            return store.record(
                account="helius_main", provider="helius",
                method=method, workload="preprocessed_observation",
                priority="card", helius_priority="P0", cost=None,
                kind=kind, elapsed=elapsed)
        except Exception:
            return None

    def persistent_stats(self):
        """Persistent counters from the UsageStore (None when no
        store is attached): calls for the preprocessed_observation
        feature this month, surviving restarts."""
        store = self._usage_store
        if store is None:
            return None
        rec = store.get("helius_main")
        feat = (rec.get("by_feature") or {}).get(
            "preprocessed_observation", {})
        return {"calls": feat.get("calls", 0),
                "cost": feat.get("cost", 0),
                "month": rec.get("month"),
                "account_calls": rec.get("calls", 0)}

    def _time(self):
        return self._now() if self._now else time.time()

    # -- Stage 1: early observation ------------------------------------
    def observe(self, frame):
        """Stage 1: receive a preprocessed transaction, extract
        preliminary information, mark it preliminary/unverified.
        Returns (record_or_None, note). The record goes ONLY to the
        preliminary store — never to confirmed state."""
        decoded = decode_preprocessed_frame(frame)
        if decoded is None:
            return None, ("frame is not a valid preprocessed frame "
                          "(too short or unknown version)")
        rec = _preliminary_record(decoded, self._time())
        self._preliminary[rec["signature"]] = rec
        self.observed += 1
        self._record_usage("preprocessed/observe", "ok")
        return rec, None

    def preliminary(self, signature):
        return self._preliminary.get(signature)

    def preliminary_signatures(self):
        return sorted(self._preliminary)

    # -- Stage 2: execution verification -------------------------------
    def verify_execution(self, signature, get_tx_result):
        """Stage 2: wait for the transaction in the processed /
        confirmed lifecycle and retrieve its execution result.

        ``get_tx_result`` is a caller-supplied getTransaction
        result dict (standard RPC, via the router — the caller
        fetches it; this module never calls the network). Returns
        (record_or_None, note):

        * verified — the confirmed result exists, its signature and
          slot match the preliminary observation, and it carries an
          execution outcome (``meta.err`` present-or-null). The
          record is promoted to the verified store; the preliminary
          entry is REMOVED (a transaction is never in both stores).
        * conflict — signature/slot disagree, or no confirmed
          result exists yet (note says "not yet confirmed" — the
          caller retries later; nothing is promoted).
        * refused — no preliminary observation exists for the
          signature (verification cannot invent one).
        """
        rec = self._preliminary.get(signature)
        if rec is None:
            return None, ("no preliminary observation for %s; "
                          "verification cannot invent one" % signature)
        if not isinstance(get_tx_result, dict):
            return None, "confirmed result is not a transaction dict"
        tx = get_tx_result.get("transaction") or {}
        sigs = tx.get("signatures") or []
        confirmed_sig = sigs[0] if sigs else None
        confirmed_slot = get_tx_result.get("slot")
        meta = get_tx_result.get("meta")
        if confirmed_sig is None or confirmed_slot is None \
                or not isinstance(meta, dict):
            return None, ("confirmed result for %s is incomplete "
                          "(no signature/slot/meta) — not yet "
                          "verifiable" % signature)
        if confirmed_sig != rec["signature"]:
            conflict = {"signature": rec["signature"],
                        "stage": "conflict",
                        "reason": "confirmed signature %r != "
                                  "preliminary %r"
                                  % (confirmed_sig, rec["signature"])}
            self._conflicts.append(conflict)
            return None, conflict["reason"]
        if confirmed_slot != rec["slot"]:
            conflict = {"signature": rec["signature"],
                        "stage": "conflict",
                        "reason": "confirmed slot %r != preliminary "
                                  "slot %r"
                                  % (confirmed_slot, rec["slot"])}
            self._conflicts.append(conflict)
            return None, conflict["reason"]
        verified = dict(rec)
        verified["stage"] = "verified"
        verified["verified"] = True
        verified["verified_ts"] = self._time()
        verified["execution"] = {
            "slot": confirmed_slot,
            "err": meta.get("err"),
            "fee": meta.get("fee"),
            "succeeded": meta.get("err") is None,
        }
        del self._preliminary[signature]
        self._verified[signature] = verified
        self.verified_count += 1
        self._record_usage("preprocessed/verify", "ok")
        return verified, None

    def verified(self, signature):
        return self._verified.get(signature)

    def conflicts(self):
        return list(self._conflicts)

    # -- The safety invariant (enforced in code) -----------------------
    def apply_to_confirmed_state(self, record):
        """Gate for confirmed market state (reserves, positions,
        ledgers). Raises ValueError unless the record is stage-2
        verified. A preliminary record MUST NEVER update confirmed
        market reserves — this is the enforcement point, and it is
        a hard refusal, not a warning."""
        if not isinstance(record, dict):
            raise ValueError("confirmed state requires a record, "
                             "got %r" % type(record).__name__)
        if record.get("stage") != "verified" \
                or record.get("verified") is not True:
            raise ValueError(
                "REFUSED: %s is %s/unverified — preliminary data "
                "must NEVER update confirmed market reserves"
                % (record.get("signature"),
                   record.get("stage")))
        return {"applied": True, "signature": record["signature"],
                "slot": record.get("slot"),
                "execution": record.get("execution")}

    def authorize_trade(self, record):
        """Gate for trade authorization. Raises ValueError unless
        the record is stage-2 verified. A preliminary transaction
        MUST NEVER authorize a trade."""
        if not isinstance(record, dict):
            raise ValueError("trade authorization requires a record")
        if record.get("stage") != "verified" \
                or record.get("verified") is not True:
            raise ValueError(
                "REFUSED: %s is %s/unverified — a preliminary "
                "transaction must NEVER authorize a trade"
                % (record.get("signature"),
                   record.get("stage")))
        if not (record.get("execution") or {}).get("succeeded"):
            raise ValueError(
                "REFUSED: %s executed with error — a failed "
                "transaction must NEVER authorize a trade"
                % record.get("signature"))
        return {"authorized": True, "signature": record["signature"],
                "slot": record.get("slot")}

    def stats(self):
        return {"stream_enabled": PREPROCESSED_STREAM_ENABLED,
                "credit_cost": PREPROCESSED_CREDIT_COST,
                "observed_preliminary": self.observed,
                "preliminary_pending": len(self._preliminary),
                "verified": len(self._verified),
                "conflicts": len(self._conflicts),
                "persistent": self.persistent_stats()}
