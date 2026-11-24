"""MainnetExecutor — the real (mainnet-only) execution path for the
live grad_select lane.

Pipeline per action (mirrors DevnetExecutor's shape):

  1. deployment-marker check (this process runs ONLY on the user's
     own deployment machine — never on the paper/dev VM)
  2. fetch a recent blockhash        (MainnetRPC)
  3. build the swap transaction      (Jupiter v6, versioned)
  4. sign with the operator's keypair (signer.py — secret stays transient)
  5. simulate, then send + wait for confirmation (MainnetRPC)

What it can do:
  * connect() / get_balance_sol() / get_token_balance(mint)
  * execute_swap(...) — Jupiter quote -> sanity check -> simulate ->
    sign -> send -> confirm -> executed amounts (Step 2)

What it deliberately cannot do:
  * run without a deployment marker (FOREX_AGENT_HOME/live/
    deployment.json, created by the user on his own machine).
  * send anything while dry_run is True (the default). Arming is an
    explicit, logged action that also requires "armed": true in the
    deployment marker.
  * anything on devnet (MainnetRPC refuses non-mainnet hosts).

Every send is confirmed before returning: methods return only after
the network reports confirmed/finalized (or raise BROKER_UNAVAILABLE
on timeout/failure). An unconfirmed send is UNKNOWN, never failed —
callers must reconcile (signature status / balances) before retrying.
"""

import json
import logging
import os
import time
import urllib.request
from typing import Any, Dict, Optional

from broker import BROKER_UNAVAILABLE, CONFIG_INVALID, BrokerError
from broker.memecoin.execution import rpc as rpc_mod
from broker.memecoin.execution import signer as signer_mod
from broker.memecoin.execution.jupiter import JupiterClient
from broker.memecoin.wallet import WalletKeyProvider

logger = logging.getLogger("broker.memecoin.execution.mainnet_executor")

LAMPORTS_PER_SOL = 1_000_000_000
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Priority fee per transaction, capped at this fraction of the stake
# (plan SS4): a fee that would cost more is a skipped trade, never
# a paid one.
PRIORITY_FEE_MAX_FRAC_OF_STAKE = 0.05
# Default priority fee lamports when no fee estimate is available
# (~$0.001 at $100/SOL). Small by design; measured at shakedown.
DEFAULT_PRIORITY_FEE_LAMPORTS = 10_000

_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_ASSOCIATED_TOKEN_PROGRAM_ID = \
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"


def associated_token_address(wallet_pubkey: str, mint: str) -> str:
    """The SPL associated token account for (wallet, mint)."""
    from solders.pubkey import Pubkey
    wallet = Pubkey.from_string(wallet_pubkey)
    mint_k = Pubkey.from_string(mint)
    token_prog = Pubkey.from_string(_TOKEN_PROGRAM_ID)
    ata_prog = Pubkey.from_string(_ASSOCIATED_TOKEN_PROGRAM_ID)
    (ata, _bump) = Pubkey.find_program_address(
        [bytes(wallet), bytes(token_prog), bytes(mint_k)], ata_prog)
    return str(ata)


def deployment_marker_path(live_home: Optional[str] = None) -> str:
    """Path of the deployment marker. Created by the user on his own
    deployment machine; its absence means 'not a deployment machine'."""
    home = live_home or os.environ.get(
        "FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
    return os.path.join(home, "live", "deployment.json")


def read_deployment_marker(live_home: Optional[str] = None) -> Dict[str, Any]:
    """Load the deployment marker, or raise CONFIG_INVALID when this
    machine is not a deployment machine. The marker is NOT secret —
    it only names the machine and whether live sends are armed."""
    path = deployment_marker_path(live_home)
    try:
        with open(path, "r", encoding="utf-8") as f:
            marker = json.load(f)
    except FileNotFoundError:
        raise BrokerError(
            CONFIG_INVALID,
            "no deployment marker at %s — this machine is not a "
            "live deployment machine; the live executor refuses "
            "to run here" % path)
    except (ValueError, OSError) as exc:
        raise BrokerError(
            CONFIG_INVALID,
            "deployment marker %s unreadable (%s) — refusing to "
            "run" % (path, type(exc).__name__))
    if not isinstance(marker, dict) or \
            not str(marker.get("machine") or "").strip():
        raise BrokerError(
            CONFIG_INVALID,
            "deployment marker %s has no machine name — refusing "
            "to run" % path)
    return marker


class MainnetExecutor:
    """Real mainnet execution. Mainnet only, deployment-machine only,
    dry-run by default."""

    def __init__(self,
                 key_provider: WalletKeyProvider,
                 rpc=None,
                 rpc_url: Optional[str] = None,
                 jupiter=None,
                 chain: str = "solana",
                 expected_pubkey: str = "",
                 live_home: Optional[str] = None,
                 dry_run: bool = True,
                 sol_price_fn=None):
        if key_provider is None:
            raise BrokerError(
                CONFIG_INVALID,
                "mainnet execution needs a key provider "
                "(file provider on the deployment machine)")
        if rpc is not None:
            self._rpc = rpc
        elif rpc_url:
            self._rpc = rpc_mod.MainnetRPC(rpc_url)
        else:
            raise BrokerError(
                CONFIG_INVALID,
                "mainnet execution needs an RPC endpoint "
                "(rpc or rpc_url)")
        self._jupiter = jupiter if jupiter is not None \
            else JupiterClient()
        self._signer = signer_mod.DevnetSigner(key_provider, chain)
        self._chain = chain
        self._expected_pubkey = (expected_pubkey or "").strip()
        self._live_home = live_home
        self._dry_run = bool(dry_run)
        self._sol_price_fn = sol_price_fn
        self._wallet_pubkey: Optional[str] = None
        self._sol_price_cache: Dict[str, Any] = {"ts": 0.0, "price": None}
        self._decimals_cache: Dict[str, int] = {}

    # -- state ---------------------------------------------------------
    @property
    def wallet_pubkey(self) -> Optional[str]:
        return self._wallet_pubkey

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def require_deployment(self) -> Dict[str, Any]:
        """Fail unless this machine carries a deployment marker."""
        return read_deployment_marker(self._live_home)

    def arm_live(self) -> None:
        """Arm live sends. Requires the deployment marker to carry
        "armed": true. Deliberate, logged, reversible with disarm()."""
        marker = self.require_deployment()
        if marker.get("armed") is not True:
            raise BrokerError(
                CONFIG_INVALID,
                "deployment marker does not carry \"armed\": true — "
                "live sends stay off (dry-run)")
        self._dry_run = False
        logger.warning("mainnet executor ARMED on machine %r",
                       str(marker.get("machine")))

    def disarm(self) -> None:
        self._dry_run = True
        logger.warning("mainnet executor disarmed (dry-run)")

    # -- connection ----------------------------------------------------
    def connect(self) -> str:
        """Deployment-marker check, RPC health, key load, identity
        check. Returns the wallet public key (base58). Fails closed
        when: no deployment marker, RPC unhealthy, no key available,
        or the key's pubkey != expected_pubkey (when configured)."""
        self.require_deployment()
        health = self._rpc.get_health()
        if health != "ok":
            raise BrokerError(BROKER_UNAVAILABLE,
                              f"mainnet RPC unhealthy: {health!r}")
        pubkey = self._signer.pubkey_str()
        if self._expected_pubkey and pubkey != self._expected_pubkey:
            raise BrokerError(
                CREDENTIALS_INVALID,
                "wallet key does not match MEMECOIN_WALLET_PUBKEY — "
                "refusing to sign with an unexpected key")
        self._wallet_pubkey = pubkey
        slot = self._rpc.get_slot()
        logger.info("MainnetExecutor connected: wallet=%s slot=%d "
                    "dry_run=%s", pubkey, slot, self._dry_run)
        return pubkey

    def get_balance_sol(self) -> float:
        self._require_connected()
        lamports = self._rpc.get_balance(self._wallet_pubkey)
        return lamports / LAMPORTS_PER_SOL

    def get_token_balance(self, mint: str) -> int:
        """On-chain token balance (base units) of the wallet's ATA
        for mint. A missing ATA counts as zero, never an error."""
        self._require_connected()
        ata = associated_token_address(self._wallet_pubkey, mint)
        try:
            bal = self._rpc.get_token_account_balance(ata)
        except BrokerError as exc:
            if getattr(exc, "detail", {}).get("reason") == "rpc_error":
                return 0
            raise
        try:
            return int(bal.get("amount") or 0)
        except (TypeError, ValueError):
            return 0

    def get_mint_decimals(self, mint: str) -> int:
        """Decimals for a token mint (cached per process).

        Raises BrokerError on any RPC/parse failure — callers treat
        an unknown decimal count as unknown and refuse to trade.
        """
        self._require_connected()
        if mint == SOL_MINT:
            return 9
        if mint in self._decimals_cache:
            return self._decimals_cache[mint]
        res = self._rpc.call(
            "getAccountInfo",
            [mint, {"encoding": "jsonParsed",
                    "commitment": "confirmed"}])
        try:
            dec = int(res["value"]["data"]["parsed"]["info"]["decimals"])
        except (KeyError, TypeError, ValueError):
            raise BrokerError(
                "rpc_error", "cannot read decimals for mint",
                detail={"reason": "rpc_error", "mint": mint})
        self._decimals_cache[mint] = dec
        return dec

    # -- swaps ---------------------------------------------------------
    def execute_swap(self, input_mint: str, output_mint: str,
                     amount: int, slippage_bps: int = 100,
                     price_impact_cap_pct: float = 2.0,
                     priority_fee_lamports: int = None,
                     confirm_timeout: float = 60.0
                     ) -> Dict[str, Any]:
        """One Jupiter swap, end to end.

        quote -> price-impact cap -> swap tx -> instruction sanity
        check -> simulate -> sign -> send -> confirm -> executed
        amounts measured from on-chain balances.

        Returns a result dict with measured in/out amounts (base
        units), SOL/token balances before/after, the priority fee
        actually paid, and the signature. In dry-run mode it builds
        + simulates only (no send) and returns simulated=True with
        the QUOTE's expected out amount.

        Skips (no trade, NOT an executor failure — the caller must
        not count these toward the fail-streak halt) raise
        BrokerError with detail reason in {"no_route",
        "price_impact_over_cap", "fee_over_cap",
        "price_impact_unknown"}. Anything else is a failure.
        An unconfirmed send raises reason "confirm_timeout" WITH
        the signature attached — the send is UNKNOWN, never failed;
        the caller must reconcile from on-chain balances before
        retrying anything.
        """
        self._require_connected()
        self.require_deployment()
        if int(amount or 0) <= 0:
            raise BrokerError(BROKER_UNAVAILABLE,
                              "swap amount must be positive")
        fee = (DEFAULT_PRIORITY_FEE_LAMPORTS
               if priority_fee_lamports is None
               else int(priority_fee_lamports))
        if fee < 0:
            raise BrokerError(BROKER_UNAVAILABLE,
                              "priority fee must not be negative")

        in_is_sol = input_mint == SOL_MINT
        out_is_sol = output_mint == SOL_MINT

        # -- quote ------------------------------------------------------
        try:
            quote = self._jupiter.get_quote(
                input_mint, output_mint, int(amount), slippage_bps)
        except BrokerError as exc:
            det = getattr(exc, "detail", {}) or {}
            if det.get("http_status") == 400:
                raise BrokerError(
                    BROKER_UNAVAILABLE,
                    "jupiter has no route for this swap — skipped",
                    detail={"reason": "no_route"}) from exc
            raise
        if not isinstance(quote, dict) or not quote.get("outAmount"):
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "jupiter quote missing outAmount — no trade",
                detail={"reason": "no_route"})
        try:
            out_expected = int(quote["outAmount"])
        except (TypeError, ValueError):
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "jupiter quote outAmount unparseable — no trade",
                detail={"reason": "no_route"})
        impact_raw = quote.get("priceImpactPct")
        if impact_raw is None:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "jupiter quote has no priceImpactPct — refusing to "
                "trade blind",
                detail={"reason": "price_impact_unknown"})
        try:
            impact = float(impact_raw)
        except (TypeError, ValueError):
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "jupiter quote priceImpactPct unparseable — "
                "refusing to trade blind",
                detail={"reason": "price_impact_unknown"})
        if impact > float(price_impact_cap_pct):
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "jupiter price impact %.2f%% over cap %.2f%% — "
                "skipped" % (impact, price_impact_cap_pct),
                detail={"reason": "price_impact_over_cap",
                        "price_impact_pct": impact})

        # -- priority-fee cap (5% of the trade's SOL value) ------------
        trade_sol = int(amount) if in_is_sol else (
            out_expected if out_is_sol else None)
        if trade_sol is not None and fee > \
                PRIORITY_FEE_MAX_FRAC_OF_STAKE * trade_sol:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "priority fee %d lamports exceeds 5%% of the "
                "trade's SOL value — skipped" % fee,
                detail={"reason": "fee_over_cap",
                        "priority_fee_lamports": fee})

        # -- balances before ---------------------------------------------
        sol_before = self._rpc.get_balance(self._wallet_pubkey)
        in_before = self._balance_of(input_mint)
        out_before = self._balance_of(output_mint)

        # -- swap tx: build, sanity-check, sign -------------------------
        swap_resp = self._jupiter.get_swap_transaction(
            quote, self._wallet_pubkey,
            prioritization_fee_lamports=fee)
        swap_b64 = (swap_resp or {}).get("swapTransaction")
        if not swap_b64:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "jupiter swap response has no swapTransaction")
        from broker.memecoin.execution import tx as tx_mod
        vtx = tx_mod.deserialize_versioned_tx(swap_b64)
        tx_mod.check_swap_tx(vtx, self._wallet_pubkey,
                             input_mint, output_mint)
        kp = self._signer.keypair()
        try:
            signed_vtx, _sig = tx_mod.sign_versioned_tx(vtx, kp)
            b64 = tx_mod.versioned_tx_to_base64(signed_vtx)
            sig = tx_mod.versioned_signature_str(signed_vtx)
        finally:
            del kp

        # -- simulate (always, dry-run and live) ------------------------
        sim = self._rpc.simulate_transaction(b64)
        if isinstance(sim, dict) and sim.get("err"):
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "swap simulation failed on-chain: "
                f"{str(sim['err'])[:160]}",
                detail={"reason": "simulate_failed"})
        if self._dry_run:
            logger.info("dry-run swap simulated OK (no send): %s -> "
                        "%s amount=%d expected_out=%d sig=%s",
                        input_mint[:12], output_mint[:12],
                        int(amount), out_expected, sig)
            return {
                "signature": None, "simulated": True,
                "input_mint": input_mint, "output_mint": output_mint,
                "in_amount": int(amount),
                "out_amount": out_expected,
                "sol_before": sol_before, "sol_after": sol_before,
                "in_before": in_before, "in_after": in_before,
                "out_before": out_before, "out_after": out_before,
                "priority_fee_lamports": fee,
                "price_impact_pct": impact,
                "quote_out_amount": out_expected,
            }

        # -- send + confirm ----------------------------------------------
        sent = self._rpc.send_transaction(b64)
        if sent != sig:
            logger.warning("RPC echoed signature %s != local %s",
                           sent, sig)
        try:
            self._await_confirmation(sig, confirm_timeout)
        except BrokerError as exc:
            det = getattr(exc, "detail", {}) or {}
            if det.get("reason") == "confirm_timeout":
                raise BrokerError(
                    BROKER_UNAVAILABLE,
                    "swap send unconfirmed (UNKNOWN, not failed) — "
                    "reconcile from on-chain balances before "
                    "retrying",
                    detail={"reason": "confirm_timeout",
                            "signature": sig}) from exc
            raise

        # -- executed amounts from on-chain balances ---------------------
        sol_after = self._rpc.get_balance(self._wallet_pubkey)
        in_after = self._balance_of(input_mint)
        out_after = self._balance_of(output_mint)
        out_amount = out_after - out_before
        if out_amount <= 0:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "confirmed swap moved no output tokens — "
                "reconcile before retrying",
                detail={"reason": "zero_output", "signature": sig})
        if out_amount < 0.5 * out_expected:
            logger.warning(
                "swap executed far below quote: out=%d expected=%d "
                "sig=%s", out_amount, out_expected, sig)
        if in_is_sol:
            fee_paid = max(0, (sol_before - sol_after) - int(amount))
        elif out_is_sol:
            fee_paid = max(0, out_expected - out_amount)
        else:
            fee_paid = fee
        logger.info("swap confirmed: %s -> %s in=%d out=%d fee=%d "
                    "sig=%s", input_mint[:12], output_mint[:12],
                    int(amount), out_amount, fee_paid, sig)
        return {
            "signature": sig, "simulated": False,
            "input_mint": input_mint, "output_mint": output_mint,
            "in_amount": int(amount), "out_amount": out_amount,
            "sol_before": sol_before, "sol_after": sol_after,
            "in_before": in_before, "in_after": in_after,
            "out_before": out_before, "out_after": out_after,
            "priority_fee_lamports": fee_paid,
            "price_impact_pct": impact,
            "quote_out_amount": out_expected,
        }

    def _balance_of(self, mint: str) -> int:
        """Balance of mint for the wallet: lamports for SOL, base
        units for a token (missing ATA counts as zero)."""
        if mint == SOL_MINT:
            return self._rpc.get_balance(self._wallet_pubkey)
        return self.get_token_balance(mint)

    def sol_price_usd(self) -> Optional[float]:
        """SOL price in USD (accounting only — never a trade input).
        DexScreener first, Jupiter price API fallback, 60s cache.
        None when both sources fail (callers must fail soft)."""
        if self._sol_price_fn is not None:
            try:
                return self._sol_price_fn()
            except Exception:
                return None
        now = time.monotonic()
        cached = self._sol_price_cache
        if cached.get("price") and now - float(cached.get("ts") or 0) < 60:
            return float(cached["price"])
        price = None
        for url in (
                "https://api.dexscreener.com/latest/dex/tokens/"
                + SOL_MINT,
                "https://price.jup.ag/v6/price?ids=SOL"):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent":
                                  "forex-agent-memecoin-mainnet/1.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    body = json.loads(resp.read())
                if "pairs" in body:
                    pairs = body.get("pairs") or []
                    if pairs and pairs[0].get("priceUsd"):
                        price = float(pairs[0]["priceUsd"])
                elif "data" in body:
                    sol = (body.get("data") or {}).get("SOL") or {}
                    if sol.get("price"):
                        price = float(sol["price"])
                if price and price > 0:
                    break
            except Exception:
                continue
        if price and price > 0:
            self._sol_price_cache = {"ts": now, "price": price}
            return price
        return None

    # -- internals -----------------------------------------------------
    def _require_connected(self) -> None:
        if not self._wallet_pubkey:
            raise BrokerError(BROKER_UNAVAILABLE,
                              "MainnetExecutor not connected — call "
                              "connect() first")

    def _await_confirmation(self, signature: str,
                            timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            statuses = self._rpc.get_signature_statuses([signature])
            st = statuses[0] if statuses else None
            if st:
                if st.get("err"):
                    raise BrokerError(
                        BROKER_UNAVAILABLE,
                        "mainnet transaction failed on-chain: "
                        f"{str(st['err'])[:160]}",
                        detail={"reason": "tx_failed",
                                "signature": signature})
                conf = st.get("confirmationStatus")
                if conf in ("confirmed", "finalized"):
                    return
            time.sleep(2.0)
        raise BrokerError(BROKER_UNAVAILABLE,
                          f"mainnet transaction {signature} not confirmed "
                          f"within {timeout:.0f}s",
                          detail={"reason": "confirm_timeout",
                                  "signature": signature})
