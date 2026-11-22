"""Jito block-engine client (Phase 2): bundle send path.

STATUS NOTE: Phase 1 is still UNPROVEN (one post-fix close, Helius
quota spent, zero new entries). This module is built on the user's
explicit order with the Phase 1 deep-test gate waived — nothing
here claims Phase 1 is proven or clean.

A Jito bundle is an ordered set of up to 5 signed transactions
that either all land in the same slot or none of them land. The
bundle carries its own incentive: a SOL transfer (the tip) to one
of Jito's public tip accounts, built as a separate legacy transfer
transaction inside the bundle. The tip is paid ONLY if the bundle
lands — unlike a priority fee, which is paid whether or not the
transaction is included.

FAILURE TAXONOMY — read this before using the client:
  * A bundle id is NOT a landing confirmation. It only means the
    block engine accepted the entry into its auction.
  * A bundle that never lands is UNKNOWN, never "failed". The
    transactions may still land later (or have landed without the
    status poll seeing it). Callers MUST reconcile from on-chain
    balances before retrying anything — exactly the same rule as
    MainnetExecutor's confirm_timeout.
  * A bundle that loses the auction simply never lands; there is
    no error to catch. Poll get_bundle_statuses, then reconcile.

What this module does NOT do: NO secrets, NO keys, NO key
handling. The tip transaction is built UNSIGNED here
(build_tip_tx); the caller signs it with its own signer, exactly
as it signs the swap. The client only ever handles base64 of
already-signed transactions.

Endpoints: the public mainnet block engine plus the documented
regional engines (pick the closest). The JSON-RPC API needs no
authentication. Tip accounts are Jito's published public list —
note the published lists disagree on ONE entry (see TIP_ACCOUNTS);
get_tip_accounts() fetches the authoritative current list from the
block engine, and callers that care should refresh from it rather
than trusting this baked-in list forever.

HTTP is via an injectable callable so tests never touch the
network: http_post(url, payload_dict) -> response dict. The
default posts JSON-RPC to <base_url>/api/v1/bundles with stdlib
urllib. dry_run mode builds + validates the payload and returns it
WITHOUT any HTTP POST.
"""

import itertools
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from broker import BROKER_UNAVAILABLE, BrokerError
from broker.memecoin.execution import tx as tx_mod

logger = logging.getLogger("broker.memecoin.execution.jito")

DEFAULT_BLOCK_ENGINE_URL = "https://mainnet.block-engine.jito.wtf"

# Documented regional mainnet block engines (Jito docs). Pick the
# one closest to the deployment machine; the global endpoint is the
# default. No authentication is required for the JSON-RPC API.
REGION_ENDPOINTS = {
    "global": DEFAULT_BLOCK_ENGINE_URL,
    "amsterdam": "https://amsterdam.mainnet.block-engine.jito.wtf",
    "frankfurt": "https://frankfurt.mainnet.block-engine.jito.wtf",
    "new-york": "https://ny.mainnet.block-engine.jito.wtf",
    "tokyo": "https://tokyo.mainnet.block-engine.jito.wtf",
}

BUNDLES_PATH = "/api/v1/bundles"

# Jito's 8 published mainnet tip accounts (public keys — not
# secrets; anyone may send to them). Picked round-robin so no
# single account takes all contention. WARNING: published lists
# disagree on exactly one entry — the 6th account appears as
# ADuUkR4vqLUMWXxW9gh6D6L8pivKeVQqoZjfFp9LVD8e in some lists and
# ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt in others. The
# authoritative list is getTipAccounts on the block engine (see
# JitoClient.get_tip_accounts); refresh from it before relying on
# this baked-in list for real sends.
TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pivKeVQqoZjfFp9LVD8e",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]

MAX_TXS_PER_BUNDLE = 5

# Default tip: the low end of the documented Jito tip range
# (0.001-0.01 SOL). Even this low end eats a $1 trade — see
# fee_policy.jito_tip_verdict. Sizing is decided by the shakedown
# test, not by this build.
DEFAULT_TIP_LAMPORTS = 1_000_000  # 0.001 SOL


def _default_http_post(url: str, payload: Dict[str, Any],
                       timeout: float) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "forex-agent-memecoin-jito/1.0",
                 "Connection": "close"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise BrokerError(BROKER_UNAVAILABLE,
                          f"jito block engine HTTP {exc.code}",
                          detail={"reason": "jito_http_error",
                                  "http_status": exc.code}) from exc
    except (urllib.error.URLError, OSError, TimeoutError,
            ValueError) as exc:
        raise BrokerError(BROKER_UNAVAILABLE,
                          f"jito block engine unreachable "
                          f"({type(exc).__name__})",
                          detail={"reason": "jito_unreachable"}) from exc
    if not isinstance(body, dict):
        raise BrokerError(BROKER_UNAVAILABLE,
                          "jito block engine returned malformed response",
                          detail={"reason": "jito_bad_response"})
    if body.get("error"):
        err = body["error"]
        raise BrokerError(BROKER_UNAVAILABLE,
                          "jito block engine error: "
                          f"{str(err.get('message', err))[:160]}",
                          detail={"reason": "jito_error"})
    return body


class JitoClient:
    """Send + poll Jito bundles. No keys, no secrets, no signing.

    http_post: callable(url, payload_dict) -> response dict. When
    omitted, the default urllib POST is used. dry_run=True builds
    and validates payloads and returns them WITHOUT any HTTP POST.
    """

    def __init__(self, base_url: str = DEFAULT_BLOCK_ENGINE_URL,
                 http_post=None, dry_run: bool = False,
                 tip_accounts: Optional[List[str]] = None,
                 timeout: float = 30.0):
        self._base = (base_url or DEFAULT_BLOCK_ENGINE_URL).rstrip("/")
        self._http_post = http_post
        self._dry_run = bool(dry_run)
        self._timeout = float(timeout)
        self._tip_accounts = list(tip_accounts or TIP_ACCOUNTS)
        if not self._tip_accounts:
            raise BrokerError(BROKER_UNAVAILABLE,
                              "jito client needs at least one tip account")
        self._tip_cycle = itertools.cycle(self._tip_accounts)
        self._id = 0

    @property
    def base_url(self) -> str:
        return self._base

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def tip_accounts(self) -> List[str]:
        return list(self._tip_accounts)

    # -- tip accounts ----------------------------------------------------
    def pick_tip_account(self) -> str:
        """Next tip account, round-robin (spreads load; random pick
        is equally valid — round-robin is deterministic for tests)."""
        return next(self._tip_cycle)

    def get_tip_accounts(self) -> List[str]:
        """Authoritative current tip accounts from the block engine
        (JSON-RPC getTipAccounts). Refreshes the local list."""
        body = self._post("getTipAccounts", [])
        accounts = body.get("result") or []
        if isinstance(accounts, list) and accounts:
            self._tip_accounts = [str(a) for a in accounts]
            self._tip_cycle = itertools.cycle(self._tip_accounts)
        return list(self._tip_accounts)

    # -- tip transaction -------------------------------------------------
    def build_tip_tx(self, from_pubkey: str, tip_lamports: int,
                     blockhash: str, tip_account: Optional[str] = None):
        """Build the tip transfer as an UNSIGNED legacy transaction.

        Returns the unsigned solders Transaction. The CALLER signs
        it with its own signer — this module never touches keys.
        The tip account defaults to the next round-robin pick.
        """
        if int(tip_lamports) <= 0:
            raise BrokerError(BROKER_UNAVAILABLE,
                              "jito tip must be positive")
        to = tip_account or self.pick_tip_account()
        return tx_mod.build_transfer_tx(from_pubkey, to,
                                        int(tip_lamports), blockhash)

    # -- bundles ---------------------------------------------------------
    def build_bundle_payload(self, signed_b64_txs: List[str],
                             tip_lamports: int) -> Dict[str, Any]:
        """Validate + build the sendBundle JSON-RPC payload. No
        network. params is [ <list of base64 signed txs> ] — the
        shape the block engine's /api/v1/bundles endpoint takes."""
        txs = list(signed_b64_txs or [])
        if not txs:
            raise BrokerError(BROKER_UNAVAILABLE,
                              "jito bundle needs at least one signed "
                              "transaction")
        if len(txs) > MAX_TXS_PER_BUNDLE:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "jito bundle holds at most %d transactions, got %d"
                % (MAX_TXS_PER_BUNDLE, len(txs)))
        if any(not isinstance(t, str) or not t.strip() for t in txs):
            raise BrokerError(BROKER_UNAVAILABLE,
                              "jito bundle transactions must be non-empty "
                              "base64 strings")
        if int(tip_lamports) <= 0:
            raise BrokerError(BROKER_UNAVAILABLE,
                              "jito bundle needs a positive tip "
                              "(the tip is the bid for inclusion)")
        self._id += 1
        return {"jsonrpc": "2.0", "id": self._id,
                "method": "sendBundle", "params": [txs]}

    def send_bundle(self, signed_b64_txs: List[str],
                    tip_lamports: int) -> Dict[str, Any]:
        """Submit a bundle. Returns {"bundle_id", "tip_lamports",
        "tx_count", "dry_run"}.

        In dry_run mode the payload is built + validated and
        returned WITHOUT any HTTP POST (bundle_id None). A returned
        bundle_id means the engine ACCEPTED the bundle — it is NOT
        a landing confirmation. A bundle that never lands is
        UNKNOWN: reconcile from on-chain balances before retrying.
        """
        payload = self.build_bundle_payload(signed_b64_txs,
                                            tip_lamports)
        if self._dry_run:
            logger.info("jito dry-run: bundle payload built, not "
                        "posted (%d txs, tip %d lamports)",
                        len(signed_b64_txs), int(tip_lamports))
            return {"bundle_id": None, "tip_lamports": int(tip_lamports),
                    "tx_count": len(signed_b64_txs), "dry_run": True,
                    "payload": payload}
        body = self._post_raw(payload)
        result = body.get("result")
        bundle_id = None
        if isinstance(result, str) and result:
            bundle_id = result
        elif isinstance(result, dict):
            bundle_id = result.get("bundle_id") or result.get("uuid")
        if not bundle_id:
            raise BrokerError(BROKER_UNAVAILABLE,
                              "jito sendBundle returned no bundle id",
                              detail={"reason": "jito_no_bundle_id"})
        return {"bundle_id": str(bundle_id),
                "tip_lamports": int(tip_lamports),
                "tx_count": len(signed_b64_txs), "dry_run": False}

    def get_bundle_statuses(self, bundle_ids: List[str]) -> List[Any]:
        """Poll bundle statuses (JSON-RPC getBundleStatuses).

        Returns the engine's status list (each entry carries the
        bundle's confirmation status when landed). An empty/absent
        entry means NOT LANDED YET — still UNKNOWN, never failed.
        Raises in dry_run mode: a bundle that was never sent has no
        status to poll.
        """
        ids = list(bundle_ids or [])
        if not ids:
            return []
        if self._dry_run:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "jito dry-run client never sent a bundle — nothing "
                "to poll",
                detail={"reason": "jito_dry_run_no_poll"})
        body = self._post("getBundleStatuses", [ids])
        result = body.get("result")
        if isinstance(result, dict):
            return result.get("value") or []
        if isinstance(result, list):
            return result
        return []

    # -- internals ---------------------------------------------------------
    def _post(self, method: str, params: list) -> Dict[str, Any]:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id,
                   "method": method, "params": params}
        return self._post_raw(payload)

    def _post_raw(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = self._base + BUNDLES_PATH
        if self._http_post is not None:
            body = self._http_post(url, payload)
        else:
            body = _default_http_post(url, payload, self._timeout)
        if not isinstance(body, dict):
            raise BrokerError(BROKER_UNAVAILABLE,
                              "jito block engine returned malformed response",
                              detail={"reason": "jito_bad_response"})
        if body.get("error"):
            err = body["error"]
            raise BrokerError(BROKER_UNAVAILABLE,
                              "jito block engine error: "
                              f"{str(err.get('message', err))[:160]}",
                              detail={"reason": "jito_error"})
        return body
