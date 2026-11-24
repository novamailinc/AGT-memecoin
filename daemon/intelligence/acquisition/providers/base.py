"""Standardized provider adapter interface (Phases 3-4).

Every provider adapter implements the same interface:

    adapter.name            # "helius" | "alchemy" | "chainstack" | "public"
    adapter.account         # capability-account the calls are billed to
                            # ("helius_main", "alchemy", "chainstack",
                            # "public"). Usage is recorded per ACCOUNT.
    adapter.configured()    # a call could be attempted right now
    adapter.supports(method)    # the provider CAN serve the method on
                            # its free tier (capability matching — see
                            # provider_capabilities.py, the single source)
    adapter.unverified(method)  # support/cost UNVERIFIED: callable, but
                            # the result must be flagged provisional
    adapter.cost(method)    # per-call cost in the provider's own unit
                            # (credits / CU / RU), or None when UNVERIFIED
    adapter.call(method, params, timeout) -> RpcResult
    adapter.wss_url()       # WebSocket endpoint, or None when the layer
                            # cannot build one (Helius: the live transport
                            # owns the keys; armed_watch owns its WSS URL)

One contract per call (the same shape the live ``reserves.rpc``
returns in spirit, extended so failover and shadow comparison can
tell a 429 from a timeout from a real error):

    RpcResult: {"result": ..., "kind": "ok"|"rate_limited"|"timeout"|
                "error"|"unsupported"|"unconfigured",
                "elapsed": float seconds, "provider": str,
                "http_status": int|None, "wait_hint_s": float|None,
                "rate_limit_kind": "throughput"|"monthly_quota"|None,
                "note": str|None}

Capability matching comes from ``provider_capabilities.py`` — quotas
and support are config there, never assumptions here. The cost
tables that used to live in ``rpc.py`` (``ALCHEMY_CU`` /
``CHAINSTACK_RU``) are derived from ``METHOD_COSTS`` in that module
now; this module keeps no second copy.

Rules enforced here (from the 2026-11-22 audits):
- Helius stays a config-disabled optional provider; it is never
  hard-removed.
- NEVER hardcode keys. NEVER log key material or a full URL (Helius
  and Alchemy URLs carry the key). Only hostnames are ever logged.
- A 429 is never retried blindly: the wait hint (Retry-After, or
  Chainstack ``error.data.try_again_in``) is honoured; a monthly-quota
  429 is a different condition from a throughput 429.
- 429 signals differ per provider: Alchemy HTTP 429 (optional
  Retry-After; WS is JSON-RPC error 429); Chainstack HTTP 429 +
  JSON-RPC -32005 with the wait hint in ``error.data.try_again_in``,
  no Retry-After header. -32005/-32029 are NOT Alchemy codes.
- Do NOT send a request to a provider that cannot perform the
  required method (``supports()`` is the gate; ``call()`` refuses
  with kind "unsupported" without touching the network).
"""

from __future__ import annotations

import json
import logging
import math
from email.utils import parsedate_to_datetime
import os
import re
import threading
import time
from urllib.parse import urlsplit
import urllib.error
import urllib.request

from .. import provider_capabilities as caps

logger = logging.getLogger(
    "forex_agent.daemon.intelligence.acquisition.providers.base")

# METHOD_COSTS tuples are (helius credits, alchemy CU, chainstack RU,
# public cost). Map adapter name -> tuple index.
COST_INDEX = {"helius": 0, "alchemy": 1, "chainstack": 2, "public": 3}

PUBLIC_URL = "https://api.mainnet-beta.solana.com"
PUBLIC_WSS_URL = "wss://api.mainnet-beta.solana.com"
ALCHEMY_HTTPS_TMPL = "https://solana-mainnet.g.alchemy.com/v2/{key}"
ALCHEMY_WSS_TMPL = "wss://solana-mainnet.streaming.alchemy.com/v2/{key}"
CHAINSTACK_HTTPS_TMPL = "https://solana-mainnet.core.chainstack.com/{key}"
HELIUS_WSS_TMPL = "wss://mainnet.helius-rpc.com/?api-key={key}"
# Parsed Streams (parsedTransactionSubscribe) are served ONLY on the
# Gatekeeper host, not on the standard mainnet WS host: a subscribe
# sent to mainnet.helius-rpc.com is rejected with -32601 (Method not
# found). Verified live 2026-11-23: same subscribe to this host is
# ACKed and delivers parsedTransactionNotification events on Free.
HELIUS_PARSED_STREAMS_WSS_TMPL = "wss://beta.helius-rpc.com/?api-key={key}"


# ---------------------------------------------------------------------------
# 429 classification
# ---------------------------------------------------------------------------
def classify_429(provider, http_status=None, rpc_error=None,
                 retry_after=None):
    """Classify a rate-limit signal.

    Returns (rate_limit_kind, wait_hint_s) where kind is
    "throughput" | "monthly_quota" | None and wait_hint_s is the
    seconds to wait before the next attempt (None = no hint given).

    A monthly-quota 429 is a DIFFERENT condition from a throughput
    429: a throughput 429 honours the hint / exponential backoff and
    retries later; a monthly-quota 429 must NOT be retried into the
    same provider this month — the provider is down for the workload
    until the allowance renews.
    """
    if http_status != 429 and not (rpc_error or {}).get("code") in (-32005, -32029, 429):
        return None, None
    message = ""
    wait_hint = None
    if rpc_error:
        message = str(rpc_error.get("message", "")).lower()
        data = rpc_error.get("data") or {}
        if isinstance(data, dict):
            hint = data.get("try_again_in")
            if hint is not None:
                try:
                    wait_hint = float(hint)
                except (TypeError, ValueError):
                    wait_hint = None
    if retry_after is not None:
        try:
            wait_hint = float(retry_after)
        except (TypeError, ValueError):
            try:
                wait_hint = max(0., parsedate_to_datetime(str(retry_after)).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    if wait_hint is not None and (not math.isfinite(wait_hint) or wait_hint < 0):
        wait_hint = None
    monthly_markers = ("monthly", "quota", "allowance", "limit exceeded",
                       "exceeded your", "upgrade", "billing")
    kind = "monthly_quota" if any(m in message for m in monthly_markers) \
        else "throughput"
    return kind, wait_hint


def is_rate_limit_error(provider, http_status=None, rpc_error=None):
    """True when the signal is a provider rate limit (any kind)."""
    if http_status == 429:
        return True
    if rpc_error:
        code = rpc_error.get("code")
        # -32005/-32029 are Chainstack codes, NOT Alchemy codes.
        if provider == "chainstack" and code in (-32005, -32029):
            return True
        if code == 429:  # Alchemy WS JSON-RPC error 429
            return True
    return False


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
class RpcResult(dict):
    """One provider call outcome. Dict-shaped so callers can treat it
    like the legacy (result, kind, elapsed) triple via .result/.kind."""

    @property
    def result(self):
        return self.get("result")

    @property
    def kind(self):
        return self.get("kind")

    @property
    def elapsed(self):
        return self.get("elapsed", 0.0)


def _result(result, kind, elapsed, provider, http_status=None,
            wait_hint_s=None, rate_limit_kind=None, note=None):
    return RpcResult({
        "result": result,
        "kind": kind,
        "elapsed": elapsed,
        "provider": provider,
        "http_status": http_status,
        "wait_hint_s": wait_hint_s,
        "rate_limit_kind": rate_limit_kind,
        "note": note,
    })


# ---------------------------------------------------------------------------
# Key / endpoint resolution (env wins over file; never logs material)
# ---------------------------------------------------------------------------
def _read_key_file(path):
    try:
        with open(path) as fh:
            value = fh.read().strip()
        return value or None
    except OSError:
        return None


def alchemy_key(home=None):
    """ALCHEMY_API_KEY env var, else $HOME/alchemy/api_key file,
    else $HOME/.forex-agent/alchemy/api_key (the runtime-home
    convention where the VM fix wrote the disk key, 2026-11-22).
    The dual lookup mirrors chainstack_endpoint: callers pass
    either the user home or FOREX_AGENT_HOME and the key is found
    either way (previously home=None missed the key entirely)."""
    env = os.environ.get("ALCHEMY_API_KEY", "").strip()
    if env:
        return env
    home = home or os.path.expanduser("~")
    key = _read_key_file(os.path.join(home, "alchemy", "api_key"))
    if key:
        return key
    return _read_key_file(os.path.join(
        home, ".forex-agent", "alchemy", "api_key"))


def chainstack_endpoint(home=None):
    """CHAINSTACK_ENDPOINT env var, else
    $HOME/.forex-agent/chainstack/endpoint file (full URL), else
    built from $HOME/.forex-agent/chainstack/api_key (the node
    auth_key): https://solana-mainnet.core.chainstack.com/<key>.
    Returns the full endpoint URL (or None when nothing is
    configured). The api_key fallback covers the disk key file the
    VM fix created (2026-11-22) — before it, a disk key alone left
    the adapter unconfigured (first live shadow comparison §7).
    Callers that pass home=FOREX_AGENT_HOME (the runtime dir)
    instead of the user home are covered by the second api_key
    lookup below."""
    env = os.environ.get("CHAINSTACK_ENDPOINT", "").strip()
    if env:
        return env
    home = home or os.path.expanduser("~")
    endpoint = _read_key_file(os.path.join(
        home, ".forex-agent", "chainstack", "endpoint"))
    if not endpoint:
        # Runtime-home callers use the exact path seeded by AGT preflight.
        endpoint = _read_key_file(os.path.join(home, "chainstack", "endpoint"))
    if endpoint:
        return endpoint
    key = _read_key_file(os.path.join(
        home, ".forex-agent", "chainstack", "api_key"))
    if not key:
        # home=FOREX_AGENT_HOME calling convention: same file, one
        # level up (mirrors how alchemy_key reads {home}/alchemy/).
        key = _read_key_file(os.path.join(home, "chainstack", "api_key"))
    if not key:
        return None
    if key.startswith("http://") or key.startswith("https://"):
        # A full URL stored in the key file by mistake: use as-is.
        return key
    return CHAINSTACK_HTTPS_TMPL.format(key=key)


def _alchemy_credential_key(home=None):
    """Accept a bare key or the exact supported Solana endpoint URL.

    Never interpolate a full endpoint into another credential path.
    Unrecognized URL-shaped values are refused without network access.
    """
    value = alchemy_key(home=home)
    if not value:
        return None
    if "://" not in value:
        return value if not any(c in value for c in "/?#@") else None
    try:
        parsed = urlsplit(value)
        expected = {"https": "solana-mainnet.g.alchemy.com",
                    "wss": "solana-mainnet.streaming.alchemy.com"}
        if (parsed.scheme not in expected or
                parsed.netloc != expected[parsed.scheme] or
                parsed.query or parsed.fragment or
                not parsed.path.startswith("/v2/")):
            return None
        key = parsed.path[len("/v2/"):]
        return key if key and not any(c in key for c in "/?#@%") else None
    except ValueError:
        return None


def alchemy_url(home=None):
    key = _alchemy_credential_key(home=home)
    return ALCHEMY_HTTPS_TMPL.format(key=key) if key else None


def alchemy_wss_url(home=None):
    key = _alchemy_credential_key(home=home)
    return ALCHEMY_WSS_TMPL.format(key=key) if key else None


def chainstack_wss_url(home=None):
    """Chainstack WSS uses the same host as HTTPS."""
    endpoint = chainstack_endpoint(home=home)
    if not endpoint:
        return None
    return endpoint.replace("https://", "wss://", 1)


def _host_of(url):
    try:
        return url.split("://", 1)[1].split("/", 1)[0]
    except Exception:
        return "unknown-host"


# ---------------------------------------------------------------------------
# Cost tables — derived from provider_capabilities.METHOD_COSTS, the
# single source. (The old rpc.py ALCHEMY_CU / CHAINSTACK_RU dicts are
# kept as aliases in rpc_client.py for compatibility.)
# ---------------------------------------------------------------------------
def method_cost(provider, method):
    """CU (Alchemy) / RU (Chainstack) / credits (Helius) cost of one
    call, or None when unknown/UNVERIFIED. Public has no metered
    cost (None)."""
    costs = caps.method_cost(provider, method)
    if costs is None:
        return None
    idx = COST_INDEX.get(provider)
    if idx is None:
        return None
    return costs[idx]


def alchemy_cu_table():
    """{method: CU} for Alchemy (None = UNVERIFIED cost)."""
    return {m: c[1] for m, c in caps.METHOD_COSTS.items()}


def chainstack_ru_table():
    """{method: RU} for Chainstack (None = paid-only / conflict)."""
    return {m: c[2] for m, c in caps.METHOD_COSTS.items()}


# ---------------------------------------------------------------------------
# HTTP transport (urllib; injectable for tests — never import requests)
# ---------------------------------------------------------------------------
_RL_NUMERIC_HEADERS = ("retry-after",)  # plus any *ratelimit*/x-cu* name
_RL_CONTENT_TYPES = ("application/json", "text/plain", "text/html")
_RL_BODY_VOCAB = ("monthly", "quota", "allowance", "capacity", "compute units",
                  "throughput", "rate limit", "too many", "exceeded", "upgrade",
                  "billing", "concurrent", "burst", "daily", "per second")
_NUM_RE = re.compile(r"^\d{1,10}(\.\d{1,3})?$")
_last_429 = threading.local()


def _capture_429(exc, raw):
    """Keep ONLY structured, allowlisted 429 facts: status, numeric
    rate-limit header values, a content-type from a fixed enum, the JSON
    error code (int), a body length, and which fixed-vocabulary words the
    message contained. Free text, header strings and identifiers are
    never stored, so privacy does not rest on pattern matching."""
    nums, ctype = {}, None
    try:
        for k, v in (exc.headers or {}).items():
            kl = k.lower()
            v = str(v).strip()
            if kl == "content-type":
                base = v.split(";")[0].strip().lower()
                ctype = base if base in _RL_CONTENT_TYPES else "other"
            elif kl in _RL_NUMERIC_HEADERS or "ratelimit" in kl \
                    or "rate-limit" in kl or kl.startswith("x-cu"):
                nums[kl[:40]] = v if _NUM_RE.match(v) else "<non-numeric>"
    except Exception:
        pass
    code, words, valid = None, [], False
    try:
        body = json.loads(raw.decode("utf-8", "replace")) if raw else None
        valid = isinstance(body, dict)
        err = (body or {}).get("error") if valid else None
        if isinstance(err, dict):
            c = err.get("code")
            code = c if isinstance(c, int) and not isinstance(c, bool) else None
            msg = str(err.get("message", "")).lower()
            words = [w for w in _RL_BODY_VOCAB if w in msg]
    except ValueError:
        pass
    _last_429.meta = {"status": 429, "numeric_headers": nums,
                      "content_type": ctype, "body_len": len(raw or b""),
                      "json_valid": valid, "error_code": code,
                      "message_vocab": words}


def http_json_rpc(url, method, params, timeout=12, _opener=None):
    """One JSON-RPC POST. Returns (http_status, parsed_body_or_None,
    error_kind, retry_after) where error_kind is None | "timeout" | "transport" |
    "rate_limited" | "http". Never raises. Never logs the URL."""
    _last_429.meta = None  # evidence belongs to THIS request only
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    opener = _opener or urllib.request.urlopen
    try:
        with opener(req, timeout=timeout) as resp:
            raw = resp.read()
            status = getattr(resp, "status", 200)
            retry_after = (getattr(resp,"headers",None) or {}).get("Retry-After")
    except urllib.error.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        status = exc.code
        try:
            parsed = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except ValueError:
            parsed = {}
        if status == 429:
            _capture_429(exc, raw)
            return status, parsed, "rate_limited", (exc.headers or {}).get("Retry-After")
        return status, parsed, "http", None
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        # urllib raises URLError wrapping timeouts; distinguish by text.
        if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
            return None, None, "timeout", None
        return None, None, "transport", None
    except Exception:
        return None, None, "transport", None
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return status, None, "http", None
    return status, parsed, None, retry_after


def _parse_rpc_body(provider, status, body, error_kind, retry_after=None):
    """Turn (status, body, error_kind) into (result, kind, wait, rl_kind)."""
    if error_kind == "timeout":
        return None, "timeout", None, None
    if error_kind == "rate_limited":
        err = (body or {}).get("error") or {}
        kind, wait = classify_429(provider, http_status=429,
                                  rpc_error=err or None, retry_after=retry_after)
        return None, "rate_limited", wait, kind
    if error_kind in ("transport", "http"):
        return None, "error", None, None
    if not isinstance(body, dict):
        return None, "error", None, None
    if "error" in body and body["error"] is not None:
        err = body["error"]
        if is_rate_limit_error(provider, http_status=status, rpc_error=err):
            kind, wait = classify_429(provider, http_status=status,
                                      rpc_error=err, retry_after=retry_after)
            return None, "rate_limited", wait, kind
        return None, "error", None, None
    return body.get("result"), "ok", None, None


# ---------------------------------------------------------------------------
# Adapter base
# ---------------------------------------------------------------------------
class BaseProviderAdapter:
    """The interface every provider adapter implements.

    Capability matching lives in provider_capabilities.py:
    ``supports()`` refuses the free-tier refusals recorded there
    (Chainstack: getSignaturesForAddress / getProgramAccounts /
    getTokenLargestAccounts); ``unverified()`` flags methods whose
    support or cost is UNVERIFIED there (Alchemy getTokenLargestAccounts
    / getTokenSupply; Chainstack getTokenLargestAccounts).
    """

    name = "base"
    # Capability-account this adapter's calls are billed to (see
    # provider_capabilities.ACCOUNTS). The usage tracker records
    # every serviced request against this account.
    account = None

    def __init__(self, home=None, transport=None):
        self.home = home or os.path.expanduser("~")
        self._transport = transport  # injectable opener for tests

    def configured(self):
        return True

    def supports(self, method):
        refusals = caps.FREE_TIER_REFUSALS.get(self.account, set())
        return method not in refusals

    def unverified(self, method):
        return method in caps.UNVERIFIED_METHODS.get(self.account, set())

    def cost(self, method):
        return method_cost(self.name, method)

    def wss_url(self):
        """WebSocket endpoint for this provider, or None when this
        layer cannot build one."""
        return None

    def call(self, method, params, timeout=12):
        raise NotImplementedError


# Backwards-compatible alias (the Phase 2 name).
BaseAdapter = BaseProviderAdapter


class _HttpAdapter(BaseProviderAdapter):
    """Shared HTTP JSON-RPC adapter for Alchemy / Chainstack / public."""

    endpoint = None  # override in subclass / instance

    def _url(self):
        return self.endpoint

    def call(self, method, params, timeout=12):
        start = time.monotonic()
        if not self.supports(method):
            out = _result(None, "unsupported", 0.0, self.name,
                          note="%s does not serve %s on its free tier"
                          % (self.name, method))
            out["transport_attempts"] = 0  # pre-I/O: nothing sent
            return out
        url = self._url()
        if not url:
            out = _result(None, "unconfigured", 0.0, self.name,
                          note="no key/endpoint configured")
            out["transport_attempts"] = 0  # pre-I/O: nothing sent
            return out
        status, body, error_kind, retry_after = http_json_rpc(
            url, method, params, timeout=timeout, _opener=self._transport)
        elapsed = time.monotonic() - start
        result, kind, wait, rl_kind = _parse_rpc_body(
            self.name, status, body, error_kind, retry_after=retry_after)
        note = None
        if self.unverified(method):
            note = "%s support on %s is UNVERIFIED" % (method, self.name)
        if kind == "rate_limited":
            out_meta = getattr(_last_429, "meta", None)
            _last_429.meta = None
            logger.warning("%s 429 on %s (%s)", _host_of(url), method,
                           rl_kind or "throughput")
        out = _result(result, kind, elapsed, self.name,
                      http_status=status, wait_hint_s=wait,
                      rate_limit_kind=rl_kind, note=note)
        out["transport_attempts"] = 1  # exactly one request was sent
        if kind == "rate_limited" and out_meta:
            out["rate_limit_evidence"] = out_meta  # redacted, capped
        if error_kind in ("timeout", "transport"):
            # Unknown post-I/O outcome: the request may have reached the
            # vendor and been billed. An uncertain request is never
            # retried - not on another key, not on another provider.
            out["outcome_unknown"] = True
        return out
