"""Pre-signed transaction templates (Phase 2).

A SignedTxTemplate is built ONCE from a Jupiter swap-transaction
(base64) + a signer: sign once, hold the signed base64 + blockhash +
creation time/slot, and send it (or don't) within its validity
window. Signing once and reusing the signed bytes is what makes a
fast bundle path possible — no re-sign, no re-fetch, no second
Jupiter round-trip between decision and send.

Rules, enforced here, not by convention:
  * A template is valid only for the exact parameters it was built
    with (wallet, input_mint, output_mint, amount, slippage_bps).
    Any mismatch refuses reuse (raises BrokerError) — it NEVER
    silently rebuilds, because a rebuild would sign a different
    trade than the one that was checked.
  * A template expires after its TTL (default 60s / 150 slots — the
    blockhash validity window). A template is NEVER sent after
    expiry: reading the signed bytes past expiry raises.
  * invalidate() kills a template immediately (e.g. the trade was
    skipped or the slot moved on).
  * The keypair is passed in transiently for the single signing and
    then dropped — this module stores NO key material, only the
    signed bytes and public parameters.

No network, no key storage. Phase 1 is UNPROVEN (one post-fix
close, Helius quota spent, zero new entries); this module claims
nothing about Phase 1.
"""

import logging
import time
from typing import Optional

from broker import BROKER_UNAVAILABLE, BrokerError

logger = logging.getLogger("broker.memecoin.execution.tx_templates")

DEFAULT_TEMPLATE_TTL_SECONDS = 60.0
DEFAULT_TEMPLATE_TTL_SLOTS = 150


class SignedTxTemplate:
    """One signed swap transaction, held for fast (re)send."""

    def __init__(self, signed_base64: str, signature: str,
                 blockhash: str, wallet: str, input_mint: str,
                 output_mint: str, amount: int, slippage_bps: int,
                 slot: Optional[int] = None,
                 created_at: Optional[float] = None,
                 ttl_seconds: float = DEFAULT_TEMPLATE_TTL_SECONDS,
                 ttl_slots: int = DEFAULT_TEMPLATE_TTL_SLOTS):
        self._signed_base64 = signed_base64
        self._signature = signature
        self._blockhash = blockhash
        self.wallet = wallet
        self.input_mint = input_mint
        self.output_mint = output_mint
        self.amount = int(amount)
        self.slippage_bps = int(slippage_bps)
        self.slot = slot
        self.created_at = (time.monotonic() if created_at is None
                           else float(created_at))
        self.ttl_seconds = float(ttl_seconds)
        self.ttl_slots = int(ttl_slots)
        self._invalidated = False

    # -- construction --------------------------------------------------
    @classmethod
    def build(cls, swap_b64: str, keypair, wallet: str,
              input_mint: str, output_mint: str, amount: int,
              slippage_bps: int = 100, slot: Optional[int] = None,
              ttl_seconds: float = DEFAULT_TEMPLATE_TTL_SECONDS,
              ttl_slots: int = DEFAULT_TEMPLATE_TTL_SLOTS,
              created_at: Optional[float] = None,
              lookup_table_resolver=None):
        """Build + sign a template from a Jupiter swap transaction.

        Deserializes, runs the same sanity check as the live path
        (fee payer is our wallet, allowlisted programs only, both
        mints touched — including mints loaded via address lookup
        tables, resolved through `lookup_table_resolver`), signs
        ONCE with the transiently passed keypair, and holds the
        signed bytes. The keypair reference is local to this call
        and dropped on return — it is never stored on the template.

        `lookup_table_resolver` is a callable taking a table
        address (base58) and returning its addresses (base58 list)
        or None — see tx.rpc_lookup_table_resolver. A swap tx that
        uses lookup tables and gets no resolver is refused
        (fail-closed), never signed blind.
        """
        from broker.memecoin.execution import tx as tx_mod
        vtx = tx_mod.deserialize_versioned_tx(swap_b64)
        tx_mod.check_swap_tx(vtx, wallet, input_mint, output_mint,
                             lookup_table_resolver=lookup_table_resolver)
        signed_vtx, _sig = tx_mod.sign_versioned_tx(vtx, keypair)
        try:
            blockhash = str(vtx.message.recent_blockhash)
        except Exception:
            blockhash = ""
        return cls(
            signed_base64=tx_mod.versioned_tx_to_base64(signed_vtx),
            signature=tx_mod.versioned_signature_str(signed_vtx),
            blockhash=blockhash, wallet=wallet,
            input_mint=input_mint, output_mint=output_mint,
            amount=int(amount), slippage_bps=int(slippage_bps),
            slot=slot, created_at=created_at,
            ttl_seconds=ttl_seconds, ttl_slots=ttl_slots)

    # -- validity ------------------------------------------------------
    def is_expired(self, now: Optional[float] = None,
                   current_slot: Optional[int] = None) -> bool:
        """True when invalidated, past the time TTL, or past the
        slot TTL. A slot that moved BACKWARDS from the build slot is
        treated as expired (fail-closed: something is wrong with the
        slot source, so the template is not trusted)."""
        if self._invalidated:
            return True
        at = time.monotonic() if now is None else float(now)
        if at - self.created_at > self.ttl_seconds:
            return True
        if current_slot is not None and self.slot is not None:
            if int(current_slot) - int(self.slot) > self.ttl_slots:
                return True
            if int(current_slot) < int(self.slot):
                return True
        return False

    def invalidate(self) -> None:
        """Kill this template immediately. Idempotent."""
        self._invalidated = True

    @property
    def invalidated(self) -> bool:
        return self._invalidated

    @property
    def blockhash(self) -> str:
        return self._blockhash

    # -- access (expiry-enforced) --------------------------------------
    def require_valid(self, now: Optional[float] = None,
                      current_slot: Optional[int] = None) -> None:
        """Raise unless this template is still sendable."""
        if self.is_expired(now=now, current_slot=current_slot):
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "signed tx template expired (TTL %.0fs / %d slots) "
                "— never send after expiry; rebuild instead"
                % (self.ttl_seconds, self.ttl_slots),
                detail={"reason": "template_expired"})

    @property
    def signed_base64(self) -> str:
        """Signed bytes. Raises if the template is expired or
        invalidated — a template is never sent after expiry."""
        self.require_valid()
        return self._signed_base64

    @property
    def signature(self) -> str:
        """Base58 signature of the signed transaction. Raises if
        the template is expired or invalidated."""
        self.require_valid()
        return self._signature

    def reuse_for(self, wallet: str, input_mint: str,
                  output_mint: str, amount: int,
                  slippage_bps: int = 100,
                  now: Optional[float] = None,
                  current_slot: Optional[int] = None) -> str:
        """Return the signed bytes for an identical trade, or raise.

        Any parameter mismatch raises BrokerError
        (reason "template_param_mismatch") — reuse is ONLY for the
        identical (wallet, input_mint, output_mint, amount,
        slippage_bps). An expired template raises
        (reason "template_expired"). It never silently rebuilds.
        """
        mismatch = (
            wallet != self.wallet
            or input_mint != self.input_mint
            or output_mint != self.output_mint
            or int(amount) != self.amount
            or int(slippage_bps) != self.slippage_bps)
        if mismatch:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "signed tx template does not match this trade "
                "(wallet/input/output/amount/slippage must be "
                "identical) — refusing reuse, never rebuilding "
                "silently",
                detail={"reason": "template_param_mismatch"})
        self.require_valid(now=now, current_slot=current_slot)
        return self._signed_base64
