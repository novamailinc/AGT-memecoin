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

Phase 2 (optional): with a fee policy + Jito client configured,
execute_swap(..., use_bundle=True) prices the fee from the policy,
checks Jito-tip economics, signs the swap once into a template,
and sends via Jito bundles instead of rpc.send_transaction. Default
(None/False) is today's behavior, unchanged. Phase 1 is UNPROVEN
(one post-fix close, Helius quota spent, zero new entries); this
build is on the user's direct order, not because a gate passed.

Compute-unit sizing (Phase 2 item 5): after the simulation that
already runs in execute_swap, the sim's unitsConsumed feeds
sized_compute_units() — simulate-then-pad (max(estimate + 100k,
estimate * 1.2), else the 200k default) — and the sized value is
recorded on the result ("compute_units", "compute_units_source",
"units_consumed"). It is recorded, NOT applied: Jupiter builds the
swap transaction today, so the executor cannot inject a
setComputeUnitLimit instruction into the already-built (and then
signed) tx. The plug-in point for applying it is a future step
that builds the tx locally: build unsigned -> simulate ->
sized_compute_units() -> setComputeUnitLimit(compute_units) ->
sign -> send. A missing or unparseable unitsConsumed falls back
to the default and never fails a trade; a simulation that itself
errors still fails the trade exactly as before (that is the
pre-existing on-chain-error safety behavior, unchanged).
"""

import json
import logging
import os
import time
import urllib.request
from typing import Any, Dict, Optional

from broker import (BROKER_UNAVAILABLE, CONFIG_INVALID,
                    CREDENTIALS_INVALID, BrokerError)
from broker.memecoin.execution import fee_policy as fee_policy_mod
from broker.memecoin.execution import rpc as rpc_mod
from broker.memecoin.execution import signer as signer_mod
from broker.memecoin.execution import tx_templates as tx_templates_mod
from broker.memecoin.execution.jito import DEFAULT_TIP_LAMPORTS
from broker.memecoin.execution.jupiter import JupiterClient
from broker.memecoin.wallet import WalletKeyProvider
from daemon import solana_ids as _solana_ids

logger = logging.getLogger("broker.memecoin.execution.mainnet_executor")

LAMPORTS_PER_SOL = 1_000_000_000
# Canonical mints live in daemon/solana_ids.py — never redefine the
# literals here (Phase 2, D-04: the USDC mint is not wSOL).
SOL_MINT = _solana_ids.WSOL_MINT
USDC_MINT = _solana_ids.USDC_MINT

# Priority fee per transaction, capped at this fraction of the stake
# (plan SS4): a fee that would cost more is a skipped trade, never
# a paid one.
PRIORITY_FEE_MAX_FRAC_OF_STAKE = 0.05
# Default priority fee lamports when no fee estimate is available
# (~$0.001 at $100/SOL). Small by design; measured at shakedown.
DEFAULT_PRIORITY_FEE_LAMPORTS = 10_000

_TOKEN_PROGRAM_ID = _solana_ids.TOKEN_PROGRAM_ID
_ASSOCIATED_TOKEN_PROGRAM_ID = _solana_ids.ASSOCIATED_TOKEN_PROGRAM_ID


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
                 sol_price_fn=None,
                 fee_policy=None,
                 jito=None):
        # Construction is always dry-run. Only arm_live() may enable
        # sends after checking the exact deployment-marker arming flag.
        # Do not coerce false/truthy configuration into a sending mode.
        if dry_run is not True:
            raise BrokerError(
                CONFIG_INVALID,
                "executor construction requires dry_run=True; "
                "use explicit arm_live() for the arming checks")
        # Phase 2 (optional, default None = today's behavior):
        # fee_policy is a fee_policy.PriorityFeePolicy and jito is a
        # jito.JitoClient. Both must be set for use_bundle=True in
        # execute_swap; without them the executor runs exactly as
        # before (same fee, same send path).
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
        self._dry_run = True
        self._sol_price_fn = sol_price_fn
        self._fee_policy = fee_policy
        self._jito = jito
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

    @staticmethod
    def sized_compute_units(sim_result) -> Dict[str, Any]:
        """Simulate-then-pad CU sizing for an already-run simulation.

        Delegates to fee_policy.sized_compute_units_from_sim: the
        sim's unitsConsumed padded to max(estimate + 100_000,
        estimate * 1.2), else the 200_000 default. Never raises —
        any failure (no sim, failed sim, unparseable units) returns
        the default with source "default", so sizing can never fail
        a trade on a missing estimate. The returned compute_units
        is the value a future local-tx build would pass to
        setComputeUnitLimit before signing (see module docstring);
        today Jupiter builds the tx, so execute_swap records it
        on the result instead of applying it.
        """
        try:
            return fee_policy_mod.sized_compute_units_from_sim(
                sim_result)
        except Exception:
            return {"compute_units":
                    fee_policy_mod.DEFAULT_COMPUTE_UNITS,
                    "source": "default", "units_consumed": None}

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
                     confirm_timeout: float = 60.0,
                     use_bundle: bool = False,
                     jito_tip_lamports: int = None
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
        "price_impact_unknown", "fee_uneconomic"}. Anything else is
        a failure.
        An unconfirmed send raises reason "confirm_timeout" WITH
        the signature attached — the send is UNKNOWN, never failed;
        the caller must reconcile from on-chain balances before
        retrying anything.

        Phase 2 bundle mode (use_bundle=True): requires a fee policy
        AND a Jito client on the constructor (else BrokerError
        "jito_not_configured" / "fee_policy_required"). The priority
        fee then comes from the policy (skip "fee_over_cap" when it
        says so), the Jito tip is checked for economics (skip
        "fee_uneconomic" when the tip eats the stake — a 0.001 SOL
        tip on a $1 stake always skips), the swap is built via a
        SignedTxTemplate (sign once), and the send goes through
        jito.send_bundle instead of rpc.send_transaction. In
        dry-run mode a bundle is simulated only and NEVER posted —
        the would-be bundle payload is returned in the result dict
        under "bundle" for inspection. A bundle id is acceptance,
        not landing: an unlanded bundle is UNKNOWN, same rule as
        confirm_timeout. With use_bundle=False (the default) the
        existing path runs exactly as before.
        """
        self._require_connected()
        self.require_deployment()
        bundle_mode = bool(use_bundle)
        if bundle_mode:
            if self._jito is None:
                raise BrokerError(
                    BROKER_UNAVAILABLE,
                    "use_bundle=True but no Jito client configured — "
                    "refusing to bundle",
                    detail={"reason": "jito_not_configured"})
            if self._fee_policy is None:
                raise BrokerError(
                    BROKER_UNAVAILABLE,
                    "use_bundle=True but no fee policy configured — "
                    "refusing to bundle blind",
                    detail={"reason": "fee_policy_required"})
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
        # -- Phase 2 bundle mode: fee + tip from the policy -----------
        fee_tier = None
        tip = None
        if bundle_mode:
            if trade_sol is None:
                raise BrokerError(
                    BROKER_UNAVAILABLE,
                    "bundle mode needs a SOL-denominated stake to "
                    "price the fee and tip — token-to-token swaps "
                    "cannot be bundled",
                    detail={"reason": "bundle_stake_unknown"})
            decision = self._fee_policy.choose_fee(trade_sol)
            if decision.get("verdict") != "ok":
                raise BrokerError(
                    BROKER_UNAVAILABLE,
                    "priority fee %d lamports rejected by fee policy "
                    "(%s) — skipped"
                    % (int(decision.get("fee_lamports") or 0),
                       decision.get("detail") or decision.get("reason")),
                    detail={"reason": decision.get("reason")
                            or "fee_over_cap",
                            "priority_fee_lamports":
                                int(decision.get("fee_lamports") or 0)})
            fee = int(decision["fee_lamports"])
            fee_tier = decision.get("tier")
            tip = (int(jito_tip_lamports)
                   if jito_tip_lamports is not None
                   else int(getattr(self._fee_policy,
                                    "default_tip_lamports",
                                    DEFAULT_TIP_LAMPORTS)))
            if tip <= 0:
                raise BrokerError(
                    BROKER_UNAVAILABLE,
                    "bundle mode needs a positive Jito tip — skipped",
                    detail={"reason": "fee_uneconomic"})
            tip_check = self._fee_policy.check_jito_tip(
                trade_sol / LAMPORTS_PER_SOL, tip / LAMPORTS_PER_SOL)
            if tip_check.get("verdict") != "economic":
                raise BrokerError(
                    BROKER_UNAVAILABLE,
                    "jito tip %d lamports (%.1f%% of stake) eats the "
                    "trade — skipped; sizing is decided by the "
                    "shakedown test"
                    % (tip, tip_check.get("tip_frac", 0) * 100),
                    detail={"reason": "fee_uneconomic",
                            "tip_lamports": tip,
                            "tip_sol": tip_check.get("tip_sol"),
                            "stake_sol": tip_check.get("stake_sol"),
                            "tip_frac": tip_check.get("tip_frac")})
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
        tip_b64 = None
        bundle_payload = None
        if bundle_mode:
            # Sign ONCE into a template; the tip transfer is a
            # separate legacy tx in the same bundle. The template
            # refuses reuse after expiry — it is consumed here,
            # immediately, while fresh.
            try:
                current_slot = self._rpc.get_slot()
            except Exception:
                current_slot = None
            kp = self._signer.keypair()
            try:
                template = tx_templates_mod.SignedTxTemplate.build(
                    swap_b64, kp, self._wallet_pubkey, input_mint,
                    output_mint, int(amount), slippage_bps,
                    slot=current_slot,
                    lookup_table_resolver=(
                        tx_mod.rpc_lookup_table_resolver(self._rpc)))
                b64 = template.signed_base64
                sig = template.signature
                tip_tx = self._jito.build_tip_tx(
                    self._wallet_pubkey, tip, template.blockhash)
                signer_mod.DevnetSigner.sign_transaction(
                    tip_tx, kp, template.blockhash)
                tip_b64 = tx_mod.tx_to_base64(tip_tx)
                bundle_payload = self._jito.build_bundle_payload(
                    [b64, tip_b64], tip)
            finally:
                del kp
        else:
            vtx = tx_mod.deserialize_versioned_tx(swap_b64)
            tx_mod.check_swap_tx(
                vtx, self._wallet_pubkey, input_mint, output_mint,
                lookup_table_resolver=(
                    tx_mod.rpc_lookup_table_resolver(self._rpc)))
            kp = self._signer.keypair()
            try:
                signed_vtx, _sig = tx_mod.sign_versioned_tx(vtx, kp)
                b64 = tx_mod.versioned_tx_to_base64(signed_vtx)
                sig = tx_mod.versioned_signature_str(signed_vtx)
            finally:
                del kp

        # -- simulate (always, dry-run and live) ------------------------
        # The sim is also the CU estimate source: its unitsConsumed
        # feeds simulate-then-pad sizing (recorded on the result;
        # see module docstring — Jupiter builds the tx, so the sized
        # value cannot be injected into this already-built tx).
        sim = self._rpc.simulate_transaction(b64)
        if isinstance(sim, dict) and sim.get("err"):
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "swap simulation failed on-chain: "
                f"{str(sim['err'])[:160]}",
                detail={"reason": "simulate_failed"})
        sized_cu = self.sized_compute_units(sim)
        if self._dry_run:
            logger.info("dry-run swap simulated OK (no send): %s -> "
                        "%s amount=%d expected_out=%d sig=%s",
                        input_mint[:12], output_mint[:12],
                        int(amount), out_expected, sig)
            result = {
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
                "compute_units": sized_cu["compute_units"],
                "compute_units_source": sized_cu["source"],
                "units_consumed": sized_cu["units_consumed"],
            }
            if bundle_mode:
                # Simulated only — the bundle is NEVER posted in
                # dry-run; the payload is returned for inspection.
                result["bundle"] = {
                    "dry_run": True, "bundle_id": None,
                    "payload": bundle_payload,
                    "tip_lamports": tip, "fee_tier": fee_tier,
                }
                result["bundle_id"] = None
                result["jito_tip_lamports"] = tip
                result["fee_tier"] = fee_tier
            return result

        # -- send + confirm ----------------------------------------------
        bundle_id = None
        if bundle_mode:
            sent_bundle = self._jito.send_bundle([b64, tip_b64], tip)
            bundle_id = sent_bundle.get("bundle_id")
            # A bundle id is acceptance, not landing: confirmation
            # below still polls the swap signature, and an unlanded
            # bundle stays UNKNOWN (see confirm_timeout handling).
        else:
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
        result = {
            "signature": sig, "simulated": False,
            "input_mint": input_mint, "output_mint": output_mint,
            "in_amount": int(amount), "out_amount": out_amount,
            "sol_before": sol_before, "sol_after": sol_after,
            "in_before": in_before, "in_after": in_after,
            "out_before": out_before, "out_after": out_after,
            "priority_fee_lamports": fee_paid,
            "price_impact_pct": impact,
            "quote_out_amount": out_expected,
            "compute_units": sized_cu["compute_units"],
            "compute_units_source": sized_cu["source"],
            "units_consumed": sized_cu["units_consumed"],
        }
        if bundle_mode:
            result["bundle_id"] = bundle_id
            result["jito_tip_lamports"] = tip
            result["fee_tier"] = fee_tier
        return result

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
