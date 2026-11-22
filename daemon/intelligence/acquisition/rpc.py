"""Compatibility shim (Phases 3-4).

The implementation moved to ``rpc_client.py`` and the standardized
adapters in ``providers/``. This module re-exports the whole public
surface so existing imports (``from daemon.intelligence.acquisition
import rpc``) keep working unchanged. New code imports
``rpc_client`` directly.

No key material lives here; only URL templates with a ``{key}``
placeholder (the key-hygiene test reads this file).
"""

from __future__ import annotations

from .rpc_client import (
    ADAPTERS,
    ALCHEMY_CU,
    ALCHEMY_HTTPS_TMPL,
    ALCHEMY_WSS_TMPL,
    CHAINSTACK_ARCHIVE_METHODS,
    CHAINSTACK_RU,
    HELIUS_WSS_TMPL,
    PROVIDERS,
    PUBLIC_URL,
    UNSUPPORTED,
    UNVERIFIED_METHODS,
    AlchemyAdapter,
    BaseAdapter,
    BaseProviderAdapter,
    ChainstackAdapter,
    HeliusAdapter,
    PublicAdapter,
    RpcResult,
    _HttpAdapter,
    _host_of,
    _parse_rpc_body,
    _read_key_file,
    _result,
    alchemy_key,
    alchemy_url,
    alchemy_wss_url,
    chainstack_endpoint,
    chainstack_wss_url,
    classify_429,
    configured_providers,
    http_json_rpc,
    is_rate_limit_error,
    make_adapter,
    method_cost,
    rpc,
    wss_url,
)

__all__ = [
    "PROVIDERS",
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
