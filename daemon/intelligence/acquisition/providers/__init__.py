"""Standardized provider adapters (Phases 3-4).

One adapter per provider, all implementing the interface in
``base.BaseProviderAdapter``. Capability matching comes from
``provider_capabilities.py`` (quotas as config, not assumptions).
"""

from .alchemy import AlchemyAdapter
from .base import (
    BaseAdapter,
    BaseProviderAdapter,
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
from .chainstack import ChainstackAdapter
from .helius import HeliusAdapter
from .public import PublicAdapter

ADAPTERS = {
    "helius": HeliusAdapter,
    "alchemy": AlchemyAdapter,
    "chainstack": ChainstackAdapter,
    "public": PublicAdapter,
}

# Account each adapter's calls are billed to (capability-account
# names; the usage tracker records per account).
ADAPTER_ACCOUNTS = {name: cls.account for name, cls in ADAPTERS.items()}

__all__ = [
    "ADAPTERS",
    "ADAPTER_ACCOUNTS",
    "AlchemyAdapter",
    "BaseAdapter",
    "BaseProviderAdapter",
    "ChainstackAdapter",
    "HeliusAdapter",
    "PublicAdapter",
    "RpcResult",
    "_host_of",
    "_parse_rpc_body",
    "_read_key_file",
    "_result",
    "alchemy_cu_table",
    "alchemy_key",
    "alchemy_url",
    "alchemy_wss_url",
    "chainstack_endpoint",
    "chainstack_ru_table",
    "chainstack_wss_url",
    "classify_429",
    "http_json_rpc",
    "is_rate_limit_error",
    "method_cost",
]


def make_adapter(provider, home=None, transport=None):
    """Build the adapter for one provider name."""
    cls = ADAPTERS[provider]
    return cls(home=home, transport=transport)
