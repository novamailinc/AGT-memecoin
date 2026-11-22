"""Vault-surrogate Helius auth bridge (2026-11-18, resilience build).

The bot today reads raw Helius keys from disk files
``~/.forex-agent/helius/api_key`` and ``api_key_discovery``. The
vault connector ``custom.helius`` holds a credential but does NOT
feed those files, so a fresh key handed over through secure entry
can never reach the bot. This module is the bridge: it obtains a
short-lived surrogate (``hsurr:*``) for ``custom.helius`` from
authd and builds the Helius RPC URL with the surrogate as the
``api-key`` query parameter — the placement the credential is
registered with — exactly as the connector CLI does. Sentinel/authd
replace the surrogate with the real key on approved egress; the
raw key never enters this process, a log line, or a file.

STATUS — QUERY-PARAM SWAP PROVEN 2026-11-22 (supersedes BLOCKED 2026-11-18):
  A Helius ``getHealth`` call carrying the surrogate URL returned
  HTTP 200, result ok (first ~02:02 PST, reproduced the same
  morning) — the egress proxy DOES swap query-param surrogates.
  The 2026-11-18 HTTP 401 was therefore almost certainly the
  stored ``custom.helius`` credential being invalid/stale at the
  time (a fresh vault key was wired in 2026-11-20), not a missing
  swap. ``url_path_segment`` swap (Alchemy, Chainstack) remains
  UNPROVEN from this VM: both still return HTTP 401 and neither
  provider echoes the received credential, so a broken path-segment
  swap and a wrong/inactive key cannot be distinguished here.
  This module is still NOT wired into the live path:
  ``reserves.rpc`` keeps disk keys — wiring it in is a router
  decision, not a swap problem. Never work around this by
  extracting or handling the raw key in any form.

Fail-soft throughout: any failure returns None and callers fall
back to disk keys. This module never performs HTTP itself — it
only builds a URL; the HTTP call is made by the paced
``reserves.rpc``. Never logs a URL (it carries the surrogate).
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(
    "forex_agent.daemon.launch_scanner.helius_vault")

CREDENTIAL_NAME = "custom.helius"
HELIUS_RPC_BASE = "https://mainnet.helius-rpc.com/"
ALLOWED_HOSTS = ("mainnet.helius-rpc.com", "api.helius.xyz")
_HELPER_PATH = "/opt/muse/skills/custom-connector-setup/bin"

_HELPER = None
_HELPER_TRIED = False
_WARNED = set()


def _load_helper():
    """The dynamic_credentials helper (lazy; None when absent).

    Test hook: set module-level _HELPER / _HELPER_TRIED directly
    to inject a fake or simulate a missing helper without touching
    the real authd socket."""
    global _HELPER, _HELPER_TRIED
    if _HELPER_TRIED:
        return _HELPER
    _HELPER_TRIED = True
    try:
        if _HELPER_PATH not in sys.path:
            sys.path.insert(0, _HELPER_PATH)
        import dynamic_credentials as helper  # noqa: PLC0415
        _HELPER = helper
    except Exception:
        _HELPER = None
    return _HELPER


def _warn_once(key, msg):
    if key not in _WARNED:
        _WARNED.add(key)
        logger.warning(msg)


def vault_surrogate_url(base: str = HELIUS_RPC_BASE):
    """Helius RPC URL carrying a vault surrogate, or None.

    None means: no helper, no vault credential, or the surrogate
    could not be issued — callers must fall back to disk keys.
    The returned URL carries only the ``hsurr:*`` surrogate, never
    a raw key. Never raises; never logs the URL."""
    helper = _load_helper()
    if helper is None:
        _warn_once("no-helper",
                   "helius_vault: dynamic_credentials helper "
                   "unavailable — vault auth disabled, disk keys "
                   "in use")
        return None
    try:
        return helper.url_with_surrogate_query_param(
            base, CREDENTIAL_NAME, allowed_hosts=ALLOWED_HOSTS)
    except Exception as exc:
        _warn_once("issue-failed",
                   "helius_vault: surrogate issuance failed (%s) — "
                   "vault auth disabled, disk keys in use"
                   % type(exc).__name__)
        return None


def vault_available() -> bool:
    """True when a vault surrogate URL can be issued right now."""
    return vault_surrogate_url() is not None


def reset_for_tests() -> None:
    """Test hook: clear the helper cache and the once-logs."""
    global _HELPER, _HELPER_TRIED
    _HELPER = None
    _HELPER_TRIED = False
    _WARNED.clear()
