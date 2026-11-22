"""Unified provider client (Phases 3-4).

The ONE per-provider call seam for the acquisition layer:

    rpc(provider, method, params, timeout=...) -> RpcResult
    RpcResult: {"result": ..., "kind": "ok"|"rate_limited"|"timeout"|
                "error"|"unsupported"|"unconfigured",
                "elapsed": float seconds, "provider": str,
                "http_status": int|None, "wait_hint_s": float|None,
                "rate_limit_kind": "throughput"|"monthly_quota"|None}

Providers are the standardized adapters in ``providers/`` — one
adapter per provider, all implementing the same interface
(``providers/base.py``). Capability matching comes from
``provider_capabilities.py`` (quotas as config, not assumptions);
the cost tables here are derived from its ``METHOD_COSTS``, not a
second copy.

This module is the successor of Phase 2's ``rpc.py``. ``rpc.py``
remains as a thin compatibility shim re-exporting this module so
existing imports keep working; new code imports ``rpc_client``.

No failover here — failover and ALL provider selection live in
``provider_router.py`` (Phase 4). This is the per-provider seam
the router, the scheduler, and the shadow harness call.

Providers: helius (live leg — READ-ONLY use of the existing
transport in ``daemon/launch_scanner/reserves.py``; this module
never re-implements Helius pacing), alchemy, chainstack, public
(keyless mainnet-beta).

LIVE STATUS (corrected 2026-11-22 ~02:02 PST): Alchemy/Chainstack
live verification from this VM is NOT possible through the
surrogate helpers, and the keys may be wrong after all — do NOT
declare them correct until a path-segment test proves it. The
query-param swap was PROVEN working (Helius vault getHealth HTTP
200), superseding the Nov 18 BLOCKED status for that placement;
the url_path_segment swap (Alchemy + Chainstack) is still
UNPROVEN. Alchemy's surrogate never changed (stored value likely
never changed); Chainstack's value changed and still 401s (either
the path-segment swap is broken or the node/key is inactive or
wrong). Live verification is DEFERRED to the AGT target.
Alchemy/Chainstack calls are verified here with mocked transport +
recorded fixtures only. Helius remains the live leg and the ONLY
provider verifiable from here.
"""

from __future__ import annotations

from . import provider_capabilities as caps
from .providers import ADAPTERS, ADAPTER_ACCOUNTS, make_adapter
from .providers import (
    AlchemyAdapter,
    BaseAdapter,
    BaseProviderAdapter,
    ChainstackAdapter,
    HeliusAdapter,
    PublicAdapter,
    RpcResult,
    _host_of,
    _parse_rpc_body,
    _read_key_file,
    _result,
    alchemy_cu_table,
    alchemy_key,
    alchemy_url,
    alchemy_wss_url,
    chainstack_endpoint,
    chainstack_ru_table,
    chainstack_wss_url,
    classify_429,
    http_json_rpc,
    is_rate_limit_error,
    method_cost,
)
from .providers.base import (
    ALCHEMY_HTTPS_TMPL,
    ALCHEMY_WSS_TMPL,
    HELIUS_WSS_TMPL,
    PUBLIC_URL,
    _HttpAdapter,
)

PROVIDERS = ("helius", "alchemy", "chainstack", "public")

# Provider name -> capability-account name (usage is tracked per
# account; see provider_usage.py).
PROVIDER_ACCOUNTS = dict(ADAPTER_ACCOUNTS)

# Per-method cost tables, derived from provider_capabilities.
# METHOD_COSTS — the single source (see the module docstring).
# Alchemy CU: 10 account reads, 20 slot/program/multi/send/simulate,
# 40 history reads. getTokenLargestAccounts: UNVERIFIED on Alchemy.
# Chainstack RU: 1 RU standard, 2 RU archive-class.
ALCHEMY_CU = alchemy_cu_table()
CHAINSTACK_RU = chainstack_ru_table()

# Methods a provider cannot serve on its free tier at all (from
# provider_capabilities.FREE_TIER_REFUSALS — the single source).
UNSUPPORTED = {
    "chainstack": set(caps.FREE_TIER_REFUSALS.get("chainstack", set())),
    "alchemy": set(caps.FREE_TIER_REFUSALS.get("alchemy", set())),
    "helius": set(caps.FREE_TIER_REFUSALS.get("helius_main", set())),
    "public": set(caps.FREE_TIER_REFUSALS.get("public", set())),
}
# Methods whose support is UNVERIFIED on a provider: callable, but the
# result must be treated as provisional and the call flagged.
UNVERIFIED_METHODS = {
    "alchemy": set(caps.UNVERIFIED_METHODS.get("alchemy", set())),
    "chainstack": set(caps.UNVERIFIED_METHODS.get("chainstack", set())),
    "helius": set(caps.UNVERIFIED_METHODS.get("helius_main", set())),
    "public": set(caps.UNVERIFIED_METHODS.get("public", set())),
}

# Methods that must never be routed to Chainstack even if a caller asks:
# the archive leg is the entry-critical path (curve reads, deployer).
CHAINSTACK_ARCHIVE_METHODS = {"getSignaturesForAddress"}

__all__ = [
    "PROVIDERS",
    "PROVIDER_ACCOUNTS",
    "ALCHEMY_CU",
    "CHAINSTACK_RU",
    "UNSUPPORTED",
    "UNVERIFIED_METHODS",
    "CHAINSTACK_ARCHIVE_METHODS",
    "PUBLIC_URL",
    "ALCHEMY_HTTPS_TMPL",
    "ALCHEMY_WSS_TMPL",
    "HELIUS_WSS_TMPL",
    "classify_429",
    "is_rate_limit_error",
    "RpcResult",
    "_result",
    "_read_key_file",
    "alchemy_key",
    "chainstack_endpoint",
    "alchemy_url",
    "alchemy_wss_url",
    "chainstack_wss_url",
    "_host_of",
    "http_json_rpc",
    "_parse_rpc_body",
    "BaseAdapter",
    "BaseProviderAdapter",
    "_HttpAdapter",
    "HeliusAdapter",
    "AlchemyAdapter",
    "ChainstackAdapter",
    "PublicAdapter",
    "ADAPTERS",
    "make_adapter",
    "configured_providers",
    "rpc",
    "method_cost",
    "wss_url",
]


def configured_providers(home=None):
    """Providers that can actually be called right now (key present
    or keyless). Helius counts only if the live transport is importable
    and holds keys."""
    out = []
    for name in PROVIDERS:
        try:
            if make_adapter(name, home=home).configured():
                out.append(name)
        except Exception:
            continue
    return out


def wss_url(provider, home=None):
    """WebSocket endpoint for a provider, or None when this layer
    cannot build one (Helius: the live transport owns the keys)."""
    try:
        return make_adapter(provider, home=home).wss_url()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# One-call seam: rpc(provider, method, params) -> RpcResult
# ---------------------------------------------------------------------------
def rpc(provider, method, params, timeout=12, home=None, transport=None):
    """Single-provider call on the unified contract. No failover here —
    failover lives in provider_router; this is the per-provider seam
    the router, the scheduler, and the shadow harness call."""
    adapter = make_adapter(provider, home=home, transport=transport)
    return adapter.call(method, params, timeout=timeout)
