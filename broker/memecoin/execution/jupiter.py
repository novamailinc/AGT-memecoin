"""Jupiter swap API client (NOT used by the devnet executor).

Jupiter's public swap API serves mainnet. There is no devnet swap
liquidity for memecoins, so this client exists for the FUTURE live
path only — it is deliberately not wired into DevnetExecutor and not
reachable from adapter devnet mode.

Kept thin and honest: quote -> swap-transaction, both against the
public Jupiter Swap API v1 (api.jup.ag/swap/v1). The old v6 host
(quote-api.jup.ag) is unreachable from this VM and /v6/ 404s on
the new host — verified live 2026-11-20. Any future live wiring
must go through the same deliberate gates as everything else
(paper soak, devnet validation, explicit per-chain arming,
review).

HTTP via stdlib urllib only (repo convention).
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

from broker import BROKER_UNAVAILABLE, BrokerError

logger = logging.getLogger("broker.memecoin.execution.jupiter")

DEFAULT_JUPITER_BASE = "https://api.jup.ag"


def _get_json(url: str, params: Dict[str, str],
              timeout: float) -> Any:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{url}?{qs}",
        headers={"User-Agent": "forex-agent-memecoin-devnet/1.0",
                 "Connection": "close"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # Surface the status + a short body excerpt so callers can
        # tell "no route" (400) from "rate-limited / down" (429/5xx).
        # The body is an API error message, never a secret.
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            body = ""
        raise BrokerError(BROKER_UNAVAILABLE,
                          f"jupiter quote failed (HTTP {exc.code})",
                          detail={"reason": "jupiter_http_error",
                                  "http_status": exc.code,
                                  "body": body}) from exc
    except (urllib.error.URLError, OSError, TimeoutError,
            ValueError) as exc:
        raise BrokerError(BROKER_UNAVAILABLE,
                          f"jupiter quote failed ({type(exc).__name__})"
                          ) from exc


def _post_json(url: str, payload: Dict[str, Any],
               timeout: float) -> Any:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "forex-agent-memecoin-devnet/1.0",
                 "Connection": "close"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, TimeoutError,
            ValueError) as exc:
        raise BrokerError(BROKER_UNAVAILABLE,
                          f"jupiter swap build failed ({type(exc).__name__})"
                          ) from exc


def quote_price_impact_pct(quote: Any) -> float:
    """Price impact of a Jupiter /quote in PERCENT (comparable with price_impact_cap_pct and the owner
    max_price_impact_pct). Jupiter documents the quote field priceImpactPct as a decimal ratio in [0, 1]
    (0.01 = 1%, 1 = 100%; https://developers.jup.ag/docs/guides/how-to-build-a-custom-swap-with-metis), so the
    result is ratio * 100. Fail closed (BrokerError, reason price_impact_unknown) on a missing, non-numeric,
    non-finite, negative or >1 value: a value above 1 contradicts the documented range (e.g. a percent-form
    value), so it is refused rather than guessed."""
    raw = quote.get("priceImpactPct") if isinstance(quote, dict) else None
    if raw is None or isinstance(raw, bool):
        raise BrokerError(
            BROKER_UNAVAILABLE,
            "jupiter quote has no priceImpactPct — refusing to trade blind",
            detail={"reason": "price_impact_unknown"})
    try:
        ratio = float(raw)
    except (TypeError, ValueError):
        ratio = float("nan")
    if not (ratio == ratio) or ratio in (float("inf"), float("-inf")):
        raise BrokerError(
            BROKER_UNAVAILABLE, "jupiter quote priceImpactPct not finite — refusing to trade blind",
            detail={"reason": "price_impact_unknown", "price_impact_raw": str(raw)[:64]})
    if ratio < 0.0:
        # V1 sign semantics are NOT documented (signed language exists only for V2 /order). Conservative refusal, not a favorable-sign reading.
        raise BrokerError(
            BROKER_UNAVAILABLE, "jupiter quote priceImpactPct negative: V1 sign semantics undocumented — refusing",
            detail={"reason": "price_impact_negative_unsupported", "price_impact_raw": str(raw)[:64]})
    if ratio > 1.0:
        raise BrokerError(
            BROKER_UNAVAILABLE, "jupiter quote priceImpactPct above the documented 0..1 ratio range — refusing",
            detail={"reason": "price_impact_out_of_documented_range", "price_impact_raw": str(raw)[:64]})
    return ratio * 100.0


class JupiterClient:
    """Thin client for Jupiter Swap API v1 quote + swap endpoints
    (api.jup.ag/swap/v1)."""

    def __init__(self, base_url: str = DEFAULT_JUPITER_BASE,
                 timeout: float = 30.0):
        self._base = (base_url or DEFAULT_JUPITER_BASE).rstrip("/")
        self._timeout = float(timeout)

    def get_quote(self, input_mint: str, output_mint: str,
                  amount_lamports: int,
                  slippage_bps: int = 100) -> Dict[str, Any]:
        """Best route quote for amount_lamports of input_mint."""
        if amount_lamports <= 0:
            raise BrokerError(BROKER_UNAVAILABLE,
                              "jupiter quote amount must be positive")
        return _get_json(
            f"{self._base}/swap/v1/quote",
            {"inputMint": input_mint, "outputMint": output_mint,
             "amount": str(amount_lamports),
             "slippageBps": str(slippage_bps)},
            self._timeout)

    def get_swap_transaction(self, quote: Dict[str, Any],
                             user_pubkey: str,
                             prioritization_fee_lamports: int = None
                             ) -> Dict[str, Any]:
        """Build the swap transaction for a quote. Returns the
        /swap/v1/swap response (contains base64 'swapTransaction')."""
        payload = {"quoteResponse": quote, "userPublicKey": user_pubkey,
                   "wrapAndUnwrapSol": True}
        if prioritization_fee_lamports is not None:
            payload["prioritizationFeeLamports"] = int(
                prioritization_fee_lamports)
        return _post_json(
            f"{self._base}/swap/v1/swap", payload, self._timeout)
